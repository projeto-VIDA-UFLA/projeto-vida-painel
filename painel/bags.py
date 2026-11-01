"""Gravar e reproduzir bags com o ros2 bag, na pasta de bags do notebook."""
import asyncio
import datetime as dt
import os
import re
import shutil
import signal
import time

import yaml

ROOT = os.environ.get("VIDA_BAGS", "/bags")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")


def _chown_tree(path):
    uid = int(os.environ.get("VIDA_UID", "0") or 0)
    if not uid:
        return
    try:
        os.chown(path, uid, uid)
        for folder, dirs, files in os.walk(path):
            for name in dirs + files:
                os.chown(os.path.join(folder, name), uid, uid)
    except OSError:
        pass


def list_bags(root=None):
    root = root or ROOT
    result = []
    try:
        names = os.listdir(root)
    except OSError:
        return result
    for name in names:
        folder = os.path.join(root, name)
        meta = os.path.join(folder, "metadata.yaml")
        if not os.path.isfile(meta):
            continue
        duration = None
        try:
            with open(meta, encoding="utf-8") as stream:
                info = yaml.safe_load(stream)["rosbag2_bagfile_information"]
            duration = info["duration"]["nanoseconds"] / 1e9
        except (OSError, KeyError, TypeError, yaml.YAMLError):
            pass
        size = sum(os.path.getsize(os.path.join(folder, f)) for f in os.listdir(folder))
        result.append({"name": name, "seconds": duration, "size_mb": round(size / 1e6), "mtime": os.path.getmtime(meta)})
    result.sort(key=lambda b: b["mtime"], reverse=True)
    return result


class BagManager:
    def __init__(self, root=None):
        self.root = root or ROOT
        self.recording = None    # {"name", "process", "started"}
        self.player = None       # {"name", "process", "started", "paused"}

    @property
    def playing(self):
        if self.player and self.player["process"].returncode is not None:
            self.player = None
        return self.player is not None

    def player_elapsed(self, now):
        """Tempo decorrido do bag, sem contar as pausas. O ros2 bag não informa a posição."""
        player = self.player
        paused_now = now - player["paused_at"] if player["paused_at"] is not None else 0.0
        elapsed = now - player["started"] - player["paused_total"] - paused_now
        total = player["total"]
        return max(0.0, min(elapsed, total) if total is not None else elapsed)

    def status(self):
        now = time.time()
        recording = None
        if self.recording:
            recording = {"name": self.recording["name"], "seconds": round(now - self.recording["started"]),
                         "alive": self.recording["process"].returncode is None}
        playing = None
        if self.playing:
            playing = {"name": self.player["name"], "paused": self.player["paused"],
                       "elapsed": round(self.player_elapsed(now)), "total": self.player["total"]}
        return {"recording": recording, "playing": playing}

    def listing(self):
        try:
            usage = shutil.disk_usage(self.root)
            free = round(usage.free / 1e9, 1)
        except OSError:
            free = None
        return {"bags": list_bags(self.root), "free_gb": free,
                "next_name": dt.datetime.now().strftime("teste_%Y-%m-%d_%H-%M")}

    def _in_use(self, name):
        if self.recording and self.recording["name"] == name:
            raise ValueError("este bag está sendo gravado: pare a gravação antes")
        if self.playing and self.player["name"] == name:
            raise ValueError("este bag está em reprodução: pare a reprodução antes")

    def _existing(self, name):
        if not any(b["name"] == name for b in list_bags(self.root)):
            raise ValueError(f"bag desconhecido: {name}")
        return os.path.join(self.root, name)

    def rename(self, name, new_name):
        """Só troca o nome da pasta: o metadata aponta os arquivos pelo caminho relativo."""
        path = self._existing(name)
        self._in_use(name)
        new_name = (new_name or "").strip()
        if not NAME_RE.match(new_name):
            raise ValueError("use só letras, números, _ e - no nome do bag")
        target = os.path.join(self.root, new_name)
        if os.path.exists(target):
            raise ValueError(f"já existe um bag chamado {new_name}")
        os.rename(path, target)
        return new_name

    def delete(self, name):
        """Move para .lixeira: um clique errado não perde o teste. Não libera espaço em disco."""
        path = self._existing(name)
        self._in_use(name)
        trash = os.path.join(self.root, ".lixeira", dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
        os.makedirs(trash, exist_ok=True)
        _chown_tree(os.path.join(self.root, ".lixeira"))
        os.rename(path, os.path.join(trash, name))
        return os.path.relpath(trash, self.root)

    async def _spawn(self, *command):
        return await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL)

    async def start_record(self, name):
        if self.recording:
            raise ValueError("já existe um bag sendo gravado")
        if self.playing:
            raise ValueError("pare a reprodução antes de gravar: o bag novo gravaria o bag antigo")
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise ValueError("use só letras, números, _ e - no nome do bag")
        if os.path.exists(os.path.join(self.root, name)):
            raise ValueError(f"já existe um bag chamado {name}")
        process = await self._spawn("ros2", "bag", "record", "-a", "-o", os.path.join(self.root, name))
        self.recording = {"name": name, "process": process, "started": time.time()}
        return name

    async def _end(self, process, grace=15.0):
        """SIGINT: é assim que o ros2 bag fecha o arquivo e escreve o metadata."""
        if process.returncode is None:
            process.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(process.wait(), grace)
            except asyncio.TimeoutError:
                process.terminate()
                await process.wait()

    async def stop_record(self):
        if not self.recording:
            return None
        name, process = self.recording["name"], self.recording["process"]
        await self._end(process)
        self.recording = None
        _chown_tree(os.path.join(self.root, name))
        return name

    async def play(self, name):
        if self.playing:
            raise ValueError("já existe um bag em reprodução")
        if self.recording:
            raise ValueError("pare a gravação antes de reproduzir")
        bag = next((b for b in list_bags(self.root) if b["name"] == name), None)
        if bag is None:
            raise ValueError(f"bag desconhecido: {name}")
        process = await self._spawn("ros2", "bag", "play", os.path.join(self.root, name))
        self.player = {"name": name, "process": process, "started": time.time(), "paused": False,
                       "total": round(bag["seconds"]) if bag["seconds"] is not None else None,
                       "paused_at": None, "paused_total": 0.0}
        return name

    async def toggle_pause(self):
        if not self.playing:
            return False
        process = await self._spawn("ros2", "service", "call", "/rosbag2_player/toggle_paused",
                                    "rosbag2_interfaces/srv/TogglePaused")
        await asyncio.wait_for(process.wait(), 10.0)
        if process.returncode == 0 and self.player:
            now = time.time()
            if self.player["paused"]:
                self.player["paused_total"] += now - self.player["paused_at"]
                self.player["paused_at"] = None
            else:
                self.player["paused_at"] = now
            self.player["paused"] = not self.player["paused"]
        return process.returncode == 0

    async def stop_play(self):
        if self.playing:
            await self._end(self.player["process"], grace=5.0)
        self.player = None
