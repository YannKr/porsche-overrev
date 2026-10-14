"""`overrev setup-auto`: read the car once per boot, as a systemd user service (Linux)."""
from __future__ import annotations

import getpass
import io
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

from .bluetooth import open_adapter, pair, paired_obdlinks
from .cli import FAILURES, say
from .decode import UNTESTED_NOTE, read_identity, read_vin
from .steps import ADAPTER, CAR, READ, Steps
from .uds import ProtocolError, UdsClient

SERVICE = "overrev-auto.service"
TTY = "/dev/tty"
SYSTEM_UNIT = Path("/etc/systemd/system") / SERVICE  # what deploy/overrev-auto.service installs as
UNSAFE = '"\\%$'  # a quote or backslash ends the quoting; systemd expands % and $ in ExecStart
RETRY = "Run 'overrev setup-auto' again once the car answers."
WEBHOOK_RULE = ("the webhook must be an https:// URL: a Discord webhook URL or an ntfy topic URL "
                "such as https://ntfy.sh/my-topic")
UNIT = """[Unit]
Description=overrev: read the DME over-rev counters once per boot

[Service]
Type=simple
ExecStart={command}
Restart=on-failure
RestartSec=30
RestartPreventExitStatus=2

[Install]
WantedBy=default.target
"""


def open_tty():
    """The terminal itself, for reading and writing. Under `curl | sh` stdin is the pipe, so ask here instead."""
    fd = os.open(TTY, os.O_RDWR | os.O_NOCTTY)  # open(TTY, "r+") fails: a terminal cannot seek
    return io.TextIOWrapper(io.FileIO(fd, "r+"))


def ask(prompt: str) -> str | None:
    """The answer typed on the terminal, or None when there is no terminal."""
    try:
        tty = open_tty()
    except OSError:
        return None
    try:
        tty.write(prompt)
        tty.flush()
        return tty.readline().strip()
    finally:
        tty.close()


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def valid_webhook(url: str) -> bool:
    try:
        return url.startswith("https://") and not re.search(r"\s", url) and bool(urllib.parse.urlsplit(url).hostname)
    except ValueError:
        return False


def vin_tail(vin: str) -> str:
    dots = "…"
    try:
        dots.encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        dots = "..."  # a terminal that cannot show it must not crash the end of a finished setup
    return dots + vin[-6:]


def save_webhook(path: Path, url: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        os.fchmod(f.fileno(), 0o600)  # also for a file that already existed, before the secret goes in
        f.write(url + "\n")


def run_quietly(run, *argv: str) -> subprocess.CompletedProcess:
    try:
        return run(list(argv), capture_output=True, text=True)
    except OSError as e:  # the program is not installed
        return subprocess.CompletedProcess(list(argv), 127, "", str(e))


def remove(run, unit_file: Path, log_dir: Path) -> int:
    run_quietly(run, "systemctl", "--user", "disable", "--now", SERVICE)  # fine when it was never set up
    unit_file.unlink(missing_ok=True)
    run_quietly(run, "systemctl", "--user", "daemon-reload")
    print(f"Removed the automatic read. Your logs stay in {log_dir} and settings in ~/.config/overrev.")
    return 0


def main(args, run=subprocess.run, platform=sys.platform) -> int:
    if not platform.startswith("linux") or not shutil.which("systemctl"):
        return fail("automatic mode needs Linux with systemd.")
    home = Path.home()
    unit_file = home / ".config" / "systemd" / "user" / SERVICE
    log_dir = home / ".local" / "state" / "overrev"
    webhook_file = home / ".config" / "overrev" / "webhook"
    if args.remove:
        return remove(run, unit_file, log_dir)

    # The running program first: another overrev may come earlier on PATH. A symlink stays one, so updates are followed.
    running = sys.argv[0]
    if os.path.isabs(running) and os.path.isfile(running):
        overrev = running
    else:
        overrev = os.path.abspath(shutil.which("overrev") or running)  # abspath keeps a symlink a symlink
    for path in (overrev, log_dir, webhook_file):
        if any(c in str(path) for c in UNSAFE):
            return fail(f"cannot use {path} in a systemd unit: it holds a quote, backslash, % or $.")
    if args.webhook and not valid_webhook(args.webhook):
        return fail(WEBHOOK_RULE)
    if SYSTEM_UNIT.exists():
        say(f"warning: a system-wide overrev-auto service is also installed ({SYSTEM_UNIT}). Two services would "
            "both read the car at boot; remove one of them, for example: sudo systemctl disable --now "
            f"{SERVICE}")

    # A running instance (it retries every 30 s, for ever) would compete for the one RFCOMM connection.
    run_quietly(run, "systemctl", "--user", "stop", SERVICE)
    steps = Steps()
    try:
        with steps.step(ADAPTER):
            mac = args.mac or next(iter(paired_obdlinks()), None) or pair(say)
            transport = open_adapter(mac, say=say)
        with transport:
            client = UdsClient(transport)
            with steps.step(CAR):
                client.start_session()
            with steps.step(READ):
                vin = read_vin(client)
                if vin is None:
                    raise ProtocolError("the car did not give its VIN")
                if not re.fullmatch(r"[A-Za-z0-9]+", vin):
                    raise ProtocolError("the car gave a VIN that is not made of letters and digits")
                identity = read_identity(client)
    except FAILURES as e:
        if e is not steps.reported:
            print(f"error: {e}", file=sys.stderr)
        say(RETRY)
        return 1
    if not identity.tested:
        say("note: " + UNTESTED_NOTE.format(describe=identity.describe().removeprefix("DME ")))

    stored = webhook_file.is_file()
    url = args.webhook or ask("Discord webhook or ntfy topic URL for the results "
                              f"({'Enter keeps the current one' if stored else 'Enter for none'}): ")
    if url:
        if not valid_webhook(url):
            return fail(WEBHOOK_RULE)
        save_webhook(webhook_file, url)
        say(f"Webhook saved to {webhook_file}.")
        stored = True

    command = f'"{overrev}" auto --mac {mac} --vin {vin} --log-dir "{log_dir}"'
    if stored:
        command += f' --webhook-file "{webhook_file}"'
    unit_file.parent.mkdir(parents=True, exist_ok=True)
    unit_file.write_text(UNIT.format(command=command), encoding="utf-8")

    # restart, not enable --now: that leaves a running instance on the old ExecStart
    for argv in (("systemctl", "--user", "daemon-reload"), ("systemctl", "--user", "enable", SERVICE),
                 ("systemctl", "--user", "restart", SERVICE)):
        done = run_quietly(run, *argv)
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip() or f"exit status {done.returncode}"
            return fail(f"{' '.join(argv)} failed: {detail}")

    user = getpass.getuser()
    lingering = run_quietly(run, "loginctl", "show-user", user, "--property=Linger").stdout.strip() == "Linger=yes"
    if not lingering and run_quietly(run, "loginctl", "enable-linger", user).returncode != 0:
        print(f"To have it run at boot without logging in, run once: sudo loginctl enable-linger {user}")

    print(f"Automatic read is set up for VIN {vin_tail(vin)} via the OBDLink {mac}. It runs once at every boot; "
          "turn the Pi on, plug in the adapter, ignition on.")
    print("A first automatic read starts now; expect its result in about a minute.")
    print(f"Logs: {log_dir}/readings.jsonl and overrev.log, or: journalctl --user -u overrev-auto")
    print("Remove it with: overrev setup-auto --remove")
    return 0
