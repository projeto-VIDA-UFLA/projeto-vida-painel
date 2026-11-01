import math
import datetime as dt

import numpy as np
import pytest
from vida_navigation.geo import Origin, to_geodetic
from vida_navigation.path import PathFile

from painel import flags, logic, routes
from painel.control import DockerControl, route_from_env


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
    ("rtk", "NONE", "RTK"), ("gamepad_fresh", False, "Gamepad"), ("nav_ready", False, "Navegação"), ("route", False, "Rota")])
def test_each_condition_blocks(key, value, label):
    c = base()
    c[key] = value
    failed = [i["label"] for i in logic.entry_checks(c) if not i["ok"]]
    assert len(failed) == 1 and failed[0].startswith(label)


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


def test_compose_commands_are_fixed():
    control = DockerControl("/projeto", dry_run=True)
    import asyncio
    asyncio.run(control.start_navigation("2026-11-01/rota_6.csv"))
    assert control.commands[-1][-3:] == ["-d", "--no-build", "navegacao"]
    assert asyncio.run(control.status())["route"] == "2026-11-01/rota_6.csv"
    asyncio.run(control.stop())
    assert asyncio.run(control.status())["navigation"] is False
    assert route_from_env(["A=1", "VIDA_PATH_FILE=/caminhos/x/y.csv"]) == "x/y.csv"
