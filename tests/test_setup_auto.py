import getpass
import io
import os
import pty
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from overrev import cli, setup_auto
from overrev.cli import build_parser
from overrev.steps import HINT_IGNITION
from overrev.transport import ElmTransport, TransportError
from test_auto import THIS_CAR, car
from test_elm import FakeElm

MAC = "00:11:22:33:44:55"
HOOK = "https://discord.com/api/webhooks/1/test"
NTFY = "https://ntfy.sh/test-topic"
RETRY = "Run 'overrev setup-auto' again once the car answers."
STOP = ["systemctl", "--user", "stop", "overrev-auto.service"]
RELOAD = ["systemctl", "--user", "daemon-reload"]
ENABLE = ["systemctl", "--user", "enable", "overrev-auto.service"]
RESTART = ["systemctl", "--user", "restart", "overrev-auto.service"]


class Run:
    """Stands in for subprocess.run: records the argv, answers like a healthy systemd."""

    def __init__(self, fail=None, linger="Linger=no\n", missing=()):
        self.calls, self.fail, self.linger, self.missing = [], fail or {}, linger, missing

    def __call__(self, argv, **kwargs):
        assert isinstance(argv, list) and kwargs == {"capture_output": True, "text": True}
        self.calls.append(argv)
        if argv[0] in self.missing:
            raise FileNotFoundError(argv[0])
        for needle, stderr in self.fail.items():
            if needle in " ".join(argv):
                return subprocess.CompletedProcess(argv, 1, "", stderr)
        out = self.linger if argv[:2] == ["loginctl", "show-user"] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")


class FakeTty:
    def __init__(self, answer):
        self.answer, self.shown = answer, ""

    def write(self, text):
        self.shown += text

    def flush(self):
        pass

    def readline(self):
        return self.answer + "\n"

    def close(self):
        pass


def no_tty():
    raise OSError(6, "Device not configured")


def setup(run, *argv, platform="linux"):
    return setup_auto.main(build_parser().parse_args(["setup-auto", *argv]), run=run, platform=platform)


@pytest.fixture
def opened():
    """The MAC addresses the adapter was opened with."""
    return []


@pytest.fixture
def home(tmp_path, monkeypatch, opened):
    monkeypatch.setenv("HOME", str(tmp_path))
    tools = {"systemctl": "/usr/bin/systemctl", "overrev": str(tmp_path / ".local" / "bin" / "overrev")}
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: tools.get(name))
    monkeypatch.setattr(sys, "argv", ["overrev", "setup-auto"])  # not an absolute path, so which() decides

    def open_adapter(mac, say):
        opened.append(mac)
        return ElmTransport(FakeElm(car()))

    monkeypatch.setattr(setup_auto, "open_adapter", open_adapter)
    monkeypatch.setattr(setup_auto, "paired_obdlinks", lambda: [MAC])
    monkeypatch.setattr(setup_auto, "open_tty", no_tty)
    monkeypatch.setattr(setup_auto, "SYSTEM_UNIT", tmp_path / "etc-systemd-system" / "overrev-auto.service")
    return tmp_path


def unit(home):
    return home / ".config" / "systemd" / "user" / "overrev-auto.service"


def webhook(home):
    return home / ".config" / "overrev" / "webhook"


def start_with_car(monkeypatch, respond):
    monkeypatch.setattr(setup_auto, "open_adapter", lambda mac, say: ElmTransport(FakeElm(respond)))


def car_without(request_hex, lines):
    respond = car()
    return lambda r: lines if r.hex() == request_hex else respond(r)


def test_writes_the_unit_and_starts_it(home, capsys):
    run = Run()
    assert setup(run, "--mac", MAC, "--webhook", HOOK) == 0
    exe, log_dir = home / ".local" / "bin" / "overrev", home / ".local" / "state" / "overrev"
    assert unit(home).read_text() == f"""[Unit]
Description=overrev: read the DME over-rev counters once per boot

[Service]
Type=simple
ExecStart="{exe}" auto --mac {MAC} --vin {THIS_CAR} --log-dir "{log_dir}" --webhook-file "{webhook(home)}"
Restart=on-failure
RestartSec=30
RestartPreventExitStatus=2

[Install]
WantedBy=default.target
"""
    assert run.calls[:4] == [STOP, RELOAD, ENABLE, RESTART]
    out, err = capsys.readouterr()
    assert f"Automatic read is set up for VIN …100001 via the OBDLink {MAC}. It runs once at every boot; " \
           "turn the Pi on, plug in the adapter, ignition on." in out
    assert "A first automatic read starts now; expect its result in about a minute." in out
    assert f"Logs: {log_dir}/readings.jsonl and overrev.log, or: journalctl --user -u overrev-auto" in out
    assert "Remove it with: overrev setup-auto --remove" in out
    assert "✓ Connected to the adapter" in err and "✓ The car answers" in err and "✓ Read the over-rev counters" in err


def test_the_hand_installed_unit_does_not_restart_a_configuration_error_either():
    deploy = Path(__file__).resolve().parent.parent / "deploy" / "overrev-auto.service"
    text = deploy.read_text()
    assert "\nRestart=on-failure\n" in text and "\nRestartPreventExitStatus=2\n" in text  # exit 2: bad webhook or usage
    assert text.startswith("# Once per boot: wait for the OBDLink MX+, read VIN + over-revs (read-only), log, "
                           "post to Discord or ntfy.\n")


def test_a_terminal_that_cannot_show_the_ellipsis_still_gets_the_summary(home, monkeypatch):
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii"))
    assert setup(Run(), "--mac", MAC) == 0
    sys.stdout.flush()
    assert f"set up for VIN ...100001 via the OBDLink {MAC}.".encode() in raw.getvalue()


def test_the_webhook_is_stored_privately_and_never_printed(home, capsys):
    assert setup(Run(), "--mac", MAC, "--webhook", HOOK) == 0
    assert webhook(home).read_text() == HOOK + "\n"
    assert stat.S_IMODE(webhook(home).stat().st_mode) == 0o600
    assert stat.S_IMODE(webhook(home).parent.stat().st_mode) == 0o700
    out, err = capsys.readouterr()
    assert f"Webhook saved to {webhook(home)}." in err
    assert HOOK not in out + err


def test_an_existing_webhook_file_becomes_private(home):
    webhook(home).parent.mkdir(parents=True)
    webhook(home).write_text("https://discord.com/api/webhooks/2/old\n")
    webhook(home).chmod(0o644)
    assert setup(Run(), "--mac", MAC, "--webhook", NTFY) == 0
    assert webhook(home).read_text() == NTFY + "\n"
    assert stat.S_IMODE(webhook(home).stat().st_mode) == 0o600


def test_an_ntfy_topic_works_too(home):
    assert setup(Run(), "--mac", MAC, "--webhook", NTFY) == 0
    assert webhook(home).read_text() == NTFY + "\n"


def test_a_failed_enable_linger_prints_the_sudo_line(home, capsys):
    user = getpass.getuser()
    run = Run(fail={"enable-linger": "Interactive authentication required."})
    assert setup(run, "--mac", MAC) == 0
    assert ["loginctl", "show-user", user, "--property=Linger"] in run.calls
    assert ["loginctl", "enable-linger", user] in run.calls
    out = capsys.readouterr().out
    assert f"To have it run at boot without logging in, run once: sudo loginctl enable-linger {user}" in out


def test_linger_that_is_already_on_is_left_alone(home, capsys):
    run = Run(linger="Linger=yes\n")
    assert setup(run, "--mac", MAC) == 0
    assert not any("enable-linger" in call for call in run.calls)
    assert "sudo loginctl" not in capsys.readouterr().out


def test_linger_that_works_prints_no_sudo_line(home, capsys):
    run = Run()
    assert setup(run, "--mac", MAC) == 0
    assert ["loginctl", "enable-linger", getpass.getuser()] in run.calls
    assert "sudo loginctl" not in capsys.readouterr().out


def test_a_missing_loginctl_prints_the_sudo_line(home, capsys):
    assert setup(Run(missing=("loginctl",)), "--mac", MAC) == 0
    assert "sudo loginctl enable-linger" in capsys.readouterr().out


def test_a_second_run_without_a_webhook_and_no_terminal_keeps_the_stored_one(home):
    assert setup(Run(), "--mac", MAC, "--webhook", HOOK) == 0
    first = unit(home).read_text()
    assert setup(Run(), "--mac", MAC) == 0
    assert webhook(home).read_text() == HOOK + "\n"
    assert unit(home).read_text() == first and first.count("ExecStart=") == 1
    assert "--webhook-file" in first


def test_no_webhook_at_all_leaves_the_flag_out(home):
    assert setup(Run(), "--mac", MAC) == 0
    assert "--webhook-file" not in unit(home).read_text()
    assert not webhook(home).exists() and not webhook(home).parent.exists()


def test_the_prompt_stores_the_answer_and_rewrites_the_unit(home, monkeypatch):
    assert setup(Run(), "--mac", MAC) == 0
    tty = FakeTty(NTFY)
    monkeypatch.setattr(setup_auto, "open_tty", lambda: tty)
    assert setup(Run(), "--mac", MAC) == 0
    assert tty.shown == "Discord webhook or ntfy topic URL for the results (Enter for none): "
    assert webhook(home).read_text() == NTFY + "\n"
    text = unit(home).read_text()
    assert text.count("ExecStart=") == 1 and text.endswith("\n") and "--webhook-file" in text


def test_the_prompt_offers_to_keep_the_current_webhook(home, monkeypatch):
    assert setup(Run(), "--mac", MAC, "--webhook", HOOK) == 0
    tty = FakeTty("")
    monkeypatch.setattr(setup_auto, "open_tty", lambda: tty)
    assert setup(Run(), "--mac", MAC) == 0
    assert tty.shown.endswith("(Enter keeps the current one): ")
    assert webhook(home).read_text() == HOOK + "\n" and "--webhook-file" in unit(home).read_text()


def test_the_prompt_can_replace_the_current_webhook(home, monkeypatch):
    assert setup(Run(), "--mac", MAC, "--webhook", HOOK) == 0
    monkeypatch.setattr(setup_auto, "open_tty", lambda: FakeTty(NTFY))
    assert setup(Run(), "--mac", MAC) == 0
    assert webhook(home).read_text() == NTFY + "\n" and unit(home).read_text().count("ExecStart=") == 1


def test_an_empty_answer_with_no_stored_webhook_means_none(home, monkeypatch):
    monkeypatch.setattr(setup_auto, "open_tty", lambda: FakeTty(""))
    assert setup(Run(), "--mac", MAC) == 0
    assert "--webhook-file" not in unit(home).read_text() and not webhook(home).exists()


def test_a_prompt_answer_that_is_not_https_is_refused(home, monkeypatch, capsys):
    monkeypatch.setattr(setup_auto, "open_tty", lambda: FakeTty("http://example.com/hook"))
    run = Run()
    assert setup(run, "--mac", MAC) == 1
    err = capsys.readouterr().err
    assert "error: " in err and "https://" in err and "example.com" not in err
    assert not unit(home).exists() and not webhook(home).exists() and run.calls == [STOP]


def test_a_webhook_argument_that_is_not_https_is_refused_before_the_car_is_touched(home, monkeypatch, capsys, opened):
    run = Run()
    assert setup(run, "--mac", MAC, "--webhook", "http://x") == 1
    err = capsys.readouterr().err
    assert "error: " in err and "https://" in err and "http://x" not in err
    assert opened == [] and not unit(home).exists() and run.calls == []


@pytest.mark.parametrize("url", ["https://", "https:// space", "ftp://example.com/x", "example.com/x"])
def test_other_malformed_webhooks_are_refused(home, url):
    assert setup(Run(), "--mac", MAC, "--webhook", url) == 1
    assert not unit(home).exists()


def test_a_car_that_does_not_answer_writes_nothing(home, monkeypatch, capsys):
    start_with_car(monkeypatch, car_without("1003", ["NO DATA"]))
    run = Run()
    assert setup(run, "--mac", MAC, "--webhook", HOOK) == 1
    err = capsys.readouterr().err
    assert "✗ The car does not answer" in err and f"hint: {HINT_IGNITION}" in err
    assert err.rstrip().endswith(RETRY)
    assert not unit(home).exists() and not webhook(home).exists() and run.calls == [STOP]


def test_a_car_that_gives_no_vin_writes_nothing(home, monkeypatch, capsys):
    start_with_car(monkeypatch, car_without("22f190", ["7F2231"]))
    run = Run()
    assert setup(run, "--mac", MAC) == 1
    err = capsys.readouterr().err
    assert "error: the car did not give its VIN" in err and err.rstrip().endswith(RETRY)
    assert not unit(home).exists() and run.calls == [STOP]


def test_a_vin_with_odd_characters_is_not_written_into_the_unit(home, monkeypatch, capsys):
    monkeypatch.setattr(setup_auto, "read_vin", lambda client: "WP0 %ZZZ")
    assert setup(Run(), "--mac", MAC) == 1
    assert "error: " in capsys.readouterr().err and not unit(home).exists()


def test_an_adapter_that_cannot_be_reached_writes_nothing(home, monkeypatch, capsys):
    def open_adapter(mac, say):
        raise TransportError("cannot reach the OBDLink")

    monkeypatch.setattr(setup_auto, "open_adapter", open_adapter)
    assert setup(Run(), "--mac", MAC) == 1
    err = capsys.readouterr().err
    assert "✗ Could not connect to the adapter" in err and "error: cannot reach the OBDLink" in err
    assert err.rstrip().endswith(RETRY) and not unit(home).exists()


def test_without_mac_the_first_paired_obdlink_is_used(home, monkeypatch, opened):
    assert setup(Run()) == 0
    assert opened == [MAC] and f"--mac {MAC} " in unit(home).read_text()


def test_without_mac_and_nothing_paired_it_pairs(home, monkeypatch, opened):
    other = "00:11:22:33:44:66"
    monkeypatch.setattr(setup_auto, "paired_obdlinks", lambda: [])
    monkeypatch.setattr(setup_auto, "pair", lambda say: other)
    assert setup(Run()) == 0
    assert opened == [other] and f"--mac {other} " in unit(home).read_text()


def test_pairing_that_fails_is_a_clean_error(home, monkeypatch, capsys):
    def pair(say):
        raise TransportError("no OBDLink in pairing mode found within 2 minutes")

    monkeypatch.setattr(setup_auto, "paired_obdlinks", lambda: [])
    monkeypatch.setattr(setup_auto, "pair", pair)
    assert setup(Run()) == 1
    assert "error: no OBDLink in pairing mode" in capsys.readouterr().err and not unit(home).exists()


def test_a_dme_that_was_not_tested_is_noted_but_set_up(home, monkeypatch, capsys):
    start_with_car(monkeypatch, car(dme=("99161869299", "1234")))
    assert setup(Run(), "--mac", MAC) == 0
    err = capsys.readouterr().err
    assert "note: This DME (991.618.692.99, software 1234) has not been tested with overrev" in err


def test_a_tested_dme_is_not_noted(home, capsys):
    assert setup(Run(), "--mac", MAC) == 0
    assert "note:" not in capsys.readouterr().err


@pytest.mark.parametrize("argv", [(), ("--remove",)])
def test_other_platforms_and_no_systemd_are_refused(home, monkeypatch, capsys, argv, opened):
    assert setup(Run(), *argv, platform="darwin") == 1
    assert capsys.readouterr().err == "error: automatic mode needs Linux with systemd.\n"
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert setup(Run(), *argv) == 1
    assert capsys.readouterr().err == "error: automatic mode needs Linux with systemd.\n"
    assert opened == [] and not unit(home).exists()


@pytest.mark.parametrize("failing", ["daemon-reload", "enable", "restart"])
def test_a_failing_systemctl_is_reported_with_its_message(home, capsys, failing):
    assert setup(Run(fail={failing: "boom\n"}), "--mac", MAC) == 1
    err = capsys.readouterr().err
    assert err.rstrip().splitlines()[-1].startswith("error: systemctl --user ") and err.rstrip().endswith("failed: boom")


@pytest.mark.parametrize("failing, ran", [("daemon-reload", [STOP, RELOAD]),
                                          ("enable", [STOP, RELOAD, ENABLE]),
                                          ("restart", [STOP, RELOAD, ENABLE, RESTART])])
def test_nothing_runs_after_the_first_failing_command(home, failing, ran):
    run = Run(fail={failing: "boom"})
    assert setup(run, "--mac", MAC) == 1
    assert run.calls == ran


def test_a_running_service_is_stopped_before_the_adapter_is_used(home, monkeypatch):
    run = Run()

    def open_adapter(mac, say):
        run.calls.append(["<open the adapter>", mac])
        return ElmTransport(FakeElm(car()))

    monkeypatch.setattr(setup_auto, "open_adapter", open_adapter)
    assert setup(run, "--mac", MAC) == 0
    assert run.calls[:5] == [STOP, ["<open the adapter>", MAC], RELOAD, ENABLE, RESTART]


def test_a_failing_stop_does_not_stop_the_setup(home):
    run = Run(fail={"stop": "Unit overrev-auto.service not loaded."})
    assert setup(run, "--mac", MAC) == 0
    assert run.calls[:4] == [STOP, RELOAD, ENABLE, RESTART] and unit(home).exists()


def test_a_system_wide_service_of_the_same_name_is_warned_about(home, monkeypatch, capsys):
    system = home / "etc-systemd-system" / "overrev-auto.service"
    system.parent.mkdir()
    system.write_text("[Unit]\n")
    assert setup(Run(), "--mac", MAC) == 0
    err = capsys.readouterr().err
    warning = (f"warning: a system-wide overrev-auto service is also installed ({system}). Two services would "
               "both read the car at boot; remove one of them, for example: sudo systemctl disable --now "
               "overrev-auto.service\n")
    assert warning in err and err.index(warning) < err.index("Connected to the adapter")
    assert unit(home).exists()


def test_no_warning_without_a_system_wide_service(home, capsys):
    assert setup(Run(), "--mac", MAC) == 0
    assert "warning:" not in capsys.readouterr().err


def test_remove_stops_and_deletes_the_unit(home, capsys):
    unit(home).parent.mkdir(parents=True)
    unit(home).write_text("[Unit]\n")
    webhook(home).parent.mkdir(parents=True)
    webhook(home).write_text(HOOK + "\n")
    run = Run(fail={"disable": "Unit overrev-auto.service does not exist"})  # a failure here is fine
    assert setup(run, "--remove") == 0
    assert run.calls == [["systemctl", "--user", "disable", "--now", "overrev-auto.service"],
                         ["systemctl", "--user", "daemon-reload"]]
    assert not unit(home).exists() and webhook(home).exists()
    assert capsys.readouterr().out == (
        f"Removed the automatic read. Your logs stay in {home}/.local/state/overrev and settings in ~/.config/overrev.\n")


def test_remove_without_a_unit_is_fine(home):
    assert setup(Run(), "--remove") == 0


@pytest.mark.parametrize("bad", ['"', "\\", "%", "$"])
def test_a_path_systemd_would_misread_is_refused(home, monkeypatch, capsys, bad, opened):
    odd = home / f"a{bad}b"
    odd.mkdir()
    monkeypatch.setenv("HOME", str(odd))
    assert setup(Run(), "--mac", MAC) == 1
    assert "error: " in capsys.readouterr().err
    assert opened == [] and not unit(odd).exists()


def test_an_overrev_path_systemd_would_misread_is_refused(home, monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: {"systemctl": "/usr/bin/systemctl",
                                                                  "overrev": '/opt/we"ird/overrev'}.get(name))
    assert setup(Run(), "--mac", MAC) == 1
    assert "error: " in capsys.readouterr().err and not unit(home).exists()


def test_the_overrev_link_is_kept_so_updates_are_followed(home):
    real = home / "share" / "venv" / "bin" / "overrev"
    real.parent.mkdir(parents=True)
    real.write_text("")
    link = home / ".local" / "bin" / "overrev"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    assert setup(Run(), "--mac", MAC) == 0
    text = unit(home).read_text()
    assert f'ExecStart="{link}" auto' in text and str(real) not in text


def test_the_running_overrev_is_preferred_over_another_one_on_the_path(home, monkeypatch):
    real = home / "share" / "venv" / "bin" / "overrev"
    real.parent.mkdir(parents=True)
    real.write_text("")
    running = home / "bin" / "overrev"
    running.parent.mkdir()
    running.symlink_to(real)
    monkeypatch.setattr(sys, "argv", [str(running), "setup-auto"])
    assert setup(Run(), "--mac", MAC) == 0
    text = unit(home).read_text()
    assert f'ExecStart="{running}" auto' in text  # not which()'s ~/.local/bin/overrev, and the link is not resolved
    assert str(real) not in text


def test_without_overrev_on_the_path_the_running_one_is_used(home, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.chdir(home)
    monkeypatch.setattr(sys, "argv", ["bin/overrev", "setup-auto"])
    assert setup(Run(), "--mac", MAC) == 0
    assert f'ExecStart="{home}/bin/overrev" auto' in unit(home).read_text()


def test_the_subcommand_is_wired_into_the_cli(monkeypatch):
    seen = []
    monkeypatch.setattr(setup_auto, "main", lambda args: seen.append(args) or 0)
    assert cli.main(["setup-auto", "--mac", MAC, "--webhook", NTFY]) == 0
    assert (seen[0].mac, seen[0].webhook, seen[0].remove) == (MAC, NTFY, False)
    assert cli.main(["setup-auto", "--remove"]) == 0 and seen[1].remove is True


def test_the_real_terminal_opener_reads_a_line_and_shows_the_prompt(monkeypatch):
    primary, replica = pty.openpty()
    try:
        monkeypatch.setattr(setup_auto, "TTY", os.ttyname(replica))
        os.write(primary, b"https://ntfy.sh/test-topic\n")
        assert setup_auto.ask("Prompt: ") == "https://ntfy.sh/test-topic"
        assert b"Prompt: " in os.read(primary, 4096)
    finally:
        os.close(primary)
        os.close(replica)


def test_ask_without_a_terminal_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_auto, "TTY", str(tmp_path / "no-such-tty"))
    assert setup_auto.ask("Prompt: ") is None
