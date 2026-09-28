"""
Proposer: given a fault + tier, proposes a candidate action (op + params).
Never decides whether the action is safe to run — that's the Guard's job.
"""
import json
import os

from dotenv import load_dotenv

from app_logging import get_logger
from models import Proposal

load_dotenv()

logger = get_logger(__name__)

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


# The fixed menu of actions the Tier 2 agent may choose from. Anything it
# proposes outside this list either has no controller method (op_has_controller_support
# gate denies it) or is caught by params_in_bounds — this catalog just keeps
# the model from wasting a turn proposing something structurally impossible.
AGENT_OP_CATALOG = {
    "clear_protective_stop": "Clear a protective stop / safety popup. Takes no params.",
    "reset_program_pointer": "Stop and restart the currently loaded program from the top. Takes no params.",
    "move_joints": "Move to specific joint angles (radians). Params: angles_rad (6 floats), a (acceleration), v (velocity).",
    "set_payload": "Set the active payload mass (kg) and optional center of gravity. Params: mass_kg, cog ([x,y,z] or null).",
    "emergency_stop": "Immediately halt whatever program is running. Takes no params.",
}

AGENT_SYSTEM_INSTRUCTIONS = """You are the Tier 2 proposer for a factory robot arm's safety-gated recovery system.

A fault occurred that isn't on the Tier 1 auto-recovery whitelist, so a human will review whatever you propose before anything runs — you are not executing anything, only recommending one action.

Propose exactly ONE action from this catalog, or "escalate" if none of them are appropriate for this fault:
{catalog}

You may also be given how long the fault has been ongoing, and recent history of past cycles for this same fault type. Use that history: if the same action was already tried and denied or failed, do not just propose it again — consider why it failed, or escalate instead.

Only fill in params that apply to the op you chose; leave every other param field null. Keep reasoning to one or two sentences a human can quickly judge."""

AGENT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": list(AGENT_OP_CATALOG.keys()) + ["escalate"],
        },
        "params": {
            "type": "object",
            "properties": {
                "angles_rad": {"type": ["array", "null"], "items": {"type": "number"}},
                "a": {"type": ["number", "null"]},
                "v": {"type": ["number", "null"]},
                "mass_kg": {"type": ["number", "null"]},
                "cog": {"type": ["array", "null"], "items": {"type": "number"}},
            },
            "required": ["angles_rad", "a", "v", "mass_kg", "cog"],
            "additionalProperties": False,
        },
        "reasoning": {"type": "string"},
    },
    "required": ["op", "params", "reasoning"],
    "additionalProperties": False,
}

# Which raw param keys actually apply to each op — everything else gets
# dropped even if the model filled it in (it shouldn't have, but don't trust it).
_OP_PARAM_KEYS = {
    "move_joints": ("angles_rad", "a", "v"),
    "set_payload": ("mass_kg", "cog"),
}


def _clean_params(op: str, raw_params: dict) -> dict:
    keys = _OP_PARAM_KEYS.get(op, ())
    return {k: raw_params[k] for k in keys if raw_params.get(k) is not None}


class AgentProposer:
    """Tier 2 proposer: calls an LLM to reason about an unrecognized fault and
    propose one action. Never executes anything — Guard + human approval still
    gate everything downstream, unchanged from any other proposer."""

    def __init__(self, controller=None, monitor=None, audit=None, model=None):
        self.controller = controller
        self.monitor = monitor
        self.audit = audit
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4.1")
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI()
        return self._client

    def propose(self, fault, tier) -> Proposal:
        try:
            return self._propose(fault)
        except Exception as e:
            logger.exception(f"AgentProposer failed to get a proposal for fault_type={fault.fault_type}")
            return Proposal(op="escalate", params={}, reasoning=f"AgentProposer error: {e}")

    def _propose(self, fault) -> Proposal:
        live_status = self.controller.get_status() if self.controller else None
        fault_duration = self.monitor.fault_duration_seconds() if self.monitor else None
        recent_history = self.audit.read_recent(n=5, fault_type=fault.fault_type) if self.audit else []

        user_input = (
            f"Fault detected:\n"
            f"  fault_type: {fault.fault_type}\n"
            f"  safetystatus (at detection): {fault.safetystatus}\n"
            f"  robotmode (at detection): {fault.robotmode}\n"
        )
        if live_status:
            user_input += (
                f"\nCurrent live status (just re-checked):\n"
                f"  safetystatus: {live_status['safetystatus']}\n"
                f"  robotmode: {live_status['robotmode']}\n"
            )
        if fault_duration is not None:
            user_input += f"\nThis fault has been ongoing for {fault_duration:.1f} seconds (not yet resolved).\n"
        if recent_history:
            user_input += "\nRecent history for this same fault_type (most recent last):\n"
            for entry in recent_history:
                line = (f"  - tier={entry.get('tier')} proposed_op={entry.get('proposed_op')} "
                        f"decision={entry.get('decision')} executed={entry.get('executed')}")
                if entry.get("proposed_reasoning"):
                    line += f" reasoning=\"{entry['proposed_reasoning']}\""
                user_input += line + "\n"

        client = self._get_client()
        response = client.responses.create(
            model=self.model,
            instructions=AGENT_SYSTEM_INSTRUCTIONS.format(
                catalog="\n".join(f"- {op}: {desc}" for op, desc in AGENT_OP_CATALOG.items())
            ),
            input=user_input,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "tier2_proposal",
                    "schema": AGENT_RESPONSE_SCHEMA,
                    "strict": True,
                }
            },
        )

        parsed = json.loads(response.output_text)
        op = parsed["op"]
        params = _clean_params(op, parsed["params"])
        return Proposal(op=op, params=params, reasoning=parsed["reasoning"])


class AetherionProposer:
    """Tier 2 proposer backed by the factory_arm_agent published on Aetherion sbox.
    The agent does the LLM proposal AND the human approval (Slack, with one retry on
    rejection), so this blocks until that run finishes. Only an agent run that ends
    "approved" counts as human approval — approval() reports it for Guard's
    tier2_approval gate, and Guard still re-verifies live state before anything runs."""

    def __init__(self, controller=None, monitor=None, audit=None, client=None, wait_seconds=None):
        self.controller = controller
        self.monitor = monitor
        self.audit = audit
        if client is None:
            import aetherion_client as client
        self.client = client
        self.wait_seconds = wait_seconds or int(os.environ.get("AETHERION_TIER2_WAIT_SECONDS", "900"))
        self._approved_proposal = None

    def propose(self, fault, tier) -> Proposal:
        self._approved_proposal = None
        self._run_id = ""
        try:
            proposal = self._propose(fault)
        except Exception as e:
            logger.exception(f"AetherionProposer failed to get a proposal for fault_type={fault.fault_type}")
            proposal = Proposal(op="escalate", params={}, reasoning=f"AetherionProposer error: {e}")
        proposal.agent_run_id = self._run_id
        return proposal

    def approval(self, fault, proposal) -> bool:
        """Approval hook for Service: True only for the exact proposal the agent's run
        returned as approved in Slack — never for an escalation or a stale proposal."""
        return proposal is not None and proposal is self._approved_proposal

    def _agent_params(self, fault):
        live_status = self.controller.get_status() if self.controller else None
        fault_duration = self.monitor.fault_duration_seconds() if self.monitor else None
        recent_history = self.audit.read_recent(n=5, fault_type=fault.fault_type) if self.audit else []
        params = {
            "fault": {"fault_type": fault.fault_type, "safetystatus": fault.safetystatus,
                      "robotmode": fault.robotmode},
            "recent_history": recent_history,
        }
        if live_status:
            params["live_status"] = {"safetystatus": live_status["safetystatus"],
                                     "robotmode": live_status["robotmode"]}
        if fault_duration is not None:
            params["fault_duration_seconds"] = int(fault_duration)
        return params

    def _propose(self, fault) -> Proposal:
        run_id, status, text = self.client.run_agent(self._agent_params(fault))
        if not run_id or not 200 <= status < 300:
            return Proposal(op="escalate", params={}, reasoning=f"Aetherion agent did not start ({status}): {text}")
        self._run_id = run_id
        logger.info(f"Tier 2 fault {fault.fault_type} sent to Aetherion agent, run_id={run_id}")

        print(f"\n--- Tier 2: sent to the Aetherion agent (run {run_id}) ---")
        print("Waiting for a person to approve or reject the proposed fix in Slack...")
        run = self.client.wait_for_run(run_id, timeout_seconds=self.wait_seconds)
        if run is None:
            return Proposal(op="escalate", params={},
                            reasoning=f"no decision from the Aetherion agent within {self.wait_seconds}s "
                                      f"(run {run_id}); a later Slack reply will not be acted on")
        if run.get("status") != "COMPLETED":
            return Proposal(op="escalate", params={},
                            reasoning=f"Aetherion agent run {run_id} ended {run.get('status')}: "
                                      f"{(run.get('status_details') or {}).get('message', '')}")

        output = run.get("output_payload") or {}
        outcome = output.get("status")
        agent_proposal = output.get("proposal") or output.get("last_proposal") or {}
        print(f"Agent outcome: {outcome}")
        if outcome != "approved":
            reason = output.get("feedback") or agent_proposal.get("reasoning", "")
            return Proposal(op="escalate", params={}, reasoning=f"agent outcome '{outcome}': {reason}")

        op = agent_proposal.get("op", "")
        # The agent's own output schema doesn't restrict op to the catalog, so enforce it
        # here: otherwise a Slack-approved op like power_off would pass Guard's
        # op_has_controller_support gate just because the controller happens to have it.
        if op not in AGENT_OP_CATALOG:
            return Proposal(op="escalate", params={},
                            reasoning=f"agent proposed '{op}', which is not in the Tier 2 catalog")
        proposal = Proposal(op=op, params=_clean_params(op, agent_proposal.get("params") or {}),
                            reasoning=agent_proposal.get("reasoning", ""))
        self._approved_proposal = proposal
        return proposal


class CompositeProposer:
    """Dispatches to a different proposer per tier. Tier 1 stays on the deterministic
    DefaultProposer (no API calls, no cost) — only Tier 2 faults reach the LLM.
    Tier 2 defaults to the Aetherion agent; TIER2_BACKEND=local uses AgentProposer
    (direct OpenAI call + terminal approval) instead."""

    def __init__(self, tier1_proposer=None, tier2_proposer=None):
        self.tier1_proposer = tier1_proposer or DefaultProposer()
        if tier2_proposer is None:
            backend = os.environ.get("TIER2_BACKEND", "aetherion")
            tier2_proposer = AgentProposer() if backend == "local" else AetherionProposer()
        self.tier2_proposer = tier2_proposer

    def propose(self, fault, tier) -> Proposal:
        if tier == 1:
            return self.tier1_proposer.propose(fault, tier)
        return self.tier2_proposer.propose(fault, tier)
