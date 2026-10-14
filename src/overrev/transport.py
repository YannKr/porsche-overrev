"""Transports: read-only guard, live CAN (python-can + can-isotp), OBDLink/ELM327, and CAN log replay."""
from __future__ import annotations

import logging
import time
from collections import deque

from .capture import ECU_ID, TESTER_ID, pair_exchanges, parse_log, reassemble
from .uds import FINAL, classify


log = logging.getLogger(__name__)


class ReadOnlyViolation(Exception):
    pass


class TransportError(Exception):
    pass


class ReplayError(TransportError):
    pass


_ALLOWED_SUBFUNCTIONS = {0x10: (0x01, 0x03), 0x3E: (0x00,)}


def check_read_only(payload: bytes) -> None:
    """Permit only 10 01/03, 22 <one DID>, 3E 00, 19 02 <mask>. Raise on anything else."""
    sid = payload[0] if payload else None
    if sid == 0x22 and len(payload) == 3:
        return
    if sid == 0x19 and len(payload) == 3 and payload[1] == 0x02:  # read stored fault codes; clearing (14) stays blocked
        return
    if sid in _ALLOWED_SUBFUNCTIONS and len(payload) == 2 and payload[1] in _ALLOWED_SUBFUNCTIONS[sid]:
        return
    raise ReadOnlyViolation(
        f"refusing to transmit {payload.hex() or '<empty>'}: only 10 01/03, 22 <DID>, 3E 00 and 19 02 <mask> are permitted"
    )


class Transport:
    def send(self, payload) -> None:
        payload = bytes(payload)
        check_read_only(payload)
        self._send(payload)

    def _send(self, payload: bytes) -> None:
        raise NotImplementedError

    def recv(self, timeout: float) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class LiveTransport(Transport):
    """0x7E0 -> 0x7E8 over a python-can bus, ISO-TP handled by can-isotp's CanStack."""

    def __init__(self, channel: str, interface: str = "socketcan"):
        import can
        import isotp

        kwargs = {} if interface == "socketcan" else {"bitrate": 500_000}
        try:
            self._bus = can.Bus(
                channel=channel, interface=interface,
                can_filters=[{"can_id": ECU_ID, "can_mask": 0x7FF, "extended": False}],
                **kwargs,
            )
        except (can.CanError, OSError, ValueError) as e:
            raise TransportError(f"cannot open CAN interface {interface}:{channel}: {e}") from e
        address = isotp.Address(isotp.AddressingMode.Normal_11bits, txid=TESTER_ID, rxid=ECU_ID)
        self._stack = isotp.CanStack(self._bus, address=address, params={
            "stmin": 5,  # conservative STmin; the DME accepts it
            "blocksize": 0,
            "tx_padding": 0xAA,
            "rx_flowcontrol_timeout": 1000,
            "rx_consecutive_frame_timeout": 1000,
        })
        self._stack.start()

    def _send(self, payload: bytes) -> None:
        while self._stack.recv(block=False) is not None:
            pass  # a stale answer (a late 7F 22 xx carries no DID echo) must not be taken for ours
        self._stack.send(payload)

    def recv(self, timeout: float) -> bytes:
        data = self._stack.recv(block=True, timeout=timeout)
        if data is None:
            raise TimeoutError("no ISO-TP message from DME")
        return bytes(data)

    def close(self) -> None:
        self._stack.stop()
        self._bus.shutdown()


class ElmTransport(Transport):
    """OBDLink (STN, ELM327 command set) on a serial port, e.g. an MX+ bound to /dev/rfcomm0.

    The adapter does ISO-TP itself: a request goes out as one line of hex,
    each answer comes back as one line, then the '>' prompt. Answers are
    handed up line by line so a 7F xx 78 reaches UdsClient before the final
    answer (the STN keeps listening after a pending by default, PP 2A bit 2).
    A multi-frame answer (VIN, part numbers) arrives as its length, then one
    "<index>:" line per CAN frame.
    """

    # None of these transmit on the bus. ATSP6 pins ISO 15765-4 11-bit 500 kbps:
    # protocol auto-search would broadcast OBD 01 00 on 7DF. ATST FF waits ~1 s
    # for the first frame of an answer.
    SETUP = ("ATE0", "ATH0", "ATSP6", "ATSTFF", f"ATSH{TESTER_ID:03X}", f"ATCRA{ECU_ID:03X}")
    COMMAND_TIMEOUT = 3.0
    RESET_TIMEOUT = 2.5  # the MX+ answers ATZ in ~1.8 s
    RESET_TRIES = 3  # a command sent while the Bluetooth link is still coming up is lost

    def __init__(self, port):
        self._port = port
        self._buf = bytearray()
        self._busy = False
        try:
            self._reset()
            for cmd in self.SETUP:
                lines = self._command(cmd, self.COMMAND_TIMEOUT)
                if lines[-1:] != ["OK"]:
                    raise TransportError(f"adapter rejected {cmd}: {' / '.join(lines) or 'no reply'}")
        except BaseException:
            port.close()
            raise

    def _reset(self) -> None:
        for attempt in range(1, self.RESET_TRIES + 1):
            try:
                self._command("ATZ", self.RESET_TIMEOUT)
                return
            except TimeoutError:
                if attempt == self.RESET_TRIES:
                    raise

    def _command(self, cmd: str, timeout: float) -> list[str]:
        self._write(cmd)
        deadline = time.monotonic() + timeout
        lines = []
        while (line := self._read_line(deadline)) is not None:
            lines.append(line)
        return lines

    def _write(self, text: str) -> None:
        log.debug("tx %s", text)
        self._port.write(text.encode("ascii") + b"\r")
        self._busy = True

    def _read_line(self, deadline: float):
        """Next non-empty line from the adapter, or None once the '>' prompt is read."""
        while True:
            while self._buf[:1] in (b"\r", b"\n", b"\0"):
                del self._buf[:1]
            if self._buf[:1] == b">":
                del self._buf[:1]
                self._busy = False
                return None
            end = self._buf.find(b"\r")
            if end >= 0:
                line = self._buf[:end].replace(b"\0", b"").replace(b"\n", b"").strip()
                del self._buf[:end + 1]
                if line:
                    line = line.decode("ascii", "replace")
                    log.debug("rx %s", line)
                    return line
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("no answer from the adapter")
            self._buf += self._port.read(1)

    def _send(self, payload: bytes) -> None:
        deadline = time.monotonic() + self.COMMAND_TIMEOUT
        try:
            while self._busy and self._read_line(deadline) is not None:
                pass  # the rest of the previous answer; the adapter takes no input until '>'
        except TimeoutError:
            raise TransportError("adapter never returned to its prompt") from None
        self._write(payload.hex().upper())

    def recv(self, timeout: float) -> bytes:
        if not self._busy:
            raise TimeoutError("adapter finished without (another) answer")
        deadline = time.monotonic() + timeout
        line = self._read_line(deadline)
        if line is None or line == "NO DATA":
            raise TimeoutError(f"adapter: {line or 'no answer'}")
        try:
            if len(line) == 3:  # multi-frame: ISO-TP length, then "0:", "1:", ... lines
                return self._read_frames(int(line, 16), deadline)
            return bytes.fromhex(line)  # tolerates the adapter's byte spacing
        except ValueError:
            raise TransportError(f"adapter: {line}") from None

    def _read_frames(self, length: int, deadline: float) -> bytes:
        data = bytearray()
        index = 0
        while len(data) < length:
            line = self._read_line(deadline)
            if line is None:
                raise TransportError(f"adapter: multi-frame answer cut short at {len(data)} of {length} bytes")
            head, _, body = line.partition(":")
            if head.strip() != f"{index:X}":
                raise TransportError(f"adapter: expected frame {index:X}, got {line}")
            data += bytes.fromhex(body)
            index = (index + 1) % 16
        return bytes(data[:length])

    def close(self) -> None:
        self._port.close()


def open_elm(path: str) -> ElmTransport:
    import serial

    return ElmTransport(serial.Serial(path, 115200, timeout=0.1))


class ReplayTransport(Transport):
    """Serve the DME responses recorded in a CL2000 log, in recorded order per request.

    Only exchanges that contain a final response (positive with matching DID
    echo, or a negative response other than 78 pending) are kept, so a read
    whose answer only arrived after the next tester request is dropped rather
    than misattributed.
    """

    def __init__(self, path):
        self._recorded: dict[bytes, list[list[bytes]]] = {}
        self._cursor: dict[bytes, int] = {}
        self._rx: deque[bytes] = deque()
        for ex in pair_exchanges(reassemble(parse_log(path))):
            sequence = _complete_sequence(ex.request, ex.responses)
            if sequence is not None:
                self._recorded.setdefault(ex.request, []).append(sequence)

    def _send(self, payload: bytes) -> None:
        sequences = self._recorded.get(payload)
        if not sequences:
            raise ReplayError(f"log has no complete response for request {payload.hex()}")
        i = self._cursor.get(payload, 0)
        self._cursor[payload] = i + 1
        self._rx.clear()
        self._rx.extend(sequences[i % len(sequences)])

    def recv(self, timeout: float) -> bytes:
        if not self._rx:
            raise TimeoutError("replay: no response queued")
        return self._rx.popleft()


def _complete_sequence(request: bytes, responses: list[bytes]):
    for i, r in enumerate(responses):
        if classify(request, r) == FINAL:
            return responses[:i + 1]
    return None
