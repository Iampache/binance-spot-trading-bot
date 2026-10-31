"""Process-local observations only; never authorize an order or persist state."""

import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import safety

_lock = threading.Lock()
_tasks = []
_checks = {}
_started = time.monotonic()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def reset():
    global _started
    with _lock:
        _tasks.clear()
        _checks.clear()
        _started = time.monotonic()


def checked(name, error=None):
    """Keep last success when a later read fails; never store raw exception text."""
    with _lock:
        item = _checks.setdefault(name, {})
        item["attempt"] = stamp()
        if error is None:
            item.update(success=stamp(), error=None)
        else:
            item["error"] = safety.safe_error(error)


class Observation:
    def __init__(self, kind, identity):
        self.data = {
            "kind": kind,
            "identity": identity,
            "thread": threading.current_thread(),
            "running": True,
            "phase": "starting",
            "attempt": None,
            "success": None,
            "error": None,
            "at": time.monotonic(),
        }

    def attempt(self):
        with _lock:
            self.data.update(phase="checking", attempt=stamp(), at=time.monotonic())

    def success(self):
        with _lock:
            self.data.update(phase="waiting", success=stamp(), error=None, at=time.monotonic())

    def failure(self, error):
        with _lock:
            self.data.update(phase="retrying", error=safety.safe_error(error), at=time.monotonic())


@contextmanager
def worker(kind, identity=""):
    observation = Observation(kind, identity)
    with _lock:
        _tasks.append(observation.data)
    try:
        yield observation
    except Exception as error:
        observation.failure(error)
        raise
    finally:
        with _lock:
            observation.data["running"] = False


def snapshot():
    with _lock:
        now = time.monotonic()
        tasks = []
        for item in _tasks:
            tasks.append(
                {
                    **{k: v for k, v in item.items() if k not in {"thread", "at"}},
                    "running": item["running"] and item["thread"].is_alive(),
                    "age": max(0, now - item["at"]),
                }
            )
        return {
            "uptime": max(0, now - _started),
            "tasks": tasks,
            "checks": {key: dict(value) for key, value in _checks.items()},
        }
