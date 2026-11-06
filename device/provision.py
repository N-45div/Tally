"""Set up a desk device over USB: the server it talks to, its token, and (if asked) the Wi-Fi network to join.

    py provision.py                       server and token
    py provision.py --wifi                and the Wi-Fi network too
    py provision.py --port COM8 --server https://<host>

The token comes from Tally's Devices page, is typed at a hidden prompt, goes only to the board (which keeps it in
its own storage), and is never printed. Needs pyserial (it comes with esptool and PlatformIO).
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import time

import serial
from serial.tools import list_ports

ESPRESSIF_USB = 0x303A


def find_port() -> str:
    ports = [p.device for p in list_ports.comports() if p.vid == ESPRESSIF_USB]
    if len(ports) != 1:
        sys.exit(f"found {len(ports)} boards on USB ({', '.join(ports) or 'none'}); say which with --port")
    return ports[0]


def send(port: serial.Serial, line: str, wait: float = 3.0, show: str | None = None) -> str:
    """Send one command and return the board's OK/ERR reply. `show` is what to print instead of the line."""
    port.reset_input_buffer()
    port.write((line + "\n").encode())
    until = time.monotonic() + wait
    while time.monotonic() < until:
        reply = port.readline().decode(errors="replace").strip()
        if reply.startswith(("OK", "ERR")):
            print(f"> {show or line}\n  {reply}")
            return reply
    sys.exit(f"no reply to: {show or line}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port")
    ap.add_argument("--server", default=f"https://{os.environ['TALLY_HOST']}" if os.environ.get("TALLY_HOST") else None)
    ap.add_argument("--wifi", action="store_true", help="also set the Wi-Fi network (2.4 GHz)")
    a = ap.parse_args()
    server = a.server or input("Tally's address (https://...): ").strip()
    token = os.environ.get("TALLY_DEVICE_TOKEN") or getpass.getpass("The device's token, from the Devices page (hidden): ").strip()
    with serial.Serial(a.port or find_port(), 115200, timeout=0.5) as port:
        time.sleep(0.5)
        if a.wifi:
            ssid = input("Wi-Fi name: ").strip()
            password = getpass.getpass("Wi-Fi password (hidden): ")
            if send(port, f"wifi {ssid}\t{password}", wait=30, show=f"wifi {ssid} <password>").startswith("ERR"):
                sys.exit("the board could not join that network")
        if send(port, f"server {server}").startswith("ERR"):
            sys.exit(1)
        if send(port, f"token {token}", show="token <hidden>").startswith("ERR"):
            sys.exit(1)
        time.sleep(4)  # let it call the server once
        send(port, "status", wait=20)  # it answers between calls, and a call can take a few seconds


if __name__ == "__main__":
    main()
