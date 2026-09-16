"""
Live, watchable demo of the Tier 1 pipeline — framed as a real pick-and-place
task instead of arbitrary joint moves, so the fault and recovery mean something.

Open http://localhost:6080/vnc.html in a browser BEFORE running this, so you
can watch the arm on the pendant's 3D view while it runs.

The task: pick up a part at Station A, carry it to Station B, place it down.

What happens, in order:
  1. Powers on, moves to Station A, and picks up the part.
  2. Starts carrying the part toward Station B — and partway through that
     carry, fires a real overspeed fault on purpose (the "something goes
     wrong mid-task" part).
  3. service.py's monitor loop is polling the whole time — it detects the
     fault, Tier 1 kicks in, the Guard checks it, and clear_protective_stop()
     is executed automatically. No human step.
  4. The instant that recovery executes, the arm RESUMES the interrupted
     task — finishes carrying the part to Station B and places it down.
     That's the actual proof it's working again, not just a status flag.
  5. Prints the final status once the task is complete and the arm is back
     at NORMAL.

Run with: python3 demo.py
"""
import socket
import threading
import time

from service import Service
from ursim_controller import URSimController

HOME = [0, -1.57, 0, -1.57, 0, 0]
ABOVE_STATION_A = [-0.5, -1.2, 1.3, -1.6, -1.5, 0]
AT_STATION_A = [-0.5, -1.0, 1.5, -2.0, -1.5, 0]
ABOVE_STATION_B = [0.9, -1.2, 1.3, -1.6, -1.5, 0]
AT_STATION_B = [0.9, -1.0, 1.5, -2.0, -1.5, 0]


def trigger_fault_after(delay):
    time.sleep(delay)
    print("\n>>> Something goes wrong mid-carry — triggering a real overspeed fault...\n")
    script = """
def unsafe_speed():
  speedj([10, 10, 10, 10, 10, 10], a=40, t=3)
end
unsafe_speed()
"""
    with socket.create_connection(("127.0.0.1", 30002), timeout=5) as s:
        s.sendall(script.encode())


def resume_task(controller):
    """Proves the arm is actually usable again after a Tier 1 fix by finishing
    the job it was interrupted mid-way through, not just moving for the sake of it."""
    time.sleep(1)
    print(">>> Recovered — resuming the task: carrying the part the rest of the way to Station B...\n")
    controller.move_joints(ABOVE_STATION_B, a=0.3, v=0.2)
    time.sleep(2.5)

    print(">>> Placing the part down at Station B...\n")
    controller.move_joints(AT_STATION_B, a=0.3, v=0.2)
    time.sleep(2)

    print(">>> Part placed. Retreating and returning home...\n")
    controller.move_joints(ABOVE_STATION_B, a=0.3, v=0.2)
    time.sleep(1.5)
    controller.move_joints(HOME, a=0.3, v=0.2)
    print(">>> Task complete — pick-and-place finished despite the mid-task fault.\n")


def make_report(controller):
    def report(result):
        print(f">>> CYCLE: fault={result.fault.fault_type}  tier={result.tier}  "
              f"proposed_op={result.proposal.op}  decision={result.decision}  "
              f"executed={result.executed}")
        if result.decision == "APPROVED" and result.executed:
            threading.Thread(target=resume_task, args=(controller,)).start()
    return report


def main():
    controller = URSimController()

    print("Powering on / releasing brakes...")
    controller.power_on()
    print("Status:", controller.get_status())

    print("\nMoving to Station A to pick up the part — watch the pendant now.")
    controller.move_joints(ABOVE_STATION_A, a=0.3, v=0.2)
    time.sleep(2.5)

    print("Picking up the part at Station A...")
    controller.move_joints(AT_STATION_A, a=0.3, v=0.2)
    time.sleep(2)

    print("Lifting the part off Station A...")
    controller.move_joints(ABOVE_STATION_A, a=0.3, v=0.2)
    time.sleep(2)

    print("Carrying the part toward Station B...")
    controller.move_joints(ABOVE_STATION_B, a=0.3, v=0.2)

    threading.Thread(target=trigger_fault_after, args=(1.5,)).start()

    service = Service(controller=controller)
    print("\nMonitoring for faults for ~20 seconds...\n")
    service.run(cycles=100, on_cycle=make_report(controller))

    print("\nFinal status:", controller.get_status())


if __name__ == "__main__":
    main()
