"""Over-rev DIDs and scaling, verified against PIWIS on a 991.2 GT3 (GT3_991II_USA)."""
from __future__ import annotations

from dataclasses import dataclass

from .uds import ProtocolError, UdsClient


@dataclass(frozen=True)
class OverRevRange:
    number: int
    count_did: int  # EngPrt_nrIgnNMax<n>: ignitions in this range, uint16 big-endian, x1
    time_did: int   # EngPrt_tiNMax<n>: operating-time counter, uint16 big-endian, x6 = minutes


RANGES = (
    OverRevRange(1, 0x1B68, 0x1741),
    OverRevRange(2, 0x1B69, 0x1743),
    OverRevRange(3, 0x1B6A, 0x1745),
    OverRevRange(4, 0x1BBB, 0x1BB8),
    OverRevRange(5, 0x1BBC, 0x1BB9),
    OverRevRange(6, 0x1BBD, 0x1BBA),
)
TIME_SCALE_MIN = 6


@dataclass(frozen=True)
class RangeReading:
    range: int
    ignitions: int
    time_min: int
    count_raw: int
    time_raw: int


def _u16(data: bytes) -> int:
    if len(data) != 2:
        raise ProtocolError(f"expected a 2-byte value, got {data.hex() or '<empty>'}")
    return int.from_bytes(data, "big")


def decode_count(data: bytes) -> int:
    return _u16(data)


def decode_time_min(data: bytes) -> int:
    return _u16(data) * TIME_SCALE_MIN


def read_over_revs(client: UdsClient) -> list[RangeReading]:
    readings = []
    for r in RANGES:
        count = client.read_did(r.count_did)
        time_ = client.read_did(r.time_did)
        readings.append(RangeReading(
            r.number, decode_count(count), decode_time_min(time_), _u16(count), _u16(time_),
        ))
    return readings
