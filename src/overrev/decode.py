"""Over-rev DIDs and scaling, verified against PIWIS on a 991.2 GT3 (GT3_991II_USA)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .uds import ProtocolError, UdsClient


@dataclass(frozen=True)
class OverRevRange:
    number: int
    count_did: int  # EngPrt_nrIgnNMax<n>: ignitions in this range, uint16 big-endian, x1
    time_did: int   # EngPrt_tiNMax<n>: operating-time counter, uint16 big-endian, x6 = minutes


TABLE_EXPLANATION = "Op. h at last event: the engine's operating hours when that range was last reached."

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

    @property
    def hours(self) -> float:
        """Engine operating hours when this range was last reached."""
        return self.time_min / 60


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

    def describe(self) -> str:
        return f"Peak engine speed: {self.max_rpm:g} rpm. Time above the over-speed limit: {self.over_speed_s:.1f} s."


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


PART_NUMBER_DID = 0xF187
SOFTWARE_DID = 0xF189
VIN_DID = 0xF190

# (part number, software) pairs the over-rev DIDs were verified on.
TESTED_DMES = {("99161869217", "3671")}

ISSUE_LINK = "https://github.com/YannKr/porsche-overrev/issues/new?template=new-car.yml"
UNTESTED_NOTE = (
    "This DME ({describe}) has not been tested with overrev, so the values may not mean what the labels say. "
    f"Please share the output of 'overrev probe': {ISSUE_LINK}"
)


def text_field(data: bytes) -> str | None:
    """An identification DID as text: ASCII as is, nothing as None, anything else as hex."""
    text = data.strip(b"\0 ")
    if not text:
        return None
    if all(0x20 <= b < 0x7F for b in text):
        return text.decode("ascii")
    return data.hex().upper()


def format_part_number(part: str) -> str:
    """99161869217 -> 991.618.692.17; anything that is not 11 digits is returned unchanged."""
    if re.fullmatch(r"[0-9]{11}", part):
        return f"{part[:3]}.{part[3:6]}.{part[6:9]}.{part[9:]}"
    return part


@dataclass(frozen=True)
class Identity:
    part_number: str | None
    software: str | None

    @property
    def tested(self) -> bool:
        return (self.part_number, self.software) in TESTED_DMES

    def describe(self) -> str:
        part = format_part_number(self.part_number) if self.part_number else "part number unknown"
        software = f"software {self.software}" if self.software else "software unknown"
        return f"DME {part}, {software}"


def _read_text(client: UdsClient, did: int) -> str | None:
    try:
        return text_field(client.read_did(did))
    except (ProtocolError, TimeoutError):
        return None  # refused or silent: the field stays unknown, the read goes on


def read_identity(client: UdsClient) -> Identity:
    return Identity(_read_text(client, PART_NUMBER_DID), _read_text(client, SOFTWARE_DID))


def read_vin(client: UdsClient) -> str | None:
    """The VIN as text, or None when the DME refuses, stays silent or answers with padding only."""
    try:
        return client.read_did(VIN_DID).decode("ascii", "replace").strip("\0 ") or None
    except (ProtocolError, TimeoutError):
        return None
