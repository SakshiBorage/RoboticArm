"""
Live, watchable demo of the Tier 1 pipeline.

Open http://localhost:6080/vnc.html in a browser BEFORE running this, so you
can watch the arm on the pendant's 3D view while it runs.

What happens, in order:
  1. Powers on and sends a visible move.
  2. Sends a second move, and partway through it, fires a real overspeed
     fault on purpose (this is the "something goes wrong mid-motion" part).
  3. service.py's monitor loop is polling the whole time — it detects the
     fault, Tier 1 kicks in, the Guard checks it, and clear_protective_stop()
     is executed automatically. No human step.
  4. The instant that recovery executes, sends a clearly visible
     "post-recovery" move — proof the arm is actually back to work, not
     just that a status flag flipped to NORMAL.
  5. Prints the final status once it's back to NORMAL.

Run with: python3 demo.py
"""
import socket
import threading
import time

from service import Service
from ursim_controller import URSimController


def trigger_fault_after(delay):
    time.sleep(delay)
    print(f"\n>>> Triggering a real overspeed fault mid-motion...\n")
    script = """
def unsafe_speed():
  speedj([10, 10, 10, 10, 10, 10], a=40, t=3)
end
unsafe_speed()
"""
    with socket.create_connection(("127.0.0.1", 30002), timeout=5) as s:
        s.sendall(script.encode())


def confirm_recovery(controller):
    """Proves the arm is actually usable again after a Tier 1 fix — not just that
    safetystatus says NORMAL, but that it will accept and run a real move again."""
    time.sleep(1)
    print(">>> Recovery executed — confirming the arm actually works: sending a move...\n")
    controller.move_joints([0, -1.2, 1.2, -1.5, -1.5, 0], a=0.3, v=0.2)
    time.sleep(2)
    controller.move_joints([0.5, -0.9, 0.9, -1.6, -1.0, 0.3], a=0.3, v=0.2)
    print(">>> Post-recovery moves sent — watch the pendant, the arm is working normally again.\n")


def make_report(controller):
    def report(result):
        print(f">>> CYCLE: fault={result.fault.fault_type}  tier={result.tier}  "
              f"proposed_op={result.proposal.op}  decision={result.decision}  "
              f"executed={result.executed}")
        if result.decision == "APPROVED" and result.executed:
            threading.Thread(target=confirm_recovery, args=(controller,)).start()
    return report


def main():
    controller = URSimController()

    print("Powering on / releasing brakes...")
    controller.power_on()
    print("Status:", controller.get_status())

    print("\nSending a visible move — watch the pendant now.")
    controller.move_joints([0, -1.57, 0, -1.57, 0, 0], a=0.3, v=0.2)
    time.sleep(2.5)

    print("Sending a second move...")
    controller.move_joints([1.57, -0.8, 1.4, -1.9, -1.2, 0.8], a=0.3, v=0.2)

    threading.Thread(target=trigger_fault_after, args=(1.5,)).start()

    service = Service(controller=controller)
    print("\nMonitoring for faults for ~20 seconds...\n")
    service.run(cycles=100, on_cycle=make_report(controller))

    print("\nFinal status:", controller.get_status())


if __name__ == "__main__":
    main()
