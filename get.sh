#!/bin/sh
# Install (or update) overrev and read the DME, in one line:
#   curl -fsSL https://raw.githubusercontent.com/YannKr/porsche-overrev/main/get.sh | sh
# Afterwards just run: overrev
set -eu
SRC="${OVERREV_SRC:-https://github.com/YannKr/porsche-overrev/archive/refs/heads/main.tar.gz}"
DIR="${XDG_DATA_HOME:-$HOME/.local/share}/overrev"
BIN="$HOME/.local/bin"

command -v python3 >/dev/null || { echo "overrev needs python3" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "overrev needs Python 3.10 or newer" >&2; exit 1; }
if [ ! -x "$DIR/venv/bin/python" ]; then
  python3 -m venv "$DIR/venv" || { echo "python3 -m venv failed. On Debian or Raspberry Pi OS: sudo apt install python3-venv" >&2; exit 1; }
fi
echo "Installing overrev into $DIR ..."
"$DIR/venv/bin/pip" install --quiet --disable-pip-version-check --force-reinstall "$SRC"
mkdir -p "$BIN"
ln -sf "$DIR/venv/bin/overrev" "$BIN/overrev"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Add $BIN to your PATH to run 'overrev' later." ;; esac
exec "$DIR/venv/bin/overrev" read
