"""Shared data shapes for the fault -> tier -> proposal -> guard pipeline."""
from dataclasses import dataclass, field


@dataclass
class Fault:
    fault_type: str      # e.g. "PROTECTIVE_STOP", extracted from safetystatus
    safetystatus: str
    robotmode: str
    detected_at: float


@dataclass
class Proposal:
    op: str
    params: dict = field(default_factory=dict)


@dataclass
class GateResult:
    name: str
    passed: bool
    reason: str = ""


@dataclass
class CycleResult:
    fault: Fault
    tier: int
    proposal: Proposal
    gate_results: list
    decision: str          # "APPROVED" | "DENIED" | "AWAITING_APPROVAL"
    executed: bool
