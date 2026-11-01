import math
import datetime as dt

import numpy as np
import pytest
from vida_navigation.geo import Origin, to_geodetic
from vida_navigation.path import PathFile

from painel import flags, logic, routes
from painel.control import DockerControl, route_from_command


def base():
    return dict(route=True, distance=1.0, max_distance=3.0, motion_permitted=True, flags=0, esp_fresh=True,
                stationary=True, ekf_fresh=True, age=1.0, age_max=30.0, gnss_fresh=True, fix_ok=True,
                corrections=True, rtk="FIXED", gamepad_fresh=True, nav_ready=True, phase_text="rodando")


def test_ready_when_all_ok():
    items = logic.entry_checks(base())
    assert all(i["ok"] for i in items)
    assert "△" in logic.next_step(items)


@pytest.mark.parametrize("key,value,label", [
    ("distance", 5.6, "Veículo até"), ("distance", None, "Veículo até"), ("motion_permitted", False, "ESP32"),
    ("stationary", False, "Veículo parado"), ("age", 45.0, "Correção"), ("corrections", False, "Correção"),
    ("rtk", "NONE", "RTK"), ("gamepad_fresh", False, "Controle"), ("nav_ready", False, "Navegação"), ("route", False, "Rota")])
def test_each_condition_blocks(key, value, label):
    c = base()
    c[key] = value
    failed = [i["label"] for i in logic.entry_checks(c) if not i["ok"]]
    assert len(failed) == 1 and failed[0].startswith(label)


def test_no_start_hint_when_car_is_running():
    c = base()
    c.update(nav_ready=False, car_running=True)
    items = logic.entry_checks(c)
    assert "Iniciar" not in logic.next_step(items, True) and "Iniciar" in logic.next_step(items, False)


def test_controller_off_is_shown():
    c = base()
    c.update(gamepad_fresh=False, gamepad_text="desligado")
    item = [i for i in logic.entry_checks(c) if i["label"] == "Controle conectado"][0]
    assert item["value"] == "desligado" and not item["ok"]
    assert "controle" in logic.next_step(logic.entry_checks(c))
    assert "desligado" in flags.decode(4)[0][2]


def test_float_is_accepted_like_mode_manager():
    c = base()
    c["rtk"] = "FLOAT"
    assert all(i["ok"] for i in logic.entry_checks(c))


def test_battery_scale():
    assert logic.battery_percent(36.0) == 0
    assert logic.battery_percent(38.7) == 100
    assert logic.battery_percent(36.7) == pytest.approx(25.93, abs=.01)
    assert logic.battery_percent(30.0) == 0 and logic.battery_percent(45.0) == 100
    assert logic.battery_percent(float("nan")) is None
    assert [logic.battery_level(p) for p in (80, 26, 5, None)] == ["ok", "warn", "bad", "old"]


def test_battery_only_updates_at_rest():
    b = logic.BatteryTracker()
    b.update(0.0, 36.7, True, False, False)
    b.update(59.0, 36.7, True, False, False)
    assert b.percent is None
    b.update(60.0, 36.7, True, False, False)
    assert b.percent == pytest.approx(25.93, abs=.01)
    b.update(61.0, 34.1, True, True, True)
    assert b.percent == pytest.approx(25.93, abs=.01) and b.minimum == 34.1
    b.update(62.0, 36.2, True, False, False)
    assert b.rest_since == 62.0 and not b.resting(100.0)


def test_mode_labels_and_block():
    assert logic.mode_label(logic.NAV_MANUAL, False, False, True)[0] == "MANUAL"
    assert logic.mode_label(None, False, False, True)[0] == "MANUAL"
    assert logic.mode_label(None, False, False, False)[0] == "SEM COMANDO"
    assert logic.mode_label(logic.NAV_AUTOMATIC, False, False, True)[:2] == ("AUTOMÁTICO", "Seguindo a rota")
    assert logic.mode_label(logic.NAV_MANUAL, True, False, True)[0] == "MANOBRA"
    assert logic.control_block(logic.NAV_MANUAL, False, 0.0) is None
    assert logic.control_block(None, False, None) is None
    assert logic.control_block(logic.NAV_MANEUVER, False, 0.0)
    assert logic.control_block(logic.NAV_MANUAL, True, 0.0)
    assert logic.control_block(logic.NAV_MANUAL, False, 1.0)


def test_link_status():
    # ping, ms, esp_publica, esp_dados, odroid_publica, odroid_dados, replay
    assert logic.link_status(None, None, False, False, False, False, False)[1] == "VERIFICANDO"
    assert logic.link_status(True, 1.2, True, True, True, True, False)[:2] == ("ok", "CONECTADOS")
    assert logic.link_status(True, 1.2, False, False, True, True, False)[:2] == ("bad", "SEM ESP32")
    assert logic.link_status(True, 1.2, True, True, False, False, False)[1] == "SEM NÓS DO ODROID"
    assert logic.link_status(True, 1.2, False, False, False, False, False)[1] == "ODROID SEM DADOS"
    assert logic.link_status(False, None, False, False, False, False, False)[:2] == ("bad", "SEM ODROID")
    # O bag entrega dados, mas o publicador não é o carro: não pode aparecer conectado.
    assert logic.link_status(False, None, False, True, False, True, True)[1] == "SEM ODROID"
    assert logic.link_status(True, 1.2, False, True, False, True, True)[1] == "ODROID SEM DADOS"


def test_flags_decode_and_json():
    assert [f[0] for f in flags.decode(1 | 512)] == [1, 512]
    assert flags.decode(512)[0][3] is True
    assert flags.decode(2048)[0][1] == "flag desconhecida"
    assert logic.clean_json({"a": float("nan"), "b": [1.0, float("inf")]}) == {"a": None, "b": [1.0, None]}


def circle(radius=10.0, n=200, turns=1.0, noise=0.0):
    origin = Origin(-21.2238, -44.9732)
    t = np.linspace(0, 2 * math.pi * turns, n, endpoint=False)
    rng = np.random.default_rng(1)
    x = radius * np.cos(t) + noise * rng.standard_normal(n)
    y = radius * np.sin(t) + noise * rng.standard_normal(n)
    return to_geodetic(origin, x, y)


def test_smooth_closed_circle():
    lat, lon = circle(noise=0.01)
    a, b, closed, length, radius, below = routes.smooth_route(lat, lon)
    assert closed
    assert length == pytest.approx(2 * math.pi * 10, rel=.03)
    assert radius == pytest.approx(10, rel=.15)
    assert below == 0


def test_open_route_and_tight_curve():
    lat, lon = circle(radius=20, turns=.5)
    assert not routes.smooth_route(lat, lon)[2]
    lat, lon = circle(radius=2.5)
    assert routes.smooth_route(lat, lon)[5] > 0


def test_recorder_saves_and_lists(tmp_path):
    rec = routes.RouteRecorder(str(tmp_path))
    assert rec.start(0.0) == "rota_1"
    with pytest.raises(ValueError):
        rec.start(0.0)
    lat, lon = circle(noise=0.005)
    for a, b in zip(lat, lon):
        rec.add(float(a), float(b), True)
    assert rec.status(10.0)["points"] == 200
    summary = rec.finish(dt.date(2026, 11, 2))
    assert summary["closed"] and summary["id"] == "2026-11-02/rota_1.csv"
    listed = routes.list_routes(str(tmp_path))
    assert [r["name"] for r in listed] == ["rota_1"]     # a bruta fica de fora
    assert routes.next_name(str(tmp_path)) == "rota_2"
    assert PathFile.load(tmp_path / "2026-11-02" / "rota_1.csv").closed
    assert routes.load_points("2026-11-02/rota_1.csv", str(tmp_path))["closed"]
    rec = routes.RouteRecorder(str(tmp_path))
    with pytest.raises(ValueError):
        rec.start(0.0, "rota_1")
    with pytest.raises(ValueError):
        rec.start(0.0, "../fora")


def test_rename_and_delete_route(tmp_path):
    rec = routes.RouteRecorder(str(tmp_path))
    rec.start(0.0)
    lat, lon = circle(noise=0.005)
    for a, b in zip(lat, lon):
        rec.add(float(a), float(b), True)
    route_id = rec.finish(dt.date(2026, 11, 2))["id"]
    new_id = routes.rename_route(route_id, "pista_kart", str(tmp_path))
    assert new_id == "2026-11-02/pista_kart.csv"
    folder = tmp_path / "2026-11-02"
    assert sorted(p.name for p in folder.iterdir()) == ["pista_kart.csv", "pista_kart_bruta.csv"]
    assert PathFile.load(folder / "pista_kart.csv").meta["name"] == "pista_kart"
    for bad in ("../x", "", "pista_kart"):
        with pytest.raises(ValueError):
            routes.rename_route(new_id, bad, str(tmp_path))
    with pytest.raises(ValueError):
        routes.rename_route("../../etc/passwd", "x", str(tmp_path))
    trash = routes.delete_route(new_id, str(tmp_path))
    assert routes.list_routes(str(tmp_path)) == []
    assert (tmp_path / trash / "pista_kart.csv").exists()     # recuperável
    with pytest.raises(ValueError):
        routes.delete_route(new_id, str(tmp_path))


def test_bag_list_and_validation(tmp_path):
    import asyncio
    from painel.bags import BagManager, list_bags
    bag = tmp_path / "teste1"
    bag.mkdir()
    (bag / "metadata.yaml").write_text("rosbag2_bagfile_information:\n  duration:\n    nanoseconds: 31000000000\n")
    (tmp_path / "sem_metadata").mkdir()
    listed = list_bags(str(tmp_path))
    assert [b["name"] for b in listed] == ["teste1"] and listed[0]["seconds"] == 31
    manager = BagManager(str(tmp_path))
    for bad in ("../x", "", "teste1"):
        with pytest.raises(ValueError):
            asyncio.run(manager.start_record(bad))
    with pytest.raises(ValueError):
        asyncio.run(manager.play("nao_existe"))
    assert manager.status() == {"recording": None, "playing": None}


def test_bag_rename_and_delete(tmp_path):
    from painel.bags import BagManager, list_bags
    bag = tmp_path / "teste1"
    bag.mkdir()
    (bag / "metadata.yaml").write_text("rosbag2_bagfile_information:\n  duration:\n    nanoseconds: 1000000000\n")
    (bag / "teste1_0.db3").write_text("x")
    manager = BagManager(str(tmp_path))
    for bad in ("../x", "", "teste1"):
        with pytest.raises(ValueError):
            manager.rename("teste1", bad)
    assert manager.rename("teste1", "pista_1") == "pista_1"
    assert (tmp_path / "pista_1" / "teste1_0.db3").exists()
    with pytest.raises(ValueError):
        manager.rename("../../etc", "x")
    manager.recording = {"name": "pista_1"}
    for action in (lambda: manager.rename("pista_1", "y"), lambda: manager.delete("pista_1")):
        with pytest.raises(ValueError):
            action()
    manager.recording = None
    trash = manager.delete("pista_1")
    assert list_bags(str(tmp_path)) == []
    assert (tmp_path / trash / "pista_1" / "metadata.yaml").exists()
    with pytest.raises(ValueError):
        manager.delete("pista_1")


def test_bag_elapsed_ignores_pauses():
    from painel.bags import BagManager
    manager = BagManager("/nao/existe")
    manager.player = {"started": 100.0, "total": 31, "paused_at": None, "paused_total": 0.0}
    assert manager.player_elapsed(110.0) == 10
    manager.player.update(paused_at=110.0)
    assert manager.player_elapsed(115.0) == 10           # parado na pausa
    manager.player.update(paused_at=None, paused_total=5.0)
    assert manager.player_elapsed(120.0) == 15
    assert manager.player_elapsed(500.0) == 31           # não passa do total
    manager.player.update(total=None)
    assert manager.player_elapsed(120.0) == 15


def test_recorder_rejects_short(tmp_path):
    rec = routes.RouteRecorder(str(tmp_path))
    rec.start(0.0)
    lat, lon = circle(radius=.5, n=20)
    for a, b in zip(lat, lon):
        rec.add(float(a), float(b), True)
    with pytest.raises(ValueError):
        rec.finish()


def test_resolve_only_listed(tmp_path):
    with pytest.raises(ValueError):
        routes.resolve("../../etc/passwd", str(tmp_path))


def test_trail_is_sent_only_once():
    """Ao abrir a página, o rastro duplicado criava uma reta do fim até o começo."""
    import threading
    from types import SimpleNamespace
    from painel.bridge import Bridge
    bridge = SimpleNamespace(lock=threading.RLock(), trail=[(1, 1), (2, 2)], trail_generation=3)
    generation, index, points, reset = Bridge.trail_since(bridge, -1, 0)
    assert (generation, index, points, reset) == (3, 2, [(1, 1), (2, 2)], True)
    assert Bridge.trail_since(bridge, generation, index)[2] == []
    bridge.trail.append((3, 3))
    generation, index, points, reset = Bridge.trail_since(bridge, generation, index)
    assert (index, points, reset) == (3, [(3, 3)], False)


def test_clear_trail_reaches_open_pages():
    import threading
    from types import SimpleNamespace
    from painel.bridge import Bridge
    bridge = SimpleNamespace(lock=threading.RLock(), trail=[(1, 1), (2, 2)], trail_generation=0)
    generation, index, _, _ = Bridge.trail_since(bridge, -1, 0)
    Bridge.clear_trail(bridge)
    assert bridge.trail == []
    assert Bridge.trail_since(bridge, generation, index) == (1, 0, [], True)
    bridge.trail.append((3, 3))
    assert Bridge.trail_since(bridge, 1, 0)[2] == [(3, 3)]


def _request(sockname, host, origin=None, method="GET"):
    from types import SimpleNamespace
    from aiohttp.test_utils import make_mocked_request
    headers = {"Host": host}
    if origin:
        headers["Origin"] = origin
    transport = SimpleNamespace(get_extra_info=lambda name, default=None: (sockname, 8080) if name == "sockname" else default)
    return make_mocked_request(method, "/", headers=headers, transport=transport)


@pytest.mark.parametrize("sockname,host,origin,method,allowed", [
    ("127.0.0.1", "localhost:8080", None, "GET", True),
    ("127.0.0.1", "localhost:8080", "http://localhost:8080", "POST", True),
    ("127.0.0.1", "localhost:8080", None, "POST", False),                  # sem origem
    ("127.0.0.1", "localhost:8080", "http://mau.example", "POST", False),
    ("127.0.0.1", "notebook-vida:8080", None, "GET", False),                  # DNS rebinding
    ("100.64.0.10", "notebook-vida:8080", None, "GET", True),
    ("100.64.0.10", "Notebook-Vida:8080", None, "GET", True),
    ("100.64.0.10", "100.64.0.10:8080", None, "GET", True),
    ("100.64.0.10", "notebook-vida:8080", "http://notebook-vida:8080", "POST", False),   # só visualização
    ("100.64.0.10", "localhost:8080", "http://localhost:8080", "POST", False),     # forjar Host não dá comando
    ("100.64.0.10", "localhost:8080", None, "GET", False),
    ("100.64.0.10", "outro:8080", None, "GET", False),
])
def test_access_rules(monkeypatch, sockname, host, origin, method, allowed):
    import asyncio
    from aiohttp import web
    from painel import server
    monkeypatch.setattr(server, "PORT", 8080)
    monkeypatch.setattr(server, "TAILSCALE_IP", "100.64.0.10")
    monkeypatch.setattr(server, "REMOTE_NAMES", "notebook-vida,notebook-vida.exemplo.ts.net")
    async def handler(request):
        return "ok"
    async def call():
        return await server.access_rules(_request(sockname, host, origin, method), handler)
    if allowed:
        assert asyncio.run(call()) == "ok"
    else:
        with pytest.raises(web.HTTPForbidden):
            asyncio.run(call())


def test_compose_commands_are_fixed():
    control = DockerControl("/projeto", dry_run=True)
    import asyncio
    asyncio.run(control.start_navigation("2026-11-01/rota_6.csv"))
    assert control.commands[-1][-3:] == ["-d", "--no-build", "navegacao"]
    assert asyncio.run(control.status())["route"] == "2026-11-01/rota_6.csv"
    asyncio.run(control.stop())
    assert asyncio.run(control.status())["navigation"] is False
    assert route_from_command(["bash", "-lc", "exec ros2 launch vida_navigation navigation.launch.py path_file:=/caminhos/x/y.csv"]) == "x/y.csv"
    assert route_from_command(["bash"]) is None
