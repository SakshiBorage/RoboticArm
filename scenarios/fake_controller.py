"""In-memory ControllerAdapter for scenario tests — no sockets, no real URSim."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from controller_adapter import ControllerAdapter


class FakeControllerAdapter(ControllerAdapter):
    def __init__(self, safetystatus="NORMAL", robotmode="RUNNING"):
        self.safetystatus = safetystatus
        self.robotmode = robotmode
        self.calls = []

    def power_on(self):
        self.calls.append(("power_on", {}))
        self.robotmode = "RUNNING"
        return True

    def power_off(self):
        self.calls.append(("power_off", {}))
        self.robotmode = "POWER_OFF"
        return True

    def get_status(self):
        return {"safetystatus": f"Safetystatus: {self.safetystatus}",
                "robotmode": f"Robotmode: {self.robotmode}"}

    def clear_protective_stop(self):
        self.calls.append(("clear_protective_stop", {}))
        self.safetystatus = "NORMAL"
        return True

    def reset_program_pointer(self):
        self.calls.append(("reset_program_pointer", {}))
        return True

    def move_joints(self, angles_rad, a=0.5, v=0.3):
        self.calls.append(("move_joints", {"angles_rad": angles_rad, "a": a, "v": v}))
        return True

    def set_payload(self, mass_kg, cog=None):
        self.calls.append(("set_payload", {"mass_kg": mass_kg, "cog": cog}))
        return True

    def emergency_stop(self):
        self.calls.append(("emergency_stop", {}))
        return True

    def notify(self, message):
        self.calls.append(("notify", {"message": message}))
        return True
