"""Health reads cannot trade, repair a journal, or mistake stale evidence for OK."""

import copy
from contextlib import ExitStack

import pytest
from test_bot import approved, sig
from test_fractional_dust import fill_pepe_exits, replay_pepe
from test_fractional_dust import market as market

import bot_health as health
import exit_manager as em
import runtime_health as runtime
import telegram_assistant as tg
import trade_assistant as ta
import trading_settings as settings


def buy():
    plan = ta.build_plan(sig())
    return ta.execute(plan, approval=approved(plan), monitor=False)


def files():
    return {str(path): path.read_bytes() for path in em.STATE_DIR.rglob("*.json")}


def message(command="/health", *, sender=None, chat=None):
    return {
        "from": {"id": tg.CHAT_ID if sender is None else sender},
        "chat": {"id": tg.CHAT_ID if chat is None else chat},
        "text": command,
    }


def test_idle_health_has_fresh_verified_reads_without_changing_any_state(sim, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Health may not run recovery/planning or write a journal")

    for name in (
        "_atomic_write",
        "operation_lock",
        "reconcile_trade",
        "refresh",
        "submit_order",
        "cancel_protection",
    ):
        monkeypatch.setattr(em, name, forbidden)
    monkeypatch.setattr(ta, "build_plan", forbidden)
    before = files()
    report = health.report(auto_scan=False)
    assert "Health: OK" in report and "Exchange check: OK" in report
    assert "Position monitoring: not needed" in report and "Automatic scanner: OFF" in report
    assert "no account-wide blocker detected" in report and "fresh plan checks required" in report
    assert "not observed" not in report and "Dubai" in report
    assert "Normal daily risk budget remaining: 3.0000" in report
    assert files() == before and not sim.writes


def test_real_monitor_success_and_full_slots_are_separate_from_a_health_fault(sim, monkeypatch):
    rec = buy()
    reports = []

    def during_wait(seconds):
        before = files(), len(sim.writes)
        reports.append(health.report(auto_scan=False))
        assert (files(), len(sim.writes)) == before
        em.STOP.set()

    monkeypatch.setattr(em.STOP, "wait", during_wait)
    em.monitor_trade(rec)
    report = reports[0]
    assert "Health: OK" in report and "RUNNING (waiting" in report
    assert "[TRADE_LIMIT]" in report and "New entries (all modes): BLOCKED" in report
    assert "Last successful reconciliation: not observed" not in report
    assert not runtime.snapshot()["tasks"][-1]["running"]


def test_missing_monitor_is_reported_without_removing_the_position_or_stops(sim):
    buy()
    before = files(), len(sim.writes)
    report = health.report(auto_scan=False)
    assert "[MONITOR_STOPPED]" in report and "Health: NEEDS ATTENTION" in report
    assert "Last successful reconciliation: not observed" in report
    assert (files(), len(sim.writes)) == before and em.active_trades()


def test_failed_reconciliation_is_retrying_with_a_fixed_reason_not_a_false_success(
    sim, monkeypatch
):
    rec = buy()
    reports = []

    def unavailable(record):
        raise em.ex.Uncertain("API_SECRET=private", reason_code="BNB_REFERENCE_MISSING")

    def during_wait(seconds):
        reports.append(health.report(auto_scan=False))
        em.STOP.set()

    monkeypatch.setattr(em, "reconcile_trade", unavailable)
    monkeypatch.setattr(em.STOP, "wait", during_wait)
    em.monitor_trade(rec)
    report = reports[0]
    assert "RETRYING" in report and "[MONITOR_RETRY]" in report
    assert (
        "BNB_REFERENCE_MISSING" in report
        and "Last successful reconciliation: not observed" in report
    )
    assert "API_SECRET" not in report and "private" not in report


def test_exchange_failure_preserves_last_success_without_presenting_current_data_as_verified(
    sim, monkeypatch
):
    health.report(auto_scan=False)
    success = runtime.snapshot()["checks"]["exchange"]["success"]

    def unavailable(**kwargs):
        raise TimeoutError("https://host?signature=private&api_key=private")

    monkeypatch.setattr(sim.client, "get_open_orders", unavailable)
    report = health.report(auto_scan=False)
    assert runtime.snapshot()["checks"]["exchange"]["success"] == success
    assert "Exchange check: UNAVAILABLE" in report and "[EXCHANGE_UNAVAILABLE]" in report
    assert "New entries (all modes): NOT VERIFIED" in report and "Health: NEEDS ATTENTION" in report
    assert "USDT free:" not in report and "signature" not in report and "private" not in report


@pytest.mark.parametrize("field,value", [("uid", 99999), ("accountType", "MARGIN")])
def test_a_different_authenticated_account_cannot_be_reported_as_synchronized(sim, field, value):
    sim.response_override = lambda m, path, data: (
        {"uid": 12345, "accountType": "SPOT", "canTrade": True, field: value}
        if path == "account"
        else None
    )
    report = health.report(auto_scan=False)
    assert (
        "Exchange check: UNAVAILABLE" in report
        and "Last successful exchange check: not observed" in report
    )
    assert em.CONTEXT["account_uid"] == "12345" and not sim.writes


@pytest.mark.parametrize("broken", ["journal", "settings"])
def test_invalid_local_state_is_unavailable_not_zero_positions_or_default_limits(sim, broken):
    if broken == "journal":
        path = em.TRADES_DIR / "invalid.json"
    else:
        path = em.TRADES_DIR.parent / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"API_SECRET":"private"}')
    report = health.report(auto_scan=False)
    assert f"[{broken.upper()}_INVALID]" in report and "Health: NEEDS ATTENTION" in report
    assert "New entries (all modes): BLOCKED" in report and "private" not in report
    assert (
        ("Active/unresolved bot trades: 0" not in report)
        if broken == "journal"
        else ("Limits:" not in report)
    )


@pytest.mark.parametrize("change", ["missing", "level", "external"])
def test_live_order_mismatches_and_external_orders_are_reported_without_touching_them(sim, change):
    rec = buy()
    opened = sim.client.get_open_orders()
    if change == "missing":
        opened.pop()
        code = "ORDER_MISSING"
    elif change == "level":
        opened[0]["price"] = "123"
        code = "ORDER_CHANGED"
    else:
        extra = copy.deepcopy(opened[0])
        extra.update(orderId=999999, clientOrderId="external")
        opened.append(extra)
        code = "EXTERNAL_ORDERS"
    sim.response_override = lambda m, path, data: opened if path == "openOrders" else None
    before = files(), len(sim.writes)
    with runtime.worker("monitor", rec["id"]) as task:
        task.success()
        report = health.report(auto_scan=False)
    assert f"[{code}]" in report and "Health: NEEDS ATTENTION" in report
    assert (files(), len(sim.writes)) == before


def test_an_order_fill_racing_with_health_is_labeled_changing_not_external_ownership(
    sim, monkeypatch
):
    rec = buy()
    original = health.exchange_check

    def while_checking():
        probe = original()
        rec["state"] = "closing"
        em.save_trade(rec)
        return probe

    monkeypatch.setattr(health, "exchange_check", while_checking)
    report = health.report(auto_scan=False)
    assert "[STATE_CHANGING]" in report and "Health: NEEDS ATTENTION" in report
    assert "ORDER_CHANGED" not in report and "EXTERNAL_ORDERS" not in report


def test_result_delivery_metadata_does_not_create_a_false_lifecycle_change(sim, monkeypatch):
    rec = buy()
    original = health.exchange_check

    def while_checking():
        probe = original()
        rec["result_notices"] = {"fills:synthetic": {"text": "delivered", "sent": True}}
        em.save_trade(rec)
        return probe

    monkeypatch.setattr(health, "exchange_check", while_checking)
    with runtime.worker("monitor", rec["id"]) as task:
        task.success()
        report = health.report(auto_scan=False)
    assert "STATE_CHANGING" not in report and "Health: OK" in report


def test_retained_dust_is_not_an_unresolved_position_or_a_health_failure(sim, market):
    rec = replay_pepe(sim)
    fill_pepe_exits(sim, rec)
    em.reconcile_trade(rec)
    before = files(), len(sim.writes)
    report = health.report(auto_scan=False)
    assert "Health: OK" in report and "Active/unresolved bot trades: 0 / 2" in report
    assert "Retained dust — no active slots used:\nPEPE: 0.05" in report
    assert "RECONCILIATION_PENDING" not in report and "MONITOR_STOPPED" not in report
    assert (files(), len(sim.writes)) == before


def test_a_live_fractional_tail_has_coverage_without_becoming_a_recovery_fault(sim, market):
    rec = replay_pepe(sim)
    with runtime.worker("monitor", rec["id"]) as task:
        task.success()
        report = health.report(auto_scan=False)
    assert "Health: OK" in report and "Below-step fraction retained: 0.05 PEPE" in report
    assert "RECONCILIATION_PENDING" not in report and "ORDER_MISSING" not in report


def test_exhausted_normal_risk_is_distinct_from_manual_override_and_other_gates(sim):
    settings.update("max_open_trades", 2, expected_revision=0, actor="offline-owner")
    rec = buy()
    rec["plan"]["loss"] = "3"
    em.save_trade(rec)
    with runtime.worker("monitor", rec["id"]) as task:
        task.success()
        report = health.report(auto_scan=False)
    assert "[DAILY_RISK]" in report and "Normal entries: BLOCKED" in report
    assert "New entries (all modes): no account-wide blocker detected" in report
    assert "Manual retains its explicit risk override" in report


@pytest.mark.parametrize("failure", ["funds", "permission", "live", "shutdown"])
def test_hard_entry_gates_are_named(sim, monkeypatch, failure):
    if failure == "funds":
        sim.free["USDT"] = em.D(0)
        code = "FUNDS_UNAVAILABLE"
    elif failure == "permission":
        sim.response_override = lambda m, path, data: (
            {"uid": 12345, "accountType": "SPOT", "canTrade": False} if path == "account" else None
        )
        code = "ACCOUNT_PERMISSION"
    elif failure == "live":
        monkeypatch.setattr(em, "USE_TESTNET", False)
        em.CONTEXT["environment"] = "live"
        code = "LIVE_DISABLED"
    else:
        em.STOP.set()
        code = "SHUTTING_DOWN"
    report = health.report(auto_scan=False)
    assert f"[{code}]" in report and "New entries (all modes): BLOCKED" in report
    assert not sim.writes


def test_enabled_scanner_needs_a_running_worker_and_failures_are_visible(sim):
    assert "[SCANNER_STOPPED]" in health.report(auto_scan=True)
    with runtime.worker("scanner") as task:
        task.attempt()
        task.success()
        assert "Health: OK" in health.report(auto_scan=True)
        task.failure(TimeoutError("API_SECRET=private"))
        report = health.report(auto_scan=True)
        assert "[SCANNER_ERROR]" in report and "Automatic scanner: RETRYING" in report
        assert "private" not in report


def test_real_scanner_loop_records_the_completed_cycle_and_then_stops(sim, monkeypatch):
    reports = []
    monkeypatch.setattr(tg, "scan_once", lambda: None)

    def after_cycle(seconds):
        reports.append(health.report(auto_scan=True))
        em.STOP.set()

    monkeypatch.setattr(em.STOP, "wait", after_cycle)
    tg.scanner_loop()
    assert "Health: OK" in reports[0] and "last successful cycle not observed" not in reports[0]
    assert not runtime.snapshot()["tasks"][-1]["running"] and not sim.writes


def test_scanner_stalled_beyond_its_normal_interval_is_not_falsely_healthy(sim, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])
    with runtime.worker("scanner") as task:
        task.success()
        now[0] += 1210
        report = health.report(auto_scan=True)
        assert (
            "[SCANNER_DELAYED]" in report and "Automatic scanner: DELAYED — thread alive" in report
        )


def test_an_exit_monitor_starting_cannot_hide_the_same_trades_previous_success(sim):
    rec = buy()
    with runtime.worker("monitor", rec["id"]) as first:
        first.success()
    success = runtime.snapshot()["tasks"][-1]["success"]
    with runtime.worker("monitor", rec["id"]):
        report = health.report(auto_scan=False)
        assert "Last successful reconciliation: " + health.timestamp(success) in report


def test_worker_crash_remains_visible_and_restart_clears_only_observations(sim):
    with pytest.raises(RuntimeError):
        with runtime.worker("scanner"):
            raise RuntimeError("API_SECRET=private")
    observed = runtime.snapshot()["tasks"][-1]
    assert not observed["running"] and "private" not in observed["error"]
    report = health.report(auto_scan=True)
    assert "[SCANNER_STOPPED]" in report and "Health: NEEDS ATTENTION" in report


def test_slow_monitor_is_not_mistaken_for_a_dead_thread_and_elapsed_uses_monotonic_time(
    sim, monkeypatch
):
    rec = buy()
    now = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])
    with runtime.worker("monitor", rec["id"]) as task:
        task.attempt()
        now[0] += 181
        report = health.report(auto_scan=False)
        assert "DELAYED — thread alive" in report and "[MONITOR_DELAYED]" in report
        assert "MONITOR_STOPPED" not in report


@pytest.mark.parametrize("field", ["sender", "chat"])
def test_health_is_owner_only(sim, monkeypatch, field):
    monkeypatch.setattr(health, "report", lambda **kwargs: pytest.fail("Unauthorized read"))
    monkeypatch.setattr(tg, "say", lambda *args, **kwargs: pytest.fail("Unauthorized reply"))
    tg.handle_message(message(**{field: "stranger"}))
    assert not sim.writes


def test_command_preserves_pending_approval_forms_and_chunks_a_large_report(sim, monkeypatch):
    captured = []
    tg.pending["existing"] = {"approved": False}
    tg.state.update(step="manual_levels", symbol="FOOUSDT")
    state = copy.deepcopy((tg.pending, tg.state))
    monkeypatch.setattr(tg, "say", lambda text, **kwargs: captured.append(text))
    monkeypatch.setattr(health, "report", lambda **kwargs: "a" * 7200)
    tg.handle_message(message("/health@NexvantBot"))
    assert list(map(len, captured)) == [3500, 3500, 200]
    assert (tg.pending, tg.state) == state and not sim.writes


def test_process_restart_does_not_reuse_old_worker_or_exchange_success_observations(sim):
    with runtime.worker("scanner") as task:
        task.success()
    health.report(auto_scan=False)
    runtime.reset()
    observed = runtime.snapshot()
    assert not observed["tasks"] and not observed["checks"]
    assert "[SCANNER_STOPPED]" in health.report(auto_scan=True)


def test_many_active_positions_have_independent_monitor_states(sim):
    # Registry identity stays the immutable record ID, not a reusable ticker.
    with ExitStack() as stack:
        first = stack.enter_context(runtime.worker("monitor", "first"))
        second = stack.enter_context(runtime.worker("monitor", "second"))
        first.success()
        second.failure(TimeoutError("private"))
        lines, issues = health.monitor_lines(
            [{"id": "first", "symbol": "FOOUSDT"}, {"id": "second", "symbol": "BARUSDT"}],
            runtime.snapshot(),
        )
    report = "\n".join(lines)
    assert "FOOUSDT | first: RUNNING" in report and "BARUSDT | second: RETRYING" in report
    assert any("MONITOR_RETRY" in issue for issue in issues) and "private" not in report
