from datetime import datetime, timedelta, timezone

from overrev import probe
from overrev.cli import main
from overrev.decode import FaultCode, Identity, PeakReading, RangeReading
from overrev.report import render, write
from test_auto import THIS_CAR, car
from test_cli import elm_car

WHEN = datetime(2026, 10, 13, 21, 5, 30, tzinfo=timezone(timedelta(hours=-7)))
TESTED = Identity("99161869217", "3671")
READINGS = [RangeReading(1, 3382, 30948, 3382, 5158), RangeReading(2, 1135, 29928, 1135, 4988)]
PEAK = PeakReading(11000.0, 4.6, 22000, 23)
CODES = [FaultCode("00ABCD", 0x60)]


def html_of(**changes):
    fields = dict(when=WHEN, vin=THIS_CAR, identity=TESTED, readings=READINGS, peak=PEAK, codes=CODES, version="0.3.0")
    return render(**{**fields, **changes})


def test_render_is_a_complete_document_with_the_whole_result():
    page = html_of()
    assert page.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in page and '<meta name="viewport"' in page
    assert "<title>DME over-rev report</title>" in page and "<h1>DME over-rev report</h1>" in page
    assert "2026-10-13 21:05:30-07:00" in page
    assert f"VIN {THIS_CAR}" in page
    assert "DME 991.618.692.17, software 3671 (tested)" in page
    assert "<td>515.8</td>" in page and "<td>3382</td>" in page and "<td>498.8</td>" in page
    assert "Op. h at last event: the engine's operating hours when that range was last reached." in page
    assert "Peak engine speed: 11000 rpm. Time above the over-speed limit: 4.6 s." in page
    assert "Stored fault codes (1):" in page and "00ABCD" in page and "stored (failed since last clear)" in page
    assert ("Read with overrev 0.3.0, which sends only read requests and changes nothing in the car." in page)
    assert "https://github.com/YannKr/porsche-overrev" in page


def test_render_says_when_the_vin_or_the_dme_are_not_known():
    page = html_of(vin=None, identity=Identity(None, None))
    assert "VIN unreadable" in page
    assert "DME part number unknown, software unknown (not tested)" in page


def test_render_says_when_peak_and_fault_codes_are_missing():
    page = html_of(peak=None, codes=None)
    assert "Peak record: not available on this DME." in page
    assert "Fault memory: not available." in page
    assert "Stored fault codes: none." in html_of(codes=[])


def test_render_escapes_everything_that_comes_from_the_car():
    page = html_of(vin="'<b>\"&'", identity=Identity("<i>", "&amp;"), version="<v>")
    assert "&lt;b&gt;&quot;&amp;" in page
    assert "<b>" not in page and "<i>" not in page and "<v>" not in page
    assert "&amp;amp;" in page


def test_render_loads_nothing_and_runs_nothing():
    page = html_of().lower()
    assert "<script" not in page and "<link" not in page and "src=" not in page
    assert "http://" not in page and "https://" in page  # only the repository URL, as text
    assert "<style>" in page and "prefers-color-scheme" in page and "@media print" in page


def test_write_returns_the_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    saved = write("out.html", "<p>é</p>")
    assert saved == tmp_path / "out.html" and saved.is_absolute()
    assert saved.read_text(encoding="utf-8") == "<p>é</p>"


def test_read_report_writes_the_page_and_says_where(monkeypatch, capsys, tmp_path):
    elm_car(monkeypatch, car())
    target = tmp_path / "report.html"
    assert main(["read", "--elm", "/dev/rfcomm0", "--report", str(target)]) == 0
    captured = capsys.readouterr()
    page = target.read_text(encoding="utf-8")
    for expected in (THIS_CAR, "3382", "515.8", "11000", "00ABCD", "DME 991.618.692.17, software 3671 (tested)"):
        assert expected in page, expected
    assert f"Report saved: {target}" in captured.err.splitlines()
    assert "Report saved" not in captured.out and "Range  Ignitions" in captured.out  # results still printed


def test_read_report_reports_the_installed_version(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(probe, "version", lambda name: "9.8.7")
    elm_car(monkeypatch, car())
    target = tmp_path / "report.html"
    assert main(["read", "--elm", "/dev/rfcomm0", "--report", str(target)]) == 0
    assert "Read with overrev 9.8.7," in target.read_text(encoding="utf-8")


def test_read_report_works_with_json_output(monkeypatch, capsys, tmp_path):
    elm_car(monkeypatch, car())
    target = tmp_path / "report.html"
    assert main(["read", "--elm", "/dev/rfcomm0", "--json", "--report", str(target)]) == 0
    captured = capsys.readouterr()
    assert captured.out.lstrip().startswith("{") and "Report saved" not in captured.out
    assert THIS_CAR in target.read_text(encoding="utf-8")


def test_read_report_with_a_refused_vin_still_reports(monkeypatch, capsys, tmp_path):
    respond = car()
    elm_car(monkeypatch, lambda r: ["7F2231"] if r.hex() == "22f190" else respond(r))
    target = tmp_path / "report.html"
    assert main(["read", "--elm", "/dev/rfcomm0", "--report", str(target)]) == 0
    assert "VIN unreadable" in target.read_text(encoding="utf-8")


def test_read_report_with_a_silent_vin_still_reports(monkeypatch, capsys, tmp_path):
    respond = car()
    elm_car(monkeypatch, lambda r: ["NO DATA"] if r.hex() == "22f190" else respond(r))
    target = tmp_path / "report.html"
    assert main(["read", "--elm", "/dev/rfcomm0", "--report", str(target)]) == 0
    assert "VIN unreadable" in target.read_text(encoding="utf-8")
    assert "3382" in capsys.readouterr().out


def test_read_report_into_a_missing_directory_fails_after_printing_the_results(monkeypatch, capsys, tmp_path):
    elm_car(monkeypatch, car())
    assert main(["read", "--elm", "/dev/rfcomm0", "--report", str(tmp_path / "nope" / "report.html")]) == 1
    captured = capsys.readouterr()
    assert "error: cannot write the report: No such file or directory" in captured.err
    assert "Report saved" not in captured.err
    assert "Range  Ignitions  Op. h at last event" in captured.out and "3382" in captured.out


def test_read_without_report_never_asks_for_the_vin(monkeypatch, capsys):
    seen, respond = [], car()
    elm_car(monkeypatch, lambda r: (seen.append(r.hex()), respond(r))[1])
    assert main(["read", "--elm", "/dev/rfcomm0"]) == 0
    assert "22f190" not in seen
    assert "Report saved" not in capsys.readouterr().err


def test_read_report_asks_for_the_vin_after_the_identity(monkeypatch, tmp_path):
    seen, respond = [], car()
    elm_car(monkeypatch, lambda r: (seen.append(r.hex()), respond(r))[1])
    assert main(["read", "--elm", "/dev/rfcomm0", "--report", str(tmp_path / "r.html")]) == 0
    assert seen.index("22f187") < seen.index("22f189") < seen.index("22f190") < seen.index("221b68")


def test_a_failed_read_writes_no_report(monkeypatch, capsys, tmp_path):
    respond = car()
    elm_car(monkeypatch, lambda r: ["7F2233"] if r.hex() == "221b68" else respond(r))
    target = tmp_path / "report.html"
    assert main(["read", "--elm", "/dev/rfcomm0", "--report", str(target)]) == 1
    assert not target.exists() and "Report saved" not in capsys.readouterr().err
