"""
Powers on the URSim robot via the Dashboard Server (port 29999) — plain text
protocol, no libraries needed beyond Python's built-in socket.
Run this once after starting the URSim container, before sending any move command.
"""
import socket
import time

HOST = "127.0.0.1"
DASHBOARD_PORT = 29999


def send(sock, cmd):
    sock.sendall((cmd + "\n").encode())
    time.sleep(0.3)
    reply = sock.recv(4096).decode().strip()
    print(f"> {cmd}\n< {reply}")
    return reply


with socket.create_connection((HOST, DASHBOARD_PORT), timeout=5) as s:
    # The dashboard sends a welcome banner on connect — read and discard it.
    print(s.recv(4096).decode().strip())

    send(s, "power on")
    time.sleep(2)  # powering on takes a moment
    send(s, "brake release")
    time.sleep(5)  # brake release takes a few seconds on a fresh sim
    send(s, "robotmode")  # should now say RUNNING