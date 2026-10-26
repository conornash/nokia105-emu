#!/bin/sh
# Launch the emulator in a live window. Options are passed through to
# live.py / emu.py, e.g.  ./run.sh --headset --scale 2
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
    echo "first-time setup: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
make -s || exit 1
exec .venv/bin/python -I live.py "$@"
