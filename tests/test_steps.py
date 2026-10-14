from overrev.cli import main
from overrev.steps import HINT_ADAPTER_SILENT, HINT_IGNITION, HINT_READ_INTERRUPTED, HINT_SESSION_REFUSED, SECURITY_HINT, Steps
from test_auto import car
from test_cli import elm_car


def with_reply(request_hex, lines):
    respond = car()
    return lambda r: lines if r.hex() == request_hex else respond(r)


def test_a_good_read_ticks_every_step_in_order(monkeypatch, capsys):
    elm_car(monkeypatch, car())
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 0
    err = capsys.readouterr().err
    marks = [line for line in err.splitlines() if line.startswith("✓")]
    assert marks == ["✓ Connected to the adapter", "✓ The car answers", "✓ Read the over-rev counters"]


def test_a_sleeping_car_says_turn_the_ignition_on(monkeypatch, capsys):
    elm_car(monkeypatch, with_reply("1003", ["CAN ERROR"]))
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 1
    err = capsys.readouterr().err
    assert "✗ The car does not answer" in err and "error: adapter: CAN ERROR" in err
    assert f"hint: {HINT_IGNITION}" in err


def test_no_answer_to_the_session_says_turn_the_ignition_on(monkeypatch, capsys):
    elm_car(monkeypatch, with_reply("1003", ["NO DATA"]))
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 1
    assert f"hint: {HINT_IGNITION}" in capsys.readouterr().err


def test_refused_session_names_the_tested_dme(monkeypatch, capsys):
    elm_car(monkeypatch, with_reply("1003", ["7F1012"]))
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 1
    err = capsys.readouterr().err
    assert f"hint: {HINT_SESSION_REFUSED}" in err and SECURITY_HINT not in err


def test_refused_did_gives_the_security_hint(monkeypatch, capsys):
    elm_car(monkeypatch, with_reply("221b68", ["7F2233"]))
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 1
    err = capsys.readouterr().err
    assert "✗ Could not read the over-rev counters" in err and f"hint: {SECURITY_HINT}" in err


def test_car_going_quiet_mid_read_says_keep_the_ignition_on(monkeypatch, capsys):
    elm_car(monkeypatch, with_reply("221b69", ["NO DATA"]))
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 1
    assert f"hint: {HINT_READ_INTERRUPTED}" in capsys.readouterr().err


def test_silent_adapter_says_unplug_and_replug(monkeypatch, capsys):
    from overrev import cli
    from overrev.transport import ElmTransport
    from test_elm import FakeElm
    monkeypatch.setattr(ElmTransport, "RESET_TIMEOUT", 0.05)
    monkeypatch.setattr(cli, "open_elm", lambda path: ElmTransport(FakeElm(car(), swallow=99)))
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 1
    err = capsys.readouterr().err
    assert "✗ Could not connect to the adapter" in err and f"hint: {HINT_ADAPTER_SILENT}" in err


def test_ascii_terminal_gets_ascii_marks():
    import io
    out = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    with Steps(out).step("adapter"):
        pass
    out.flush()
    assert out.buffer.getvalue().decode() == "ok Connected to the adapter\n"
