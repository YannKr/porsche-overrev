import socket

import pytest

from overrev import bluetooth
from overrev.transport import ElmTransport, TransportError
from test_auto import car
from test_elm import FakeElm

MX = "00:11:22:33:44:55"


def test_parse_devices_reads_bluetoothctl_lines():
    text = ("Device 00:11:22:33:44:55 OBDLink MX+ 12345\n"
            "Device 00:11:22:33:44:66 [TV] Living Room (55)\n"
            "Agent registered\n")
    assert bluetooth.parse_devices(text) == [
        ("00:11:22:33:44:55", "OBDLink MX+ 12345"), ("00:11:22:33:44:66", "[TV] Living Room (55)")]


def test_socket_port_reads_what_the_adapter_sent():
    ours, adapter = socket.socketpair()
    port = bluetooth.SocketPort(ours)
    adapter.sendall(b"OK\r>")
    assert port.read(1) == b"O"
    port.write(b"ATZ\r")
    assert adapter.recv(16) == b"ATZ\r"


def test_socket_port_read_times_out_with_no_bytes():
    ours, adapter = socket.socketpair()
    assert bluetooth.SocketPort(ours, timeout=0.01).read(1) == b""


def test_socket_port_raises_when_the_adapter_hangs_up():
    ours, adapter = socket.socketpair()
    adapter.close()
    with pytest.raises(OSError):
        bluetooth.SocketPort(ours).read(1)


def fake_bluez(monkeypatch, paired=(), pairable=None):
    calls = []
    monkeypatch.setattr(bluetooth, "paired_obdlinks", lambda: list(paired))
    monkeypatch.setattr(bluetooth, "pair", lambda say: calls.append("pair") or pairable)
    monkeypatch.setattr(bluetooth, "connect", lambda mac: calls.append(mac) or object())
    monkeypatch.setattr(bluetooth, "ElmTransport", lambda port: ElmTransport(FakeElm(car())))
    return calls


def test_uses_an_already_paired_obdlink(monkeypatch):
    calls = fake_bluez(monkeypatch, paired=[MX])
    bluetooth.open_adapter(platform="linux", say=lambda msg: None)
    assert calls == [MX]


def test_pairs_when_no_obdlink_is_paired(monkeypatch):
    calls = fake_bluez(monkeypatch, pairable=MX)
    bluetooth.open_adapter(platform="linux", say=lambda msg: None)
    assert calls == ["pair", MX]


def test_explicit_mac_skips_discovery(monkeypatch):
    calls = fake_bluez(monkeypatch, paired=["00:11:22:33:44:66"])
    bluetooth.open_adapter(MX, platform="linux", say=lambda msg: None)
    assert calls == [MX]


def test_unreachable_adapter_says_what_to_check(monkeypatch):
    fake_bluez(monkeypatch, paired=[MX])

    def down(mac):
        raise OSError(112, "Host is down")

    monkeypatch.setattr(bluetooth, "connect", down)
    with pytest.raises(TransportError, match="ignition"):
        bluetooth.open_adapter(platform="linux", say=lambda msg: None)


def test_macos_uses_the_paired_serial_port(monkeypatch):
    monkeypatch.setattr(bluetooth.glob, "glob", lambda pattern: ["/dev/cu.OBDLinkMXp-STN-SPP"])
    opened = []
    monkeypatch.setattr(bluetooth, "open_elm", lambda path: opened.append(path))
    bluetooth.open_adapter(platform="darwin", say=lambda msg: None)
    assert opened == ["/dev/cu.OBDLinkMXp-STN-SPP"]


def test_macos_without_a_paired_obdlink_says_how_to_pair(monkeypatch):
    monkeypatch.setattr(bluetooth.glob, "glob", lambda pattern: [])
    with pytest.raises(TransportError, match="Bluetooth"):
        bluetooth.open_adapter(platform="darwin", say=lambda msg: None)
