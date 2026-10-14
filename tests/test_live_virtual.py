"""Drive LiveTransport through the real can-isotp stack on python-can's virtual bus.

This exercises the CanStack thread, padding, filters and shutdown without
hardware.
"""
import time

import can
import pytest

from overrev.transport import LiveTransport, ReadOnlyViolation
from overrev.uds import UdsClient

CHANNEL = "overrev-virtual"






def sniff():
    bus = can.Bus(channel=CHANNEL, interface="virtual")
    reader = can.BufferedReader()
    notifier = can.Notifier(bus, [reader])
    return bus, reader, notifier


def drain(reader):
    frames = []
    while (m := reader.get_message(0)) is not None:
        frames.append(m)
    return frames


def test_live_transport_refusal_puts_nothing_on_the_wire():
    bus, reader, notifier = sniff()
    try:
        with LiveTransport(CHANNEL, interface="virtual") as transport:
            with pytest.raises(ReadOnlyViolation):
                transport.send(bytes.fromhex("2703"))
            time.sleep(0.05)
    finally:
        notifier.stop()
        bus.shutdown()
    assert drain(reader) == []


def test_live_transport_times_out_when_dme_is_silent():
    with LiveTransport(CHANNEL, interface="virtual") as transport:
        with pytest.raises(TimeoutError):
            UdsClient(transport, p2=0.2).start_session()


