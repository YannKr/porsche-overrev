import http.client
import json
import logging
import urllib.request

import pytest

from overrev import auto
from overrev.transport import ElmTransport, ReadOnlyViolation
from test_elm import VIN_FRAMES, FakeElm

THIS_CAR = "WP0ZZZ99ZKS100001"  # the made-up VIN in VIN_FRAMES
OTHER_CAR = "WP0ZZZ99ZKS999999"

COUNT_DIDS = ["1b68", "1b69", "1b6a", "1bbb", "1bbc", "1bbd"]
TIME_DIDS = ["1741", "1743", "1745", "1bb8", "1bb9", "1bba"]


def did_answer(did, data):
    """The lines an ELM327 prints for a positive answer to 22 <did>: one line, or ISO-TP lines past 7 bytes."""
    payload = bytes((0x62, did >> 8, did & 0xFF)) + data
    if len(payload) <= 7:
        return [payload.hex().upper()]
    lines = [f"{len(payload):03X}", "0:" + payload[:6].hex().upper()]
    for i, start in enumerate(range(6, len(payload), 7), start=1):
        lines.append(f"{i % 16:X}:" + payload[start:start + 7].ljust(7, b"\xaa").hex().upper())
    return lines


def car(counts=(3382, 1135, 664, 272, 166, 104), times_raw=(5158, 4988, 4542, 4542, 4542, 1052), refuse=None,
        peak_raw=(0x55F0, 0x0017), dme=("99161869217", "3671")):
    """dme is (part number, software); None for a field makes the DME refuse that DID."""
    table = {"1003": ["5003003201F4"], "22f190": VIN_FRAMES,
             "221b8a": [f"621B8A{peak_raw[0]:04X}"], "221b8b": [f"621B8B{peak_raw[1]:04X}"],
             "19022c": ["5902FF00ABCD60"]}
    for did, text in zip((0xF187, 0xF189), dme):
        table[f"22{did:04x}"] = ["7F2231"] if text is None else did_answer(did, text.encode())
    for did, value in [*zip(COUNT_DIDS, counts), *zip(TIME_DIDS, times_raw)]:
        table["22" + did] = [f"62{did.upper()}{value:04X}"]
    if refuse:
        table["22" + refuse] = ["7F2231"]
    return lambda request: table[request.hex()]


class Recorder:
    def __init__(self, fail=0):
        self.messages, self.fail = [], fail

    def __call__(self, content):
        if self.fail:
            self.fail -= 1
            raise OSError("network unreachable")
        self.messages.append(content)


def run(tmp_path, respond, vins=(THIS_CAR,), post=None, opener=None, sleeps=None):
    post = post or Recorder()
    sleeps = [] if sleeps is None else sleeps
    record = auto.run(
        opener or (lambda: ElmTransport(FakeElm(respond))),
        allowed_vins=set(vins), log_dir=tmp_path, post=post, retry=30, sleep=sleeps.append,
    )
    return record, post


def records(tmp_path):
    return [json.loads(line) for line in (tmp_path / "readings.jsonl").read_text().splitlines()]


def test_reads_this_car_and_appends_a_record(tmp_path):
    record, post = run(tmp_path, car())
    assert record["vin"] == THIS_CAR and record["verified"] is True
    assert [r["ignitions"] for r in record["ranges"]] == [3382, 1135, 664, 272, 166, 104]
    assert [r["time_min"] for r in record["ranges"]] == [30948, 29928, 27252, 27252, 27252, 6312]
    assert records(tmp_path) == [record]
    assert len(post.messages) == 1 and "OTHER CAR" not in post.messages[0]


def test_other_car_is_read_but_flagged(tmp_path):
    record, post = run(tmp_path, car(), vins=(OTHER_CAR,))
    assert record["verified"] is False and len(record["ranges"]) == 6
    assert "OTHER CAR" in post.messages[0]


def test_discord_shows_the_vin_masked(tmp_path):
    _, post = run(tmp_path, car())
    assert "100001" in post.messages[0]
    assert THIS_CAR not in post.messages[0]


def test_retries_until_the_adapter_answers(tmp_path):
    attempts = []

    def opener():
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("Host is down")
        return ElmTransport(FakeElm(car()))

    sleeps = []
    record, _ = run(tmp_path, None, opener=opener, sleeps=sleeps)
    assert len(attempts) == 3 and sleeps == [30, 30]
    assert len(record["ranges"]) == 6


def test_retries_when_the_car_does_not_answer(tmp_path):
    answers = [lambda request: ["NO DATA"], car()]
    sleeps = []
    record, _ = run(tmp_path, None, opener=lambda: ElmTransport(FakeElm(answers.pop(0))), sleeps=sleeps)
    assert sleeps == [30] and len(record["ranges"]) == 6


def test_dme_refusal_is_recorded_and_posted_once_without_retrying(tmp_path):
    sleeps = []
    record, post = run(tmp_path, car(refuse="1bbb"), sleeps=sleeps)
    assert "NRC 0x31" in record["error"] and "ranges" not in record
    assert sleeps == [] and records(tmp_path) == [record]
    assert "NRC 0x31" in post.messages[0]


def test_message_shows_new_over_revs_since_the_previous_read(tmp_path):
    run(tmp_path, car(counts=(3380, 1135, 664, 272, 166, 104)))
    _, post = run(tmp_path, car(counts=(3382, 1135, 664, 272, 166, 104)))
    row1 = next(line for line in post.messages[0].splitlines() if line.split()[:1] == ["1"])
    assert row1.split()[:3] == ["1", "3382", "+2"]


def test_first_read_has_no_previous_to_compare(tmp_path):
    _, post = run(tmp_path, car())
    row1 = next(line for line in post.messages[0].splitlines() if line.split()[:1] == ["1"])
    assert row1.split()[:3] == ["1", "3382", "-"]


@pytest.mark.parametrize("error", [ValueError("unknown url type: 'my-topic'"), http.client.HTTPException("bad reply")])
def test_a_post_failure_of_any_kind_never_escapes_after_the_record_is_written(tmp_path, error):
    def post(_):
        raise error

    sleeps = []
    record, _ = run(tmp_path, car(), post=post, sleeps=sleeps)
    assert records(tmp_path) == [record]
    assert sleeps == [30, 30]  # still retried, then given up


def test_post_is_retried_and_its_failure_keeps_the_record(tmp_path):
    record, post = run(tmp_path, car(), post=Recorder(fail=1))
    assert len(post.messages) == 1
    record, post = run(tmp_path, car(), post=Recorder(fail=99))
    assert post.messages == [] and records(tmp_path)[-1] == record


def test_read_only_violation_is_not_retried(tmp_path):
    def opener():
        raise ReadOnlyViolation("refusing to transmit 2703")

    def no_retry(_):
        raise SystemExit("retried a read-only violation")

    with pytest.raises(ReadOnlyViolation):
        auto.run(opener, allowed_vins=set(), log_dir=tmp_path, post=Recorder(), retry=30, sleep=no_retry)


@pytest.mark.parametrize("answer", [["7F2231"], ["NO DATA"]], ids=["refused", "silent"])
def test_unreadable_vin_does_not_stop_the_read_and_is_not_called_another_car(tmp_path, answer):
    respond = car()
    no_vin = lambda request: answer if request.hex() == "22f190" else respond(request)

    def no_retry(_):
        raise AssertionError("an unreadable VIN made auto read the car again")

    post = Recorder()
    record = auto.run(lambda: ElmTransport(FakeElm(no_vin)), allowed_vins={THIS_CAR}, log_dir=tmp_path, post=post,
                      retry=30, sleep=no_retry)
    assert record["vin"] is None and record["verified"] is False and "error" not in record
    assert record["dme"]["tested"] is True and len(record["ranges"]) == 6
    assert record["peak"]["max_rpm"] == 11000 and record["fault_codes"] == [{"code": "00ABCD", "status": 0x60}]
    assert records(tmp_path) == [record]
    assert "OTHER CAR" not in post.messages[0] and "VIN unreadable" in post.messages[0].splitlines()


def test_refused_session_is_recorded_not_crashed(tmp_path):
    respond = car()
    no_session = lambda request: ["7F1022"] if request.hex() == "1003" else respond(request)
    record, post = run(tmp_path, no_session)
    assert "NRC 0x22" in record["error"] and record["vin"] is None
    assert "OTHER CAR" not in post.messages[0] and "VIN unreadable" in post.messages[0].splitlines()


def test_peak_record_is_recorded_and_posted(tmp_path):
    record, post = run(tmp_path, car())
    assert record["peak"]["max_rpm"] == 11000 and record["peak"]["over_speed_s"] == 4.6
    assert "11000 rpm" in post.messages[0] and "4.6 s" in post.messages[0]


def test_new_over_speed_time_since_the_previous_read_is_shown(tmp_path):
    run(tmp_path, car(peak_raw=(0x55F0, 0x0016)))
    _, post = run(tmp_path, car(peak_raw=(0x55F0, 0x0017)))
    assert "+0.2 s" in post.messages[0]


def test_dme_without_the_peak_dids_still_records_the_over_revs(tmp_path):
    respond = car()
    no_peak = lambda request: ["7F2231"] if request.hex() in ("221b8a", "221b8b") else respond(request)
    record, post = run(tmp_path, no_peak)
    assert len(record["ranges"]) == 6 and record["peak"] is None and "error" not in record


def test_fault_codes_are_recorded_and_posted(tmp_path):
    record, post = run(tmp_path, car())
    assert record["fault_codes"] == [{"code": "00ABCD", "status": 0x60}]
    assert "00ABCD" in post.messages[0]


def test_message_names_the_tested_dme(tmp_path):
    record, post = run(tmp_path, car())
    assert record["dme"] == {"part_number": "99161869217", "software": "3671", "tested": True}
    lines = post.messages[0].splitlines()
    assert lines[0].startswith("**DME over-rev read** ") and lines[0].endswith(", read-only)")
    assert "DME 991.618.692.17, software 3671 (tested)" in lines
    assert "UNTESTED DME" not in post.messages[0]


def test_untested_dme_is_flagged_but_still_read(tmp_path):
    record, post = run(tmp_path, car(dme=("99161869299", "4000")))
    assert record["dme"] == {"part_number": "99161869299", "software": "4000", "tested": False}
    assert len(record["ranges"]) == 6 and "error" not in record
    assert ("**UNTESTED DME**: DME 991.618.692.99, software 4000; "
            "the values may not mean what the labels say.") in post.messages[0]
    assert "(tested)" not in post.messages[0]


def test_unknown_dme_is_untested_and_does_not_abort_the_read(tmp_path):
    record, post = run(tmp_path, car(dme=(None, None)))
    assert record["dme"] == {"part_number": None, "software": None, "tested": False}
    assert len(record["ranges"]) == 6 and "error" not in record
    assert "**UNTESTED DME**: DME part number unknown, software unknown;" in post.messages[0]


def test_other_car_line_names_the_vin_and_what_it_was_set_up_for(tmp_path):
    _, post = run(tmp_path, car(), vins=(OTHER_CAR,))
    assert "**OTHER CAR**: VIN …100001 is not the car this was set up for." in post.messages[0]
    assert "UNVERIFIED" not in post.messages[0]


def test_configured_car_shows_just_the_masked_vin(tmp_path):
    _, post = run(tmp_path, car())
    assert "VIN …100001" in post.messages[0].splitlines()


def test_without_a_configured_vin_nothing_is_flagged_as_another_car(tmp_path):
    record, post = run(tmp_path, car(), vins=())
    assert record["verified"] is False and len(record["ranges"]) == 6
    assert "OTHER CAR" not in post.messages[0] and "VIN …100001" in post.messages[0].splitlines()


def test_dme_is_recorded_as_none_when_the_session_is_refused(tmp_path):
    respond = car()
    no_session = lambda request: ["7F1022"] if request.hex() == "1003" else respond(request)
    record, post = run(tmp_path, no_session)
    assert record["dme"] is None and "UNTESTED DME" not in post.messages[0]


class Sent:
    """Stands in for urllib.request.urlopen and keeps the Request it was given."""

    def __init__(self, monkeypatch):
        self.requests = []
        monkeypatch.setattr(urllib.request, "urlopen", self)

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        return self

    def close(self):
        pass


def test_poster_sends_json_to_discord(monkeypatch):
    sent = Sent(monkeypatch)
    auto.poster("https://discord.com/api/webhooks/1/test")("**hello** …1")
    (request,) = sent.requests
    assert request.full_url == "https://discord.com/api/webhooks/1/test"
    assert json.loads(request.data) == {"content": "**hello** …1"}
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("User-agent") == "overrev"


def test_poster_sends_plain_text_to_ntfy(monkeypatch):
    sent = Sent(monkeypatch)
    auto.poster("https://ntfy.sh/test-topic")("**hello** …1")
    (request,) = sent.requests
    assert request.full_url == "https://ntfy.sh/test-topic"
    assert request.data == "**hello** …1".encode("utf-8")
    assert request.get_header("Content-type") == "text/plain; charset=utf-8"
    assert request.get_header("Title") == "DME over-rev read"
    assert request.get_header("Markdown") == "yes"
    assert request.get_header("User-agent") == "overrev"


@pytest.mark.parametrize("url", [
    "https://discord.com/api/webhooks/1/test", "https://ptb.discord.com/api/webhooks/1/test",
    "https://DISCORDAPP.com/api/webhooks/1/test", "https://canary.discordapp.com/api/webhooks/1/test",
])
def test_poster_treats_discord_hosts_as_discord(monkeypatch, url):
    sent = Sent(monkeypatch)
    auto.poster(url)("hi")
    assert json.loads(sent.requests[0].data) == {"content": "hi"}


@pytest.mark.parametrize("url", [
    "https://ntfy.example.org/test-topic", "https://notdiscord.com/api/webhooks/1/test",
    "https://discord.com.example.org/api/webhooks/1/test", "https://example.org/discord.com",
    "https://user@example.org:8443/discord.com",
])
def test_poster_sends_plain_text_to_every_other_host(monkeypatch, url):
    sent = Sent(monkeypatch)
    auto.poster(url)("hi")
    assert sent.requests[0].data == b"hi" and sent.requests[0].get_header("Title") == "DME over-rev read"


@pytest.mark.parametrize("url", ["my-topic", "ntfy.sh/my-topic", "", "https://", "ftp://example.org/x", "file:///etc/hosts"])
def test_poster_rejects_a_url_that_is_not_http_with_a_host(url):
    with pytest.raises(ValueError, match="http"):
        auto.poster(url)


def test_poster_error_does_not_repeat_the_secret_url():
    with pytest.raises(ValueError) as e:
        auto.poster("secret-topic-name")
    assert "secret-topic-name" not in str(e.value)


def test_auto_with_a_bad_webhook_stops_before_reading_the_car(monkeypatch, tmp_path):
    from overrev import cli
    opened = []
    monkeypatch.setattr(auto, "open_adapter", lambda mac, say=print: opened.append(mac))
    webhook = tmp_path / "webhook"
    webhook.write_text("my-topic\n")
    argv = ["auto", "--mac", "00:11:22:33:44:55", "--log-dir", str(tmp_path), "--webhook-file", str(webhook)]
    assert cli.main(argv) == 2
    assert opened == [] and not (tmp_path / "readings.jsonl").exists()


@pytest.mark.parametrize("make, reason", [(lambda path: None, "No such file or directory"),
                                          (lambda path: path.mkdir(), "Is a directory")], ids=["missing", "unreadable"])
def test_auto_with_a_webhook_file_it_cannot_read_still_reads_the_car_without_posting(monkeypatch, tmp_path, caplog,
                                                                                      make, reason):
    from overrev import cli
    sent = Sent(monkeypatch)
    monkeypatch.setattr(auto, "open_adapter", lambda mac, say=print: ElmTransport(FakeElm(car())))
    webhook = tmp_path / "webhook"
    make(webhook)
    argv = ["auto", "--mac", "00:11:22:33:44:55", "--vin", THIS_CAR, "--log-dir", str(tmp_path),
            "--webhook-file", str(webhook)]
    assert cli.main(argv) == 0
    (record,) = records(tmp_path)
    assert record["verified"] is True and len(record["ranges"]) == 6
    assert sent.requests == []
    warning = f"--webhook-file {webhook} cannot be read ({reason}); not posting"
    assert ("overrev.auto", logging.WARNING, warning) in caplog.record_tuples


def test_auto_by_mac_connects_over_bluetooth_and_logs_the_read(monkeypatch, tmp_path):
    from overrev import cli
    opened = []

    def fake_adapter(mac, say=print):
        opened.append(mac)
        return ElmTransport(FakeElm(car()))

    monkeypatch.setattr(auto, "open_adapter", fake_adapter)
    assert cli.main(["auto", "--mac", "00:11:22:33:44:55", "--vin", THIS_CAR, "--log-dir", str(tmp_path)]) == 0
    assert opened == ["00:11:22:33:44:55"]
    (record,) = records(tmp_path)
    assert record["vin"] == THIS_CAR and record["verified"] is True and len(record["ranges"]) == 6


def test_auto_by_serial_port_still_works_as_the_service_unit_calls_it(monkeypatch, tmp_path):
    from overrev import cli
    opened = []

    def fake_port(port):
        opened.append(port)
        return ElmTransport(FakeElm(car()))

    monkeypatch.setattr(auto, "open_elm", fake_port)
    assert cli.main(["auto", "--elm", "/dev/rfcomm0", "--vin", THIS_CAR, "--log-dir", str(tmp_path)]) == 0
    assert opened == ["/dev/rfcomm0"] and records(tmp_path)[0]["verified"] is True


@pytest.mark.parametrize("argv", [
    ["auto", "--log-dir", "x"],
    ["auto", "--elm", "/dev/rfcomm0", "--mac", "00:11:22:33:44:55", "--log-dir", "x"],
])
def test_auto_needs_exactly_one_way_to_reach_the_adapter(argv):
    from overrev import cli
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2
