"""Servidor web do painel: página, WebSocket com os dados e as ações de operação."""
import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request

from aiohttp import WSMsgType, web

from painel import routes as routelib
from painel.bags import BagManager
from painel.bridge import start_ros
from painel.control import DockerControl

PORT = int(os.environ.get("VIDA_PAINEL_PORT", "8080"))
HOST = "127.0.0.1"
WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
DATA = os.environ.get("VIDA_PAINEL_DATA", "/data")
TILES = os.path.join(DATA, "tiles")
STATE = os.path.join(DATA, "state.json")
TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
TILE_ZOOM = 18
RATE_S = 0.1
ODROID_IP = os.environ.get("VIDA_ODROID_IP", "192.168.10.1")


def allowed_hosts(port=PORT):
    return {f"localhost:{port}", f"127.0.0.1:{port}"}


@web.middleware
async def only_local(request, handler):
    """Só o próprio notebook: barra DNS rebinding e pedidos vindos de outros sites."""
    if request.host not in allowed_hosts():
        raise web.HTTPForbidden(text="acesso só por localhost")
    origin = request.headers.get("Origin")
    if origin and origin.split("://", 1)[-1] not in allowed_hosts():
        raise web.HTTPForbidden(text="origem não permitida")
    if request.method == "POST" and not origin:
        raise web.HTTPForbidden(text="origem ausente")
    return await handler(request)


class App:
    def __init__(self, bridge, control):
        self.bridge = bridge
        self.control = control
        self.car_status = {"manual": False, "navigation": False, "route": None}
        self.selected = self._load_state()
        self.busy = False
        self.failed_tiles = {}
        self.bags = BagManager()
        if self.selected:
            self._apply_route(self.selected)

    # -------------------------------------------------------- estado
    @staticmethod
    def _load_state():
        try:
            with open(STATE, encoding="utf-8") as stream:
                return json.load(stream).get("route")
        except (OSError, ValueError):
            return None

    def _save_state(self):
        os.makedirs(DATA, exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as stream:
            json.dump({"route": self.selected}, stream)

    def _apply_route(self, route_id):
        if route_id is None:
            self.bridge.set_route(None)
            self.selected = None
            return
        try:
            self.bridge.set_route(routelib.load_points(route_id))
            self.selected = route_id
        except (OSError, ValueError):
            self.bridge.set_route(None)
            self.selected = None

    async def poll_car(self):
        while True:
            try:
                self.car_status = await self.control.status()
                running = self.car_status.get("route")
                if running and running != self.selected:
                    self._apply_route(running)
            except OSError:
                pass
            await asyncio.sleep(2.0)

    async def poll_link(self):
        """Ping real na rede ponto a ponto: bag nenhum faz o Odroid responder."""
        while True:
            ok, milliseconds = False, None
            try:
                process = await asyncio.create_subprocess_exec(
                    "ping", "-n", "-c", "1", "-W", "1", ODROID_IP,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                output, _ = await process.communicate()
                ok = process.returncode == 0
                match = re.search(rb"time=([\d.]+)", output)
                milliseconds = float(match.group(1)) if match else None
            except OSError:
                pass
            self.bridge.set_ping(ok, milliseconds)
            await asyncio.sleep(1.0)

    def snapshot(self):
        data = self.bridge.snapshot(self.car_status)
        data["selected"] = self.selected
        data["busy"] = self.busy
        data["bags"] = self.bags.status()
        return data

    # -------------------------------------------------------- páginas
    async def index(self, request):
        return web.FileResponse(os.path.join(WEB, "index.html"), headers={"Cache-Control": "no-cache"})

    async def api_routes(self, request):
        return web.json_response({"routes": routelib.list_routes(), "selected": self.selected,
                                  "next_name": routelib.next_name()})

    async def api_route(self, request):
        route_id = request.query.get("id", "")
        try:
            return web.json_response(routelib.load_points(route_id))
        except ValueError as error:
            raise web.HTTPNotFound(text=str(error))

    async def websocket(self, request):
        socket = web.WebSocketResponse(heartbeat=10.0)
        await socket.prepare(request)
        generation, index = -1, 0
        try:
            while not socket.closed:
                data = self.snapshot()
                generation, index, tail, reset = self.bridge.trail_since(generation, index)
                data["trail"] = {"points": tail, "reset": reset}
                await socket.send_str(json.dumps(data, allow_nan=False))
                await asyncio.sleep(RATE_S)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return socket

    # -------------------------------------------------------- ações
    def _guard(self):
        reason = self.bridge.snapshot(self.car_status)["blocked"]
        if reason:
            raise web.HTTPConflict(text=f"Não é possível agora: {reason}.")
        if self.bridge.recorder.active:
            raise web.HTTPConflict(text="Pare a gravação da rota antes.")
        if self.bags.playing:
            raise web.HTTPConflict(text="Pare a reprodução do bag antes: os dados dele se misturariam com os do carro.")

    async def _do(self, action):
        if self.busy:
            raise web.HTTPConflict(text="Aguarde a operação anterior terminar.")
        self.busy = True
        try:
            await action()
            self.car_status = await self.control.status()
        except RuntimeError as error:
            raise web.HTTPInternalServerError(text=f"Falha no Docker: {error}")
        finally:
            self.busy = False

    async def start(self, request):
        self._guard()
        route = self.selected
        await self._do((lambda: self.control.start_navigation(route)) if route else self.control.start_manual)
        self.bridge.event(f"Carro iniciado com {os.path.basename(route)[:-4]}" if route else "Carro iniciado sem rota")
        return web.json_response({"ok": True})

    async def stop(self, request):
        # Parar é permitido em qualquer modo: sem referências, o ESP32 aplica o freio.
        if self.bridge.recorder.active:
            raise web.HTTPConflict(text="Pare a gravação da rota antes.")
        await self._do(self.control.stop)
        self.bridge.event("Carro parado pelo painel")
        return web.json_response({"ok": True})

    async def select(self, request):
        body = await request.json()
        route_id = body.get("id") or None
        if route_id is not None:
            try:
                routelib.resolve(route_id)
            except ValueError as error:
                raise web.HTTPBadRequest(text=str(error))
        self._guard()
        navigation = self.car_status.get("navigation")
        running = self.car_status.get("manual") or navigation
        restarted = False
        if route_id is not None and running:
            await self._do(lambda: self.control.start_navigation(route_id))
            restarted = True
        elif route_id is None and navigation:
            await self._do(self.control.start_manual)
            restarted = True
        self._apply_route(route_id)
        self._save_state()
        self.bridge.event(f"Rota {os.path.basename(route_id)[:-4]} selecionada" if route_id else "Sem rota: só modo manual")
        if route_id:
            asyncio.get_running_loop().run_in_executor(None, self.prefetch_tiles)
        return web.json_response({"ok": True, "restarted": restarted})

    def _not_running(self, route_id):
        if self.car_status.get("route") == route_id:
            raise web.HTTPConflict(text="Esta rota está em uso na navegação. Clique em Parar ou troque de rota antes.")

    async def route_rename(self, request):
        body = await request.json()
        route_id = body.get("id", "")
        self._not_running(route_id)
        try:
            new_id = routelib.rename_route(route_id, body.get("name"))
        except (OSError, ValueError) as error:
            raise web.HTTPBadRequest(text=str(error))
        if self.selected == route_id:
            self._apply_route(new_id)
            self._save_state()
        self.bridge.event(f"Rota {os.path.basename(route_id)[:-4]} renomeada para {os.path.basename(new_id)[:-4]}")
        return web.json_response({"ok": True, "id": new_id})

    async def route_delete(self, request):
        body = await request.json()
        route_id = body.get("id", "")
        self._not_running(route_id)
        try:
            trash = routelib.delete_route(route_id)
        except (OSError, ValueError) as error:
            raise web.HTTPBadRequest(text=str(error))
        if self.selected == route_id:
            self._apply_route(None)
            self._save_state()
        self.bridge.event(f"Rota {os.path.basename(route_id)[:-4]} excluída")
        return web.json_response({"ok": True, "trash": trash})

    # -------------------------------------------------------- bags
    async def api_bags(self, request):
        return web.json_response(self.bags.listing())

    async def bag_record_start(self, request):
        body = await request.json()
        try:
            name = await self.bags.start_record(body.get("name"))
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error))
        self.bridge.event(f"Gravação do bag {name} iniciada")
        return web.json_response({"ok": True, "name": name})

    async def bag_record_stop(self, request):
        name = await self.bags.stop_record()
        self.bridge.event(f"Bag {name} salvo")
        return web.json_response({"ok": True, "name": name})

    async def bag_play(self, request):
        body = await request.json()
        # O bag repete as referências de tração e esterçamento: com o carro no
        # ar elas chegariam ao ESP32. Só com o Odroid fora da rede.
        with self.bridge.lock:
            ping, esp_live = self.bridge.odroid_ping, self.bridge.esp_live and not self.bridge.replay
        if ping is not False or esp_live:
            raise web.HTTPConflict(text="Reprodução bloqueada: o Odroid está respondendo. O bag enviaria comandos ao carro. Desligue o Odroid ou desconecte o cabo.")
        if self.car_status.get("manual") or self.car_status.get("navigation"):
            raise web.HTTPConflict(text="Clique em Parar antes: a navegação no ar se misturaria com os dados do bag.")
        try:
            name = await self.bags.play(body.get("name"))
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error))
        self.bridge.event(f"Reprodução do bag {name} iniciada")
        return web.json_response({"ok": True})

    async def bag_pause(self, request):
        return web.json_response({"ok": await self.bags.toggle_pause()})

    async def bag_stop(self, request):
        await self.bags.stop_play()
        self.bridge.event("Reprodução do bag parada")
        return web.json_response({"ok": True})

    async def record_start(self, request):
        body = await request.json()
        snapshot = self.bridge.snapshot(self.car_status)
        if snapshot["mode"]["automatic"]:
            raise web.HTTPConflict(text="Não dá para gravar durante o automático.")
        try:
            with self.bridge.lock:
                name = self.bridge.recorder.start(time.time(), body.get("name") or None)
                self.bridge.event(f"Gravação iniciada: {name}")
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error))
        return web.json_response({"ok": True, "name": name})

    async def record_stop(self, request):
        body = await request.json()
        with self.bridge.lock:
            if not self.bridge.recorder.active:
                raise web.HTTPConflict(text="Nenhuma gravação em andamento.")
            if not body.get("save", True):
                name = self.bridge.recorder.name
                self.bridge.recorder.discard()
                self.bridge.event(f"Gravação descartada: {name}")
                return web.json_response({"ok": True, "saved": False})
            try:
                summary = self.bridge.recorder.finish()
            except (OSError, ValueError) as error:
                raise web.HTTPBadRequest(text=str(error))
            self.bridge.event(f"Rota {summary['name']} salva: {summary['length_m']} m")
        return web.json_response({"ok": True, "saved": True, "summary": summary})

    async def clear_trail(self, request):
        self.bridge.clear_trail()
        return web.json_response({"ok": True})

    async def clear_errors(self, request):
        with self.bridge.lock:
            self.bridge.errors.clear()
            self.bridge.event("Histórico de erros limpo")
        return web.json_response({"ok": True})

    # -------------------------------------------------------- mapa
    def fetch_tile(self, z, y, x):
        """Guarda a imagem no disco: na pista, sem internet, usa a cópia salva."""
        path = os.path.join(TILES, f"{z}_{y}_{x}.jpg")
        if os.path.exists(path):
            return path
        key = (z, y, x)
        if time.time() - self.failed_tiles.get(key, 0.0) < 60.0:
            return None
        request = urllib.request.Request(TILE_URL.format(z=z, y=y, x=x), headers={"User-Agent": "vida-painel/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=6) as response:
                content = response.read()
        except (urllib.error.URLError, OSError, ValueError):
            self.failed_tiles[key] = time.time()
            return None
        os.makedirs(TILES, exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
        return path

    def prefetch_tiles(self):
        import math
        route = self.bridge.route
        if not route:
            return
        scale = 2 ** TILE_ZOOM
        columns, rows = [], []
        for latitude, longitude in route["points"]:
            columns.append((longitude + 180.0) / 360.0 * scale)
            rows.append((1.0 - math.log(math.tan(math.radians(latitude)) + 1.0 / math.cos(math.radians(latitude))) / math.pi) / 2.0 * scale)
        for x in range(int(min(columns)) - 1, int(max(columns)) + 2):
            for y in range(int(min(rows)) - 1, int(max(rows)) + 2):
                self.fetch_tile(TILE_ZOOM, y, x)

    async def tile(self, request):
        z, y, x = (int(request.match_info[k]) for k in ("z", "y", "x"))
        if z != TILE_ZOOM or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise web.HTTPBadRequest()
        path = await asyncio.get_running_loop().run_in_executor(None, self.fetch_tile, z, y, x)
        if path is None:
            raise web.HTTPServiceUnavailable(text="sem imagem salva e sem internet")
        return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})


def build_app(bridge, control):
    state = App(bridge, control)
    app = web.Application(middlewares=[only_local])
    app["state"] = state
    app.router.add_get("/", state.index)
    app.router.add_get("/ws", state.websocket)
    app.router.add_get("/api/routes", state.api_routes)
    app.router.add_get("/api/route", state.api_route)
    app.router.add_post("/api/start", state.start)
    app.router.add_post("/api/stop", state.stop)
    app.router.add_post("/api/select", state.select)
    app.router.add_post("/api/errors/clear", state.clear_errors)
    app.router.add_post("/api/trail/clear", state.clear_trail)
    app.router.add_post("/api/route/rename", state.route_rename)
    app.router.add_post("/api/route/delete", state.route_delete)
    app.router.add_get("/api/bags", state.api_bags)
    app.router.add_post("/api/bag/record/start", state.bag_record_start)
    app.router.add_post("/api/bag/record/stop", state.bag_record_stop)
    app.router.add_post("/api/bag/play", state.bag_play)
    app.router.add_post("/api/bag/pause", state.bag_pause)
    app.router.add_post("/api/bag/stop", state.bag_stop)
    app.router.add_post("/api/record/start", state.record_start)
    app.router.add_post("/api/record/stop", state.record_stop)
    app.router.add_get(r"/tiles/{z:\d+}/{y:\d+}/{x:\d+}", state.tile)
    app.router.add_static("/static", WEB)

    async def background(app):
        app["poll"] = asyncio.create_task(state.poll_car())
        app["link"] = asyncio.create_task(state.poll_link())
        asyncio.get_running_loop().run_in_executor(None, state.prefetch_tiles)

    app.on_startup.append(background)
    return app


def main():
    bridge = start_ros()
    web.run_app(build_app(bridge, DockerControl()), host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
