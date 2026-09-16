import socket
import time

HOST = "127.0.0.1"
SECONDARY_PORT = 30002

script = """
def unsafe_move():
  movej([6.98, -1.57, 1.57, -1.57, -1.57, 0], a=0.5, v=0.3)
end
unsafe_move()
"""

with socket.create_connection((HOST, SECONDARY_PORT), timeout=5) as s:
    s.sendall(script.encode())
    print("Sent out-of-range move — check the pendant's Log tab now")
    time.sleep(3)