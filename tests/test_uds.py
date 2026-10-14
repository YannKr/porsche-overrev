import pytest

from overrev.uds import (
    FINAL, OTHER, PENDING, NegativeResponse, UdsClient, classify,
)


class ScriptedTransport:
    """Hands back a fixed list of responses; records what was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def send(self, payload):
        self.sent.append(bytes(payload))

    def recv(self, timeout):
        if not self.responses:
            raise TimeoutError("scripted transport exhausted")
        return self.responses.pop(0)


def h(s):
    return bytes.fromhex(s)


@pytest.mark.parametrize("req, response, kind", [
    (h("221741"), h("6217410d7d"), FINAL),
    (h("221741"), h("7f2278"), PENDING),
    (h("221741"), h("7f2231"), FINAL),
    (h("221741"), h("7e00"), OTHER),          # late tester-present answer
    (h("221741"), h("6217430a63"), OTHER),    # answer for a different DID
    (h("1003"), h("5003003201f4"), FINAL),
    (h("3e00"), h("7e00"), FINAL),
    (h("3e00"), h("7f3e78"), PENDING),
    (h("3e00"), h("7e"), OTHER),
    (h("3e00"), h("7e01"), OTHER),
])
def test_classify(req, response, kind):
    assert classify(req, response) == kind


def test_read_did_returns_data_after_echo():
    t = ScriptedTransport([h("6217410d7d")])
    assert UdsClient(t).read_did(0x1741) == h("0d7d")
    assert t.sent == [h("221741")]


def test_read_did_waits_through_response_pending():
    t = ScriptedTransport([h("7f2278"), h("7f2278"), h("6217430a63")])
    assert UdsClient(t).read_did(0x1743) == h("0a63")


def test_read_did_skips_unrelated_late_response():
    t = ScriptedTransport([h("7e00"), h("6217410d7d")])
    assert UdsClient(t).read_did(0x1741) == h("0d7d")


def test_read_did_raises_negative_response_with_nrc():
    t = ScriptedTransport([h("7f2233")])
    with pytest.raises(NegativeResponse) as ei:
        UdsClient(t).read_did(0x1741)
    assert ei.value.nrc == 0x33
    assert "securityAccessDenied" in str(ei.value)


def test_read_did_times_out_when_ecu_silent():
    t = ScriptedTransport([])
    with pytest.raises(TimeoutError):
        UdsClient(t).read_did(0x1741)


def test_start_session_sends_extended_session():
    t = ScriptedTransport([h("5003003201f4")])
    UdsClient(t).start_session()
    assert t.sent == [h("1003")]


def test_tester_present_sends_3e00():
    t = ScriptedTransport([h("7e00")])
    UdsClient(t).tester_present()
    assert t.sent == [h("3e00")]


def test_read_did_sends_tester_present_after_idle():
    now = [0.0]
    t = ScriptedTransport([h("6217410d7d"), h("7e00"), h("6217430a63")])
    c = UdsClient(t, keepalive_idle=1.5, clock=lambda: now[0])
    c.read_did(0x1741)
    now[0] = 2.0
    c.read_did(0x1743)
    assert t.sent == [h("221741"), h("3e00"), h("221743")]



class TimedTransport:
    """Responses tagged with arrival times on a fake clock that recv() advances."""

    def __init__(self, clock, arrivals):
        self.clock = clock
        self.arrivals = list(arrivals)

    def send(self, payload):
        pass

    def recv(self, timeout):
        now = self.clock[0]
        if not self.arrivals or self.arrivals[0][0] > now + timeout:
            self.clock[0] = now + timeout
            raise TimeoutError
        at, payload = self.arrivals.pop(0)
        self.clock[0] = max(now, at)
        return payload


def timed_client(arrivals):
    clock = [0.0]
    return UdsClient(TimedTransport(clock, arrivals), clock=lambda: clock[0])


def test_pending_extends_the_deadline_to_p2_star():
    c = timed_client([(0.9, h("7f2278")), (4.0, h("6217410d7d"))])
    assert c.read_did(0x1741) == h("0d7d")


def test_final_answer_after_p2_without_pending_times_out():
    c = timed_client([(1.1, h("6217410d7d"))])
    with pytest.raises(TimeoutError):
        c.read_did(0x1741)


def test_every_pending_restarts_p2_star_with_margin_over_the_dme_5s():
    # The DME advertises P2* = 5000 ms and has been seen to re-send 7F 22 78 after 4.98 s.
    c = timed_client([(0.5, h("7f2278")), (5.52, h("7f2278")), (5.6, h("6217410d7d"))])
    assert c.read_did(0x1741) == h("0d7d")
