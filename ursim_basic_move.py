import socket
import math
import time

HOST = "127.0.0.1"
SECONDARY_PORT = 30002

target_angles_deg = [10, -80, 90, -90, -90, 0]
target_angles_rad = [math.radians(a) for a in target_angles_deg]

script = f"""
def basic_move():
  movej({target_angles_rad}, a=0.5, v=0.3)
end
basic_move()
"""

with socket.create_connection((HOST, SECONDARY_PORT), timeout=5) as s:
    s.sendall(script.encode())
    print("Sent move command:")
    print(script)
    time.sleep(3)