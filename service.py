"""
Orchestrates one full cycle: fault -> tier -> proposal -> guard -> execute -> audit.
Wired against the real URSimController + SafetyMonitor by default; scenarios/
inject a FakeControllerAdapter instead so tests don't touch real hardware.
"""
import argparse
import re
import time

from audit import AuditLog
from guard import Guard
from models import Fault, CycleResult
from monitor import SafetyMonitor
from proposer import DefaultProposer
from router import Router
from ursim_controller import URSimController

SAFETYSTATUS_RE = re.compile(r"Safetystatus:\s*(\w+)")


def fault_from_status(status: dict) -> Fault:
    match = SAFETYSTATUS_RE.search(status["safetystatus"])
    fault_type = match.group(1) if match else "UNKNOWN"
    return Fault(
        fault_type=fault_type,
        safetystatus=status["safetystatus"],
        robotmode=status["robotmode"],
        detected_at=time.time(),
    )


class Service:
    def __init__(self, controller=None, monitor=None, router=None, proposer=None,
                 guard=None, audit=None):
        self.controller = controller or URSimController()
        self.monitor = monitor or SafetyMonitor(controller=self.controller)
        self.router = router or Router()
        self.proposer = proposer or DefaultProposer()
        self.guard = guard or Guard()
        self.audit = audit or AuditLog()

    def _execute(self, proposal):
        return getattr(self.controller, proposal.op)(**proposal.params)

    def run_cycle(self, fault: Fault) -> CycleResult:
        tier = self.router.assign_tier(fault)
        proposal = self.proposer.propose(fault, tier)

        if tier == 2:
            result = CycleResult(fault, tier, proposal, [], "AWAITING_APPROVAL", executed=False)
        else:
            context = {"tier": tier, "fault": fault}
            gate_results, decision = self.guard.evaluate(proposal, context, self.controller)
            executed = False
            if decision == "APPROVED":
                self._execute(proposal)
                executed = True
            result = CycleResult(fault, tier, proposal, gate_results, decision, executed)

        self.audit.record({
            "fault_type": result.fault.fault_type,
            "tier": result.tier,
            "proposed_op": result.proposal.op,
            "proposed_params": result.proposal.params,
            "gate_results": result.gate_results,
            "decision": result.decision,
            "executed": result.executed,
        })
        return result

    def run(self, cycles: int, on_cycle=None):
        results = []
        for _ in range(cycles):
            _, events = self.monitor.poll_once()
            for event_type, data in events:
                if event_type == "FAULT_CONFIRMED":
                    fault = fault_from_status(data)
                    result = self.run_cycle(fault)
                    results.append(result)
                    if on_cycle:
                        on_cycle(result)
            time.sleep(self.monitor.poll_interval)
        return results


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--cycles", type=int, default=60)
    args = parser.parse_args()

    if args.command == "run":
        service = Service()

        def report(result):
            print(f"[{result.tier=} {result.proposal.op=} {result.decision=} {result.executed=}]")

        service.run(cycles=args.cycles, on_cycle=report)


if __name__ == "__main__":
    main()
