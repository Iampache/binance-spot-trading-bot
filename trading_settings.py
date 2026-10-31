"""Account-scoped operator limits; atomic revision and local change history."""

import copy
import json
import re
import threading
from datetime import datetime, timezone
from decimal import Decimal

import safety

FIELDS = {"max_open_trades", "max_position_usdt"}
MAX_PARALLEL_TRADES = 10
_session = {}
_session_lock = threading.RLock()


def parse_value(field, value, *, _legacy=False):
    text = str(value).strip()
    if field == "max_open_trades":
        maximum = 100 if _legacy else MAX_PARALLEL_TRADES
        if not re.fullmatch(r"[0-9]{1,3}", text) or not 1 <= int(text) <= maximum:
            raise ValueError(f"Choose a whole number from 1 to {maximum}.")
        return int(text)
    if field == "max_position_usdt":
        if not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,2})?", text) or Decimal(text) <= 0:
            raise ValueError("Enter a positive USDT amount with at most two decimal places.")
        return format(Decimal(text), "f")
    raise ValueError("Unknown trading setting")


def _read_saved():
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
            if parse_value(field, item[field], _legacy=True) != item[field]:
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
                or parse_value(field, event["new"], _legacy=True) != event["new"]
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


def reset_session():
    """Process startup only; retain the durable saved baseline."""
    with _session_lock:
        _session.clear()


def read():
    import exit_manager as em

    with _session_lock:
        saved = _read_saved()
        identity = (tuple(sorted(saved["context"].items())), str(em.TRADES_DIR))
        if _session.get("identity") != identity:
            _session.clear()
            _session.update(
                identity=identity,
                base_revision=saved["revision"],
                revision=saved["revision"],
                overrides={},
                events=[],
            )
        elif _session["base_revision"] != saved["revision"]:
            # An externally confirmed saved configuration supersedes old local
            # overrides. Never reuse its ordinal as an effective approval token.
            _session["revision"] = max(_session["revision"] + 1, saved["revision"])
            _session["base_revision"] = saved["revision"]
            _session["overrides"].clear()
        item = copy.deepcopy(saved)
        item.update(_session["overrides"])
        item["max_open_trades"] = min(item["max_open_trades"], MAX_PARALLEL_TRADES)
        item["parallel_ceiling"] = MAX_PARALLEL_TRADES
        item["revision"] = _session["revision"]
        item["saved_revision"] = saved["revision"]
        item["saved_limits"] = {field: saved[field] for field in FIELDS}
        item["scopes"] = {
            field: "session" if field in _session["overrides"] else "saved" for field in FIELDS
        }
        item["session_changes"] = copy.deepcopy(_session["events"])
        return item


def update(field, value, *, expected_revision, actor, scope="persistent"):
    import exit_manager as em

    value = parse_value(field, value)
    if scope not in {"session", "persistent"}:
        raise ValueError("Choose current session or save for future runs")
    with em.operation_lock(), _session_lock:
        item = read()
        if item["revision"] != expected_revision:
            raise ValueError("Settings changed. Open /setup again to review the current limits.")
        if item[field] == value and (scope == "session" and item["scopes"][field] == "session"):
            return item
        event = {
            "revision": item["revision"] + 1,
            "field": field,
            "old": item[field],
            "new": value,
            "actor": str(actor),
            "changed_utc": datetime.now(timezone.utc).isoformat(),
            "scope": scope,
        }
        if scope == "session":
            _session["revision"] += 1
            _session["overrides"][field] = value
            _session["events"].append(event)
        else:
            saved = _read_saved()
            if saved["revision"] != item["saved_revision"]:
                raise ValueError("Saved settings changed. Open /setup again before confirming.")
            changed = saved[field] != value
            if changed:
                saved["revision"] += 1
                saved["changes"].append(
                    {**event, "revision": saved["revision"], "old": saved[field]}
                )
                saved[field] = value
                # Update memory only after durable publication succeeds.
                em._atomic_write(em.TRADES_DIR.parent / "settings.json", saved)
            if changed or field in _session["overrides"]:
                _session["revision"] = max(item["revision"] + 1, saved["revision"])
                _session["base_revision"] = saved["revision"]
                _session["overrides"].pop(field, None)
                _session["events"].append(event)
        return read()
