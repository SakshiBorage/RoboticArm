import socket
import time

HOST = "127.0.0.1"
SECONDARY_PORT = 30002

script = """
def unsafe_move():
  movej([0, -1.57, 1.57, -1.57, -1.57, 0], a=500, v=500)
end
unsafe_move()
"""

with socket.create_connection((HOST, SECONDARY_PORT), timeout=5) as s:
    s.sendall(script.encode())
    print("Sent move with invalid acceleration/velocity")
    time.sleep(3)