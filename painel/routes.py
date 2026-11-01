"""Rotas salvas em /caminhos e gravação de rotas novas pelos fixes do GPS."""
import datetime as dt
import glob
import math
import os
import re

import numpy as np
from scipy.ndimage import gaussian_filter1d
from vida_navigation.geo import Origin, to_geodetic, to_local
from vida_navigation.path import PathFile

ROOT = os.environ.get("VIDA_CAMINHOS", "/caminhos")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# 20 graus com o entre-eixos medido em 01/11 (~1,2 m).
MIN_RADIUS_M = 3.3
SPACING_M = 0.25
SMOOTHING_M = 1.0
CLOSE_LOOP_M = 3.0
MIN_LENGTH_M = 10.0


def _chown(path):
    uid = int(os.environ.get("VIDA_UID", "0") or 0)
    if uid:
        try:
            os.chown(path, uid, uid)
        except OSError:
            pass


def length_m(latitude, longitude):
    x, y = to_local(Origin(float(latitude[0]), float(longitude[0])), np.asarray(latitude), np.asarray(longitude))
    return float(np.hypot(np.diff(x), np.diff(y)).sum())


def list_routes(root=None):
    root = root or ROOT
    routes = []
    for filename in glob.glob(os.path.join(root, "*", "*.csv")):
        base = os.path.basename(filename)
        if base.endswith("_bruta.csv"):
            continue
        try:
            with open(filename, encoding="utf-8") as stream:
                if "vida_path" not in stream.readline():
                    continue
            path = PathFile.load(filename)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        source = path.meta.get("source", "")
        folder = os.path.basename(os.path.dirname(filename))
        routes.append({
            "id": os.path.relpath(filename, root), "name": base[:-4], "folder": folder,
            "length_m": round(length_m(path.latitude_deg, path.longitude_deg)),
            "closed": bool(path.closed),
            # Traçado que não veio do carro não deve ir direto para o automático.
            "warning": "não gravada no carro" if "google_earth" in source else "",
            "mtime": os.path.getmtime(filename),
        })
    routes.sort(key=lambda r: (r["folder"], r["mtime"]), reverse=True)
    return routes


def resolve(route_id, root=None):
    """Caminho absoluto de uma rota da lista; ValueError fora dela."""
    root = root or ROOT
    for route in list_routes(root):
        if route["id"] == route_id:
            return os.path.join(root, route_id)
    raise ValueError(f"rota desconhecida: {route_id}")


def load_points(route_id, root=None):
    path = PathFile.load(resolve(route_id, root))
    return {"id": route_id, "closed": bool(path.closed),
            "points": [[round(float(a), 8), round(float(b), 8)] for a, b in zip(path.latitude_deg, path.longitude_deg)]}


def _siblings(path):
    """Arquivos da rota: CSV, bruta e gráficos."""
    stem = path[:-4]
    return [p for p in (path, f"{stem}.png", f"{stem}_bruta.csv", f"{stem}_bruta.png") if os.path.exists(p)]


def rename_route(route_id, new_name, root=None):
    root = root or ROOT
    path = resolve(route_id, root)
    new_name = (new_name or "").strip()
    if not NAME_RE.match(new_name):
        raise ValueError("use só letras, números, _ e - no nome da rota")
    if any(r["name"] == new_name for r in list_routes(root)):
        raise ValueError(f"já existe uma rota chamada {new_name}")
    stem, folder = path[:-4], os.path.dirname(path)
    old = os.path.basename(stem)
    for item in _siblings(path):
        target = os.path.join(folder, new_name + os.path.basename(item)[len(old):])
        os.rename(item, target)
        if target.endswith(".csv"):
            with open(target, encoding="utf-8") as stream:
                lines = stream.readlines()
            lines = [f"# name: {new_name}\n" if line.startswith("# name:") else line for line in lines]
            with open(target, "w", encoding="utf-8") as stream:
                stream.writelines(lines)
            _chown(target)
    return os.path.relpath(os.path.join(folder, f"{new_name}.csv"), root)


def delete_route(route_id, root=None):
    """Move para .lixeira, fora da lista: um clique errado não perde a rota."""
    root = root or ROOT
    path = resolve(route_id, root)
    trash = os.path.join(root, ".lixeira", dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
    os.makedirs(trash, exist_ok=True)
    _chown(os.path.dirname(trash))
    _chown(trash)
    for item in _siblings(path):
        os.replace(item, os.path.join(trash, os.path.basename(item)))
    return os.path.relpath(trash, root)


def next_name(root=None):
    root = root or ROOT
    numbers = [int(m.group(1)) for f in glob.glob(os.path.join(root, "*", "rota_*.csv"))
               if (m := re.match(r"rota_(\d+)", os.path.basename(f)))]
    return f"rota_{max(numbers, default=0) + 1}"


def _derivative(values, closed):
    if closed:
        return (np.roll(values, -1) - np.roll(values, 1)) / 2.0
    return np.gradient(values)


def _curvature(x, y, closed):
    dx, dy = _derivative(x, closed), _derivative(y, closed)
    ds = np.maximum(np.hypot(dx, dy), 1e-6)
    tx, ty = dx / ds, dy / ds
    return (tx * _derivative(ty, closed) - ty * _derivative(tx, closed)) / ds


def smooth_route(latitude, longitude, spacing=SPACING_M, sigma=SMOOTHING_M):
    """Mesmo processo da rota 6: reamostra por arco e suaviza com gaussiana de 1 m.

    Devolve (lat, lon, fechada, comprimento, raio mínimo, metros abaixo do raio mínimo)."""
    latitude, longitude = np.asarray(latitude, float), np.asarray(longitude, float)
    origin = Origin(float(latitude[0]), float(longitude[0]))
    x, y = to_local(origin, latitude, longitude)
    x, y = np.asarray(x), np.asarray(y)
    s = np.r_[0.0, np.cumsum(np.hypot(np.diff(x), np.diff(y)))]
    keep = np.r_[True, np.diff(s) > 1e-3]
    x, y, s = x[keep], y[keep], s[keep]
    if s[-1] < MIN_LENGTH_M:
        raise ValueError(f"gravação curta demais ({s[-1]:.1f} m; mínimo {MIN_LENGTH_M:.0f} m)")
    closure = float(math.hypot(x[-1] - x[0], y[-1] - y[0]))
    closed = closure <= CLOSE_LOOP_M and s[-1] > 3 * CLOSE_LOOP_M
    step = 0.1
    if closed:
        total = s[-1] + closure
        grid = np.arange(0.0, total, step)
        xs, ys = np.interp(grid, np.r_[s, total], np.r_[x, x[0]]), np.interp(grid, np.r_[s, total], np.r_[y, y[0]])
        mode = "wrap"
    else:
        grid = np.arange(0.0, s[-1], step)
        xs, ys = np.interp(grid, s, x), np.interp(grid, s, y)
        mode = "nearest"
    xg, yg = gaussian_filter1d(xs, sigma / step, mode=mode), gaussian_filter1d(ys, sigma / step, mode=mode)
    seg = np.hypot(np.diff(xg), np.diff(yg))
    cum = np.r_[0.0, np.cumsum(seg)]
    if closed:
        end = float(math.hypot(xg[0] - xg[-1], yg[0] - yg[-1]))
        length = cum[-1] + end
        final = np.arange(0.0, length, spacing)
        xr, yr = np.interp(final, np.r_[cum, length], np.r_[xg, xg[0]]), np.interp(final, np.r_[cum, length], np.r_[yg, yg[0]])
    else:
        length = cum[-1]
        final = np.arange(0.0, length, spacing)
        xr, yr = np.interp(final, cum, xg), np.interp(final, cum, yg)
    # Na malha suavizada: reamostrar antes de derivar criaria curvas falsas.
    magnitude = np.abs(_curvature(xg, yg, closed))
    min_radius = float(1.0 / max(magnitude.max(), 1e-6))
    below = float(np.sum(magnitude > 1.0 / MIN_RADIUS_M) * step)
    lat, lon = to_geodetic(origin, xr, yr)
    return np.asarray(lat), np.asarray(lon), closed, float(length), min_radius, below


class RouteRecorder:
    """Guarda os fixes do GPS enquanto a gravação está ligada. Thread-safe pelo chamador."""

    def __init__(self, root=None):
        self.root = root or ROOT
        self.name = None
        self.points = []
        self.not_fixed = 0
        self.started = 0.0

    @property
    def active(self):
        return self.name is not None

    def start(self, now, name=None):
        if self.active:
            raise ValueError("já existe uma gravação em andamento")
        name = (name or next_name(self.root)).strip()
        if not NAME_RE.match(name):
            raise ValueError("use só letras, números, _ e - no nome da rota")
        if any(r["name"] == name for r in list_routes(self.root)):
            raise ValueError(f"já existe uma rota chamada {name}")
        self.name, self.points, self.not_fixed, self.started = name, [], 0, now
        return name

    def add(self, latitude, longitude, fixed):
        if not self.active or not (math.isfinite(latitude) and math.isfinite(longitude)):
            return
        if self.points:
            lat0, lon0 = self.points[-1]
            x, y = to_local(Origin(lat0, lon0), latitude, longitude)
            if math.hypot(float(x), float(y)) < 0.05:
                return
        self.points.append((float(latitude), float(longitude)))
        if not fixed:
            self.not_fixed += 1

    def length(self):
        if len(self.points) < 2:
            return 0.0
        data = np.array(self.points)
        return length_m(data[:, 0], data[:, 1])

    def status(self, now):
        if not self.active:
            return None
        return {"name": self.name, "points": len(self.points), "length_m": round(self.length(), 1),
                "seconds": round(now - self.started), "not_fixed": self.not_fixed}

    def discard(self):
        self.name, self.points = None, []

    def finish(self, today=None):
        """Salva a gravação bruta e a rota final; devolve o resumo."""
        if not self.active:
            raise ValueError("nenhuma gravação em andamento")
        name, points, not_fixed = self.name, self.points, self.not_fixed
        if len(points) < 4:
            raise ValueError(f"só {len(points)} pontos gravados: confira o GPS e ande com o veículo")
        data = np.array(points)
        lat, lon, closed, length, radius, below = smooth_route(data[:, 0], data[:, 1])
        folder = os.path.join(self.root, (today or dt.date.today()).isoformat())
        os.makedirs(folder, exist_ok=True)
        _chown(folder)
        raw = os.path.join(folder, f"{name}_bruta.csv")
        PathFile(data[:, 0], data[:, 1], closed, {"name": name, "source": "gps_fix_painel",
                                                  "skipped_not_fixed": "0", "not_fixed": str(not_fixed)}).save(raw)
        final = os.path.join(folder, f"{name}.csv")
        PathFile(lat, lon, closed, {"name": name, "source": "gps_fix_painel_suavizado",
                                    "spacing_m": f"{SPACING_M}", "smoothing_m": f"{SMOOTHING_M}"}).save(final)
        _chown(raw)
        _chown(final)
        self.discard()
        return {"name": name, "id": os.path.relpath(final, self.root), "closed": bool(closed),
                "length_m": round(length, 1), "min_radius_m": round(radius, 2),
                "below_min_radius_m": round(below, 1), "not_fixed_points": not_fixed}
