"""Validate local configuration without exchange calls or credential output."""

import os
import sys
from pathlib import Path

from dotenv import dotenv_values

REQUIRED = ("API_KEY", "API_SECRET", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")
DEFAULTS = {
    "USE_TESTNET": "true",
    "ALLOW_LIVE_TRADING": "false",
    "AUTO_SCAN": "true",
    "MOMENTUM_ALERTS": "true",
    "ALLOW_BNB_FEES": "false",
}
OPTIONAL = {"TOP_N": 100, "APPROVAL_TTL_SECONDS": 300, "CHECK_TTL_SECONDS": 600}


def configuration(folder):
    path = folder / ".env"
    if not path.is_file():
        raise ValueError(
            "Local .env is missing. Copy .env.example to .env, set permissions to 600, "
            "and enter your own credentials locally. Never commit .env."
        )
    path.chmod(0o600)
    # No interpolation and no shell evaluation. Never include values in diagnostics.
    values = dotenv_values(path, interpolate=False)
    result = {}
    for key in REQUIRED:
        value = (values.get(key) or "").strip()
        if not value or value.lower().startswith(("your_", "replace_", "example")):
            raise ValueError(f"Configure {key} in your local .env.")
        if "\n" in value or "\r" in value or "\0" in value:
            raise ValueError(f"Invalid {key} in .env.")
        result[key] = value
    if not result["TELEGRAM_CHAT_ID"].isascii() or not result["TELEGRAM_CHAT_ID"].isdigit():
        raise ValueError("TELEGRAM_CHAT_ID must be your positive private Telegram user ID.")
    for key, default in DEFAULTS.items():
        value = (values.get(key) or default).strip().lower()
        if value not in {"true", "false"}:
            raise ValueError(f"{key} must be true or false in .env.")
        result[key] = value
    if result["USE_TESTNET"] == "false" and result["ALLOW_LIVE_TRADING"] != "true":
        raise ValueError(
            "Live startup is disabled. Set USE_TESTNET=true for Testnet, or explicitly "
            "set ALLOW_LIVE_TRADING=true with USE_TESTNET=false for Live Spot."
        )
    for key, default in OPTIONAL.items():
        value = (values.get(key) or str(default)).strip()
        if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 100_000:
            raise ValueError(f"{key} must be a positive integer no greater than 100000.")
        result[key] = value
    return result


def main():
    if sys.argv[1:] not in (["--check"], ["--run"]):
        print("Use ./bot-loader.sh to start NEXVANT.")
        return 1
    folder = Path(__file__).resolve().parent
    try:
        config = configuration(folder)
    except ValueError as error:
        print(str(error))
        return 1
    except Exception:
        print("Cannot read or secure local .env. Check its ownership and permissions.")
        return 1
    mode = "Testnet" if config["USE_TESTNET"] == "true" else "Live Spot"
    print(f"NEXVANT configuration checked — {mode}. Every new buy needs Telegram approval.")
    if sys.argv[1] == "--run":
        environment = dict(os.environ)
        environment.update(config)
        environment["PYTHON_DOTENV_DISABLED"] = "1"
        os.chdir(folder)
        os.execve(
            sys.executable,
            [sys.executable, "-u", str(folder / "telegram_assistant.py")],
            environment,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
