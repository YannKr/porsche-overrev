import pytest

from overrev.decode import RANGES, parse_fault_codes, TIME_SCALE_MIN, decode_count, decode_max_rpm, decode_over_speed_s, decode_time_min
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




@pytest.mark.parametrize("raw, rpm", [("55f0", 11000), ("55f1", 11000.5)])
def test_decode_max_engine_speed_is_half_rpm(raw, rpm):
    assert decode_max_rpm(bytes.fromhex(raw)) == rpm


@pytest.mark.parametrize("raw, seconds", [("0017", 4.6), ("0016", 4.4)])
def test_decode_over_speed_time_is_fifth_seconds(raw, seconds):
    assert decode_over_speed_s(bytes.fromhex(raw)) == seconds




def test_fault_codes_are_three_byte_dtcs_with_a_status_byte():
    codes = parse_fault_codes(bytes.fromhex("5902ff" "00123409" "00abcd60"))
    assert [(c.code, c.status) for c in codes] == [("001234", 0x09), ("00ABCD", 0x60)]
    assert [c.describe() for c in codes] == ["active, confirmed", "stored (failed since last clear)"]


def test_no_fault_codes():
    assert parse_fault_codes(bytes.fromhex("5902ff")) == []


@pytest.mark.parametrize("raw", ["5902", "5902ff001234", "5902ff0012340900"])
def test_fault_code_answer_with_a_partial_record_is_refused(raw):
    with pytest.raises(ProtocolError):
        parse_fault_codes(bytes.fromhex(raw))
