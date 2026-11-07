"""Interactive local defaults wizard; no exchange connection or credential reads."""

from decimal import Decimal
from pathlib import Path

import operator_preferences as prefs
from trading_settings import parse_value


def describe(values):
    if values is None:
        return "No wizard overrides. Startup/account defaults and .env switches apply."
    return (
        f"Parallel trades: {values['max_open_trades']} (supported maximum 10)\n"
        f"Investment cap: {values['max_position_usdt']} USDT per trade, including fees\n"
        f"Combined ceiling: {Decimal(values['max_position_usdt']) * values['max_open_trades']} USDT\n"
        f"Automatic trade scanning: {'ON' if values['auto_scan'] else 'OFF'}\n"
        f"Momentum notifications: {'ON' if values['momentum_alerts'] else 'OFF'}"
    )


def ask(label, current, parser):
    while True:
        value = input(f"{label} [{current}]: ").strip() or str(current)
        try:
            return parser(value)
        except ValueError as error:
            print(error)


def switch(value):
    if value.lower() not in {"on", "off"}:
        raise ValueError("Enter ON or OFF.")
    return value.lower() == "on"


def main(folder=None):
    folder = folder or Path(__file__).resolve().parent
    try:
        current = prefs.read(folder)
        print("NEXVANT — saved startup preferences\n" + describe(current))
        print("1: Change defaults  2: Restore previous settings  3: Exit")
        choice = input("Choose [3]: ").strip() or "3"
        if choice == "1":
            values = dict(current or prefs.DEFAULTS)
            for field, label in (
                ("max_open_trades", "Default parallel trades (1–10)"),
                ("max_position_usdt", "Maximum investment per trade (USDT)"),
            ):
                values[field] = ask(
                    label, values[field], lambda value, field=field: parse_value(field, value)
                )
            for field, label in (
                ("auto_scan", "Automatic trade scanning (ON/OFF)"),
                ("momentum_alerts", "Momentum notifications (ON/OFF)"),
            ):
                values[field] = ask(label, "ON" if values[field] else "OFF", switch)
            print(describe(values))
            print(
                "Takes effect after restart. Funds, risk checks and Telegram approval still apply. Existing positions are unchanged."
            )
            if input("Type SAVE to confirm, anything else cancels: ").strip() == "SAVE":
                prefs.save(folder, values, current)
                print("Saved. Previous settings backed up. Restart NEXVANT to apply.")
            else:
                print("Cancelled. Nothing changed.")
        elif choice == "2":
            backup = prefs.read_backup(folder)
            print("Restore:\n" + describe(backup))
            if input("Type RESTORE to confirm: ").strip() == "RESTORE":
                prefs.restore(folder, current, backup)
                print("Restored. Restart NEXVANT to apply.")
            else:
                print("Cancelled. Nothing changed.")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
    except (ValueError, OSError):
        print(
            "Configuration could not be completed. Check settings/backup files and permissions; no credentials were accessed."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
