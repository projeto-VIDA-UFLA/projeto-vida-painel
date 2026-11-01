"""Nó ROS do painel: só assina tópicos e monta o retrato enviado à página.

As assinaturas são best-effort: recebem de publicadores reliable e best-effort e
não fazem o carro esperar pelo painel. O nó não publica nada no carro.
"""
import collections
import math
import threading
import time

import rclpy
import yaml
from diagnostic_msgs.msg import DiagnosticArray
from nav_msgs.msg import Odometry
from rcl_interfaces.msg import Log
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import NavSatFix
from std_msgs.msg import Bool
from std_srvs.srv import SetBool, Trigger
from vida_interfaces.msg import (BatteryStatus, Comands, DriveReference, EkfStatus, Encoders, GnssStatus,
                                 NavigationMode, SafetyStatus, StanleyStatus, Steering, SteeringReference)
from vida_navigation.geo import Origin, to_geodetic, to_local

from painel import flags as flagdefs
from painel import logic
from painel.routes import RouteRecorder

CONFIG = "/vida_ws/install/vida_navigation/share/vida_navigation/config/navigation.yaml"
RTK_NAMES = {GnssStatus.RTK_NONE: "NONE", GnssStatus.RTK_FLOAT: "FLOAT", GnssStatus.RTK_FIXED: "FIXED"}
PHASES = {EkfStatus.PHASE_WAITING_ORIGIN: "aguardando origem", EkfStatus.PHASE_WAITING_HEADING: "aguardando rumo",
          EkfStatus.PHASE_RUNNING: "rodando"}
FRESH = {"safety": 0.5, "ekf": 0.5, "gnss": 2.5, "fix": 2.5, "gamepad": 0.3, "nav": 1.0, "encoders": 0.5,
         "steering": 0.5, "battery": 1.0, "ntrip": 5.0, "stanley": 1.0, "odom": 0.3}
HEALTH = (("Encoders", "encoders"), ("Esterç.", "steering"), ("Segurança", "safety"), ("EKF", "ekf"),
          ("GPS", "fix"), ("Correções", "rtcm"), ("Bateria", "battery"), ("Gamepad", "gamepad"))
TRAIL_MAX = 6000
LOG_MAX = 200
LOG_WARN, LOG_ERROR = 30, 40   # níveis de rcl_interfaces/Log
OWN_LOGGER = "vida_painel"
BAG_NODE = "rosbag2_player"


def load_limits(filename=CONFIG):
    limits = {"start_max_distance_m": 3.0, "max_correction_age_s": 30.0, "max_cross_track_m": 1.5}
    try:
        with open(filename, encoding="utf-8") as stream:
            manager = yaml.safe_load(stream)["mode_manager"]["ros__parameters"]
        limits.update({k: float(manager[k]) for k in limits if k in manager})
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError):
        pass
    return limits


class Latest:
    def __init__(self):
        self.msg = None
        self.t = 0.0
        self.stamps = collections.deque(maxlen=400)

    def set(self, msg, now):
        self.msg, self.t = msg, now
        self.stamps.append(now)

    def age(self, now):
        return None if self.msg is None else now - self.t

    def fresh(self, now, limit):
        return self.msg is not None and now - self.t <= limit


class Bridge(Node):
    def __init__(self):
        super().__init__(OWN_LOGGER)
        self.lock = threading.RLock()
        self.limits = load_limits()
        self.topics = collections.defaultdict(Latest)
        self.battery = logic.BatteryTracker()
        self.recorder = RouteRecorder()
        self.route = None             # {"id", "closed", "points": [[lat, lon]...]}
        self.start = None             # Origin do primeiro ponto da rota
        self.trail = []
        self.trail_generation = 0
        self.car = None
        self.errors = collections.deque(maxlen=LOG_MAX)
        self.events = collections.deque(maxlen=LOG_MAX)
        self.last_log = {}
        self._flags = None
        self._permitted = False
        self._nav_mode = None
        self._maneuver = None
        self._rtk = None
        self._ntrip = None
        self._esp_online = None
        self._odroid_online = None
        self._reason = ""
        self.odroid_ping = None       # None até a primeira resposta do ping
        self.ping_ms = None
        self.esp_live = False
        self.odroid_live = False
        self.replay = False
        self._ping_seen = None
        self._replay_seen = False
        qos = qos_profile_sensor_data
        for topic, msg, key in (
                ("/vida/safety/status", SafetyStatus, "safety"), ("/vida/safety/maneuver", Bool, "maneuver"),
                ("/vida/safety/bench_mode", Bool, "bench"), ("/robot_state/status", EkfStatus, "ekf"),
                ("/robot_state/odom", Odometry, "odom"), ("/vida/sensors/gps/status", GnssStatus, "gnss"),
                ("/vida/sensors/gps/fix", NavSatFix, "fix"), ("/vida/sensors/steering", Steering, "steering"),
                ("/vida/sensors/encoders", Encoders, "encoders"), ("/vida/sensors/battery", BatteryStatus, "battery"),
                ("/vida/actuators/drive", Comands, "pwm_drive"), ("/vida/actuators/steering", Comands, "pwm_steering"),
                ("/vida/actuators/brake", Comands, "brake"),
                ("/vida/control/drive/reference", DriveReference, "drive_ref"),
                ("/vida/control/steering/reference", SteeringReference, "steering_ref"),
                ("/gamepad/drive/reference", DriveReference, "gamepad"),
                ("/navigation/mode", NavigationMode, "nav"), ("/navigation/stanley/status", StanleyStatus, "stanley"),
                ("/vida/sensors/gps/ntrip/diagnostics", DiagnosticArray, "ntrip")):
            self.create_subscription(msg, topic, self._storer(key), qos)
        # O conteúdo das correções não interessa: só o instante, para a taxa.
        from std_msgs.msg import UInt8MultiArray
        self.create_subscription(UInt8MultiArray, "/vida/sensors/gps/rtcm", self._storer("rtcm"), qos)
        self.create_subscription(Log, "/rosout", self.on_log, qos)
        # Só para saber se estão no ar (service_is_ready), como o mode_manager. Nunca são chamados.
        self.stanley_client = self.create_client(SetBool, "/stanley/enable")
        self.ekf_client = self.create_client(Trigger, "/robot_state_ekf_node/reset")
        self.create_timer(0.2, self.on_timer)
        self.create_timer(1.0, self.check_publishers)
        self.event("Painel iniciado")

    def _storer(self, key):
        def store(msg):
            now = time.time()
            with self.lock:
                self.topics[key].set(msg, now)
                if key == "fix":
                    self.on_fix(msg, now)
                elif key == "odom":
                    self.on_odom(msg)
        return store

    # ------------------------------------------------------------ eventos e erros
    def event(self, text, now=None):
        self.events.appendleft({"t": now or time.time(), "text": text})

    def error(self, source, title, detail="", now=None):
        now = now or time.time()
        key = (source, title)
        if now - self.last_log.get(key, 0.0) < 5.0:
            return
        self.last_log[key] = now
        self.errors.appendleft({"t": now, "source": source, "title": title, "detail": detail})

    def on_log(self, msg):
        level = int.from_bytes(msg.level, "little") if isinstance(msg.level, bytes) else int(msg.level)
        if msg.name == OWN_LOGGER or level < LOG_WARN:
            return
        now = time.time()
        with self.lock:
            navigation = msg.name.startswith(("mode_manager", "stanley", "path_publisher"))
            if navigation and level >= LOG_ERROR:
                self.error("Stanley", msg.msg[:160], "", now)
            elif level >= LOG_ERROR and "gamepad" not in msg.name:
                self.error(msg.name.split(".")[-1], msg.msg[:160], "", now)

    def check_publishers(self):
        """Quem publica mostra se o dado vem do carro: um bag chega como rosbag2_player."""
        def names(topic):
            return [info.node_name for info in self.get_publishers_info_by_topic(topic)]
        esp, odroid = names("/vida/safety/status"), names("/robot_state/status")
        navigation = names("/navigation/mode")
        with self.lock:
            self.esp_live = any(name != BAG_NODE for name in esp)
            self.odroid_live = any(name != BAG_NODE for name in odroid)
            self.replay = BAG_NODE in esp + odroid + navigation

    def set_ping(self, ok, milliseconds):
        with self.lock:
            now = time.time()
            if self._ping_seen is not None and ok != self._ping_seen:
                self.event("Odroid voltou a responder ao ping" if ok else "Odroid parou de responder ao ping", now)
                if not ok:
                    self.error("Conexão", "Odroid parou de responder ao ping", "Confira o cabo de rede e a energia do Odroid.", now)
            self._ping_seen = ok
            self.odroid_ping, self.ping_ms = ok, milliseconds

    def on_timer(self):
        now = time.time()
        with self.lock:
            self.track_changes(now)

    def track_changes(self, now):
        t = self.topics
        if self.replay != self._replay_seen:
            self.event("Reprodução de bag detectada: dados não vêm do carro" if self.replay else "Reprodução de bag encerrada", now)
            self._replay_seen = self.replay
        esp_fresh = t["safety"].fresh(now, FRESH["safety"])
        esp_online = esp_fresh and self.esp_live
        if esp_online != self._esp_online:
            if self._esp_online is not None or esp_online:
                self.event("ESP32 conectado" if esp_online else "ESP32 sem dados", now)
                if not esp_online and self._esp_online:
                    self.error("Conexão", "ESP32 parou de enviar dados", "Confira a serial e o agente micro-ROS no Odroid.", now)
            self._esp_online = esp_online
        odroid = (t["ekf"].fresh(now, FRESH["ekf"]) or t["gnss"].fresh(now, FRESH["gnss"])) and self.odroid_live
        if odroid != self._odroid_online:
            if self._odroid_online is not None or odroid:
                self.event("Nós do Odroid com dados" if odroid else "Nós do Odroid sem dados", now)
            self._odroid_online = odroid
        esp = esp_fresh
        if esp:
            mask = t["safety"].msg.inhibit_flags
            if mask != self._flags:
                old = self._flags or 0
                for bit, name, hint, retained in flagdefs.decode(mask & ~old):
                    self.event(f"Flag {bit}: {name}", now)
                    # Bloqueio de quem já estava liberado: ao ligar o carro não é erro.
                    if retained or self._permitted:
                        self.error("Segurança", f"Flag {bit}: {name}", hint, now)
                for bit, name, _, _ in flagdefs.decode(old & ~mask):
                    self.event(f"Flag {bit} saiu: {name}", now)
                self._flags = mask
            self._permitted = t["safety"].msg.motion_permitted
        if t["nav"].fresh(now, FRESH["nav"]):
            nav = t["nav"].msg
            if nav.mode != self._nav_mode:
                if self._nav_mode == logic.NAV_MANUAL and nav.mode == logic.NAV_PREPARING:
                    self.trail, self.trail_generation = [], self.trail_generation + 1
                self.event(f"{logic.NAV_STEPS.get(nav.mode, 'Manual')}: {nav.reason}"[:110], now)
                if nav.aborted and nav.mode == logic.NAV_STOPPING:
                    self.error("Stanley", f"Automático abortado: {nav.reason}"[:160], "", now)
                self._nav_mode = nav.mode
        else:
            self._nav_mode = None
        if t["maneuver"].msg is not None and t["maneuver"].msg.data != self._maneuver:
            if self._maneuver is not None:
                self.event("Modo manobra " + ("ligado" if t["maneuver"].msg.data else "desligado"), now)
            self._maneuver = t["maneuver"].msg.data
        if t["gnss"].fresh(now, FRESH["gnss"]):
            rtk = RTK_NAMES.get(t["gnss"].msg.rtk_status, "NONE")
            if rtk != self._rtk:
                if self._rtk is not None:
                    self.event(f"RTK: {self._rtk} para {rtk}", now)
                self._rtk = rtk
        state = self.ntrip_state(now)
        if state and state["state"] != self._ntrip:
            if self._ntrip is not None:
                self.event(f"NTRIP: {state['state']}", now)
                if state["error"] and state["state"] != "streaming":
                    self.error("GPS", "Correções RTK interrompidas", "Confira a internet do celular e do notebook.", now)
            self._ntrip = state["state"]
        speed = self.speed(now)
        drive = t["pwm_drive"].msg
        if t["battery"].fresh(now, FRESH["battery"]):
            b = t["battery"].msg
            self.battery.update(now, b.voltage_v, b.valid, speed is None or abs(speed) > logic.STOPPED_SPEED_M_S,
                                drive is not None and drive.pwm != 0)

    # ------------------------------------------------------------ posição
    def on_fix(self, msg, now):
        latitude, longitude = float(msg.latitude), float(msg.longitude)
        if not (math.isfinite(latitude) and math.isfinite(longitude)):
            return
        gnss = self.topics["gnss"].msg
        if self.recorder.active and gnss is not None and gnss.rtk_status != GnssStatus.RTK_NONE:
            self.recorder.add(latitude, longitude, gnss.rtk_status == GnssStatus.RTK_FIXED)
        if not self.topics["odom"].fresh(now, FRESH["odom"]):
            self.set_car(latitude, longitude, None)

    def on_odom(self, msg):
        status = self.topics["ekf"].msg
        if status is None or not status.origin_valid or status.phase != EkfStatus.PHASE_RUNNING:
            return
        latitude, longitude = to_geodetic(Origin(status.origin_latitude_deg, status.origin_longitude_deg),
                                          msg.pose.pose.position.x, msg.pose.pose.position.y)
        q = msg.pose.pose.orientation
        yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))
        self.set_car(float(latitude), float(longitude), yaw)

    def set_car(self, latitude, longitude, yaw):
        self.car = {"lat": latitude, "lon": longitude, "yaw": yaw}
        if not self.trail or self._distance(self.trail[-1], (latitude, longitude)) >= 0.2:
            self.trail.append((round(latitude, 8), round(longitude, 8)))
            if len(self.trail) > TRAIL_MAX:
                del self.trail[:1000]
                self.trail_generation += 1

    @staticmethod
    def _distance(a, b):
        x, y = to_local(Origin(a[0], a[1]), b[0], b[1])
        return math.hypot(float(x), float(y))

    def set_route(self, route):
        with self.lock:
            self.route = route
            self.start = Origin(*route["points"][0]) if route else None

    # ------------------------------------------------------------ leitura
    def speed(self, now):
        t = self.topics["encoders"]
        if not t.fresh(now, FRESH["encoders"]):
            return None
        return (t.msg.left.filtered_velocity_m_s + t.msg.right.filtered_velocity_m_s) / 2.0

    def ntrip_state(self, now):
        t = self.topics["ntrip"]
        if not t.fresh(now, FRESH["ntrip"]) or not t.msg.status:
            return None
        values = {v.key: v.value for v in t.msg.status[0].values}
        return {"state": values.get("state", ""), "connected": values.get("connected") == "true",
                "reconnections": values.get("reconnections", ""), "error": values.get("last_error", "")}

    def distance_to_start(self, now):
        fix = self.topics["fix"]
        if self.start is None or not fix.fresh(now, FRESH["fix"]):
            return None
        x, y = to_local(self.start, fix.msg.latitude, fix.msg.longitude)
        value = math.hypot(float(x), float(y))
        return value if math.isfinite(value) else None

    def route_heading_error(self):
        """Rumo do veículo menos o rumo da reta inicial da rota, em graus."""
        if self.route is None or self.car is None or self.car["yaw"] is None:
            return None
        points = self.route["points"]
        x, y = to_local(self.start, points[min(16, len(points) - 1)][0], points[min(16, len(points) - 1)][1])
        difference = self.car["yaw"] - math.atan2(float(y), float(x))
        return abs(math.degrees(math.atan2(math.sin(difference), math.cos(difference))))

    def snapshot(self, car_status):
        now = time.time()
        with self.lock:
            return logic.clean_json(self._snapshot(now, car_status))

    def _snapshot(self, now, car_status):
        t = self.topics
        fresh = lambda key: t[key].fresh(now, FRESH.get(key, 1.0))
        safety = t["safety"].msg if fresh("safety") else None
        gnss = t["gnss"].msg if fresh("gnss") else None
        ekf = t["ekf"].msg if fresh("ekf") else None
        nav = t["nav"].msg if fresh("nav") else None
        stanley = t["stanley"].msg if fresh("stanley") else None
        maneuver = bool(t["maneuver"].msg.data) if t["maneuver"].fresh(now, 1.0) else False
        bench = bool(t["bench"].msg.data) if t["bench"].fresh(now, 1.0) else False
        nav_mode = nav.mode if nav else None
        speed = self.speed(now)
        running = car_status.get("manual") or car_status.get("navigation")
        mode, detail, color = logic.mode_label(nav_mode, maneuver, bench, running)
        ntrip = self.ntrip_state(now)
        age = float(gnss.correction_age_s) if gnss else None
        rtk = RTK_NAMES.get(gnss.rtk_status, "NONE") if gnss else None
        gamepad_fresh = fresh("gamepad") or t["drive_ref"].fresh(now, 0.3)
        automatic = logic.in_automatic(nav_mode)
        steering = t["steering"].msg if fresh("steering") else None
        drive_ref = t["drive_ref"].msg if t["drive_ref"].fresh(now, 1.0) else None
        steering_ref = t["steering_ref"].msg if t["steering_ref"].fresh(now, 1.0) else None
        distance = self.distance_to_start(now)
        values = {
            "route": self.route is not None, "distance": distance, "max_distance": self.limits["start_max_distance_m"],
            "motion_permitted": bool(safety and safety.motion_permitted), "flags": safety.inhibit_flags if safety else 0,
            "esp_fresh": safety is not None, "stationary": bool(ekf and ekf.stationary), "ekf_fresh": ekf is not None,
            "age": age, "age_max": self.limits["max_correction_age_s"], "gnss_fresh": gnss is not None,
            "fix_ok": bool(gnss and gnss.gnss_fix_ok), "corrections": bool(gnss and gnss.corrections_received),
            "rtk": rtk, "gamepad_fresh": gamepad_fresh,
            "nav_ready": self.stanley_client.service_is_ready() and self.ekf_client.service_is_ready() and nav is not None,
            "phase_text": PHASES.get(ekf.phase, "") if ekf else "", "car_running": bool(running),
        }
        checks = logic.entry_checks(values)
        if automatic and stanley is not None:
            limit = self.limits["max_cross_track_m"]
            cross = abs(stanley.cross_track_m)
            kept = [c for c in checks if not c["label"].startswith(("Veículo", "Navegação"))]
            checks = kept + [logic.check(f"Erro lateral até {limit:.1f} m".replace(".", ","), f"{cross:.2f} m".replace(".", ","), cross <= limit)]
        names = [flagdefs.decode(safety.inhibit_flags) if safety else []][0]
        battery = t["battery"].msg if fresh("battery") else None
        percent = self.battery.percent
        trail_status = self.recorder.status(now)
        return {
            "now": now,
            "replay": self.replay,
            "mode": {"label": mode, "detail": detail, "color": color, "automatic": automatic},
            "car": {"running": bool(running), **car_status},
            "tiles": {
                "safety": {"on": safety is not None, "permitted": bool(safety and safety.motion_permitted),
                           "flags": [{"bit": b, "name": n, "hint": h, "retained": r} for b, n, h, r in names],
                           "physical": bool(safety and safety.physical_enable), "logical": bool(safety and safety.logical_enable),
                           "refs": bool(safety and safety.drive_reference_fresh and safety.steering_reference_fresh),
                           "age": t["safety"].age(now)},
                "link": dict(zip(("cls", "big", "small"), logic.link_status(
                    self.odroid_ping, self.ping_ms, self.esp_live, safety is not None,
                    self.odroid_live, ekf is not None or gnss is not None, self.replay))),
                "rtk": {"on": gnss is not None, "status": rtk, "accuracy": float(gnss.horizontal_accuracy_m) if gnss else None,
                        "satellites": int(gnss.satellites) if gnss else None, "age": t["gnss"].age(now)},
                "corrections": {"on": gnss is not None, "age": age, "limit": self.limits["max_correction_age_s"], "ntrip": ntrip},
                "ekf": {"on": ekf is not None, "text": PHASES.get(ekf.phase, "") if ekf else "", "running": bool(ekf and ekf.phase == EkfStatus.PHASE_RUNNING)},
            },
            "battery": {"on": battery is not None and battery.valid, "voltage": float(battery.voltage_v) if battery else None,
                        "current": float(battery.current_a) if battery else None, "percent": percent,
                        "level": logic.battery_level(percent), "resting": self.battery.resting(now),
                        "minimum": self.battery.minimum, "age": t["battery"].age(now)},
            "vehicle": {"speed": speed, "speed_ref": float(drive_ref.velocity_m_s) if drive_ref else None,
                        "angle": float(steering.filtered_angle_deg) if steering else None,
                        "angle_ref": float(steering_ref.angle_deg) if steering_ref else None,
                        "pwm_drive": int(t["pwm_drive"].msg.pwm) if t["pwm_drive"].fresh(now, 1.0) else None,
                        "pwm_steering": int(t["pwm_steering"].msg.pwm) if t["pwm_steering"].fresh(now, 1.0) else None,
                        "brake": int(t["brake"].msg.pwm) if t["brake"].fresh(now, 1.0) else None,
                        "maneuver": maneuver,
                        "rear_minus_front": (t["encoders"].msg.rear.filtered_velocity_m_s - speed) if speed is not None else None},
            "position": {"car": self.car, "car_age": min((a for a in (t["fix"].age(now), t["odom"].age(now)) if a is not None), default=None),
                         "distance": distance, "heading_error": self.route_heading_error(),
                         "cross_track": float(stanley.cross_track_m) if stanley and automatic else None,
                         "stanley_heading": math.degrees(stanley.heading_error_rad) if stanley and automatic else None,
                         "progress": float(stanley.progress_m) if stanley and automatic else None,
                         "remaining": float(stanley.remaining_m) if stanley and automatic else None,
                         "saturated": bool(stanley.steering_saturated) if stanley and automatic else False},
            "checks": {"title": "Condições do automático" if automatic else "Para executar a rota",
                       "items": checks, "ready": all(c["ok"] for c in checks),
                       "next": "" if automatic else logic.next_step(checks, bool(running)), "automatic": automatic},
            "errors": list(self.errors)[:60],
            "events": list(self.events)[:60],
            "health": [{"name": n, "hz": round(logic.rate_meter(t[k].stamps, now), 1 if k in ("fix", "rtcm") else 0),
                        "age": t[k].age(now)} for n, k in HEALTH],
            "recording": trail_status,
            "blocked": logic.control_block(nav_mode, maneuver, speed),
        }

    def trail_since(self, generation, index):
        with self.lock:
            if generation != self.trail_generation:
                return self.trail_generation, len(self.trail), list(self.trail), True
            return generation, len(self.trail), self.trail[index:], False


def start_ros():
    rclpy.init()
    bridge = Bridge()

    def spin():
        # Uma exceção num callback não pode deixar o painel mostrando dado parado.
        while rclpy.ok():
            try:
                rclpy.spin(bridge)
            except Exception as error:  # pylint: disable=broad-except
                bridge.get_logger().error(f"erro no callback: {error}")
                bridge.event(f"Erro interno do painel: {error}"[:110])

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    return bridge
