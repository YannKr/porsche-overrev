"""ElmTransport against a fake OBDLink that speaks the ELM327 text protocol.

The fake honours echo, spaces, headers and linefeeds the way the adapter
does, so a missing setup command breaks parsing here as it would in the car.
It also refuses input while a command is still running (before its '>'
prompt was read), which a real adapter would answer with STOPPED.
"""
import pytest

from overrev.cli import main
from overrev.transport import ElmTransport, ReadOnlyViolation, TransportError
from overrev.uds import UdsClient


class FakeElm:
    """Serial-port stand-in: ``write``/``read``/``close`` like pyserial."""

    def __init__(self, respond, at_replies=None, swallow=0):
        self.respond = respond
        self.swallow = swallow  # commands lost before the Bluetooth link is up  # bytes request -> list of reply lines (hex or adapter text)
        self.at_replies = at_replies or {}
        self.echo, self.spaces, self.headers, self.linefeeds = True, True, False, True
        self.protocol = self.header = self.rx_filter = None
        self.commands, self.requests = [], []
        self.out = bytearray()
        self.held = []  # lines released later with release()
        self._line = bytearray()
        self.closed = False

    # pyserial surface
    def write(self, data):
        assert b">" not in self.out and not self.held, "write while the previous command is still running"
        for b in data:
            if b == 0x0D:
                self._command(self._line.decode())
                self._line.clear()
            else:
                self._line.append(b)
        return len(data)

    def read(self, size=1):
        chunk = bytes(self.out[:size])
        del self.out[:size]
        return chunk

    def close(self):
        self.closed = True

    # adapter behaviour
    def _eol(self):
        return "\r\n" if self.linefeeds else "\r"

    def _emit(self, lines, prompt=True):
        text = "".join(line + self._eol() for line in lines)
        self.out += (text + (self._eol() + ">" if prompt else "")).encode()

    def _command(self, cmd):
        if self.swallow:
            self.swallow -= 1
            return
        self.commands.append(cmd)
        if self.echo:
            self.out += (cmd + self._eol()).encode()
        c = cmd.replace(" ", "").upper()
        if c.startswith("AT"):
            self._emit([self._at(c[2:])])
            return
        request = bytes.fromhex(c)
        self.requests.append(request)
        lines = self.respond(request)
        if lines and lines[-1] == "HOLD":
            self._emit([self._format(line) for line in lines[:-2]], prompt=False)
            self.held = [lines[-2]]
            return
        self._emit([self._format(line) for line in lines])

    def release(self):
        lines, self.held = self.held, []
        self._emit([self._format(line) for line in lines])

    def _format(self, line):
        try:
            data = bytes.fromhex(line)
        except ValueError:
            return line  # adapter text such as NO DATA
        if self.headers:
            data = bytes((0x07, 0xE8, len(data))) + data
        text = data.hex().upper()
        return " ".join(text[i:i + 2] for i in range(0, len(text), 2)) if self.spaces else text

    def _at(self, c):
        if c in self.at_replies:
            return self.at_replies[c]
        if c == "Z":
            self.echo, self.spaces, self.headers, self.linefeeds = True, True, False, True
            return "ELM327 v1.4b"
        flags = {"E0": "echo", "E1": "echo", "S0": "spaces", "S1": "spaces",
                 "H0": "headers", "H1": "headers", "L0": "linefeeds", "L1": "linefeeds"}
        if c in flags:
            setattr(self, flags[c], c.endswith("1"))
        elif c.startswith("SP"):
            self.protocol = c[2:]
        elif c.startswith("SH"):
            self.header = c[2:]
        elif c.startswith("CRA"):
            self.rx_filter = c[3:]
        elif not c.startswith("ST"):
            return "?"
        return "OK"


def responder(table):
    return lambda request: table[request.hex()]


def h(s):
    return bytes.fromhex(s)


SESSION = {"1003": ["5003003201F4"]}


def test_setup_pins_protocol_and_dme_addresses_without_bus_traffic():
    fake = FakeElm(responder(SESSION))
    ElmTransport(fake)
    assert fake.protocol == "6"  # ISO 15765-4, 11-bit, 500 kbps; never auto-search
    assert (fake.header, fake.rx_filter) == ("7E0", "7E8")
    assert fake.requests == []


def test_read_did_returns_value_bytes():
    fake = FakeElm(responder({**SESSION, "221b68": ["621B680B34"]}))
    client = UdsClient(ElmTransport(fake))
    client.start_session()
    assert client.read_did(0x1B68) == h("0b34")
    assert fake.requests == [h("1003"), h("221b68")]


def test_pending_line_reaches_client_before_the_final_answer():
    fake = FakeElm(responder({"221741": ["7F2278", "6217411426", "HOLD"]}))
    t = ElmTransport(fake)
    t.send(h("221741"))
    assert t.recv(0.5) == h("7f2278")
    fake.release()
    assert t.recv(0.5) == h("6217411426")


def test_client_waits_through_pending():
    fake = FakeElm(responder({"221741": ["7F2278", "6217411426"]}))
    assert UdsClient(ElmTransport(fake)).read_did(0x1741) == h("1426")


def test_no_data_is_a_timeout_and_the_next_request_still_works():
    fake = FakeElm(responder({"221b68": ["NO DATA"], "221b69": ["621B69046F"]}))
    client = UdsClient(ElmTransport(fake))
    with pytest.raises(TimeoutError):
        client.read_did(0x1B68)
    assert client.read_did(0x1B69) == h("046f")


def test_adapter_error_line_is_a_transport_error():
    fake = FakeElm(responder({"221b68": ["CAN ERROR"]}))
    t = ElmTransport(fake)
    t.send(h("221b68"))
    with pytest.raises(TransportError, match="CAN ERROR"):
        t.recv(0.5)


# Made-up VIN: the real one stays out of git.
VIN = "WP0ZZZ99ZKS100001"
VIN_FRAMES = ["014", "0: 62 F1 90 57 50 30", "1: 5A 5A 5A 39 39 5A 4B", "2: 53 31 30 30 30 30 31"]


def test_multi_frame_answer_is_reassembled():
    fake = FakeElm(responder({"22f190": VIN_FRAMES}))
    assert UdsClient(ElmTransport(fake)).read_did(0xF190) == VIN.encode()


def test_multi_frame_answer_without_spaces_and_with_padding():
    fake = FakeElm(responder({"22f187": ["00E", "0:62F187393931", "1:36313836393231", "2:37AAAAAAAAAAAA"]}))
    t = ElmTransport(fake)
    t.send(h("22f187"))
    assert t.recv(0.5) == b"\x62\xf1\x8799161869217"


def test_multi_frame_answer_with_a_missing_frame_is_refused():
    fake = FakeElm(responder({"22f190": [VIN_FRAMES[0], VIN_FRAMES[1], VIN_FRAMES[3]]}))
    t = ElmTransport(fake)
    t.send(h("22f190"))
    with pytest.raises(TransportError):
        t.recv(0.5)


def test_multi_frame_answer_cut_short_is_refused():
    fake = FakeElm(responder({"22f190": VIN_FRAMES[:3]}))
    t = ElmTransport(fake)
    t.send(h("22f190"))
    with pytest.raises(TransportError):
        t.recv(0.5)


def test_prompt_without_an_answer_is_a_timeout():
    fake = FakeElm(responder({"221b68": []}))
    t = ElmTransport(fake)
    t.send(h("221b68"))
    with pytest.raises(TimeoutError):
        t.recv(0.5)


def test_silent_adapter_times_out():
    fake = FakeElm(responder({"221b68": ["6217411426", "HOLD"]}))
    t = ElmTransport(fake)
    t.send(h("221b68"))
    with pytest.raises(TimeoutError):
        t.recv(0.05)


def test_rejected_setup_command_raises():
    fake = FakeElm(responder({}), at_replies={"SP6": "?"})
    with pytest.raises(TransportError, match="ATSP6"):
        ElmTransport(fake)


def test_allowlist_blocks_before_anything_reaches_the_adapter():
    fake = FakeElm(responder({}))
    t = ElmTransport(fake)
    sent = len(fake.commands)
    with pytest.raises(ReadOnlyViolation):
        t.send(h("2703"))
    assert len(fake.commands) == sent


def test_setup_retries_a_reset_lost_while_the_link_comes_up(monkeypatch):
    monkeypatch.setattr(ElmTransport, "RESET_TIMEOUT", 0.05)
    fake = FakeElm(responder({"221b68": ["621B680B34"]}), swallow=1)
    assert UdsClient(ElmTransport(fake)).read_did(0x1B68) == h("0b34")


def test_failed_setup_closes_the_port(monkeypatch):
    monkeypatch.setattr(ElmTransport, "RESET_TIMEOUT", 0.05)
    fake = FakeElm(responder({}), swallow=99)
    with pytest.raises((TimeoutError, TransportError)):
        ElmTransport(fake)
    assert fake.closed


def test_adapter_traffic_is_logged(caplog):
    caplog.set_level("DEBUG", logger="overrev.transport")
    fake = FakeElm(responder({"221b68": ["621B680B34"]}))
    UdsClient(ElmTransport(fake)).read_did(0x1B68)
    assert "tx 221B68" in caplog.messages
    assert "rx 62 1B 68 0B 34" in caplog.messages


def test_close_closes_the_port():
    fake = FakeElm(responder({}))
    ElmTransport(fake).close()
    assert fake.closed






def test_cli_reports_a_missing_adapter_port_cleanly(capsys):
    assert main(["read", "--elm", "/nonexistent/rfcomm0"]) == 1
    assert capsys.readouterr().err.startswith("error:")
