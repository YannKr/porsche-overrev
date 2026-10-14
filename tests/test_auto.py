import json

import pytest

from overrev import auto
from overrev.transport import ElmTransport, ReadOnlyViolation
from test_elm import VIN_FRAMES, FakeElm

THIS_CAR = "WP0ZZZ99ZKS100001"  # the made-up VIN in VIN_FRAMES
OTHER_CAR = "WP0ZZZ99ZKS999999"

COUNT_DIDS = ["1b68", "1b69", "1b6a", "1bbb", "1bbc", "1bbd"]
TIME_DIDS = ["1741", "1743", "1745", "1bb8", "1bb9", "1bba"]


def car(counts=(3382, 1135, 664, 272, 166, 104), times_raw=(5158, 4988, 4542, 4542, 4542, 1052), refuse=None):
    table = {"1003": ["5003003201F4"], "22f190": VIN_FRAMES}
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
    assert len(post.messages) == 1 and "UNVERIFIED" not in post.messages[0]


def test_other_car_is_read_but_flagged(tmp_path):
    record, post = run(tmp_path, car(), vins=(OTHER_CAR,))
    assert record["verified"] is False and len(record["ranges"]) == 6
    assert "UNVERIFIED CAR" in post.messages[0]


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


def test_other_car_without_a_readable_vin_is_recorded_not_crashed(tmp_path):
    respond = car()
    no_vin = lambda request: ["7F2231"] if request.hex() == "22f190" else respond(request)
    sleeps = []
    record, post = run(tmp_path, no_vin, sleeps=sleeps)
    assert record["vin"] is None and record["verified"] is False and "NRC 0x31" in record["error"]
    assert sleeps == [] and "UNVERIFIED CAR" in post.messages[0]


def test_refused_session_is_recorded_not_crashed(tmp_path):
    respond = car()
    no_session = lambda request: ["7F1022"] if request.hex() == "1003" else respond(request)
    record, post = run(tmp_path, no_session)
    assert "NRC 0x22" in record["error"] and record["vin"] is None
    assert "UNVERIFIED CAR" in post.messages[0]
