"""
Regression harness: runs the fault -> tier -> proposal -> guard pipeline
against a FakeControllerAdapter (no real hardware) and asserts each
scenario's expected outcome. Run with: python3 scenarios/run.py
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fake_controller import FakeControllerAdapter
from models import Fault, Proposal
from proposer import ScriptedProposer
from service import Service


def make_fault(fault_type, safetystatus, robotmode):
    return Fault(fault_type=fault_type, safetystatus=f"Safetystatus: {safetystatus}",
                 robotmode=f"Robotmode: {robotmode}", detected_at=time.time())


SCENARIOS = [
    {
        "name": "protective_stop -> whitelisted clear -> APPROVED + executed",
        "controller_state": ("PROTECTIVE_STOP", "RUNNING"),
        "fault": make_fault("PROTECTIVE_STOP", "PROTECTIVE_STOP", "RUNNING"),
        "proposer": ScriptedProposer([Proposal(op="clear_protective_stop", params={})]),
        "expect": {"tier": 1, "decision": "APPROVED", "executed": True},
    },
    {
        "name": "protective_stop -> out-of-whitelist op (quick_master) -> DENIED",
        "controller_state": ("PROTECTIVE_STOP", "RUNNING"),
        "fault": make_fault("PROTECTIVE_STOP", "PROTECTIVE_STOP", "RUNNING"),
        "proposer": ScriptedProposer([Proposal(op="quick_master", params={})]),
        "expect": {"tier": 1, "decision": "DENIED", "executed": False},
    },
    {
        "name": "protective_stop -> out-of-bounds payload -> DENIED",
        "controller_state": ("PROTECTIVE_STOP", "RUNNING"),
        "fault": make_fault("PROTECTIVE_STOP", "PROTECTIVE_STOP", "RUNNING"),
        "proposer": ScriptedProposer([Proposal(op="set_payload", params={"mass_kg": 999})]),
        "expect": {"tier": 1, "decision": "DENIED", "executed": False},
    },
    {
        "name": "already-recovered fault -> stale clear_protective_stop -> DENIED",
        "controller_state": ("NORMAL", "RUNNING"),
        "fault": make_fault("PROTECTIVE_STOP", "PROTECTIVE_STOP", "RUNNING"),
        "proposer": ScriptedProposer([Proposal(op="clear_protective_stop", params={})]),
        "expect": {"tier": 1, "decision": "DENIED", "executed": False},
    },
    {
        "name": "unknown fault type -> Tier 2 -> AWAITING_APPROVAL, no execution",
        "controller_state": ("UNDEFINED_SAFETY_MODE", "RUNNING"),
        "fault": make_fault("UNDEFINED_SAFETY_MODE", "UNDEFINED_SAFETY_MODE", "RUNNING"),
        "proposer": ScriptedProposer([Proposal(op="escalate", params={})]),
        "expect": {"tier": 2, "decision": "AWAITING_APPROVAL", "executed": False},
    },
]


def run_scenarios():
    failures = []
    for scenario in SCENARIOS:
        safetystatus, robotmode = scenario["controller_state"]
        controller = FakeControllerAdapter(safetystatus=safetystatus, robotmode=robotmode)
        service = Service(controller=controller, proposer=scenario["proposer"],
                           audit=_NullAuditLog())

        result = service.run_cycle(scenario["fault"])
        actual = {"tier": result.tier, "decision": result.decision, "executed": result.executed}
        expected = scenario["expect"]

        if actual == expected:
            print(f"PASS  {scenario['name']}")
        else:
            print(f"FAIL  {scenario['name']}  expected={expected} actual={actual}")
            failures.append(scenario["name"])

    print(f"\n{len(SCENARIOS) - len(failures)}/{len(SCENARIOS)} scenarios passed")
    if failures:
        sys.exit(1)


class _NullAuditLog:
    def record(self, entry):
        return entry


if __name__ == "__main__":
    run_scenarios()
