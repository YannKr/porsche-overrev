
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
    assert err.startswith("error:")


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
        "t2;0;7e0;03221b68aaaaaaaa",
        "t3;0;7e8;037f223355555555",
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
