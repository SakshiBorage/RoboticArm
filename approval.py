"""Human approval gate for Tier 2 proposals — a simple blocking CLI prompt."""


def cli_approval(fault, proposal) -> bool:
    print("\n--- Tier 2 approval needed ---")
    print(f"Fault: {fault.fault_type}  (safetystatus={fault.safetystatus}, robotmode={fault.robotmode})")
    print(f"Proposed action: {proposal.op}  params={proposal.params}")
    if proposal.reasoning:
        print(f"Reasoning: {proposal.reasoning}")
    answer = input("Approve? [y/N]: ").strip().lower()
    return answer == "y"
