"""
Live, watchable demo. The arm always does the same job — pick an object up at
Station A, carry it, place it at Station B — and each run picks 3 different
points within that same job where something goes wrong:

  1. Fault while picking up the object (REAL fault on real URSim) -> Tier 1
  2. Fault while carrying the object   (REAL fault on real URSim) -> Tier 1
  3. Anomaly flagged while placing the object (SIMULATED -- this URSim setup
     can't organically produce a second real fault type, so this one is
     injected directly, clearly labeled) -> Tier 2 -> LLM proposes an action
     -> YOU approve/deny it live at a terminal prompt

Each scenario runs the same task from start to finish — the fault interrupts
it partway through, and once handled, the task picks back up from exactly
where it stopped and finishes the job.

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

MOVE_A = 0.15
MOVE_V = 0.1
STEP_PAUSE = 3.5

# The one task every scenario runs — pick from Station A, place at Station B.
TASK_STEPS = [
    ("Moving to Station A to pick up the object — watch the pendant now.", ABOVE_STATION_A),
    ("Picking up the object at Station A...", AT_STATION_A),
    ("Lifting the object off Station A...", ABOVE_STATION_A),
    ("Carrying the object toward Station B...", ABOVE_STATION_B),
    ("Placing the object down at Station B...", AT_STATION_B),
    ("Object placed. Retreating from Station B...", ABOVE_STATION_B),
    ("Returning home...", HOME),
]

OVERSPEED_SCRIPT = """
def unsafe_speed():
  speedj([10, 10, 10, 10, 10, 10], a=40, t=3)
end
unsafe_speed()
"""


def run_steps(controller, indices):
    for i in indices:
        label, target = TASK_STEPS[i]
        print(label)
        controller.move_joints(target, a=MOVE_A, v=MOVE_V)
        time.sleep(STEP_PAUSE)


def finish_task_message():
    print(">>> Task complete — object moved from Station A to Station B.\n")


def run_normal_cycle(controller):
    """One full pick-and-place cycle with no fault, so you see the arm working
    correctly before watching it break and recover on the next cycle."""
    print(">>> Running one normal cycle first, to show the baseline behavior...\n")
    run_steps(controller, [0, 1, 2, 3, 4, 5, 6])
    print(">>> Normal cycle complete, no faults. Starting a second cycle — this time something goes wrong...\n")


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


# --- Scenario 1: fault while picking up the object ---------------------------

def scenario_fault_during_pickup(controller, service):
    run_normal_cycle(controller)

    run_steps(controller, [0])
    label, target = TASK_STEPS[1]
    print(label)
    controller.move_joints(target, a=MOVE_A, v=MOVE_V)

    threading.Thread(target=trigger_overspeed_after, args=(2.0,)).start()

    def resume(controller):
        time.sleep(1.5)
        print(">>> Recovered — resuming: finishing the pickup...\n")
        run_steps(controller, [2, 3, 4, 5, 6])
        finish_task_message()

    print("\nMonitoring for the fault...\n")
    run_monitoring_window(service, controller, max_seconds=30, on_recovered=resume)


# --- Scenario 2: fault while carrying the object ------------------------------

def scenario_fault_during_carry(controller, service):
    run_normal_cycle(controller)

    run_steps(controller, [0, 1, 2])
    label, target = TASK_STEPS[3]
    print(label)
    controller.move_joints(target, a=MOVE_A, v=MOVE_V)

    threading.Thread(target=trigger_overspeed_after, args=(2.0,)).start()

    def resume(controller):
        time.sleep(1.5)
        print(">>> Recovered — resuming: carrying the object the rest of the way...\n")
        run_steps(controller, [4, 5, 6])
        finish_task_message()

    print("\nMonitoring for the fault...\n")
    run_monitoring_window(service, controller, max_seconds=30, on_recovered=resume)


# --- Scenario 3: anomaly flagged while placing the object (synthetic Tier 2) --

def scenario_anomaly_during_place(controller, service):
    run_normal_cycle(controller)

    run_steps(controller, [0, 1, 2, 3, 4])

    print(">>> [SIMULATED — not from real hardware] A joint-over-temperature anomaly")
    print(">>> was flagged during the placement. Tier 2 path: LLM proposes an action, you approve or deny it.\n")
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

    run_steps(controller, [5, 6])
    finish_task_message()


SCENARIOS = [
    ("Fault while picking up the object (real, Tier 1)", scenario_fault_during_pickup),
    ("Fault while carrying the object (real, Tier 1)", scenario_fault_during_carry),
    ("Anomaly flagged while placing the object (SIMULATED, Tier 2 — LLM + your approval)", scenario_anomaly_during_place),
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
