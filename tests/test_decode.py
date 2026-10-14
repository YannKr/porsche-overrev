import pytest

from overrev.decode import RANGES, TIME_SCALE_MIN, decode_count, decode_time_min
from overrev.uds import ProtocolError


def test_range_table_matches_protocol_doc():
    assert [(r.number, r.count_did, r.time_did) for r in RANGES] == [
        (1, 0x1B68, 0x1741),
        (2, 0x1B69, 0x1743),
        (3, 0x1B6A, 0x1745),
        (4, 0x1BBB, 0x1BB8),
        (5, 0x1BBC, 0x1BB9),
        (6, 0x1BBD, 0x1BBA),
    ]


@pytest.mark.parametrize("raw, value", [
    ("0b34", 2868), ("0404", 1028), ("0248", 584), ("00e9", 233), ("0097", 151), ("0068", 104),
])
def test_decode_count_is_plain_uint16(raw, value):
    assert decode_count(bytes.fromhex(raw)) == value


@pytest.mark.parametrize("raw, minutes", [
    ("0d7d", 20718), ("0a63", 15954), ("087f", 13050), ("041c", 6312),
])
def test_decode_time_is_uint16_times_six_minutes(raw, minutes):
    assert TIME_SCALE_MIN == 6
    assert decode_time_min(bytes.fromhex(raw)) == minutes


@pytest.mark.parametrize("raw", ["", "0d", "0d7d00"])
def test_decode_rejects_wrong_length(raw):
    with pytest.raises(ProtocolError):
        decode_count(bytes.fromhex(raw))
    with pytest.raises(ProtocolError):
        decode_time_min(bytes.fromhex(raw))


