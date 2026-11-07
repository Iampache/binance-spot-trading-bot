#!/usr/bin/env bash
# Linux launcher. Never source .env: configuration is data, not shell code.
set -euo pipefail
BOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$BOT_DIR"

if [[ ! -x .venv/bin/python ]]; then
    echo "Cannot start: create this installation's .venv and install requirements first (README.md)."
    exit 1
fi

if [[ "${1:-}" == "--run" ]]; then
    set +e
    .venv/bin/python -u linux_launcher.py --run
    code=$?
    echo "NEXVANT stopped (exit code $code). Existing Binance orders may remain active."
    echo "Press Enter to close this session, then run ./bot-loader.sh to restart."
    read -r || true
    exit "$code"
fi
if [[ $# -ne 0 ]]; then
    echo "Usage: ./bot-loader.sh"
    exit 1
fi
command -v tmux >/dev/null || {
    echo "tmux is missing. Install it with: sudo apt install tmux"
    exit 1
}

# A session name per installation; the bot additionally acquires its account lock.
SESSION="nexvant-$(printf '%s' "$BOT_DIR" | sha256sum | cut -c1-12)"
if ! tmux has-session -t "=$SESSION" 2>/dev/null; then
    .venv/bin/python linux_launcher.py --check
    printf -v launch 'bash %q --run' "$BOT_DIR/bot-loader.sh"
    # Concurrent launchers may race to create the same session: attach to the winner.
    tmux new-session -d -s "$SESSION" -c "$BOT_DIR" "$launch" ||
        tmux has-session -t "=$SESSION"
fi
echo "Ctrl+B, release, then D: detach and keep running. Ctrl+C: stop the bot."
if [[ -n "${TMUX:-}" ]]; then
    tmux switch-client -t "=$SESSION"
else
    exec tmux attach-session -t "=$SESSION"
fi
