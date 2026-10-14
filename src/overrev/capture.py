"""CL2000 log parsing and ISO-TP reassembly.

Log rows are ``Timestamp;Type;ID;Data``; ``Type 0`` is a data frame and
``Data`` is the raw 8-byte CAN payload: ISO-TP PCI byte(s) first, then
padding (0xAA from the tester, 0x55 from the DME). Padding is dropped by
honouring the ISO-TP length, not by looking at the byte values.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TESTER_ID = 0x7E0
ECU_ID = 0x7E8


@dataclass(frozen=True)
class Frame:
    ts: str
    can_id: int
    data: bytes


@dataclass(frozen=True)
class Message:
    ts: str  # timestamp of the frame that started the message
    can_id: int
    payload: bytes


@dataclass
class Exchange:
    request: bytes
    responses: list[bytes] = field(default_factory=list)


def parse_log(path) -> list[Frame]:
    frames = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("Timestamp"):
                continue
            parts = line.split(";")
            if len(parts) < 4 or parts[1] != "0" or not parts[2]:
                continue
            try:
                frames.append(Frame(parts[0], int(parts[2], 16), bytes.fromhex(parts[3])))
            except ValueError:
                raise ValueError(f"{path}:{lineno}: malformed row {line!r}") from None
    return frames


def reassemble(frames, ids=(TESTER_ID, ECU_ID)) -> list[Message]:
    """Rebuild ISO-TP messages for the given CAN IDs, in completion order."""
    messages = []
    partial = {}  # can_id -> [ts, expected_len, buffer, next_seq]
    for f in frames:
        if f.can_id not in ids or not f.data:
            continue
        pci = f.data[0] >> 4
        if pci == 0:  # single frame
            n = f.data[0] & 0x0F
            if 0 < n <= len(f.data) - 1:
                messages.append(Message(f.ts, f.can_id, bytes(f.data[1:1 + n])))
            partial.pop(f.can_id, None)
        elif pci == 1:  # first frame
            n = ((f.data[0] & 0x0F) << 8) | f.data[1]
            partial[f.can_id] = [f.ts, n, bytearray(f.data[2:]), 1]
        elif pci == 2:  # consecutive frame
            p = partial.get(f.can_id)
            if p is None:
                continue
            if (f.data[0] & 0x0F) != (p[3] & 0x0F):
                del partial[f.can_id]
                continue
            p[2] += f.data[1:]
            p[3] += 1
            if len(p[2]) >= p[1]:
                messages.append(Message(p[0], f.can_id, bytes(p[2][:p[1]])))
                del partial[f.can_id]
        # pci == 3 is flow control: nothing to reassemble
    return messages


def pair_exchanges(messages, request_id=TESTER_ID, response_id=ECU_ID) -> list[Exchange]:
    """Attach each DME message to the most recent tester request."""
    exchanges = []
    for m in messages:
        if m.can_id == request_id:
            exchanges.append(Exchange(m.payload))
        elif m.can_id == response_id and exchanges:
            exchanges[-1].responses.append(m.payload)
    return exchanges
