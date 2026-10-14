import pytest

from overrev.cli import main






def test_read_requires_channel_or_replay(capsys):
    with pytest.raises(SystemExit) as ei:
        main(["read"])
    assert ei.value.code == 2


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
