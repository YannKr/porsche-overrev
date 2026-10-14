#!/bin/sh
# overrev installer: install (or update) overrev, then read the car, in one line:
#
#   curl -fsSL https://raw.githubusercontent.com/YannKr/porsche-overrev/main/get.sh | sh
#
# Options (pass them as: ... | sh -s -- --no-run):
#   --no-run      install or update only, do not read the car
#   --yes         read the car without asking first
#   --uninstall   remove what this script installed
# Environment:
#   OVERREV_REF     branch, tag or commit to install (default: main)
#   OVERREV_SRC     install from this pip source instead (a path or URL; for testing)
#   OVERREV_PYTHON  Python interpreter to use (default: python3)
#
# Everything goes into ~/.local/share/overrev and the link ~/.local/bin/overrev. No sudo.
# The whole script is one function that runs on the last line, so a download that was
# cut short runs nothing.

main() {
    set -eu
    REF=${OVERREV_REF:-main}
    SRC=${OVERREV_SRC:-https://github.com/YannKr/porsche-overrev/archive/$REF.tar.gz}
    PY=${OVERREV_PYTHON:-python3}
    DIR=${XDG_DATA_HOME:-$HOME/.local/share}/overrev
    BIN=$HOME/.local/bin
    LINK=$BIN/overrev
    LOG=$DIR/install.log
    RUN=1
    ASK=1

    for arg in "$@"; do
        case $arg in
            --no-run) RUN=0 ;;
            --yes) ASK=0 ;;
            --uninstall) uninstall; return ;;
            *) die "unknown option '$arg'. Options: --no-run, --yes, --uninstall" ;;
        esac
    done

    say "overrev installer"
    [ "$(id -u)" -ne 0 ] || die "do not run this as root or with sudo. It installs into your home directory."
    check_system
    check_python
    check_link

    install

    if [ "$BT_READY" -eq 0 ]; then
        RUN=0
    fi
    if [ "$RUN" -eq 1 ] && [ "$ASK" -eq 1 ]; then
        confirm || RUN=0
    fi
    if [ "$RUN" -eq 1 ]; then
        say ""
        exec "$DIR/current/bin/overrev" read
    fi
    say "To read the car: plug the OBDLink in, turn the ignition on (engine off), and run: overrev"
}

say() { printf '%s\n' "$*"; }
warn() { printf 'overrev installer: warning: %s\n' "$*" >&2; }
die() { printf 'overrev installer: error: %s\n' "$*" >&2; exit 1; }

check_system() {
    BT_READY=1
    case $(uname -s) in
        Linux) check_bluetooth ;;
        Darwin)
            warn "macOS is not tested. Pair the OBDLink in System Settings > Bluetooth first (see Platforms in the README)."
            if ! ls /dev/cu.OBDLink* >/dev/null 2>&1; then
                warn "no paired OBDLink serial port (/dev/cu.OBDLink*) yet, so the car will not be read now."
                BT_READY=0
            fi ;;
        *) die "$(uname -s) is not supported by this installer. See Platforms in the README." ;;
    esac
}

check_bluetooth() {
    command -v bluetoothctl >/dev/null 2>&1 \
        || die "bluetoothctl not found. Install BlueZ first: sudo apt install bluez"
    show=$(timeout 10 bluetoothctl show 2>&1 || true)
    case $show in
        *"Controller "*) ;;
        *) die "no Bluetooth adapter found ('bluetoothctl show' lists none). Does this machine have Bluetooth, and is the bluetooth service running?" ;;
    esac
    case $show in
        *"Powered: yes"*) ;;
        *)
            warn "the Bluetooth adapter is off, so the car will not be read now. Turn it on with: bluetoothctl power on (if that fails: sudo rfkill unblock bluetooth)"
            BT_READY=0 ;;
    esac
}

check_python() {
    command -v "$PY" >/dev/null 2>&1 || die "$PY not found. Install Python 3.10 or newer."
    "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null \
        || die "$("$PY" -V 2>&1 || echo "$PY") is too old or does not run. overrev needs Python 3.10 or newer."
    "$PY" -c 'import venv, ensurepip' 2>/dev/null \
        || die "Python venv support is missing. On Debian or Raspberry Pi OS: sudo apt install python3-venv"
    if [ "$(uname -s)" = Linux ]; then
        "$PY" -c 'import socket; socket.AF_BLUETOOTH, socket.BTPROTO_RFCOMM' 2>/dev/null \
            || die "$PY has no Bluetooth socket support, which overrev needs to reach the OBDLink."
    fi
}

check_link() {
    # Never replace an `overrev` that this script did not install.
    if [ -e "$LINK" ] || [ -L "$LINK" ]; then
        case $(readlink "$LINK" 2>/dev/null || true) in
            "$DIR"/*) ;;
            *) die "$LINK already exists and was not installed by this script. Move it away and run this again." ;;
        esac
    fi
}

install() {
    mkdir -p "$DIR" "$BIN"
    NEW=$DIR/venv-$(date +%Y%m%d%H%M%S)
    trap 'rm -rf "$NEW"' EXIT INT TERM
    printf '\n=== %s install from %s\n' "$(date)" "$SRC" >>"$LOG"
    say "Creating a Python environment in $DIR ..."
    "$PY" -m venv "$NEW" >>"$LOG" 2>&1 || fail "creating the Python environment failed"
    say "Downloading and installing overrev ..."
    "$NEW/bin/python" -m pip install --disable-pip-version-check --no-input "$SRC" >>"$LOG" 2>&1 \
        || fail "installing overrev failed. Is this machine online?"
    "$NEW/bin/overrev" --help >/dev/null 2>&1 || fail "the new install does not start"
    version=$("$NEW/bin/python" -c 'import importlib.metadata as m; print(m.version("overrev"))' 2>/dev/null || echo "?")

    # The new environment works: switch to it, then remove older ones. A failed update
    # never gets this far, so the previous install keeps working.
    ln -sfn "$(basename "$NEW")" "$DIR/current"
    ln -sfn "$DIR/current/bin/overrev" "$LINK"
    trap - EXIT INT TERM
    for old in "$DIR"/venv-*; do
        [ "$old" = "$NEW" ] || rm -rf "$old"
    done
    rm -rf "$DIR/venv"  # left by the first version of this script
    say "Installed overrev $version: $LINK (log: $LOG)"
    case ":$PATH:" in
        *":$BIN:"*) ;;
        *) warn "$BIN is not on your PATH. Log in again, or run: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac
}

fail() {
    printf 'overrev installer: error: %s\nLast lines of %s:\n' "$1" "$LOG" >&2
    tail -n 5 "$LOG" >&2 || true
    if [ -e "$DIR/current" ]; then
        printf 'overrev installer: the previous install is unchanged.\n' >&2
    fi
    exit 1
}

confirm() {
    # The script arrives through a pipe, so ask on the terminal itself.
    if ! (: </dev/tty) 2>/dev/null; then
        say "No terminal to ask on, so the car will not be read now (use --yes to read without asking)."
        return 1
    fi
    printf '\nRead the car now? The OBDLink must be plugged in with the ignition on (engine off).\n' >/dev/tty
    printf 'overrev only sends read requests. [Y/n] ' >/dev/tty
    read -r answer </dev/tty || answer=n
    case $answer in
        [nN]*) return 1 ;;
    esac
}

uninstall() {
    if [ -L "$LINK" ]; then
        case $(readlink "$LINK") in
            "$DIR"/*) rm -f "$LINK" ;;
            *) warn "$LINK was not installed by this script; leaving it." ;;
        esac
    fi
    rm -rf "$DIR"
    say "Removed $DIR and $LINK. A paired OBDLink stays paired (remove it with: bluetoothctl remove <MAC>)."
}

main "$@"
