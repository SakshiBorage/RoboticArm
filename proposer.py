"""
Proposer: given a fault + tier, proposes a candidate action (op + params).
Never decides whether the action is safe to run — that's the Guard's job.
"""
from models import Proposal

# Only the Tier 1 fault types the router recognizes have a default proposal.
DEFAULT_TIER1_PROPOSALS = {
    "PROTECTIVE_STOP": lambda fault: Proposal(op="clear_protective_stop", params={}),
}


class DefaultProposer:
    """Real proposer for the current scope: one whitelisted recovery per known Tier 1 fault type."""

    def propose(self, fault, tier) -> Proposal:
        if tier == 1 and fault.fault_type in DEFAULT_TIER1_PROPOSALS:
            return DEFAULT_TIER1_PROPOSALS[fault.fault_type](fault)
        return Proposal(op="escalate", params={"reason": f"no autonomous recovery mapped for {fault.fault_type}"})


class ScriptedProposer:
    """Returns pre-scripted proposals in order, ignoring the fault/tier — for demos and tests
    where the outcome (including a deliberate denial) needs to be deterministic."""

    def __init__(self, script):
        self._script = list(script)

    def propose(self, fault, tier) -> Proposal:
        if not self._script:
            raise RuntimeError("ScriptedProposer script exhausted")
        return self._script.pop(0)
