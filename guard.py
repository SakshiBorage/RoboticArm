"""
Guard: the only thing in this pipeline allowed to deny a proposal. Runs a
fixed list of gates against a proposal; ALL must pass for APPROVED, and the
first failing gate's reason is what gets logged as the denial reason.

Note: this is a fresh, honestly-scoped gate list for the current build —
not a recovery of the "12 gates" figure mentioned in earlier project notes,
which belonged to a harness that isn't present in this codebase. Extend as
real gaps surface.

Current scope is Tier 1 only (see progress.md decision: a denied Tier 1
proposal dead-ends as escalate-only, it does not auto-reroute to Tier 2).
Tier 2's approval gate is a stub until that tier gets built out.
"""
import math

# The 3 of the 5 originally-noted Tier 1 candidates that have a controller
# implementation in this environment. quick_master (needs verified battery
# loss telemetry — not simulable in URSim) is deliberately excluded: it will
# fail op_has_controller_support below if ever proposed.
TIER1_WHITELIST = {"clear_protective_stop", "reset_program_pointer", "set_payload"}

PARAM_BOUNDS = {
    "set_payload": lambda params: 0 <= params.get("mass_kg", 0) <= 5.0,
    "move_joints": lambda params: all(abs(a) <= 2 * math.pi for a in params.get("angles_rad", [])),
}


class Guard:
    def evaluate(self, proposal, context, controller):
        gate_results = []

        def gate(name, passed, fail_reason=""):
            gate_results.append({"name": name, "passed": passed, "reason": "" if passed else fail_reason})
            return passed

        try:
            status = controller.get_status()
        except OSError as e:
            gate("controller_reachable", False, str(e))
            return gate_results, "DENIED"
        gate("controller_reachable", True)

        if not gate("op_has_controller_support", hasattr(controller, proposal.op),
                     f"controller has no method '{proposal.op}'"):
            return gate_results, "DENIED"

        tier = context["tier"]
        if tier == 1:
            if not gate("tier1_whitelist", proposal.op in TIER1_WHITELIST,
                         f"'{proposal.op}' is not in the Tier 1 whitelist"):
                return gate_results, "DENIED"
        elif tier == 2:
            if not gate("tier2_approval", context.get("approved", False),
                         "Tier 2 proposal has not been human-approved"):
                return gate_results, "DENIED"

        bounds_check = PARAM_BOUNDS.get(proposal.op)
        if bounds_check is not None:
            if not gate("params_in_bounds", bounds_check(proposal.params),
                         f"params out of bounds for '{proposal.op}': {proposal.params}"):
                return gate_results, "DENIED"
        else:
            gate("params_in_bounds", True, "no bounds defined for this op")

        if proposal.op == "clear_protective_stop":
            still_faulted = "PROTECTIVE_STOP" in status["safetystatus"]
            if not gate("live_state_matches_fault", still_faulted,
                        "fault this proposal targets is no longer present"):
                return gate_results, "DENIED"
        else:
            gate("live_state_matches_fault", True, "no live-state precondition defined for this op")

        return gate_results, "APPROVED"
