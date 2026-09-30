import socket
import time

HOST = "127.0.0.1"
DASHBOARD_PORT = 29999

with socket.create_connection((HOST, DASHBOARD_PORT), timeout=5) as s:
    s.recv(4096)  # discard welcome banner
    s.sendall(b"safetystatus\n")
    time.sleep(0.3)
    print("Safety status:", s.recv(4096).decode().strip())
    s.sendall(b"robotmode\n")
    time.sleep(0.3)
    print("Robot mode:", s.recv(4096).decode().strip())