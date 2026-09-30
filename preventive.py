"""
Preventive (pre-emptive) health checks: every few seconds, read the arm's live
telemetry and warn when something is drifting toward a limit that would cause a
fault — before the fault happens. Warn-only by design: nothing here moves,
slows or stops the arm; it only tells a person (terminal, pendant, Slack) and
records it in the run log.

Telemetry comes from the UR RTDE interface (port 30004), which the dashboard
server (29999) doesn't expose: joint positions, TCP speed, joint temperatures,
main voltage. Each check opens a short-lived RTDE connection and reads one sample.

Limits are defaults for a UR e-series arm with a factory safety configuration —
set them to the real arm's safety configuration before relying on them.
"""
import collections
import math
import socket
import struct
import threading
import time

from alerts import send_slack
from app_logging import get_logger

logger = get_logger(__name__)

RTDE_PORT = 30004
RTDE_PROTOCOL_VERSION = 2
ROBOT_MODE_RUNNING = 7

# (RTDE output name, struct format) — order is the order values arrive in.
RTDE_FIELDS = [
    ("actual_q", ">6d"),
    ("actual_TCP_speed", ">6d"),
    ("joint_temperatures", ">6d"),
    ("actual_main_voltage", ">d"),
    ("robot_mode", ">i"),
]

JOINT_NAMES = ["Base", "Shoulder", "Elbow", "Wrist 1", "Wrist 2", "Wrist 3"]

DEFAULT_LIMITS = {
    "joint_limit_rad": 2 * math.pi,      # e-series joint range is ±360° on every joint
    "joint_limit_margin_deg": 15.0,      # warn this close to a joint's end stop
    "tcp_speed_limit_m_s": 1.5,          # factory Normal-mode TCP speed limit
    "tcp_speed_warn_fraction": 0.8,      # warn at 80% of it
    "joint_temp_warn_c": 60.0,
    "joint_temp_rise_warn_c_per_min": 5.0,
    "main_voltage_nominal_v": 48.0,
    "main_voltage_tolerance": 0.10,      # warn outside ±10% while running
}

TEMP_TREND_WINDOW_S = 300
TEMP_TREND_MIN_SPAN_S = 30


class RTDEReader:
    """Reads one telemetry sample per call over a fresh RTDE connection."""

    def __init__(self, host="127.0.0.1", port=RTDE_PORT, timeout=3):
        self.host = host
        self.port = port
        self.timeout = timeout

    @staticmethod
    def _send(sock, command, payload=b""):
        sock.sendall(struct.pack(">HB", 3 + len(payload), ord(command)) + payload)

    @staticmethod
    def _recv_exact(sock, n):
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("RTDE connection closed")
            data += chunk
        return data

    def _recv(self, sock):
        size, command = struct.unpack(">HB", self._recv_exact(sock, 3))
        return chr(command), self._recv_exact(sock, size - 3)

    def _recv_command(self, sock, expected):
        # The controller can interleave text messages ('M'); skip anything else.
        while True:
            command, body = self._recv(sock)
            if command == expected:
                return body

    def read(self) -> dict:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            self._send(sock, "V", struct.pack(">H", RTDE_PROTOCOL_VERSION))
            if self._recv_command(sock, "V") != b"\x01":
                raise ConnectionError("controller refused RTDE protocol version 2")
            names = ",".join(name for name, _ in RTDE_FIELDS).encode()
            self._send(sock, "O", struct.pack(">d", 10.0) + names)
            setup = self._recv_command(sock, "O")
            if b"NOT_FOUND" in setup:
                raise ConnectionError(f"controller doesn't provide an RTDE field: {setup[1:].decode()}")
            self._send(sock, "S")
            self._recv_command(sock, "S")
            body = self._recv_command(sock, "U")
            self._send(sock, "P")

        sample, offset = {}, 1  # byte 0 is the recipe id
        for name, fmt in RTDE_FIELDS:
            values = struct.unpack_from(fmt, body, offset)
            offset += struct.calcsize(fmt)
            sample[name] = list(values) if len(values) > 1 else values[0]
        return sample


class PreventiveChecker:
    """Turns one telemetry sample into the set of warnings currently active,
    as {key: plain-language message}. Stateful only for the temperature trend."""

    def __init__(self, **limits):
        self.limits = {**DEFAULT_LIMITS, **limits}
        self._temp_history = collections.deque()

    def evaluate(self, sample, now=None) -> dict:
        now = time.monotonic() if now is None else now
        lim = self.limits
        warnings = {}

        margin_rad = math.radians(lim["joint_limit_margin_deg"])
        for name, q in zip(JOINT_NAMES, sample["actual_q"]):
            room_rad = lim["joint_limit_rad"] - abs(q)
            if room_rad < margin_rad:
                warnings[f"joint_limit:{name}"] = (
                    f"{name} joint is {math.degrees(room_rad):.0f}° from its end stop — moving further "
                    f"that way will trigger a protective stop.")

        speed = math.sqrt(sum(v * v for v in sample["actual_TCP_speed"][:3]))
        speed_warn = lim["tcp_speed_limit_m_s"] * lim["tcp_speed_warn_fraction"]
        if speed >= speed_warn:
            warnings["tcp_speed"] = (
                f"Tool is moving at {speed:.2f} m/s, close to the {lim['tcp_speed_limit_m_s']:.2f} m/s "
                f"safety limit — it will be stopped if it speeds up further.")

        temps = sample["joint_temperatures"]
        for name, temp in zip(JOINT_NAMES, temps):
            if temp >= lim["joint_temp_warn_c"]:
                warnings[f"joint_temp:{name}"] = (
                    f"{name} joint is running hot ({temp:.0f}°C, warning level {lim['joint_temp_warn_c']:.0f}°C).")
        self._temp_history.append((now, temps))
        while now - self._temp_history[0][0] > TEMP_TREND_WINDOW_S:
            self._temp_history.popleft()
        oldest_time, oldest_temps = self._temp_history[0]
        span_s = now - oldest_time
        if span_s >= TEMP_TREND_MIN_SPAN_S:
            for name, old, new in zip(JOINT_NAMES, oldest_temps, temps):
                rise_per_min = (new - old) / span_s * 60
                if rise_per_min >= lim["joint_temp_rise_warn_c_per_min"]:
                    warnings[f"joint_temp_rise:{name}"] = (
                        f"{name} joint temperature is climbing fast ({rise_per_min:.1f}°C/min, now {new:.0f}°C).")

        if sample["robot_mode"] == ROBOT_MODE_RUNNING:
            nominal = lim["main_voltage_nominal_v"]
            voltage = sample["actual_main_voltage"]
            if abs(voltage - nominal) > nominal * lim["main_voltage_tolerance"]:
                warnings["main_voltage"] = (
                    f"Main supply voltage is {voltage:.1f} V (expected about {nominal:.0f} V).")

        return warnings


class PreventiveMonitor:
    """Runs PreventiveChecker on a background thread every interval_s. Each
    condition is reported once when it starts (on_warning) and once when it
    clears (on_clear), not on every check, so a lingering condition can't
    flood the terminal or Slack."""

    UNREACHABLE_KEY = "health_data_unreachable"

    def __init__(self, reader=None, checker=None, interval_s=2.0, on_warning=None, on_clear=None):
        self.reader = reader or RTDEReader()
        self.checker = checker or PreventiveChecker()
        self.interval_s = interval_s
        self.on_warning = on_warning or (lambda key, message: None)
        self.on_clear = on_clear or (lambda key: None)
        self._active = {}
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="preventive-monitor", daemon=True)

    @property
    def active(self) -> dict:
        """Warnings currently in effect, {key: message}."""
        return dict(self._active)

    def start(self):
        self._thread.start()
        return self

    def pause(self):
        """Skip checks — e.g. while a person restarts the controller, when telemetry
        is expected to drop and a "can't read" warning would just be noise."""
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=self.interval_s + 5)

    def check_once(self):
        try:
            current = self.checker.evaluate(self.reader.read())
        except (OSError, ValueError, struct.error) as e:
            current = {self.UNREACHABLE_KEY: f"Can't read the arm's health data ({e}) — early warnings are paused."}
        for key, message in current.items():
            if key not in self._active:
                self.on_warning(key, message)
        for key in set(self._active) - set(current):
            self.on_clear(key)
        self._active = current
        return current

    def _loop(self):
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(self.interval_s)
                continue
            try:
                self.check_once()
            except Exception:
                logger.exception("Preventive check failed")
            self._stop.wait(self.interval_s)


def make_alert_handlers(controller, run_log=None, slack=send_slack, popup=False):
    """on_warning/on_clear for PreventiveMonitor: terminal + pendant + Slack + run log.
    popup=True shows new warnings as a pendant popup (not just in its Log tab)."""

    def on_warning(key, message):
        print(f"\n>>> [EARLY WARNING] {message}\n")
        logger.info(f"Preventive warning {key}: {message}")
        pendant = f"Early warning: {message}"
        try:
            (controller.notify if popup else controller.log_message)(pendant)
        except Exception:
            logger.exception("Couldn't show preventive warning on the pendant")
        slack_ok = slack(f":warning: *Early warning* (no fault yet, arm still running): {message}")
        if run_log:
            run_log.event("preventive_warning", key=key, message=message, slack_sent=slack_ok)

    def on_clear(key):
        print(f">>> [EARLY WARNING CLEARED] {key}")
        logger.info(f"Preventive warning cleared: {key}")
        if run_log:
            run_log.event("preventive_cleared", key=key)

    return on_warning, on_clear
