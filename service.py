"""
Orchestrates one full cycle: fault -> tier -> proposal -> guard -> execute -> audit.
Wired against the real URSimController + SafetyMonitor by default; scenarios/
inject a FakeControllerAdapter instead so tests don't touch real hardware.
"""
import argparse
import re
import time

from app_logging import get_logger
from approval import cli_approval
from audit import AuditLog
from guard import Guard
from models import Fault, CycleResult
from monitor import SafetyMonitor
from proposer import CompositeProposer
from router import Router
from ursim_controller import URSimController

logger = get_logger(__name__)

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
                 guard=None, audit=None, approval=None):
        self.controller = controller or URSimController()
        self.monitor = monitor or SafetyMonitor(controller=self.controller)
        self.router = router or Router()
        self.audit = audit or AuditLog()
        self.proposer = proposer or CompositeProposer()
        if isinstance(self.proposer, CompositeProposer):
            tier2 = self.proposer.tier2_proposer
            if tier2.controller is None:
                tier2.controller = self.controller
            if tier2.monitor is None:
                tier2.monitor = self.monitor
            if tier2.audit is None:
                tier2.audit = self.audit
        self.guard = guard or Guard()
        self.approval = approval or cli_approval

    def _execute(self, proposal):
        return getattr(self.controller, proposal.op)(**proposal.params)

    def run_cycle(self, fault: Fault) -> CycleResult:
        tier = self.router.assign_tier(fault)
        proposal = self.proposer.propose(fault, tier)

        approved_by_human = True
        if tier == 2:
            approved_by_human = self.approval(fault, proposal)

        context = {"tier": tier, "fault": fault, "approved": approved_by_human}
        gate_results, decision = self.guard.evaluate(proposal, context, self.controller)
        if decision == "DENIED":
            failed = next((g for g in gate_results if not g["passed"]), None)
            if failed:
                logger.info(f"DENIED: fault={fault.fault_type} op={proposal.op} "
                            f"gate={failed['name']} reason={failed['reason']}")

        executed = False
        execution_error = None
        if decision == "APPROVED":
            try:
                self._execute(proposal)
                executed = True
            except Exception as e:
                execution_error = str(e)
                logger.exception(f"Execution failed: fault={fault.fault_type} "
                                  f"op={proposal.op} params={proposal.params}")

        result = CycleResult(fault, tier, proposal, gate_results, decision, executed, execution_error)

        audit_entry = {
            "fault_type": result.fault.fault_type,
            "tier": result.tier,
            "proposed_op": result.proposal.op,
            "proposed_params": result.proposal.params,
            "proposed_reasoning": result.proposal.reasoning,
            "gate_results": result.gate_results,
            "decision": result.decision,
            "executed": result.executed,
        }
        if execution_error:
            audit_entry["execution_error"] = execution_error
        self.audit.record(audit_entry)
        return result

    def run(self, cycles: int, on_cycle=None, on_event=None):
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
                elif event_type == "POLL_ERROR":
                    logger.warning(f"Monitor poll error: {data.get('error')}")
                    if on_event:
                        on_event(event_type, data)
                elif event_type == "STUCK":
                    logger.warning(f"Fault stuck for {data.get('duration_s', 0):.1f}s: {data}")
                    if on_event:
                        on_event(event_type, data)
                elif event_type == "RECOVERED":
                    if on_event:
                        on_event(event_type, data)
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
            if result.decision == "DENIED":
                failed = next((g for g in result.gate_results if not g["passed"]), None)
                if failed:
                    print(f"    denied by gate '{failed['name']}': {failed['reason']}")
            if result.execution_error:
                print(f"    EXECUTION ERROR: {result.execution_error}")

        def report_event(event_type, data):
            print(f"[EVENT {event_type}] {data}")

        service.run(cycles=args.cycles, on_cycle=report, on_event=report_event)


if __name__ == "__main__":
    main()
