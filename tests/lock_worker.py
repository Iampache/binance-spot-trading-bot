"""Subprocess lock probe; synthetic context, no credentials or network access."""

import argparse
import builtins
import gc
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def forbid_network(*args, **kwargs):
    raise AssertionError("Network access is forbidden in lock workers")


socket.socket.connect = forbid_network
socket.socket.connect_ex = forbid_network


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["instance", "operation", "imports"])
    parser.add_argument("path", type=Path)
    parser.add_argument("ready", type=Path)
    args = parser.parse_args()

    # Portalocker selects its POSIX backend on Linux. Import it before the probe
    # disallows Unix-only application imports; native Windows uses Win32 instead.
    import portalocker  # noqa: F401

    if args.mode == "imports":
        original_import = builtins.__import__

        def reject_unix_modules(name, *args, **kwargs):
            if name in {"fcntl", "termios", "pwd", "grp", "resource"}:
                raise ModuleNotFoundError(name)
            return original_import(name, *args, **kwargs)

        builtins.__import__ = reject_unix_modules

    import exit_manager as em

    if args.mode == "imports":
        import entry_scanner  # noqa: F401
        import exchange  # noqa: F401
        import safety  # noqa: F401
        import telegram_assistant  # noqa: F401
        import trade_assistant  # noqa: F401

        assert em.client is None
        args.ready.write_text("IMPORTED", encoding="utf-8")
        return

    em.USE_TESTNET = True
    em.CONTEXT = {"environment": "testnet", "account_uid": "999"}
    em.TRADES_DIR = args.path.parent / "trades"
    args.ready.write_text("WAITING", encoding="utf-8")
    try:
        lock = em.instance_lock() if args.mode == "instance" else em.operation_lock()
        with lock:
            gc.collect()
            args.ready.write_text("HELD", encoding="utf-8")
            sys.stdin.buffer.read(1)
    except FileExistsError:
        args.ready.write_text("BLOCKED", encoding="utf-8")
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
