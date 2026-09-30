"""
Live, watchable demo of all three fault tiers plus the preventive check. The
arm always does the same job — pick an object up at Station A, carry it, place
it at Station B — and something goes wrong at a different stage each time:

  0. Baseline: one full job with no faults.
  1. Preventive check: the wrist is turned close to its end stop (still inside
     the limit) -> an EARLY WARNING fires before any fault happens. Warn-only.
  2. Tier 1 — fault while carrying the object (REAL overspeed protective stop)
     -> fixed automatically, the job resumes.
  3. Tier 2 — payload mismatch while carrying/placing the object (SIMULATED:
     URSim has no real gripper to weigh the object, so it's injected, clearly
     labeled, and for this scenario only the arm's reported status shows it
     too, so the agent and Guard see a live fault) -> the Aetherion agent
     proposes a fix (typically set_payload) -> YOU approve/reject it in Slack
     -> Guard re-checks -> it runs or doesn't.
     (TIER2_BACKEND=local instead: direct LLM call + terminal y/N prompt)
  4. Tier 2 again, reject-then-retry — payload mismatch right after lifting
     (SIMULATED), but this time the gripper's reading is wrong -> YOU reject
     the first proposal in Slack with the real weight as feedback -> the agent
     retries once with that feedback -> YOU approve the revised fix -> Guard
     re-checks -> it runs.
  5. Tier 3 — critical fault while picking up the object (REAL controller
     FAULT; the arm powers itself off) -> halted, nothing fixed automatically,
     a person is alerted in Slack -> YOU confirm at the terminal that the arm
     was inspected -> safety is restarted and the job finishes.

Our pendant messages stay up until OK is pressed (PolyScope shows the OLDEST
open one on top — press OK to see the next), and the demo pauses
PENDANT_MESSAGE_HOLD seconds after each so the arm doesn't move straight on.
For Tier 1, UR's own "Safety Message" (error code, explanation, suggestion) is
held for PENDANT_MESSAGE_HOLD seconds before the fix runs, since clearing a
protective stop dismisses it. Early warnings go to the pendant's Log tab (plus
terminal and Slack) rather than a popup, so they can't cover a fault message. Everything is recorded in a new
logs/<timestamp>_demo/ folder — see run_logs.py.

Open http://localhost:6080/vnc.html in a browser BEFORE running this, so you
can watch the arm on the pendant's 3D view while it runs.

Run with: python3 demo.py            (all scenes)
      or: python3 demo.py 3 4        (only the listed scene numbers, as printed)
"""
import contextlib
import socket
import sys
import threading
import time

from audit import AuditLog
from models import Fault
from preventive import PreventiveMonitor, make_alert_handlers
from run_logs import RunLog
from service import Service, fault_from_status
from ursim_controller import URSimController

HOME = [0, -1.57, 0, -1.57, 0, 0]
ABOVE_STATION_A = [-0.5, -1.2, 1.3, -1.6, -1.5, 0]
AT_STATION_A = [-0.5, -1.0, 1.5, -2.0, -1.5, 0]
ABOVE_STATION_B = [0.9, -1.2, 1.3, -1.6, -1.5, 0]
AT_STATION_B = [0.9, -1.0, 1.5, -2.0, -1.5, 0]
# Wrist 3 at 6.1 rad (~350°): ~10° inside its ±360° range — close enough to warn, not a fault.
WRIST_NEAR_LIMIT = [0, -1.57, 0, -1.57, 0, 6.1]

MOVE_A = 0.15
MOVE_V = 0.1
STEP_PAUSE = 3.5
PENDANT_MESSAGE_HOLD = 10
PREVENTIVE_INTERVAL = 2.0

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

# URSim-only: raises a real controller FAULT (the arm powers off), which needs
# "restart safety" to clear — a genuine Tier 3 condition, not an injected Fault object.
CRITICAL_FAULT_SCRIPT = """
def trigger_critical_fault():
  simulator_fault()
end
trigger_critical_fault()
"""


class DemoController(URSimController):
    """URSimController whose reported status can be overridden — used ONLY for the
    SIMULATED Tier 2 scenario, so the agent and Guard see the injected fault as live.
    Every other scenario reads the real controller status."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._simulated_status = None

    def get_status(self):
        if self._simulated_status is not None:
            return dict(self._simulated_status)
        return super().get_status()

    @contextlib.contextmanager
    def simulate_status(self, status):
        self._simulated_status = status
        try:
            yield
        finally:
            self._simulated_status = None


def run_steps(controller, indices):
    for i in indices:
        label, target = TASK_STEPS[i]
        print(label)
        controller.move_joints(target, a=MOVE_A, v=MOVE_V)
        time.sleep(STEP_PAUSE)


def finish_task_message():
    print(">>> Task complete — object moved from Station A to Station B.\n")


def hold_for_pendant(what="the message on the pendant"):
    """Our own pendant messages stay until OK is pressed; this pause is so the arm
    doesn't move straight on while someone is reading."""
    print(f"(Holding {PENDANT_MESSAGE_HOLD}s so {what} can be read...)")
    time.sleep(PENDANT_MESSAGE_HOLD)


def send_script_after(delay, script, message):
    time.sleep(delay)
    print(f"\n>>> Something goes wrong — {message}\n")
    with socket.create_connection(("127.0.0.1", 30002), timeout=5) as s:
        s.sendall(script.encode())


def print_cycle_result(result):
    print(f">>> CYCLE: fault={result.fault.fault_type}  tier={result.tier}  "
          f"proposed_op={result.proposal.op}  decision={result.decision}  "
          f"executed={result.executed}")
    if result.proposal.agent_run_id:
        print(f">>>   Aetherion run: {result.proposal.agent_run_id}")
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
        print(f">>> Status returned to NORMAL after {data.get('duration_s', 0):.1f}s")


def run_monitoring_window(service, run_log, max_seconds, on_recovered=None):
    """Polls until a fault is confirmed and handled, then returns its CycleResult.
    If on_recovered is given and the fix ran (Tier 1/2), it runs the resume
    sequence and waits for it before returning. Returns None if no fault shows
    up within max_seconds (a safety ceiling, not a fixed wait)."""
    deadline = time.monotonic() + max_seconds
    result = None
    resume_thread = None
    while time.monotonic() < deadline:
        _, events = service.monitor.poll_once()
        for event_type, data in events:
            if event_type == "FAULT_CONFIRMED" and result is None:
                fault = fault_from_status(data)
                if service.router.assign_tier(fault) == 1:
                    # The Tier 1 fix dismisses UR's safety popup; give people time to read it first.
                    hold_for_pendant("UR's safety message (error code + explanation)")
                result = service.run_cycle(fault)
                print_cycle_result(result)
                fixed = result.tier != 3 and result.decision == "APPROVED" and result.executed
                if on_recovered and fixed:
                    resume_thread = threading.Thread(target=on_recovered)
                    resume_thread.start()
            elif event_type != "FAULT_CONFIRMED":
                print_monitor_event(event_type, data)
                run_log.event(f"monitor_{event_type.lower()}", **data)
        if result is not None and (resume_thread is None or not resume_thread.is_alive()):
            return result
        time.sleep(service.monitor.poll_interval)
    return result


# --- Scene 0: baseline ---------------------------------------------------------

def run_normal_cycle(controller, service, preventive, run_log):
    """One full pick-and-place with no fault, so you see the arm working
    correctly before watching it break."""
    print(">>> Running one normal cycle first, to show the baseline behavior...\n")
    run_steps(controller, range(len(TASK_STEPS)))
    print(">>> Normal cycle complete, no faults.\n")


# --- Scene 1: preventive check ------------------------------------------------

def scene_preventive_wrist_near_limit(controller, service, preventive, run_log):
    print(">>> The health check runs every few seconds in the background, looking for trouble")
    print(">>> BEFORE it becomes a fault. Now turning the wrist close to its end stop (still inside")
    print(">>> the limit, so no fault) — watch for the early warning...\n")
    controller.move_joints(WRIST_NEAR_LIMIT, a=0.5, v=0.8)

    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and "joint_limit:Wrist 3" not in preventive.active:
        time.sleep(0.5)
    if "joint_limit:Wrist 3" in preventive.active:
        # Early warnings normally go to the pendant's Log tab (so they never cover a
        # fault message); this scene is about the warning, so show it as a popup too.
        controller.notify(f"Early warning: {preventive.active['joint_limit:Wrist 3']}")
        print(">>> (Press OK on the pendant to dismiss the warning.)")
        hold_for_pendant()
        print(">>> No fault happened — the warning came first, and the arm was never stopped.")
    else:
        print(">>> [!] The early warning didn't fire within 25s — check service.log.")

    print(">>> Turning the wrist back to a safe position...\n")
    controller.move_joints(HOME, a=0.5, v=0.8)
    time.sleep(10)


# --- Scene 2: Tier 1 — real protective stop while carrying --------------------

def scenario_tier1_fault_during_carry(controller, service, preventive, run_log):
    run_steps(controller, [0, 1, 2])
    label, target = TASK_STEPS[3]
    print(label)
    controller.move_joints(target, a=MOVE_A, v=MOVE_V)

    threading.Thread(target=send_script_after,
                     args=(2.0, OVERSPEED_SCRIPT, "triggering a real overspeed fault...")).start()

    def resume():
        hold_for_pendant()
        print(">>> Recovered — resuming: carrying the object the rest of the way...\n")
        run_steps(controller, [4, 5, 6])
        finish_task_message()

    print("\nMonitoring for the fault...\n")
    if run_monitoring_window(service, run_log, max_seconds=60, on_recovered=resume) is None:
        print(">>> [!] The fault never showed up — check the pendant.")


# --- Scene 3: Tier 2 — simulated payload mismatch while placing, Slack approval

SIMULATED_OBJECT_KG = 1.2


def scenario_tier2_payload_mismatch(controller, service, preventive, run_log):
    run_steps(controller, [0, 1, 2, 3, 4])

    print(">>> [SIMULATED — not from real hardware] The gripper reports it's holding "
          f"{SIMULATED_OBJECT_KG} kg, but the arm's")
    print(">>> configured payload is 0 kg — a mismatch the Tier 1 list has no fix for. Tier 2 path: the")
    print(">>> Aetherion agent proposes an action and posts it to Slack — reply there (in the channel,")
    print(">>> not a thread) with \"approve\", or anything else to reject it with that as feedback.\n")
    fault = Fault(
        fault_type="PAYLOAD_MISMATCH",
        safetystatus=(f"Safetystatus: PAYLOAD_MISMATCH (SIMULATED) - gripper reports "
                      f"{SIMULATED_OBJECT_KG} kg held, configured payload is 0.0 kg"),
        robotmode="Robotmode: RUNNING",
        detected_at=time.time(),
    )
    with controller.simulate_status({"safetystatus": fault.safetystatus, "robotmode": fault.robotmode}):
        result = service.run_cycle(fault)
    print()
    print_cycle_result(result)
    hold_for_pendant()
    print()

    run_steps(controller, [5])
    if result.executed and result.proposal.op == "set_payload":
        # Part of the task, not a fault fix: the object has been put down.
        print(">>> Object released — setting the payload back to 0 kg.")
        controller.set_payload(0.0)
    run_steps(controller, [6])
    finish_task_message()


# --- Scene 4: Tier 2 — reject, the agent retries with your feedback, approve ---

GRIPPER_READING_KG = 2.4
WORK_ORDER_KG = 1.5
REJECTION_FEEDBACK = (f"No - the gripper's load sensor is out of calibration. The part weighs "
                      f"{WORK_ORDER_KG} kg per the work order. Set the payload to {WORK_ORDER_KG} kg.")


def scenario_tier2_reject_then_retry(controller, service, preventive, run_log):
    run_steps(controller, [0, 1, 2])

    print(f">>> [SIMULATED — not from real hardware] Right after lifting, the gripper reports "
          f"{GRIPPER_READING_KG} kg, but the")
    print(">>> configured payload is 0 kg. This time the gripper's reading is WRONG, and only you know it.")
    print(">>> The agent will post a first proposal in Slack. REJECT it by replying in the channel with:\n")
    print(f"      {REJECTION_FEEDBACK}\n")
    print(">>> The agent retries once with your reply as feedback and posts a new proposal.")
    print(">>> If it now uses the right weight, reply \"approve\".\n")
    fault = Fault(
        fault_type="PAYLOAD_MISMATCH",
        safetystatus=(f"Safetystatus: PAYLOAD_MISMATCH (SIMULATED) - gripper reports "
                      f"{GRIPPER_READING_KG} kg held, configured payload is 0.0 kg"),
        robotmode="Robotmode: RUNNING",
        detected_at=time.time(),
    )
    with controller.simulate_status({"safetystatus": fault.safetystatus, "robotmode": fault.robotmode}):
        result = service.run_cycle(fault)
    print()
    print_cycle_result(result)
    if result.executed and result.proposal.op == "set_payload":
        print(f">>> The approved retry ran: payload is now {result.proposal.params.get('mass_kg')} kg "
              f"(the gripper said {GRIPPER_READING_KG} kg).")
    hold_for_pendant()
    print()

    run_steps(controller, [3, 4, 5])
    if result.executed and result.proposal.op == "set_payload":
        print(">>> Object released — setting the payload back to 0 kg.")
        controller.set_payload(0.0)
    run_steps(controller, [6])
    finish_task_message()


# --- Scene 5: Tier 3 — real critical fault while picking up --------------------

def scenario_tier3_critical_during_pickup(controller, service, preventive, run_log):
    run_steps(controller, [0])
    label, target = TASK_STEPS[1]
    print(label)
    controller.move_joints(target, a=MOVE_A, v=MOVE_V)

    threading.Thread(target=send_script_after,
                     args=(2.0, CRITICAL_FAULT_SCRIPT,
                           "triggering a real critical controller fault (the arm will power off)...")).start()

    print("\nMonitoring for the fault...\n")
    result = run_monitoring_window(service, run_log, max_seconds=60)
    if result is None:
        print(">>> [!] The fault never showed up — check the pendant.")
        return
    hold_for_pendant()

    print("\n>>> Tier 3: nothing is fixed automatically. A person has been alerted in Slack and must")
    print(">>> inspect the arm on site before it's restarted.")
    input(">>> [YOU, as the on-site person] Once the arm has been checked, press Enter to restart it... ")
    run_log.event("human_confirmed_restart", fault_type=result.fault.fault_type)

    preventive.pause()
    try:
        print(">>> Restarting safety and powering back on (a person's action, not automatic)...")
        restarted = controller.restart_safety()
    finally:
        preventive.resume()
    status = controller.get_status()
    print(f">>> Status after restart: {status}")
    run_log.event("restart_after_tier3", ok=restarted, **status)
    if not restarted:
        print(">>> [!] The arm didn't come back to RUNNING — check the pendant.")
        return

    print(">>> Restarted — resuming: redoing the pickup and finishing the job...\n")
    run_steps(controller, [1, 2, 3, 4, 5, 6])
    finish_task_message()


SCENES = [
    ("Baseline: one job with no faults", run_normal_cycle),
    ("Preventive check: early warning before a fault (wrist near its end stop)",
     scene_preventive_wrist_near_limit),
    ("Tier 1 — fault while carrying the object (real) — fixed automatically",
     scenario_tier1_fault_during_carry),
    ("Tier 2 — payload mismatch while placing the object (SIMULATED) — Aetherion agent + your Slack approval",
     scenario_tier2_payload_mismatch),
    ("Tier 2 — reject, retry with your feedback, approve (SIMULATED payload mismatch)",
     scenario_tier2_reject_then_retry),
    ("Tier 3 — critical fault while picking up the object (real) — halted, a person restarts it",
     scenario_tier3_critical_during_pickup),
]


def main():
    wanted = {int(a) for a in sys.argv[1:]}
    run_log = RunLog("demo")
    print(f"Logs for this run: {run_log.dir}\n")

    controller = DemoController()
    print("Powering on / releasing brakes...")
    controller.power_on()
    print("Status:", controller.get_status())

    service = Service(controller=controller, audit=AuditLog(copy_to=run_log.audit_path))

    on_warning, on_clear = make_alert_handlers(controller, run_log)
    preventive = PreventiveMonitor(interval_s=PREVENTIVE_INTERVAL,
                                   on_warning=on_warning, on_clear=on_clear).start()
    try:
        for i, (name, scene_fn) in enumerate(SCENES, start=1):
            if wanted and i not in wanted:
                continue
            print(f"\n{'=' * 70}")
            print(f"Scene {i}/{len(SCENES)}: {name}")
            print(f"{'=' * 70}\n")
            run_log.event("scene_start", scene=name)
            time.sleep(3)
            scene_fn(controller, service, preventive, run_log)
            run_log.event("scene_end", scene=name)
            time.sleep(3)
    finally:
        preventive.stop()

    final = controller.get_status()
    run_log.event("demo_complete", **final)
    print("\nAll scenes complete. Final status:", final)
    print(f"Logs for this run: {run_log.dir}")


if __name__ == "__main__":
    main()
