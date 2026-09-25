"""
Abstract interface every robot controller (sim or real) must implement.
Keeping this separate from URSimController means swapping in a real-hardware
controller later only requires a new class, not changes to callers.
"""
from abc import ABC, abstractmethod


class ControllerAdapter(ABC):
    @abstractmethod
    def power_on(self) -> bool:
        """Power on the arm and release brakes. Returns True once robotmode is RUNNING."""

    @abstractmethod
    def power_off(self) -> bool:
        """Power off the arm."""

    @abstractmethod
    def get_status(self) -> dict:
        """Return current {'safetystatus': ..., 'robotmode': ...}."""

    @abstractmethod
    def clear_protective_stop(self) -> bool:
        """Clear a protective stop / safety popup. The 'alarm reset' Tier 1 op."""

    @abstractmethod
    def reset_program_pointer(self) -> bool:
        """Stop and restart the currently loaded program from the top."""

    @abstractmethod
    def move_joints(self, angles_rad, a: float = 0.5, v: float = 0.3) -> bool:
        """Send a movej to the given joint angles (radians)."""

    @abstractmethod
    def set_payload(self, mass_kg: float, cog=None) -> bool:
        """Set the active payload mass (kg) and optional center of gravity [x, y, z]."""

    @abstractmethod
    def emergency_stop(self) -> bool:
        """Immediately halt whatever program is running."""

    @abstractmethod
    def notify(self, message: str) -> bool:
        """Show a message where someone watching the physical/simulated pendant can see
        it — no safety effect, purely informational (fault detected, fix applied, etc.)."""
