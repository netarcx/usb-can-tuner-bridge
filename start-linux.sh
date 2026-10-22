#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
case "${PHOENIX_PROFILE:-stable}" in
    stable) REQUIREMENTS="$SCRIPT_DIR/requirements.txt"; EXPECTED=26.3.0 ;;
    alpha) REQUIREMENTS="$SCRIPT_DIR/requirements-alpha.txt"; EXPECTED=26.70.0a2 ;;
    *) echo "ERROR: PHOENIX_PROFILE must be stable or alpha" >&2; exit 1 ;;
esac
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/usb-can-tuner/venvs/$EXPECTED"
if [[ ! -x "$VENV/bin/python" ]]; then
    python3 -m venv "$VENV"
fi
INSTALLED="$("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("phoenix6"))' 2>/dev/null || true)"
if [[ "$INSTALLED" != "$EXPECTED" ]]; then
    # Refuse to alter dependencies while any bridge holds the runtime lock.
    mkdir -p "$HOME/.local/state/usb-can-tuner"
    exec {SETUP_LOCK}>>"$HOME/.local/state/usb-can-tuner/runtime.lock"
    flock -n "$SETUP_LOCK" || { echo "ERROR: A bridge is running; its environment will not be changed." >&2; exit 1; }
    "$VENV/bin/python" -m pip install -r "$REQUIREMENTS"
    exec {SETUP_LOCK}>&-
fi
exec "$VENV/bin/python" -u "$SCRIPT_DIR/bridge.py" "$@"
