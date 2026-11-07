"""Interactive local defaults wizard; no exchange connection or credential reads."""

from decimal import Decimal
from pathlib import Path

import operator_preferences as prefs
from trading_settings import parse_value


def describe(values):
    if values is None:
        return "You have not saved settings here yet.\nNEXVANT will continue using its existing settings until you save new ones."
    return (
        f"Maximum trades open at the same time: {values['max_open_trades']} (up to 10)\n"
        f"Maximum amount per trade: {values['max_position_usdt']} USDT per trade, including fees\n"
        f"Maximum total across those trades: {Decimal(values['max_position_usdt']) * values['max_open_trades']} USDT\n"
        f"Look for trade opportunities automatically: {'ON' if values['auto_scan'] else 'OFF'}\n"
        f"Send early price-movement alerts: {'ON' if values['momentum_alerts'] else 'OFF'}"
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
        print("NEXVANT — Choose how your bot runs\n\n" + describe(current))
        print(
            "\n1. Choose settings to use whenever you start the bot\n"
            "2. Bring back your previously saved settings\n"
            "3. Leave without making changes"
        )
        print(
            "\nNothing changes until you review and confirm. Saving settings does not place a trade."
        )
        choice = input("Enter 1, 2 or 3. Press Enter to leave:  ").strip() or "3"
        if choice == "1":
            values = dict(current or prefs.DEFAULTS)
            print("\nPress Enter at each question to keep the value in brackets.")
            if current is None:
                print("These are suggested starting values, not a report of your current settings.")
            for field, label in (
                ("max_open_trades", "How many trades can be open at the same time? (1–10)"),
                (
                    "max_position_usdt",
                    "What is the most you want to spend on one trade? (USDT, including fees)",
                ),
            ):
                values[field] = ask(
                    label, values[field], lambda value, field=field: parse_value(field, value)
                )
            for field, label in (
                (
                    "auto_scan",
                    "Look for trade opportunities and send proposals for your approval? (ON/OFF)",
                ),
                (
                    "momentum_alerts",
                    "Send early alerts when a coin starts moving strongly? (ON/OFF)",
                ),
            ):
                values[field] = ask(label, "ON" if values[field] else "OFF", switch)
            print("\nPlease review your choices:\n" + describe(values))
            print(
                "These choices will be used the next time you start NEXVANT.\nThe bot may spend less or open fewer trades, depending on your balance and trading checks.\nEvery new buy still needs your approval in Telegram. Trades already open will not change."
            )
            if input("Type SAVE to confirm, anything else cancels: ").strip() == "SAVE":
                prefs.save(folder, values, current)
                print(
                    "Your settings have been saved, and the previous settings have been backed up.\nRestart NEXVANT when you are ready to use these choices."
                )
            else:
                print("Cancelled. Nothing changed.")
        elif choice == "2":
            backup = prefs.read_backup(folder)
            print("Your previous settings were:\n" + describe(backup))
            if (
                input(
                    "Type RESTORE to use these settings next time you start the bot, or press Enter to cancel:  "
                ).strip()
                == "RESTORE"
            ):
                prefs.restore(folder, current, backup)
                print("Your previous settings have been restored. Restart NEXVANT to use them.")
            else:
                print("Cancelled. Nothing changed.")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
    except (ValueError, OSError):
        print(
            "NEXVANT could not read or save these settings. The settings file may be damaged,\na previous backup may be missing, or this user may not have permission to access it.\nYour API keys have not been changed."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
