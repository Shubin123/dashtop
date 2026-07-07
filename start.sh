#!/usr/bin/env sh
# dashtop launcher for Linux/macOS — creates a local venv on first run.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
. .venv/bin/activate
pip install --quiet -r requirements.txt
exec python server.py "$@"
