#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
    echo "Create .venv and install requirements before configuring NEXVANT."
    exit 1
fi
exec .venv/bin/python configure_bot.py
