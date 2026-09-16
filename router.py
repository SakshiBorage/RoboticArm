"""
Assigns a tier to a fault BEFORE any proposal exists — per the locked
architecture, the router looks only at the fault itself, not at what an
agent later proposes to do about it. Guard is still the only thing that can
deny; this just decides which path (autonomous-candidate vs human-approval)
a fault is routed down.
"""

# Fault types with a known, whitelisted autonomous recovery path are
# candidates for Tier 1. Anything not listed here defaults to Tier 2 —
# unrecognized faults always go to a human, never assumed safe.
TIER1_CANDIDATE_FAULT_TYPES = {"PROTECTIVE_STOP"}


class Router:
    def assign_tier(self, fault) -> int:
        return 1 if fault.fault_type in TIER1_CANDIDATE_FAULT_TYPES else 2
