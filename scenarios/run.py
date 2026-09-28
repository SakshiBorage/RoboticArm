"""
Regression harness: runs the fault -> tier -> proposal -> guard pipeline
against a FakeControllerAdapter (no real hardware) and asserts each
scenario's expected outcome. Run with: python3 scenarios/run.py
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math

from fake_controller import FakeControllerAdapter
from models import Fault, Proposal
from preventive import PreventiveChecker, PreventiveMonitor
from proposer import AetherionProposer, ScriptedProposer
from service import Service


def make_fault(fault_type, safetystatus, robotmode):
    return Fault(fault_type=fault_type, safetystatus=f"Safetystatus: {safetystatus}",
                 robotmode=f"Robotmode: {robotmode}", detected_at=time.time())


class FakeAetherionClient:
    """Stands in for aetherion_client: returns one canned agent run instead of calling
    sbox, so the Tier 2 Aetherion path is testable offline (and never posts to Slack)."""

    def __init__(self, run):
        self.run = run
        self.sent = []

    def run_agent(self, agent_params):
        self.sent.append(agent_params)
        return "fake-run", 200, "{}"

    def wait_for_run(self, run_id, timeout_seconds):
        return self.run


def aetherion_scenario(run):
    """AetherionProposer wired to a fake agent run; its approval() is the approval hook,
    exactly as Service wires it by default."""
    proposer = AetherionProposer(client=FakeAetherionClient(run))
    return {"proposer": proposer, "approval": proposer.approval}


def completed_run(output):
    return {"status": "COMPLETED", "output_payload": output}


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
        "name": "unknown fault type -> Tier 2 escalate -> DENIED (no controller method), no execution",
        "controller_state": ("UNDEFINED_SAFETY_MODE", "RUNNING"),
        "fault": make_fault("UNDEFINED_SAFETY_MODE", "UNDEFINED_SAFETY_MODE", "RUNNING"),
        "proposer": ScriptedProposer([Proposal(op="escalate", params={})]),
        "approval": lambda fault, proposal: True,
        "expect": {"tier": 2, "decision": "DENIED", "executed": False},
    },
    {
        "name": "Tier 2 real op + human approves -> APPROVED + executed",
        "controller_state": ("UNDEFINED_SAFETY_MODE", "RUNNING"),
        "fault": make_fault("UNDEFINED_SAFETY_MODE", "UNDEFINED_SAFETY_MODE", "RUNNING"),
        "proposer": ScriptedProposer([Proposal(op="emergency_stop", params={})]),
        "approval": lambda fault, proposal: True,
        "expect": {"tier": 2, "decision": "APPROVED", "executed": True},
    },
    {
        "name": "Tier 2 real op + human denies -> DENIED, no execution",
        "controller_state": ("UNDEFINED_SAFETY_MODE", "RUNNING"),
        "fault": make_fault("UNDEFINED_SAFETY_MODE", "UNDEFINED_SAFETY_MODE", "RUNNING"),
        "proposer": ScriptedProposer([Proposal(op="emergency_stop", params={})]),
        "approval": lambda fault, proposal: False,
        "expect": {"tier": 2, "decision": "DENIED", "executed": False},
    },
    {
        "name": "Aetherion agent: approved in Slack -> APPROVED + executed",
        "controller_state": ("UNDEFINED_SAFETY_MODE", "RUNNING"),
        "fault": make_fault("UNDEFINED_SAFETY_MODE", "UNDEFINED_SAFETY_MODE", "RUNNING"),
        **aetherion_scenario(completed_run(
            {"status": "approved", "proposal": {"op": "emergency_stop", "params": {}, "reasoning": "halt"}})),
        "expect": {"tier": 2, "decision": "APPROVED", "executed": True},
    },
    {
        "name": "Aetherion agent: rejected twice in Slack -> escalate -> DENIED, no execution",
        "controller_state": ("UNDEFINED_SAFETY_MODE", "RUNNING"),
        "fault": make_fault("UNDEFINED_SAFETY_MODE", "UNDEFINED_SAFETY_MODE", "RUNNING"),
        **aetherion_scenario(completed_run(
            {"status": "escalated_after_retry", "last_proposal": {"op": "emergency_stop", "params": {}},
             "feedback": "no"})),
        "expect": {"tier": 2, "decision": "DENIED", "executed": False},
    },
    {
        "name": "Aetherion agent: no Slack reply in time -> escalate -> DENIED, no execution",
        "controller_state": ("UNDEFINED_SAFETY_MODE", "RUNNING"),
        "fault": make_fault("UNDEFINED_SAFETY_MODE", "UNDEFINED_SAFETY_MODE", "RUNNING"),
        **aetherion_scenario(None),
        "expect": {"tier": 2, "decision": "DENIED", "executed": False},
    },
    {
        "name": "Aetherion agent: approved op outside the Tier 2 catalog (power_off) -> DENIED",
        "controller_state": ("UNDEFINED_SAFETY_MODE", "RUNNING"),
        "fault": make_fault("UNDEFINED_SAFETY_MODE", "UNDEFINED_SAFETY_MODE", "RUNNING"),
        **aetherion_scenario(completed_run(
            {"status": "approved", "proposal": {"op": "power_off", "params": {}, "reasoning": "x"}})),
        "expect": {"tier": 2, "decision": "DENIED", "executed": False},
    },
    {
        "name": "Aetherion agent: run FAILED on the platform -> escalate -> DENIED",
        "controller_state": ("UNDEFINED_SAFETY_MODE", "RUNNING"),
        "fault": make_fault("UNDEFINED_SAFETY_MODE", "UNDEFINED_SAFETY_MODE", "RUNNING"),
        **aetherion_scenario({"status": "FAILED", "status_details": {"message": "boom"}}),
        "expect": {"tier": 2, "decision": "DENIED", "executed": False},
    },
    {
        # Empty script: ScriptedProposer raises if Tier 3 ever asks it for a proposal.
        "name": "critical FAULT -> Tier 3 -> halt only, proposer never consulted, person alerted",
        "controller_state": ("FAULT", "POWER_OFF"),
        "fault": make_fault("FAULT", "FAULT", "POWER_OFF"),
        "proposer": ScriptedProposer([]),
        "expect": {"tier": 3, "decision": "APPROVED", "executed": True},
        "expect_calls": ["emergency_stop"],
        "expect_alert": True,
    },
    {
        "name": "emergency stop -> Tier 3 -> halt only, never auto-recovered",
        "controller_state": ("SYSTEM_EMERGENCY_STOP", "RUNNING"),
        "fault": make_fault("SYSTEM_EMERGENCY_STOP", "SYSTEM_EMERGENCY_STOP", "RUNNING"),
        "proposer": ScriptedProposer([]),
        "expect": {"tier": 3, "decision": "APPROVED", "executed": True},
        "expect_calls": ["emergency_stop"],
        "expect_alert": True,
    },
]


def run_scenarios():
    failures = []
    for scenario in SCENARIOS:
        safetystatus, robotmode = scenario["controller_state"]
        controller = FakeControllerAdapter(safetystatus=safetystatus, robotmode=robotmode)
        # Tier 1 scenarios never call approval(); default it anyway so nothing
        # can block on input() if a scenario forgets to set one.
        approval = scenario.get("approval", lambda fault, proposal: False)
        alerts = []
        service = Service(controller=controller, proposer=scenario["proposer"],
                           audit=_NullAuditLog(), approval=approval,
                           alerter=lambda text: alerts.append(text) or True)

        result = service.run_cycle(scenario["fault"])
        actual = {"tier": result.tier, "decision": result.decision, "executed": result.executed}
        expected = dict(scenario["expect"])
        if "expect_calls" in scenario:
            # Only robot-affecting calls — notify()/log_message() are informational.
            actual["calls"] = [c for c, _ in controller.calls if c not in ("notify", "log_message")]
            expected["calls"] = scenario["expect_calls"]
        actual["alerted"] = bool(alerts)
        expected["alerted"] = scenario.get("expect_alert", False)

        if actual == expected:
            print(f"PASS  {scenario['name']}")
        else:
            print(f"FAIL  {scenario['name']}  expected={expected} actual={actual}")
            failures.append(scenario["name"])

    failures += run_preventive_checks()
    total = len(SCENARIOS) + len(PREVENTIVE_CHECKS)
    print(f"\n{total - len(failures)}/{total} scenarios passed")
    if failures:
        sys.exit(1)


def sample(q=None, speed=0.0, temps=None, voltage=48.0, robot_mode=7):
    return {"actual_q": q or [0, -1.57, 0, -1.57, 0, 0], "actual_TCP_speed": [speed, 0, 0, 0, 0, 0],
            "joint_temperatures": temps or [30.0] * 6, "actual_main_voltage": voltage,
            "robot_mode": robot_mode}


def _warning_keys(*samples_with_times):
    checker = PreventiveChecker()
    keys = set()
    for t, s in samples_with_times:
        keys = set(checker.evaluate(s, now=t))
    return keys


class _ListReader:
    def __init__(self, samples):
        self._samples = list(samples)

    def read(self):
        item = self._samples.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _monitor_reports(samples):
    """Runs PreventiveMonitor.check_once over samples; returns what got reported."""
    reported = []
    monitor = PreventiveMonitor(reader=_ListReader(samples),
                                on_warning=lambda key, msg: reported.append(("warn", key)),
                                on_clear=lambda key: reported.append(("clear", key)))
    for _ in samples:
        monitor.check_once()
    return reported


PREVENTIVE_CHECKS = [
    ("preventive: healthy arm -> no warnings",
     lambda: _warning_keys((0, sample())) == set()),
    ("preventive: wrist 3 near its end stop -> joint-limit warning",
     lambda: _warning_keys((0, sample(q=[0, -1.57, 0, -1.57, 0, 6.1]))) == {"joint_limit:Wrist 3"}),
    ("preventive: tool at 90% of TCP speed limit -> speed warning",
     lambda: _warning_keys((0, sample(speed=1.35))) == {"tcp_speed"}),
    ("preventive: joint over 60°C -> temperature warning",
     lambda: _warning_keys((0, sample(temps=[30, 30, 65, 30, 30, 30]))) == {"joint_temp:Elbow"}),
    ("preventive: elbow climbing 10°C/min -> rising-temperature warning",
     lambda: _warning_keys((0, sample(temps=[30] * 6)),
                           (60, sample(temps=[30, 30, 40, 30, 30, 30]))) == {"joint_temp_rise:Elbow"}),
    ("preventive: low voltage only warns while running (not while powered off)",
     lambda: _warning_keys((0, sample(voltage=0.0, robot_mode=3))) == set()
     and _warning_keys((0, sample(voltage=40.0))) == {"main_voltage"}),
    ("preventive: a lingering warning is reported once, then cleared once",
     lambda: _monitor_reports([sample(speed=1.4), sample(speed=1.4), sample()])
     == [("warn", "tcp_speed"), ("clear", "tcp_speed")]),
    ("preventive: unreachable telemetry -> one 'can't read' warning, no crash",
     lambda: _monitor_reports([OSError("refused"), OSError("refused")])
     == [("warn", PreventiveMonitor.UNREACHABLE_KEY)]),
]


def run_preventive_checks():
    failures = []
    for name, check in PREVENTIVE_CHECKS:
        if check():
            print(f"PASS  {name}")
        else:
            print(f"FAIL  {name}")
            failures.append(name)
    return failures


class _NullAuditLog:
    def record(self, entry):
        return entry


if __name__ == "__main__":
    run_scenarios()
