import pytest

from overrev.decode import RANGES, TESTED_DMES, Identity, format_part_number, parse_fault_codes, read_identity, read_vin, text_field, TIME_SCALE_MIN, decode_count, decode_max_rpm, decode_over_speed_s, decode_time_min
from overrev.uds import NegativeResponse, ProtocolError


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


@pytest.mark.parametrize("raw, text", [
    (b"3671\0 ", "3671"),
    (b"\0\0 99161869217 ", "99161869217"),
    (b"ECM40FS", "ECM40FS"),
    (b"", None),
    (b"\0\0  ", None),
    (b"\x01\xff", "01FF"),
    (b"AB\0CD", "4142004344"),
])
def test_text_field(raw, text):
    assert text_field(raw) == text


@pytest.mark.parametrize("part, shown", [
    ("99161869217", "991.618.692.17"),
    ("9916186921", "9916186921"),
    ("991618692170", "991618692170"),
    ("9916186921A", "9916186921A"),
    ("01FF", "01FF"),
])
def test_format_part_number(part, shown):
    assert format_part_number(part) == shown


def test_only_the_gt3_dme_is_tested():
    assert TESTED_DMES == {("99161869217", "3671")}
    assert Identity("99161869217", "3671").tested
    assert not Identity("99161869217", "3672").tested
    assert not Identity("99161869299", "3671").tested
    assert not Identity(None, "3671").tested
    assert not Identity("99161869217", None).tested
    assert not Identity(None, None).tested


def test_identity_describes_itself_in_plain_words():
    assert Identity("99161869217", "3671").describe() == "DME 991.618.692.17, software 3671"
    assert Identity(None, "3671").describe() == "DME part number unknown, software 3671"
    assert Identity("99161869299", None).describe() == "DME 991.618.692.99, software unknown"
    assert Identity(None, None).describe() == "DME part number unknown, software unknown"


class StubClient:
    def __init__(self, answers):
        self.answers = answers

    def read_did(self, did):
        answer = self.answers[did]
        if isinstance(answer, Exception):
            raise answer
        return answer


def refused(did):
    return NegativeResponse(bytes((0x22, did >> 8, did & 0xFF)), 0x31)


def test_read_identity_reads_part_number_and_software():
    identity = read_identity(StubClient({0xF187: b"99161869217", 0xF189: b"3671"}))
    assert identity == Identity("99161869217", "3671") and identity.tested


def test_read_identity_with_a_refused_part_number_is_unknown_and_untested():
    identity = read_identity(StubClient({0xF187: refused(0xF187), 0xF189: b"3671"}))
    assert identity == Identity(None, "3671") and not identity.tested


def test_read_identity_with_both_refused():
    identity = read_identity(StubClient({0xF187: refused(0xF187), 0xF189: refused(0xF189)}))
    assert identity == Identity(None, None) and not identity.tested


def test_read_identity_with_padded_empty_and_binary_answers():
    identity = read_identity(StubClient({0xF187: b"\0" * 11, 0xF189: b"\x01\xff"}))
    assert identity == Identity(None, "01FF") and not identity.tested
    identity = read_identity(StubClient({0xF187: b"99161869217\0\0", 0xF189: b"3671 "}))
    assert identity.tested


def test_read_identity_with_a_silent_did_is_unknown_and_untested():
    identity = read_identity(StubClient({0xF187: TimeoutError("no response to 22f187"), 0xF189: b"3671"}))
    assert identity == Identity(None, "3671") and not identity.tested
    identity = read_identity(StubClient({0xF187: b"99161869217", 0xF189: TimeoutError("no response to 22f189")}))
    assert identity == Identity("99161869217", None) and not identity.tested


def test_read_vin_strips_padding():
    assert read_vin(StubClient({0xF190: b"WP0ZZZ99ZKS100001"})) == "WP0ZZZ99ZKS100001"
    assert read_vin(StubClient({0xF190: b"\0\0WP0ZZZ99ZKS100001 \0"})) == "WP0ZZZ99ZKS100001"


def test_read_vin_is_none_when_refused_silent_or_empty():
    assert read_vin(StubClient({0xF190: refused(0xF190)})) is None
    assert read_vin(StubClient({0xF190: TimeoutError("no response to 22f190")})) is None
    assert read_vin(StubClient({0xF190: b"\0" * 17})) is None
