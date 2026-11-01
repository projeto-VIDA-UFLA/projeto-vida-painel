"""Iniciar, parar e trocar de rota pelo docker compose do alto-nivel.

Só estas ações existem, com a rota vinda da lista: nada do navegador vira comando.
"""
import asyncio
import json
import os

MANUAL = "ros_ws"
NAVIGATION = "navegacao"
NAVIGATION_CONTAINER = "ros_ws_navegacao"
CONTAINER_ROOT = "/caminhos"


class DockerControl:
    def __init__(self, project_dir=None, dry_run=None):
        self.project_dir = project_dir or os.environ.get("VIDA_ALTO_NIVEL_DIR", "")
        self.dry_run = (os.environ.get("VIDA_PAINEL_DRY_RUN", "0") == "1") if dry_run is None else dry_run
        self.fake = {MANUAL: False, NAVIGATION: False, "route": None}
        self.commands = []

    def _compose(self, *args):
        return ["docker", "compose", "--project-directory", self.project_dir,
                "-f", os.path.join(self.project_dir, "docker-compose.yml"), "--profile", NAVIGATION, *args]

    async def _run(self, command, env=None, timeout=90):
        self.commands.append(command)
        if self.dry_run:
            return 0, ""
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, **(env or {})})
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return 1, "tempo esgotado"
        return process.returncode, output.decode(errors="replace").strip()

    async def _check(self, command, env=None):
        code, output = await self._run(command, env)
        if code:
            raise RuntimeError(output[-400:] or f"falha ao executar {command[2:]}")

    async def status(self):
        """{"manual": bool, "navigation": bool, "route": id da rota em uso ou None}."""
        if self.dry_run:
            return {"manual": self.fake[MANUAL], "navigation": self.fake[NAVIGATION], "route": self.fake["route"]}
        code, output = await self._run(["docker", "inspect", MANUAL, NAVIGATION_CONTAINER], timeout=10)
        self.commands.pop()
        # Com um dos containers ausente o inspect dá erro, mas lista o outro.
        result = {"manual": False, "navigation": False, "route": None}
        start = output.find("[")
        end = output.rfind("]")
        if start < 0 or end < 0:
            return result
        try:
            items = json.loads(output[start:end + 1])
        except ValueError:
            return result
        for item in items:
            running = item.get("State", {}).get("Running", False)
            name = item.get("Name", "").lstrip("/")
            if name == MANUAL:
                result["manual"] = running
            elif name == NAVIGATION_CONTAINER:
                result["navigation"] = running
                if running:
                    result["route"] = route_from_env(item.get("Config", {}).get("Env", []))
        return result

    async def start_manual(self):
        await self._check(self._compose("stop", NAVIGATION))
        await self._check(self._compose("up", "-d", "--no-build", MANUAL))
        if self.dry_run:
            self.fake.update({MANUAL: True, NAVIGATION: False, "route": None})

    async def start_navigation(self, route_id):
        """Sobe a navegação com a rota; se ela já rodava com outra, recria o container."""
        await self._check(self._compose("stop", MANUAL))
        path = f"{CONTAINER_ROOT}/{route_id}"
        await self._check(self._compose("up", "-d", "--no-build", NAVIGATION), env={"VIDA_PATH_FILE": path})
        if self.dry_run:
            self.fake.update({MANUAL: False, NAVIGATION: True, "route": route_id})

    async def stop(self):
        await self._check(self._compose("stop", MANUAL, NAVIGATION))
        if self.dry_run:
            self.fake.update({MANUAL: False, NAVIGATION: False, "route": None})


def route_from_env(env):
    prefix = f"VIDA_PATH_FILE={CONTAINER_ROOT}/"
    for item in env:
        if item.startswith(prefix):
            return item[len(prefix):]
    return None
