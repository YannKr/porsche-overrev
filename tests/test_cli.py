
from overrev.cli import main






def test_read_without_a_source_finds_the_adapter_itself(monkeypatch, capsys):
    from overrev import cli
    from overrev.transport import ElmTransport
    from test_auto import car
    from test_elm import FakeElm
    monkeypatch.setattr(cli, "open_adapter", lambda mac, say: ElmTransport(FakeElm(car())))
    assert main(["read"]) == 0
    assert "3382" in capsys.readouterr().out


def test_bare_overrev_reads(monkeypatch, capsys):
    from overrev import cli
    from overrev.transport import ElmTransport
    from test_auto import car
    from test_elm import FakeElm
    monkeypatch.setattr(cli, "open_adapter", lambda mac, say: ElmTransport(FakeElm(car())))
    assert main([]) == 0
    assert "3382" in capsys.readouterr().out


def write_log(tmp_path, rows):
    p = tmp_path / "log.TXT"
    p.write_text("Timestamp;Type;ID;Data\n" + "".join(r + "\n" for r in rows))
    return p


def test_read_missing_replay_file_is_a_clean_error(capsys):
    assert main(["read", "--replay", "/nonexistent/dir/log.TXT"]) == 1
    err = capsys.readouterr().err
    assert "✗ Could not connect to the adapter" in err and "\nerror:" in err
    assert "Traceback" not in err


def test_read_reports_refused_session_without_the_security_hint(tmp_path, capsys):
    log = write_log(tmp_path, [
        "t0;0;7e0;021003aaaaaaaaaa",
        "t1;0;7e8;037f102255555555",
    ])
    assert main(["read", "--replay", str(log)]) == 1
    err = capsys.readouterr().err
    assert "NRC 0x22 conditionsNotCorrect" in err
    assert "SecurityAccess" not in err


def test_read_prints_security_hint_when_a_did_is_refused(tmp_path, capsys):
    log = write_log(tmp_path, [
        "t0;0;7e0;021003aaaaaaaaaa",
        "t1;0;7e8;065003003201f455",
        "t2;0;7e0;0322f187aaaaaaaa",
        "t3;0;7e8;037f223155555555",
        "t4;0;7e0;0322f189aaaaaaaa",
        "t5;0;7e8;037f223155555555",
        "t6;0;7e0;03221b68aaaaaaaa",
        "t7;0;7e8;037f223355555555",
    ])
    assert main(["read", "--replay", str(log)]) == 1
    err = capsys.readouterr().err
    assert "negative response to 221b68: NRC 0x33 securityAccessDenied" in err
    assert "never sends SecurityAccess" in err


def elm_car(monkeypatch, respond):
    from overrev import cli
    from overrev.transport import ElmTransport
    from test_elm import FakeElm
    monkeypatch.setattr(cli, "open_elm", lambda path: ElmTransport(FakeElm(respond)))


def test_read_shows_the_peak_record(monkeypatch, capsys):
    from test_auto import car
    elm_car(monkeypatch, car())
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 0
    out = capsys.readouterr().out
    assert "11000 rpm" in out and "4.6 s" in out


def test_read_json_has_the_peak_record(monkeypatch, capsys):
    import json as json_
    from test_auto import car
    elm_car(monkeypatch, car())
    assert main(["read", "--elm", "/dev/rfcomm0", "--json"]) == 0
    peak = json_.loads(capsys.readouterr().out)["peak"]
    assert (peak["max_rpm"], peak["over_speed_s"]) == (11000, 4.6)


def test_read_without_the_peak_dids_still_prints_the_ranges(monkeypatch, capsys):
    from test_auto import car
    respond = car()
    elm_car(monkeypatch, lambda r: ["7F2231"] if r.hex() == "221b8a" else respond(r))
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 0
    out = capsys.readouterr().out
    assert "3382" in out and "not available" in out


def test_read_lists_stored_fault_codes(monkeypatch, capsys):
    from test_auto import car
    respond = car()
    elm_car(monkeypatch, lambda r: ["5902FF00ABCD60"] if r.hex() == "19022c" else respond(r))
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 0
    out = capsys.readouterr().out
    assert "00ABCD" in out and "stored" in out


def test_read_with_refused_fault_memory_still_prints_the_ranges(monkeypatch, capsys):
    from test_auto import car
    respond = car()
    elm_car(monkeypatch, lambda r: ["7F1931"] if r.hex() == "19022c" else respond(r))
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 0
    out = capsys.readouterr().out
    assert "3382" in out and "Fault memory: not available" in out


UNTESTED_NOTE_91299 = (
    "note: This DME (991.618.692.99, software 1234) has not been tested with overrev, so the values may not mean "
    "what the labels say. Please share the output of 'overrev probe': "
    "https://github.com/YannKr/porsche-overrev/issues/new?template=new-car.yml"
)


def test_read_names_the_tested_dme_and_stays_quiet(monkeypatch, capsys):
    from test_auto import car
    elm_car(monkeypatch, car())
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == "DME 991.618.692.17, software 3671 (tested)"
    assert "3382" in captured.out
    assert "note:" not in captured.err


def test_read_flags_a_dme_it_was_not_tested_on(monkeypatch, capsys):
    from test_auto import car
    elm_car(monkeypatch, car(dme=("99161869299", "1234")))
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == "DME 991.618.692.99, software 1234 (not tested)"
    assert "3382" in captured.out  # the values are still shown
    assert UNTESTED_NOTE_91299 in captured.err.splitlines()


def test_read_with_the_identity_refused_still_reads_and_flags_it(monkeypatch, capsys):
    from test_auto import car
    elm_car(monkeypatch, car(dme=(None, None)))
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == "DME part number unknown, software unknown (not tested)"
    assert "3382" in captured.out
    assert "note: This DME (part number unknown, software unknown) has not been tested" in captured.err


def test_read_json_has_the_dme(monkeypatch, capsys):
    import json as json_
    from test_auto import car
    elm_car(monkeypatch, car())
    assert main(["read", "--elm", "/dev/rfcomm0", "--json"]) == 0
    captured = capsys.readouterr()
    assert json_.loads(captured.out)["dme"] == {"part_number": "99161869217", "software": "3671", "tested": True}
    assert "note:" not in captured.err


def test_read_json_has_an_untested_dme_and_the_note_stays_on_stderr(monkeypatch, capsys):
    import json as json_
    from test_auto import car
    elm_car(monkeypatch, car(dme=("99161869299", None)))
    assert main(["read", "--elm", "/dev/rfcomm0", "--json"]) == 0
    captured = capsys.readouterr()
    assert json_.loads(captured.out)["dme"] == {"part_number": "99161869299", "software": None, "tested": False}
    assert "note: This DME (991.618.692.99, software unknown)" in captured.err


def test_read_table_shows_operating_hours_at_the_last_event(monkeypatch, capsys):
    from test_auto import car
    elm_car(monkeypatch, car())
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "Range  Ignitions  Op. h at last event" in lines
    assert "    1       3382                515.8" in lines
    assert "    2       1135                498.8" in lines
    assert "Op. h at last event: the engine's operating hours when that range was last reached." in lines
    assert not any("raw" in line or "Time (min)" in line for line in lines)


def test_read_json_is_unchanged_by_the_new_table(monkeypatch, capsys):
    import json as json_
    from test_auto import car
    elm_car(monkeypatch, car())
    assert main(["read", "--elm", "/dev/rfcomm0", "--json"]) == 0
    first = json_.loads(capsys.readouterr().out)["ranges"][0]
    assert first == {"range": 1, "ignitions": 3382, "time_min": 30948, "count_raw": 3382, "time_raw": 5158}


def test_read_with_a_silent_identity_did_still_reads(monkeypatch, capsys):
    from test_auto import car
    respond = car()
    elm_car(monkeypatch, lambda r: ["NO DATA"] if r.hex() == "22f187" else respond(r))
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == "DME part number unknown, software 3671 (not tested)"
    assert "3382" in captured.out
