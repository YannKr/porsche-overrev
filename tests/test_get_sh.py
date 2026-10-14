"""get.sh under dash (the strictest POSIX sh), with stand-ins for the programs it calls."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

GET_SH = Path(__file__).resolve().parent.parent / "get.sh"
DASH = shutil.which("dash")

pytestmark = [
    pytest.mark.skipif(DASH is None, reason="dash is not installed"),
    pytest.mark.skipif(os.getuid() == 0, reason="get.sh refuses to run as root"),
]

UNIT = ".config/systemd/user/overrev-auto.service"


def script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def box(tmp_path):
    """HOME, a bin directory that comes first on PATH, and a log the stand-ins write to."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return bin_dir, home, tmp_path / "calls.log"


def get(box, *args, **kwargs):
    bin_dir, home, log = box
    env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_LOG": str(log), **kwargs.pop("env", {})}
    return subprocess.run([DASH, str(GET_SH), *args], env=env, capture_output=True, text=True, timeout=60,
                          start_new_session=True,  # no controlling terminal: a prompt on /dev/tty fails instead of waiting
                          **kwargs)


def calls(box):
    return box[2].read_text().splitlines() if box[2].exists() else []


def test_a_wrong_option_lists_the_options(box):
    done = get(box, "--bogus")
    assert done.returncode == 1
    assert "unknown option '--bogus'. Options: --no-run, --yes, --auto, --uninstall" in done.stderr


def test_uninstall_stops_and_removes_the_user_service(box):
    bin_dir, home, _ = box
    script(bin_dir / "systemctl", 'echo "$@" >>"$FAKE_LOG"\n')
    (home / UNIT).parent.mkdir(parents=True)
    (home / UNIT).write_text("[Unit]\n")
    done = get(box, "--uninstall")
    assert done.returncode == 0, done.stderr
    assert not (home / UNIT).exists()
    assert calls(box) == ["--user disable --now overrev-auto.service", "--user daemon-reload"]
    assert "automatic read" in done.stdout


def test_uninstall_removes_the_unit_even_when_systemctl_fails(box):
    bin_dir, home, _ = box
    script(bin_dir / "systemctl", 'echo "$@" >>"$FAKE_LOG"\nexit 1\n')
    (home / UNIT).parent.mkdir(parents=True)
    (home / UNIT).write_text("[Unit]\n")
    assert get(box, "--uninstall").returncode == 0
    assert not (home / UNIT).exists() and len(calls(box)) == 2


def test_uninstall_without_a_unit_does_not_touch_systemd(box):
    script(box[0] / "systemctl", 'echo "$@" >>"$FAKE_LOG"\n')
    done = get(box, "--uninstall")
    assert done.returncode == 0 and calls(box) == []
    assert "automatic read" not in done.stdout


# --auto: stand-ins for everything the installer calls, so no network and no Python install.
PYTHON = '''
case $1 in
    -c) case $2 in *importlib.metadata*) echo 0.0.0 ;; esac ;;
    -m) case $2 in
            venv) mkdir -p "$3/bin"
                  cp "$0" "$3/bin/python"
                  printf '#!/bin/sh\\ncase $1 in --help) exit 0 ;; esac\\necho "overrev $*" >>"$FAKE_LOG"\\n' >"$3/bin/overrev"
                  chmod +x "$3/bin/overrev" ;;
        esac ;;
esac
'''


@pytest.fixture
def linux(box):
    bin_dir = box[0]
    script(bin_dir / "uname", "echo Linux\n")
    script(bin_dir / "timeout", 'shift\nexec "$@"\n')
    script(bin_dir / "python3", PYTHON)
    return box


def bluetooth(box, powered):
    script(box[0] / "bluetoothctl", f'printf "Controller 00:00:00:00:00:00 host\\n\\tPowered: {powered}\\n"\n')


def test_auto_with_yes_hands_over_to_setup_auto(linux):
    bluetooth(linux, "yes")
    done = get(linux, "--auto", "--yes")
    assert done.returncode == 0, done.stderr
    assert calls(linux) == ["overrev setup-auto"]


def test_without_auto_it_still_reads(linux):
    bluetooth(linux, "yes")
    assert get(linux, "--yes").returncode == 0
    assert calls(linux) == ["overrev read"]


def test_auto_without_a_terminal_does_not_set_up_unasked(linux):
    bluetooth(linux, "yes")
    done = get(linux, "--auto")
    assert done.returncode == 0 and calls(linux) == []
    assert "No terminal to ask on" in done.stdout and "use --yes" in done.stdout
    assert "overrev setup-auto" in done.stdout


def test_auto_with_bluetooth_off_prints_the_manual_command(linux):
    bluetooth(linux, "no")
    done = get(linux, "--auto", "--yes")
    assert done.returncode == 0 and calls(linux) == []
    assert "run: overrev setup-auto" in done.stdout


def test_no_run_wins_over_auto(linux):
    bluetooth(linux, "yes")
    done = get(linux, "--auto", "--yes", "--no-run")
    assert done.returncode == 0 and calls(linux) == []
    assert "run: overrev setup-auto" in done.stdout


def test_auto_on_a_system_that_is_not_linux_stops_before_installing(box):
    bin_dir, home, _ = box
    script(bin_dir / "uname", "echo Darwin\n")
    script(bin_dir / "python3", PYTHON)
    done = get(box, "--auto", "--yes")
    assert done.returncode == 1
    assert done.stderr == ("overrev installer: error: --auto needs Linux with systemd; "
                           "on this system run overrev read instead\n")
    assert calls(box) == [] and not (home / ".local").exists()
