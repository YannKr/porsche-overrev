from pathlib import Path

import pytest

from overrev import probe
from overrev.cli import main
from overrev.transport import ElmTransport
from test_auto import car, did_answer
from test_elm import FakeElm

ISSUE_LINK = "https://github.com/YannKr/porsche-overrev/issues/new?template=new-car.yml"
TEMPLATE = Path(__file__).resolve().parent.parent / ".github" / "ISSUE_TEMPLATE" / "new-car.yml"

IDENTIFICATION = {  # DIDs the plain car() does not answer
    "22f191": did_answer(0xF191, b"HW123456"),
    "22f1a3": did_answer(0xF1A3, b"H01"),
    "22f19e": did_answer(0xF19E, b"ECM40FS"),
}
RANGE_DIDS = ["1b68", "1741", "1b69", "1743", "1b6a", "1745", "1bbb", "1bb8", "1bbc", "1bb9", "1bbd", "1bba"]


def probe_car(**kwargs):
    respond = car(**kwargs)
    return lambda request: IDENTIFICATION.get(request.hex()) or respond(request)


def fake_car(monkeypatch, respond):
    from overrev import cli
    fake = FakeElm(respond)
    monkeypatch.setattr(cli, "open_elm", lambda path: ElmTransport(fake))
    return fake


def run_probe(monkeypatch, capsys, respond=None, argv=("probe", "--elm", "/dev/rfcomm0")):
    fake = fake_car(monkeypatch, respond or probe_car())
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err, fake


def test_probe_prints_a_markdown_report(monkeypatch, capsys):
    code, out, err, _ = run_probe(monkeypatch, capsys)
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "### overrev probe"
    for row in [
        "| Part number (F187) | 99161869217 |",
        "| Software (F189) | 3671 |",
        "| Hardware part number (F191) | HW123456 |",
        "| Hardware version (F1A3) | H01 |",
        "| Variant (F19E) | ECM40FS |",
        "| Tested DME | yes |",
        "| 1B68 | Range 1 ignitions | 0D36 (3382) |",
        "| 1741 | Range 1 op. time at last event | 1426 (5158) |",
        "| 1BBD | Range 6 ignitions | 0068 (104) |",
        "| 1BBA | Range 6 op. time at last event | 041C (1052) |",
        "| 1B8A | Peak engine speed | 55F0 (22000) |",
        "| 1B8B | Time above the over-speed limit | 0017 (23) |",
        "| 19 02 2C | Stored fault codes | 1 code |",
    ]:
        assert row in lines, row
    assert "| DID | Meaning on the tested DME | Answer |" in lines


def test_probe_reports_the_installed_version(monkeypatch, capsys):
    monkeypatch.setattr(probe, "version", lambda name: "9.8.7")
    _, out, _, _ = run_probe(monkeypatch, capsys)
    assert "overrev 9.8.7" in out.splitlines()


def test_probe_says_unknown_when_overrev_is_not_installed(monkeypatch, capsys):
    from importlib.metadata import PackageNotFoundError

    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(probe, "version", missing)
    _, out, _, _ = run_probe(monkeypatch, capsys)
    assert "overrev unknown" in out.splitlines()


def test_probe_asks_for_exactly_the_identification_and_over_rev_dids_and_never_the_vin(monkeypatch, capsys):
    code, _, _, fake = run_probe(monkeypatch, capsys)
    assert code == 0
    expected = ["1003", "22f187", "22f189", "22f191", "22f1a3", "22f19e",
                *("22" + did for did in RANGE_DIDS), "221b8a", "221b8b", "19022c"]
    assert [r.hex() for r in fake.requests] == expected
    assert not {"22f190", "22f17c", "22f18c"} & {r.hex() for r in fake.requests}


def test_probe_output_holds_no_vin(monkeypatch, capsys):
    _, out, err, _ = run_probe(monkeypatch, capsys)
    assert "WP0ZZZ99ZKS100001" not in out + err


def test_probe_shows_a_refused_did_as_its_nrc_and_goes_on(monkeypatch, capsys):
    code, out, _, _ = run_probe(monkeypatch, capsys, probe_car(refuse="1b69"))
    assert code == 0
    assert "| 1B69 | Range 2 ignitions | NRC 0x31 |" in out.splitlines()
    assert "| 1BBD | Range 6 ignitions | 0068 (104) |" in out.splitlines()


def test_probe_shows_a_silent_did_as_no_answer(monkeypatch, capsys):
    respond = probe_car()
    code, out, _, _ = run_probe(monkeypatch, capsys, lambda r: ["NO DATA"] if r.hex() == "221b6a" else respond(r))
    assert code == 0
    assert "| 1B6A | Range 3 ignitions | no answer |" in out.splitlines()
    assert "| 1B8A | Peak engine speed | 55F0 (22000) |" in out.splitlines()


def test_probe_shows_an_adapter_error_and_goes_on(monkeypatch, capsys):
    respond = probe_car()
    code, out, _, _ = run_probe(monkeypatch, capsys, lambda r: ["CAN ERROR"] if r.hex() == "221b6a" else respond(r))
    assert code == 0
    assert "| 1B6A | Range 3 ignitions | adapter error: adapter: CAN ERROR |" in out.splitlines()
    assert "| 1745 | Range 3 op. time at last event | 11BE (4542) |" in out.splitlines()  # the next DID still reads
    assert "| 1B8A | Peak engine speed | 55F0 (22000) |" in out.splitlines()


def test_probe_shows_refused_identification_and_an_untested_dme(monkeypatch, capsys):
    code, out, _, _ = run_probe(monkeypatch, capsys, probe_car(dme=(None, "1234")))
    lines = out.splitlines()
    assert code == 0
    assert "| Part number (F187) | NRC 0x31 |" in lines
    assert "| Software (F189) | 1234 |" in lines
    assert "| Tested DME | no |" in lines


def test_probe_shows_binary_and_empty_text_fields_without_breaking_the_table(monkeypatch, capsys):
    respond = probe_car()
    answers = {"22f1a3": did_answer(0xF1A3, b"\x01\xff"), "22f19e": did_answer(0xF19E, b"\0\0 "),
               "22f191": did_answer(0xF191, b"A|B")}
    _, out, _, _ = run_probe(monkeypatch, capsys, lambda r: answers.get(r.hex()) or respond(r))
    lines = out.splitlines()
    assert "| Hardware version (F1A3) | 01FF |" in lines
    assert "| Variant (F19E) | empty |" in lines
    assert "| Hardware part number (F191) | A\\|B |" in lines


def test_probe_shows_longer_answers_as_hex_only(monkeypatch, capsys):
    respond = probe_car()
    answers = {"221b68": did_answer(0x1B68, bytes.fromhex("0102030405")),
               "221b69": did_answer(0x1B69, bytes.fromhex("01020304"))}
    _, out, _, _ = run_probe(monkeypatch, capsys, lambda r: answers.get(r.hex()) or respond(r))
    lines = out.splitlines()
    assert "| 1B68 | Range 1 ignitions | 0102030405 |" in lines
    assert "| 1B69 | Range 2 ignitions | 01020304 (16909060) |" in lines


@pytest.mark.parametrize("answer, shown", [
    ("5902FF", "0 codes"),
    ("5902FF00ABCD60", "1 code"),
    ("5902FF00ABCD6000ABCE60", "2 codes"),
])
def test_probe_counts_the_stored_fault_codes(monkeypatch, capsys, answer, shown):
    respond = probe_car()
    _, out, _, _ = run_probe(monkeypatch, capsys, lambda r: [answer] if r.hex() == "19022c" else respond(r))
    assert f"| 19 02 2C | Stored fault codes | {shown} |" in out.splitlines()


def test_probe_with_the_fault_memory_refused(monkeypatch, capsys):
    respond = probe_car()
    code, out, _, _ = run_probe(monkeypatch, capsys, lambda r: ["7F1931"] if r.hex() == "19022c" else respond(r))
    assert code == 0 and "| 19 02 2C | Stored fault codes | NRC 0x31 |" in out.splitlines()


def test_probe_points_to_the_issue_form_on_stderr(monkeypatch, capsys):
    _, out, err, _ = run_probe(monkeypatch, capsys)
    assert (f"Paste this into a new issue (it holds no VIN or serial number): {ISSUE_LINK}") in err.splitlines()
    assert ISSUE_LINK not in out


def test_probe_ticks_the_adapter_and_car_steps(monkeypatch, capsys):
    _, _, err, _ = run_probe(monkeypatch, capsys)
    assert [l for l in err.splitlines() if l.startswith("✓")] == ["✓ Connected to the adapter", "✓ The car answers"]


def test_probe_with_a_silent_car_stops_with_the_ignition_hint(monkeypatch, capsys):
    from overrev.steps import HINT_IGNITION
    code, out, err, _ = run_probe(monkeypatch, capsys, lambda r: ["NO DATA"])
    assert code == 1 and out == ""
    assert "✗ The car does not answer" in err and f"hint: {HINT_IGNITION}" in err
    assert "Paste this" not in err


def test_probe_without_a_source_finds_the_adapter_itself(monkeypatch, capsys):
    from overrev import cli
    seen = []

    def open_adapter(mac, say):
        seen.append(mac)
        return ElmTransport(FakeElm(probe_car()))

    monkeypatch.setattr(cli, "open_adapter", open_adapter)
    assert main(["probe"]) == 0
    assert main(["probe", "--mac", "00:11:22:33:44:55"]) == 0
    assert seen == [None, "00:11:22:33:44:55"]
    assert "### overrev probe" in capsys.readouterr().out


def test_probe_takes_one_source_only(capsys):
    with pytest.raises(SystemExit):
        main(["probe", "--mac", "00:11:22:33:44:55", "--elm", "/dev/rfcomm0"])


def test_the_issue_form_exists_and_the_link_points_at_it():
    text = TEMPLATE.read_text()
    for key in ("name:", "description:", "body:"):
        assert any(line.startswith(key) for line in text.splitlines()), key
    assert ISSUE_LINK.endswith("?template=" + TEMPLATE.name)
