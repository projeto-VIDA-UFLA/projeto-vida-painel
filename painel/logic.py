"""Regras do painel, sem ROS, para poderem ser testadas sozinhas."""
import math

# Escala medida com o carro parado (01/11). Sob carga a tensão cai, então a carga
# só é recalculada depois de BATTERY_REST_S parado e sem tração.
BATTERY_EMPTY_V = 36.0
BATTERY_FULL_V = 38.7
BATTERY_REST_S = 60.0
BATTERY_GREEN_PERCENT = 50.0
BATTERY_YELLOW_PERCENT = 20.0

STOPPED_SPEED_M_S = 0.05


def battery_percent(voltage):
    if voltage is None or not math.isfinite(voltage):
        return None
    percent = (voltage - BATTERY_EMPTY_V) / (BATTERY_FULL_V - BATTERY_EMPTY_V) * 100.0
    return max(0.0, min(100.0, percent))


def battery_level(percent):
    if percent is None:
        return "old"
    if percent >= BATTERY_GREEN_PERCENT:
        return "ok"
    return "warn" if percent >= BATTERY_YELLOW_PERCENT else "bad"


class BatteryTracker:
    """Carga pela tensão em repouso e menor tensão da sessão."""

    def __init__(self):
        self.percent = None
        self.rest_since = None
        self.minimum = None

    def update(self, now, voltage, valid, moving, traction):
        if not valid or voltage is None or not math.isfinite(voltage):
            self.rest_since = None
            return
        self.minimum = voltage if self.minimum is None else min(self.minimum, voltage)
        if moving or traction:
            self.rest_since = None
            return
        if self.rest_since is None:
            self.rest_since = now
        if now - self.rest_since >= BATTERY_REST_S:
            self.percent = battery_percent(voltage)

    def resting(self, now):
        return self.rest_since is not None and now - self.rest_since >= BATTERY_REST_S


NAV_MANUAL, NAV_PREPARING, NAV_CALIBRATING, NAV_MANEUVER, NAV_AUTOMATIC, NAV_STOPPING = range(6)
NAV_STEPS = {NAV_PREPARING: "Preparando", NAV_CALIBRATING: "Calibrando", NAV_MANEUVER: "Reta inicial",
             NAV_AUTOMATIC: "Seguindo a rota", NAV_STOPPING: "Parando"}


def mode_label(nav_mode, maneuver, bench, car_running):
    """(texto, detalhe, cor). A troca de modo continua sendo só pelo controle."""
    if maneuver:
        return "MANOBRA", "freio solto para empurrar", "warn"
    if bench:
        return "BANCADA", "veículo suspenso", "warn"
    if nav_mode in (NAV_PREPARING, NAV_CALIBRATING, NAV_MANEUVER, NAV_AUTOMATIC):
        return "AUTOMÁTICO", NAV_STEPS[nav_mode], "ok"
    if nav_mode == NAV_STOPPING:
        return "AUTOMÁTICO", NAV_STEPS[nav_mode], "warn"
    if nav_mode == NAV_MANUAL or car_running:
        return "MANUAL", "", "blue"
    return "SEM COMANDO", "carro não iniciado", "old"


def in_automatic(nav_mode):
    return nav_mode in (NAV_PREPARING, NAV_CALIBRATING, NAV_MANEUVER, NAV_AUTOMATIC, NAV_STOPPING)


def control_block(nav_mode, maneuver, speed):
    """Motivo para bloquear a troca de rota ou o início; None libera."""
    if in_automatic(nav_mode):
        return "o automático está rodando"
    if maneuver:
        return "o modo manobra está ativo"
    if speed is not None and abs(speed) > STOPPED_SPEED_M_S:
        return "o veículo está em movimento"
    return None


def check(label, value, ok):
    return {"label": label, "value": value, "ok": bool(ok)}


def _number(value, decimals=0):
    return f"{value:.{decimals}f}".replace(".", ",")


def entry_checks(c):
    """Mesmas condições do entry_problem do mode_manager, na ordem do operador.

    c: dict com route, distance, max_distance, motion_permitted, flags, esp_fresh,
    stationary, ekf_fresh, age, age_max, gnss_fresh, fix_ok, corrections, rtk,
    gamepad_fresh, nav_ready, phase_text."""
    items = []
    if not c["route"]:
        items.append(check("Rota selecionada", "nenhuma", False))
    else:
        distance = c["distance"]
        text = "sem GPS" if distance is None else f"{_number(distance, 1)} m"
        items.append(check(f"Veículo até {_number(c['max_distance'])} m do início", text,
                           distance is not None and distance <= c["max_distance"]))
    if c["esp_fresh"]:
        items.append(check("ESP32 liberando movimento", f"flags {c['flags']}", c["motion_permitted"]))
    else:
        items.append(check("ESP32 liberando movimento", "sem dados", False))
    if c["ekf_fresh"]:
        items.append(check("Veículo parado", "parado" if c["stationary"] else "andando", c["stationary"]))
    else:
        items.append(check("Veículo parado", "sem EKF", False))
    if c["gnss_fresh"]:
        age_ok = c["corrections"] and c["age"] <= c["age_max"]
        items.append(check(f"Correção RTK até {_number(c['age_max'])} s", f"{_number(c['age'])} s", age_ok))
        items.append(check("RTK com solução", c["rtk"], c["fix_ok"] and c["rtk"] != "NONE"))
    else:
        items.append(check(f"Correção RTK até {_number(c['age_max'])} s", "sem GPS", False))
        items.append(check("RTK com solução", "sem GPS", False))
    items.append(check("EKF rodando", c["phase_text"] if c["ekf_fresh"] else "sem dados", c["ekf_fresh"]))
    items.append(check("Gamepad conectado", "ok" if c["gamepad_fresh"] else "sem dados", c["gamepad_fresh"]))
    waiting = "aguardando Stanley e EKF" if c.get("car_running") else "inicie o carro"
    items.append(check("Navegação no ar", "ok" if c["nav_ready"] else waiting, c["nav_ready"]))
    return items


HINTS = (
    ("Rota", "selecione uma rota na lista."),
    ("Navegação", "clique em Iniciar para subir a navegação."),
    ("ESP32", "ligue o enable físico e confira as flags em Segurança."),
    ("Veículo até", "leve o veículo até o início da rota, alinhado com a reta."),
    ("Veículo parado", "pare o veículo."),
    ("Correção", "confira a internet do celular e do notebook."),
    ("RTK", "espere o RTK sair de NONE."),
    ("EKF", "confira o Odroid."),
    ("Gamepad", "confira o controle."),
)


def next_step(items, car_running=False):
    missing = [item["label"] for item in items if not item["ok"]]
    if not missing:
        return "segure o △ no controle por 5 s para o Stanley começar."
    for prefix, hint in HINTS:
        if prefix == "Navegação" and car_running:
            continue
        if any(label.startswith(prefix) for label in missing):
            return hint
    return "aguarde: a navegação ainda está subindo." if car_running else "confira os itens em vermelho."


def rate_meter(stamps, now, window=2.0):
    """Taxa em Hz a partir dos instantes recentes de um tópico."""
    while stamps and now - stamps[0] > window:
        stamps.popleft()
    return len(stamps) / window


def clean_json(value):
    """JSON não aceita NaN: vira null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    return value
