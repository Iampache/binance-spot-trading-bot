"""Account-scoped operator limits; atomic revision and local change history."""

import json
import re
from datetime import datetime, timezone
from decimal import Decimal

import safety

FIELDS = {"max_open_trades", "max_position_usdt"}


def parse_value(field, value):
    text = str(value).strip()
    if field == "max_open_trades":
        if not re.fullmatch(r"[0-9]{1,3}", text) or not 1 <= int(text) <= 100:
            raise ValueError("Choose a whole number from 1 to 100.")
        return int(text)
    if field == "max_position_usdt":
        if not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,2})?", text) or Decimal(text) <= 0:
            raise ValueError("Enter a positive USDT amount with at most two decimal places.")
        return format(Decimal(text), "f")
    raise ValueError("Unknown trading setting")


def read():
    import exit_manager as em

    em.ensure_context()
    path = em.TRADES_DIR.parent / "settings.json"
    if not path.exists():
        return {
            "version": 1,
            "context": dict(em.CONTEXT),
            "revision": 0,
            "max_open_trades": safety.MAX_OPEN_TRADES,
            "max_position_usdt": str(safety.MAX_POSITION_USDT),
            "changes": [],
        }
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
        if (
            item["version"] != 1
            or item["context"] != em.CONTEXT
            or type(item["revision"]) is not int
            or item["revision"] < 1
            or type(item["max_open_trades"]) is not int
            or type(item["max_position_usdt"]) is not str
            or not isinstance(item["changes"], list)
            or len(item["changes"]) != item["revision"]
        ):
            raise ValueError("Invalid settings identity")
        for field in FIELDS:
            if parse_value(field, item[field]) != item[field]:
                raise ValueError("Invalid settings value")
        expected = {
            "max_open_trades": safety.MAX_OPEN_TRADES,
            "max_position_usdt": str(safety.MAX_POSITION_USDT),
        }
        for revision, event in enumerate(item["changes"], 1):
            field = event["field"]
            if (
                field not in FIELDS
                or event["revision"] != revision
                or event["old"] != expected[field]
                or parse_value(field, event["new"]) != event["new"]
                or not isinstance(event["actor"], str)
                or not datetime.fromisoformat(event["changed_utc"]).tzinfo
            ):
                raise ValueError("Invalid settings audit")
            expected[field] = event["new"]
        if any(expected[field] != item[field] for field in FIELDS):
            raise ValueError("Settings do not match change history")
        return item
    except Exception:
        raise RuntimeError(
            "Trading settings invalid; new entries blocked. Inspect local settings."
        ) from None


def update(field, value, *, expected_revision, actor):
    import exit_manager as em

    value = parse_value(field, value)
    with em.operation_lock():
        item = read()
        if item["revision"] != expected_revision:
            raise ValueError("Settings changed. Open /setup again to review the current limits.")
        if item[field] == value:
            return item
        event = {
            "revision": item["revision"] + 1,
            "field": field,
            "old": item[field],
            "new": value,
            "actor": str(actor),
            "changed_utc": datetime.now(timezone.utc).isoformat(),
        }
        item["revision"] += 1
        item[field] = value
        item["changes"].append(event)
        em._atomic_write(em.TRADES_DIR.parent / "settings.json", item)
        return item
