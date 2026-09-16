"""
Live, watchable demo covering both tiers of the pipeline, via 3 scenarios run
in random order each time:

  1. Pick-and-place overspeed (REAL fault on real URSim) -> Tier 1, autonomous
  2. Return-to-home overspeed (REAL fault on real URSim) -> Tier 1, autonomous
  3. Joint-over-temperature (SIMULATED -- injected directly, not from real
     hardware, since this URSim setup can't organically produce a second real
     fault type) -> Tier 2 -> LLM proposes an action -> YOU approve/deny it
     live at a terminal prompt

Open http://localhost:6080/vnc.html in a browser BEFORE running this, so you
can watch the arm on the pendant's 3D view while it runs.

Run with: python3 demo.py
"""
import random
import socket
import threading
import time

from models import Fault
from service import Service, fault_from_status
from ursim_controller import URSimController

HOME = [0, -1.57, 0, -1.57, 0, 0]
ABOVE_STATION_A = [-0.5, -1.2, 1.3, -1.6, -1.5, 0]
AT_STATION_A = [-0.5, -1.0, 1.5, -2.0, -1.5, 0]
ABOVE_STATION_B = [0.9, -1.2, 1.3, -1.6, -1.5, 0]
AT_STATION_B = [0.9, -1.0, 1.5, -2.0, -1.5, 0]

OVERSPEED_SCRIPT = """
def unsafe_speed():
  speedj([10, 10, 10, 10, 10, 10], a=40, t=3)
end
unsafe_speed()
"""


def trigger_overspeed_after(delay):
    time.sleep(delay)
    print("\n>>> Something goes wrong — triggering a real overspeed fault...\n")
    with socket.create_connection(("127.0.0.1", 30002), timeout=5) as s:
        s.sendall(OVERSPEED_SCRIPT.encode())


def print_cycle_result(result):
    print(f">>> CYCLE: fault={result.fault.fault_type}  tier={result.tier}  "
          f"proposed_op={result.proposal.op}  decision={result.decision}  "
          f"executed={result.executed}")
    if result.proposal.reasoning:
        print(f">>>   reasoning: {result.proposal.reasoning}")
    if result.decision == "DENIED":
        failed = next((g for g in result.gate_results if not g["passed"]), None)
        if failed:
            print(f">>>   denied by gate '{failed['name']}': {failed['reason']}")
    if result.execution_error:
        print(f">>>   EXECUTION ERROR: {result.execution_error}  (see service.log for the full traceback)")


def print_monitor_event(event_type, data):
    if event_type == "POLL_ERROR":
        print(f">>> [WARNING] Lost contact with the controller: {data.get('error')}")
    elif event_type == "STUCK":
        print(f">>> [WARNING] Fault has been stuck for {data.get('duration_s', 0):.1f}s with no auto-recovery")
    elif event_type == "RECOVERED":
        print(f">>> Status returned to NORMAL on its own after {data.get('duration_s', 0):.1f}s (no Tier action needed)")


def run_monitoring_window(service, controller, max_seconds, on_recovered):
    """Polls until the fault triggers, gets fixed, and the resume sequence finishes
    — then returns immediately instead of waiting out a fixed window. max_seconds
    is just a safety ceiling in case something never resolves."""
    done = threading.Event()

    def on_recovered_and_signal(controller):
        on_recovered(controller)
        done.set()

    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline and not done.is_set():
        status, events = service.monitor.poll_once()
        for event_type, data in events:
            if event_type == "FAULT_CONFIRMED":
                fault = fault_from_status(data)
                result = service.run_cycle(fault)
                print_cycle_result(result)
                if result.decision == "APPROVED" and result.executed:
                    threading.Thread(target=on_recovered_and_signal, args=(controller,)).start()
            else:
                print_monitor_event(event_type, data)
        time.sleep(service.monitor.poll_interval)


# --- Scenario 1: pick-and-place, fault mid-carry -----------------------------

def resume_pick_and_place(controller):
    time.sleep(1.5)
    print(">>> Recovered — resuming the task: carrying the part the rest of the way to Station B...\n")
    controller.move_joints(ABOVE_STATION_B, a=0.15, v=0.1)
    time.sleep(4)
    print(">>> Placing the part down at Station B...\n")
    controller.move_joints(AT_STATION_B, a=0.15, v=0.1)
    time.sleep(3)
    print(">>> Part placed. Retreating and returning home...\n")
    controller.move_joints(ABOVE_STATION_B, a=0.15, v=0.1)
    time.sleep(2.5)
    controller.move_joints(HOME, a=0.15, v=0.1)
    time.sleep(1)
    print(">>> Task complete — pick-and-place finished despite the mid-task fault.\n")


def scenario_pick_and_place(controller, service):
    print("Moving to Station A to pick up the part — watch the pendant now.")
    controller.move_joints(ABOVE_STATION_A, a=0.15, v=0.1)
    time.sleep(4)
    print("Picking up the part at Station A...")
    controller.move_joints(AT_STATION_A, a=0.15, v=0.1)
    time.sleep(3)
    print("Lifting the part off Station A...")
    controller.move_joints(ABOVE_STATION_A, a=0.15, v=0.1)
    time.sleep(3)
    print("Carrying the part toward Station B...")
    controller.move_joints(ABOVE_STATION_B, a=0.15, v=0.1)

    threading.Thread(target=trigger_overspeed_after, args=(2.5,)).start()

    print("\nMonitoring for the fault...\n")
    run_monitoring_window(service, controller, max_seconds=30, on_recovered=resume_pick_and_place)


# --- Scenario 2: finish a task, fault while returning home -------------------

def resume_return_home(controller):
    time.sleep(1.5)
    print(">>> Recovered — resuming: returning to home position...\n")
    controller.move_joints(HOME, a=0.15, v=0.1)
    time.sleep(3.5)
    print(">>> Arm back home despite the mid-task fault.\n")


def scenario_return_home(controller, service):
    print("Moving to a work position — watch the pendant now.")
    controller.move_joints(ABOVE_STATION_B, a=0.15, v=0.1)
    time.sleep(4)
    print("Work done — returning to home position...")
    controller.move_joints(HOME, a=0.15, v=0.1)

    threading.Thread(target=trigger_overspeed_after, args=(2,)).start()

    print("\nMonitoring for the fault...\n")
    run_monitoring_window(service, controller, max_seconds=30, on_recovered=resume_return_home)


# --- Scenario 3: synthetic Tier 2 fault (LLM + human approval) ---------------

def scenario_tier2_llm(controller, service):
    print(">>> [SIMULATED — not from real hardware] Injecting a joint-over-temperature")
    print(">>> fault to exercise the Tier 2 path: LLM proposes an action, you approve or deny it.\n")
    fault = Fault(
        fault_type="JOINT_OVER_TEMPERATURE",
        safetystatus="Safetystatus: JOINT_OVER_TEMPERATURE (SIMULATED)",
        robotmode="Robotmode: RUNNING",
        detected_at=time.time(),
    )
    result = service.run_cycle(fault)
    print()
    print_cycle_result(result)
    print()


SCENARIOS = [
    ("Pick-and-place overspeed (real, Tier 1)", scenario_pick_and_place),
    ("Return-to-home overspeed (real, Tier 1)", scenario_return_home),
    ("Joint over-temperature (SIMULATED, Tier 2 — LLM + your approval)", scenario_tier2_llm),
]


def main():
    controller = URSimController()

    print("Powering on / releasing brakes...")
    controller.power_on()
    print("Status:", controller.get_status())

    service = Service(controller=controller)

    order = list(SCENARIOS)
    random.shuffle(order)

    for i, (name, scenario_fn) in enumerate(order, start=1):
        print(f"\n{'=' * 70}")
        print(f"Scenario {i}/{len(order)}: {name}")
        print(f"{'=' * 70}\n")
        time.sleep(3)
        scenario_fn(controller, service)
        time.sleep(3)

    print("\nAll scenarios complete. Final status:", controller.get_status())


if __name__ == "__main__":
    main()
