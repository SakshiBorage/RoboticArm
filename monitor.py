"""
Polls a ControllerAdapter's status and turns raw safetystatus reads into
debounced fault/recovery/stuck events. Debounce exists because a single bad
read shouldn't be treated as a confirmed fault; stuck_timeout exists to flag
a fault that didn't auto-recover within the expected window.
"""
import time

from ursim_controller import URSimController


class SafetyMonitor:
    def __init__(self, controller=None, poll_interval=0.2, debounce_count=1, stuck_timeout=15.0):
        self.controller = controller or URSimController()
        self.poll_interval = poll_interval
        self.debounce_count = debounce_count
        self.stuck_timeout = stuck_timeout

        self._state = "NORMAL"
        self._bad_streak = 0
        self._fault_started_at = None
        self._stuck_reported = False

    def _is_normal(self, status):
        return "NORMAL" in status["safetystatus"]

    def poll_once(self):
        try:
            status = self.controller.get_status()
        except OSError as e:
            return None, [("POLL_ERROR", {"error": str(e)})]

        now = time.monotonic()
        normal = self._is_normal(status)
        events = []

        if self._state == "NORMAL":
            if not normal:
                self._bad_streak += 1
                if self._bad_streak >= self.debounce_count:
                    self._state = "FAULT"
                    self._fault_started_at = now
                    self._stuck_reported = False
                    events.append(("FAULT_CONFIRMED", status))
            else:
                self._bad_streak = 0
        else:  # FAULT
            if normal:
                duration = now - self._fault_started_at
                self._state = "NORMAL"
                self._bad_streak = 0
                events.append(("RECOVERED", {"duration_s": duration, **status}))
            elif not self._stuck_reported and now - self._fault_started_at > self.stuck_timeout:
                self._stuck_reported = True
                events.append(("STUCK", {"duration_s": now - self._fault_started_at, **status}))

        return status, events

    def run(self, duration_s, on_event=print):
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            _, events = self.poll_once()
            for event_type, data in events:
                on_event(event_type, data)
            time.sleep(self.poll_interval)
