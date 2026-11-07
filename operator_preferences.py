"""Installation defaults, loaded once at startup; never trading authorization."""

import json
import os
import tempfile
from pathlib import Path

from file_locking import ProcessLock
from trading_settings import parse_value

DEFAULTS = dict(max_open_trades=1, max_position_usdt="12", auto_scan=True, momentum_alerts=True)


def validate(data):
    if not isinstance(data, dict) or set(data) != set(DEFAULTS):
        raise ValueError("Invalid saved preferences; restore a valid backup before starting.")
    result = {
        field: parse_value(field, data[field]) for field in ("max_open_trades", "max_position_usdt")
    }
    for field in ("auto_scan", "momentum_alerts"):
        if type(data[field]) is not bool:
            raise ValueError("Alert preferences must be ON or OFF.")
        result[field] = data[field]
    return result


def read(folder):
    path = Path(folder) / "runtime_state" / "preferences.json"
    if not path.exists():
        return None
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        raise ValueError(
            "Saved preferences invalid or unreadable; inspect runtime_state/preferences.json."
        ) from None


def atomic(path, data):
    fd, name = tempfile.mkstemp(prefix=".preferences-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save(folder, values, expected):
    values = validate(values)
    parent = Path(folder) / "runtime_state"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with ProcessLock(parent / "preferences.lock"):
        previous = read(folder)
        if previous != expected:
            raise ValueError("Preferences changed in another terminal. Review them again.")
        # A null backup means no wizard overrides existed before this change.
        atomic(parent / "preferences.previous.json", previous)
        atomic(parent / "preferences.json", values)


def restore(folder, expected, backup):
    parent = Path(folder) / "runtime_state"
    with ProcessLock(parent / "preferences.lock"):
        if read(folder) != expected or read_backup(folder) != backup:
            raise ValueError("Preferences or backup changed. Review them again.")
        if backup is None:
            (parent / "preferences.json").unlink(missing_ok=True)
        else:
            atomic(parent / "preferences.json", validate(backup))
        atomic(parent / "preferences.previous.json", expected)


def read_backup(folder):
    path = Path(folder) / "runtime_state" / "preferences.previous.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return None if data is None else validate(data)
    except Exception:
        raise ValueError("No valid previous settings backup is available.") from None
