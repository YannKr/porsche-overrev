from overrev.capture import Exchange, Frame, pair_exchanges, parse_log, reassemble

HEADER = "# Logger type: CL2000\n# Bit-rate: 500000\nTimestamp;Type;ID;Data\n"


def write_log(tmp_path, lines):
    p = tmp_path / "log.TXT"
    p.write_text(HEADER + "".join(line + "\n" for line in lines))
    return p


def test_parse_log_yields_data_frames_with_int_id_and_bytes(tmp_path):
    p = write_log(tmp_path, [
        "30T010701065;0;7e0;023e00aaaaaaaaaa",
        "30T010701072;0;7e8;027e005555555555",
    ])
    frames = parse_log(p)
    assert frames == [
        Frame("30T010701065", 0x7E0, bytes.fromhex("023e00aaaaaaaaaa")),
        Frame("30T010701072", 0x7E8, bytes.fromhex("027e005555555555")),
    ]


def test_parse_log_skips_non_data_and_blank_rows(tmp_path):
    p = write_log(tmp_path, [
        "30T010701065;0;7e0;023e00aaaaaaaaaa",
        "30T010701070;1;;",
        "",
        "30T010701072;0;;",
    ])
    assert [f.can_id for f in parse_log(p)] == [0x7E0]


def test_reassemble_single_frame_strips_pci_and_padding():
    frames = [Frame("t0", 0x7E8, bytes.fromhex("056217410d7d5555"))]
    msgs = reassemble(frames, ids=(0x7E8,))
    assert [(m.can_id, m.payload) for m in msgs] == [(0x7E8, bytes.fromhex("6217410d7d"))]


def test_reassemble_multi_frame_honours_first_frame_length():
    frames = [
        Frame("t0", 0x7E8, bytes.fromhex("100e62f187393931")),
        Frame("t1", 0x7E0, bytes.fromhex("300005aaaaaaaaaa")),  # flow control, ignored
        Frame("t2", 0x7E8, bytes.fromhex("21363138363932 31".replace(" ", ""))),
        Frame("t3", 0x7E8, bytes.fromhex("2237555555555555")),
    ]
    msgs = reassemble(frames, ids=(0x7E0, 0x7E8))
    assert len(msgs) == 1
    assert msgs[0].ts == "t0"
    assert msgs[0].payload == b"\x62\xf1\x8799161869217"


def test_reassemble_ignores_other_ids():
    frames = [
        Frame("t0", 0x778, bytes.fromhex("027e005555555555")),
        Frame("t1", 0x7E0, bytes.fromhex("03221741aaaaaaaa")),
    ]
    msgs = reassemble(frames, ids=(0x7E0, 0x7E8))
    assert [m.payload for m in msgs] == [bytes.fromhex("221741")]


def test_pair_exchanges_groups_responses_until_next_request():
    frames = [
        Frame("t0", 0x7E0, bytes.fromhex("03221741aaaaaaaa")),
        Frame("t1", 0x7E8, bytes.fromhex("037f227855555555")),
        Frame("t2", 0x7E8, bytes.fromhex("056217410d7d5555")),
        Frame("t3", 0x7E0, bytes.fromhex("023e00aaaaaaaaaa")),
        Frame("t4", 0x7E8, bytes.fromhex("027e005555555555")),
        Frame("t5", 0x7E0, bytes.fromhex("03221743aaaaaaaa")),
    ]
    exchanges = pair_exchanges(reassemble(frames, ids=(0x7E0, 0x7E8)))
    assert exchanges == [
        Exchange(bytes.fromhex("221741"), [bytes.fromhex("7f2278"), bytes.fromhex("6217410d7d")]),
        Exchange(bytes.fromhex("3e00"), [bytes.fromhex("7e00")]),
        Exchange(bytes.fromhex("221743"), []),
    ]




def test_reassemble_skips_single_frame_claiming_more_than_seven_bytes():
    frames = [Frame("t0", 0x7E8, bytes.fromhex("0962174100000000"))]
    assert reassemble(frames, ids=(0x7E8,)) == []


