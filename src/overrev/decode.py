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


# The DME's all-time peak record, verified against PIWIS (EngPrt_nMaxSpd 11000 rpm, EngPrt_tiOverSpd 4.60 s).
PEAK_RPM_DID = 0x1B8A  # highest engine speed ever, 0.5 rpm per bit
OVER_SPEED_DID = 0x1B8B  # total time above the over-speed limit, 0.2 s per bit


@dataclass(frozen=True)
class PeakReading:
    max_rpm: float
    over_speed_s: float
    max_rpm_raw: int
    over_speed_raw: int


def decode_max_rpm(data: bytes) -> float:
    return _u16(data) / 2


def decode_over_speed_s(data: bytes) -> float:
    return _u16(data) / 5


def read_peak(client: UdsClient) -> PeakReading:
    rpm = client.read_did(PEAK_RPM_DID)
    over = client.read_did(OVER_SPEED_DID)
    return PeakReading(decode_max_rpm(rpm), decode_over_speed_s(over), _u16(rpm), _u16(over))


@dataclass(frozen=True)
class FaultCode:
    code: str  # the 3-byte DTC as 6 hex digits, as the DME reports it
    status: int  # ISO 14229 DTC status byte

    def describe(self) -> str:
        parts = [name for bit, name in ((0x01, "active"), (0x08, "confirmed"), (0x04, "pending")) if self.status & bit]
        if not parts and self.status & 0x20:
            parts.append("stored (failed since last clear)")
        return ", ".join(parts) or f"status 0x{self.status:02X}"


def parse_fault_codes(response: bytes) -> list[FaultCode]:
    """59 02 <availability mask> then 4 bytes per code: DTC (3) + status (1)."""
    records = response[3:]
    if len(response) < 3 or len(records) % 4:
        raise ProtocolError(f"malformed fault code answer {response.hex()}")
    return [FaultCode(records[i:i + 3].hex().upper(), records[i + 3]) for i in range(0, len(records), 4)]
