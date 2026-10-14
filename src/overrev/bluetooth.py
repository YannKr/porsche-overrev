"""Find, pair and connect an OBDLink over Bluetooth, so `overrev` needs no setup.

Linux (BlueZ): use a paired OBDLink, or pair one in pairing mode, then talk RFCOMM
directly (no root, no `rfcomm bind`). macOS: the serial port the system creates
for a paired OBDLink (untested).
"""
from __future__ import annotations

import glob
import shutil
import socket
import subprocess
import sys
import time

from .transport import ElmTransport, TransportError, open_elm

NAME = "OBDLink"
SPP_CHANNEL = 1  # the OBDLink MX+ serial port profile
PAIR_WAIT = 120  # the MX+ pairing window is 2 minutes


def parse_devices(text: str) -> list[tuple[str, str]]:
    """'Device <MAC> <name>' lines from bluetoothctl, as (mac, name)."""
    devices = []
    for line in text.splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) == 3 and parts[0] == "Device":
            devices.append((parts[1], parts[2]))
    return devices


def _ctl(*args: str, timeout: float = 30) -> str:
    if not shutil.which("bluetoothctl"):
        raise TransportError("bluetoothctl not found: install BlueZ (sudo apt install bluez)")
    return subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout).stdout


def _is_paired(mac: str) -> bool:
    return "Paired: yes" in _ctl("info", mac)


def paired_obdlinks() -> list[str]:
    return [mac for mac, name in parse_devices(_ctl("devices")) if name.startswith(NAME) and _is_paired(mac)]


def pair(say) -> str:
    say("No paired OBDLink found. Plug it in, turn the ignition on and press its Connect button "
        "(the BT LED blinks fast for 2 minutes).")
    _ctl("pairable", "on")
    deadline = time.monotonic() + PAIR_WAIT
    while time.monotonic() < deadline:
        _ctl("--timeout", "10", "scan", "on", timeout=20)
        for mac, name in parse_devices(_ctl("devices")):
            if name.startswith(NAME):
                _ctl("pair", mac, timeout=40)
                if _is_paired(mac):
                    _ctl("trust", mac)
                    say(f"Paired {name} ({mac}).")
                    return mac
    raise TransportError("no OBDLink in pairing mode found within 2 minutes")


class SocketPort:
    """An RFCOMM socket with the three pyserial calls ElmTransport uses."""

    def __init__(self, sock: socket.socket, timeout: float = 0.1):
        self._sock = sock
        sock.settimeout(timeout)

    def write(self, data: bytes) -> None:
        self._sock.sendall(data)

    def read(self, size: int = 1) -> bytes:
        try:
            data = self._sock.recv(size)
        except socket.timeout:
            return b""
        if not data:
            raise OSError("the adapter closed the connection")
        return data

    def close(self) -> None:
        self._sock.close()


def connect(mac: str, tries: int = 3, gap: float = 10.0) -> SocketPort:
    """The MX+ often refuses a connection within a few seconds of the last one, so retry."""
    for attempt in range(1, tries + 1):
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        try:
            sock.settimeout(15)
            sock.connect((mac, SPP_CHANNEL))
            return SocketPort(sock)
        except OSError:
            sock.close()
            if attempt == tries:
                raise
            time.sleep(gap)


def open_adapter(mac: str | None = None, say=print, platform: str = sys.platform) -> ElmTransport:
    if platform == "darwin":
        ports = sorted(glob.glob("/dev/cu.OBDLink*"))
        if not ports:
            raise TransportError("no OBDLink serial port: pair the adapter in System Settings > Bluetooth first")
        return open_elm(ports[0])
    mac = mac or next(iter(paired_obdlinks()), None) or pair(say)
    say(f"Connecting to the OBDLink {mac} ...")
    try:
        port = connect(mac)
    except OSError as e:
        raise TransportError(f"cannot reach the OBDLink {mac} ({e}). Is it plugged in with the ignition on, "
                             "and is no phone connected to it?") from None
    return ElmTransport(port)
