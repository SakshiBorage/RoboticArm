"""
Real URSimController: wraps the Dashboard Server (29999) and Secondary
Interface (30002) socket calls proven out in ursim_power.py, ursim_basic_move.py,
and ursim_check_status.py, behind the ControllerAdapter interface.
"""
import socket
import time

from controller_adapter import ControllerAdapter

HOST = "127.0.0.1"
DASHBOARD_PORT = 29999
SECONDARY_PORT = 30002

# The dashboard server isn't UTF-8 ("—" shows as "???" on the pendant) and a
# newline would end the command early, so pendant text is kept to plain ASCII.
_PENDANT_REPLACEMENTS = {"—": "-", "–": "-", "…": "...", "°": " deg", "’": "'", "‘": "'",
                         "“": '"', "”": '"', "\n": " "}


def _pendant_text(message):
    for char, replacement in _PENDANT_REPLACEMENTS.items():
        message = message.replace(char, replacement)
    return message.encode("ascii", "replace").decode("ascii")


class URSimController(ControllerAdapter):
    def __init__(self, host=HOST, dashboard_port=DASHBOARD_PORT,
                 secondary_port=SECONDARY_PORT, timeout=5):
        self.host = host
        self.dashboard_port = dashboard_port
        self.secondary_port = secondary_port
        self.timeout = timeout

    def _dashboard_command(self, *cmds):
        replies = []
        with socket.create_connection((self.host, self.dashboard_port), timeout=self.timeout) as s:
            s.recv(4096)  # welcome banner
            for cmd in cmds:
                s.sendall((cmd + "\n").encode())
                time.sleep(0.3)
                replies.append(s.recv(4096).decode().strip())
        return replies

    def _send_script(self, script):
        with socket.create_connection((self.host, self.secondary_port), timeout=self.timeout) as s:
            s.sendall(script.encode())

    def power_on(self):
        self._dashboard_command("power on")
        time.sleep(2)
        self._dashboard_command("brake release")
        time.sleep(5)
        mode = self._dashboard_command("robotmode")[0]
        return "RUNNING" in mode

    def power_off(self):
        reply = self._dashboard_command("power off")[0]
        return "powering off" in reply.lower()

    def get_status(self):
        safety, mode = self._dashboard_command("safetystatus", "robotmode")
        return {"safetystatus": safety, "robotmode": mode}

    def clear_protective_stop(self):
        replies = self._dashboard_command("unlock protective stop", "close safety popup")
        return all("fail" not in r.lower() for r in replies)

    def reset_program_pointer(self):
        replies = self._dashboard_command("stop", "play")
        return all("fail" not in r.lower() for r in replies)

    def move_joints(self, angles_rad, a=0.5, v=0.3):
        script = f"""
def controller_move():
  movej({list(angles_rad)}, a={a}, v={v})
end
controller_move()
"""
        self._send_script(script)
        return True

    def set_payload(self, mass_kg, cog=None):
        cog_arg = f", cog={list(cog)}" if cog else ""
        script = f"""
def controller_set_payload():
  set_payload({mass_kg}{cog_arg})
end
controller_set_payload()
"""
        self._send_script(script)
        return True

    def emergency_stop(self):
        reply = self._dashboard_command("stop")[0]
        return "fail" not in reply.lower()

    def notify(self, message):
        # `popup` shows a message box directly on the pendant screen; `addToLog`
        # also writes it into URSim's own Log tab so it's there after the popup
        # is dismissed. Neither has any effect on the robot's actual state.
        # The popup stays until someone presses OK. PolyScope stacks popups with the
        # OLDEST on top, and its "close popup" command proved unreliable in URSim
        # 5.26 (sometimes a no-op), so nothing here tries to close or replace one.
        message = _pendant_text(message)
        replies = self._dashboard_command(f"popup {message}", f"addToLog {message}")
        return all("fail" not in r.lower() for r in replies)

    def log_message(self, message):
        reply = self._dashboard_command(f"addToLog {_pendant_text(message)}")[0]
        return "fail" not in reply.lower()

    def restart_safety(self):
        """Clear a critical (Tier 3) fault and power back on. Deliberately not a
        ControllerAdapter op or in any proposal catalog: only a person calls this,
        after inspecting the arm — nothing in the pipeline can propose it."""
        self._dashboard_command("close safety popup", "restart safety")
        time.sleep(3)
        return self.power_on()
