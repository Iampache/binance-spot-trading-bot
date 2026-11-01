"""Portable subprocess tests run unchanged on Windows and Linux."""

import gc
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import portalocker
import pytest

import exit_manager as em
import file_locking
from file_locking import ProcessLock

WORKER = Path(__file__).with_name("lock_worker.py")


@pytest.fixture
def workers(tmp_path):
    processes = []

    def start(mode, lock_path):
        ready = tmp_path / f"worker-{len(processes)}.ready"
        # Whitelist platform bootstrap variables. Never copy credentials to the
        # probe processes or load a user's .env file.
        environment = {
            name: os.environ[name]
            for name in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")
            if name in os.environ
        }
        environment.update(
            PYTHON_DOTENV_DISABLED="1", USE_TESTNET="true", ALLOW_LIVE_TRADING="false"
        )
        process = subprocess.Popen(
            [sys.executable, "-u", str(WORKER), mode, str(lock_path), str(ready)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=WORKER.parent.parent,
        )
        processes.append(process)
        return process, ready

    yield start
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def wait_for(process, ready, expected, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready.exists() and ready.read_text(encoding="utf-8") == expected:
            return
        if process.poll() is not None:
            out, err = process.communicate(timeout=5)
            pytest.fail(f"Lock worker exited early: {process.returncode}, {out!r}, {err!r}")
        time.sleep(0.01)
    pytest.fail(f"Worker never reached {expected}")


def finish(process):
    out, err = process.communicate(input=b"release", timeout=10)
    assert process.returncode == 0, (out, err)


def test_application_imports_without_unix_only_modules(workers, tmp_path):
    process, ready = workers("imports", tmp_path / "unused")
    out, err = process.communicate(timeout=15)
    assert process.returncode == 0, (out, err)
    assert ready.read_text(encoding="utf-8") == "IMPORTED"


def test_windows_journal_write_without_directory_fd(sim, monkeypatch):
    monkeypatch.setattr(em, "WINDOWS", True)
    with monkeypatch.context() as platform:
        platform.delattr(em.os, "O_DIRECTORY", raising=False)
        path = em.TRADES_DIR / "journal.json"
        em._atomic_write(path, {"message": "Windows journal ✓"})
        assert json.loads(path.read_text(encoding="utf-8")) == {"message": "Windows journal ✓"}
        em._atomic_write(path, {"replacement": True})
        assert json.loads(path.read_text(encoding="utf-8")) == {"replacement": True}
        assert not list(path.parent.glob(".pending-*"))


def test_file_is_fsynced_before_windows_replace(sim, monkeypatch):
    monkeypatch.setattr(em, "WINDOWS", True)
    calls = []
    fsync, replace = em.os.fsync, em.os.replace

    def sync(fd):
        calls.append("sync")
        fsync(fd)

    def move(src, dst):
        calls.append("replace")
        replace(src, dst)

    monkeypatch.setattr(em.os, "fsync", sync)
    monkeypatch.setattr(em.os, "replace", move)
    em._atomic_write(em.TRADES_DIR / "write.json", {"saved": True})
    assert calls == ["sync", "replace"]


def test_real_directory_sync_errors_are_not_suppressed(sim, monkeypatch):
    if sys.platform == "win32":
        # Windows has no directory-fsync contract; file flush failures still
        # propagate through _atomic_write, as checked below.
        monkeypatch.setattr(em.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("IO")))
        with pytest.raises(OSError, match="IO"):
            em._atomic_write(em.TRADES_DIR / "write.json", {})
    else:

        def fail(path, flags):
            raise PermissionError("directory IO error")

        monkeypatch.setattr(em.os, "open", fail)
        with pytest.raises(PermissionError, match="directory IO"):
            em._sync_directory(em.TRADES_DIR.parent)


def test_instance_lock_stays_held_until_explicit_release(workers, tmp_path):
    path = tmp_path / "instance.lock"
    first, ready = workers("instance", path)
    wait_for(first, ready, "HELD")
    gc.collect()
    with pytest.raises(FileExistsError):
        ProcessLock(path)
    second, rejected = workers("instance", path)
    out, err = second.communicate(timeout=10)
    assert second.returncode == 3, (out, err)
    assert rejected.read_text(encoding="utf-8") == "BLOCKED"
    assert first.poll() is None
    finish(first)
    with ProcessLock(path) as acquired:
        assert not acquired.fp.closed
        assert not os.get_inheritable(acquired.fp.fileno())
    assert path.exists()  # A persistent file is not a stale lock.


@pytest.mark.parametrize("mode", ["instance", "operation"])
def test_os_releases_locks_after_forceful_process_death(workers, tmp_path, mode):
    path = tmp_path / f"{mode}.lock"
    first, ready = workers(mode, path)
    wait_for(first, ready, "HELD")
    first.kill()  # TerminateProcess on Windows; SIGKILL on POSIX. No finally runs.
    first.communicate(timeout=10)
    assert first.returncode != 0
    assert path.exists()
    second, acquired = workers(mode, path)
    wait_for(second, acquired, "HELD")
    finish(second)


def test_operation_lock_blocks_other_process_for_entire_scope(workers, tmp_path):
    path = tmp_path / "operation.lock"
    first, ready = workers("operation", path)
    wait_for(first, ready, "HELD")
    second, waiting = workers("operation", path)
    wait_for(second, waiting, "WAITING")
    time.sleep(0.15)
    assert waiting.read_text(encoding="utf-8") == "WAITING"
    assert second.poll() is None
    finish(first)
    wait_for(second, waiting, "HELD")
    finish(second)


def test_instance_lock_failure_closes_handle(tmp_path, monkeypatch):
    path = tmp_path / "instance.lock"
    opened = []
    original = Path.open

    def capture(self, *args, **kwargs):
        fp = original(self, *args, **kwargs)
        opened.append(fp)
        return fp

    def fail(fp, flags):
        raise OSError("lock backend failed")

    monkeypatch.setattr(Path, "open", capture)
    monkeypatch.setattr(file_locking.portalocker, "lock", fail)
    with pytest.raises(OSError, match="backend failed"):
        ProcessLock(path)
    assert len(opened) == 1 and opened[0].closed


def test_unlock_failure_still_closes_and_releases_os_handle(tmp_path, monkeypatch):
    path = tmp_path / "instance.lock"
    lock = ProcessLock(path)
    with monkeypatch.context() as backend:

        def fail(fp):
            raise OSError("unlock backend failed")

        backend.setattr(file_locking.portalocker, "unlock", fail)
        with pytest.raises(OSError, match="unlock backend failed"):
            lock.release()
    assert lock.fp.closed
    with ProcessLock(path):
        pass
    lock.release()  # Repeated cleanup is harmless.


def test_lock_uses_exclusive_nonblocking_flags_for_instances(tmp_path, monkeypatch):
    observed = []
    original = portalocker.lock

    def capture(fp, flags):
        observed.append(flags)
        return original(fp, flags)

    monkeypatch.setattr(portalocker, "lock", capture)
    with ProcessLock(tmp_path / "instance.lock"):
        pass
    with ProcessLock(tmp_path / "operation.lock", blocking=True):
        pass
    assert observed == [portalocker.LOCK_EX | portalocker.LOCK_NB, portalocker.LOCK_EX]


def replacement_denied(code):
    error = PermissionError(13, "synthetic Windows replacement denial")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32, 33])
def test_transient_windows_replacement_retries_same_synced_file(sim, monkeypatch, code):
    path = em.TRADES_DIR / "retry.json"
    em._atomic_write(path, {"old": True})
    old = path.read_bytes()
    calls, sleeps = [], []
    original = em.os.replace
    monkeypatch.setattr(em, "WINDOWS", True)
    monkeypatch.setattr(em.time, "sleep", sleeps.append)

    def retry(source, destination):
        calls.append((source, destination))
        assert destination == path
        assert Path(source).read_bytes()  # The already-fsynced replacement remains intact.
        if len(calls) <= 3:
            assert path.read_bytes() == old  # No truncate/delete fallback is allowed.
            raise replacement_denied(code)
        original(source, destination)

    monkeypatch.setattr(em.os, "replace", retry)
    em._atomic_write(path, {"new": True})
    assert len(calls) == 4 and len(set(str(source) for source, _ in calls)) == 1
    assert sleeps == [0.025, 0.05, 0.1]
    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}
    assert not list(path.parent.glob(".pending-*"))


def test_persistent_windows_denial_preserves_old_journal_and_raises(sim, monkeypatch):
    path = em.TRADES_DIR / "preserved.json"
    em._atomic_write(path, {"durable": "old"})
    old = path.read_bytes()
    calls, sleeps = [], []
    error = replacement_denied(5)
    monkeypatch.setattr(em, "WINDOWS", True)
    monkeypatch.setattr(em.time, "sleep", sleeps.append)

    def fail(source, destination):
        calls.append(source)
        raise error

    monkeypatch.setattr(em.os, "replace", fail)
    with pytest.raises(PermissionError) as result:
        em._atomic_write(path, {"must_not_replace": True})
    assert result.value is error
    assert len(calls) == em.JOURNAL_REPLACE_ATTEMPTS
    assert len(sleeps) == em.JOURNAL_REPLACE_ATTEMPTS - 1
    assert sum(sleeps) < 1
    assert path.read_bytes() == old
    assert not list(path.parent.glob(".pending-*"))


@pytest.mark.parametrize("windows,code", [(False, 5), (True, 2), (True, 112), (True, None)])
def test_other_replace_errors_fail_immediately(sim, monkeypatch, windows, code):
    error = replacement_denied(code)
    calls, sleeps = [], []
    monkeypatch.setattr(em, "WINDOWS", windows)
    monkeypatch.setattr(em.time, "sleep", sleeps.append)

    def fail(source, destination):
        calls.append(source)
        raise error

    monkeypatch.setattr(em.os, "replace", fail)
    with pytest.raises(PermissionError) as result:
        em._atomic_write(em.TRADES_DIR / "failure.json", {})
    assert result.value is error
    assert len(calls) == 1 and not sleeps


def test_unpersisted_journal_cannot_submit_a_buy(sim, monkeypatch):
    from test_bot import approved, sig

    import trade_assistant as ta

    plan = ta.build_plan(sig())
    monkeypatch.setattr(em, "WINDOWS", True)
    monkeypatch.setattr(em.time, "sleep", lambda delay: None)

    def fail(source, destination):
        raise replacement_denied(5)

    monkeypatch.setattr(em.os, "replace", fail)
    with pytest.raises(PermissionError):
        ta.execute(plan, approval=approved(plan))
    assert not sim.writes


def test_transient_windows_denials_preserve_durable_ids_through_kill(sim, monkeypatch):
    from test_bot import approved, sig

    import trade_assistant as ta

    original = em.os.replace
    denied = set()
    sleeps = []
    monkeypatch.setattr(em, "WINDOWS", True)
    monkeypatch.setattr(em.time, "sleep", sleeps.append)
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)

    def retry(source, destination):
        if str(source) not in denied:
            denied.add(str(source))
            raise replacement_denied(5)
        original(source, destination)

    def verify_before_request(kind, data):
        saved = em.load_trades()[0]
        if kind == "order":
            assert any(
                o["cid"] == data["newClientOrderId"] and o["submitted"] for o in saved["orders"]
            )
        if kind == "oco":
            assert any(o["cid"] == data["listClientOrderId"] for o in saved["oco"])
            assert {data["aboveClientOrderId"], data["belowClientOrderId"]} <= {
                o["cid"] for o in saved["orders"]
            }
        if kind == "cancel_list":
            assert any(o.get("cancel_cid") == data["newClientOrderId"] for o in saved["oco"])

    monkeypatch.setattr(em.os, "replace", retry)
    sim.prewrite = verify_before_request
    sim.free["FOO"] = em.D(5)
    plan = ta.build_plan(sig())
    ta.execute(plan, approval=approved(plan))
    em.close_bot_trade("FOOUSDT", "CLOSE_FOOUSDT")
    assert em.load_trades()[0]["state"] == "closed"
    assert sim.free["FOO"] == 5
    assert (
        sum(
            data.get("side") == "BUY"
            for method, path, data in sim.writes
            if method == "post" and path == "order"
        )
        == 1
    )
    assert sleeps and set(sleeps) == {0.025}
