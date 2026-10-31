"""Audit regressions: real SDK interfaces, fault injection and inventory invariants."""

import time
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from binance.client import Client
from simulator import Crash
from test_bot import approved, execute, sig

import entry_scanner as sc
import exchange as ex
import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta


def paused(sim, monkeypatch):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    return execute(ta.build_plan(sig()))


def test_01_actual_sdk_contract(sim, monkeypatch):
    ex.verify_sdk()
    assert not hasattr(Client, "get_order_list")
    assert not hasattr(Client, "cancel_order_list")
    rec = paused(sim, monkeypatch)
    ex.order_list(sim.client, "FOOUSDT", rec["oco"][0]["cid"])
    em.cancel_protection(rec, rec["oco"][0])
    assert any(method == "delete" and path == "orderList" for method, path, _ in sim.writes)


@pytest.mark.parametrize("status", ["partial", "expired", "rejected", "timeout_accept", "new"])
def test_02_all_buy_outcomes_accounted(sim, monkeypatch, status):
    sim.buy_behavior = status
    rec = paused(sim, monkeypatch)
    em.recover_trades()
    assert sum(data.get("side") == "BUY" for _, path, data in sim.writes if path == "order") == 1
    for order in rec["orders"]:
        assert order["status"] != "UNKNOWN"
    stored = em.load_trades()[0]
    if status in ("partial", "expired"):
        assert any(o["trades"] for o in stored["orders"] if o["purpose"] == "buy")
        assert em.inventory(stored) == 0
    if status in ("rejected", "new"):
        assert stored["state"] == "closed"
    if status == "timeout_accept":
        assert em.coverage(stored) == em.inventory(stored)


def test_03_tp1_then_kill_preserves_unrelated_holdings(sim, monkeypatch):
    sim.free["FOO"] = D(5)
    paused(sim, monkeypatch)
    first = sim.lists[1]["orders"][0]["orderId"]
    sim.fill(sim.orders[first], price="104.5")
    em.close_bot_trade("FOOUSDT", "CLOSE_FOOUSDT")
    assert sim.free["FOO"] == 5 and sim.locked["FOO"] == 0
    assert em.load_trades()[0]["state"] == "closed"


def test_03_first_oco_fills_second_rejected_preserves_holdings(sim, monkeypatch):
    sim.free["FOO"] = D(5)
    sim.oco_behavior = {1: "filled", 2: "rejected"}
    rec = paused(sim, monkeypatch)
    em.reconcile_trade(rec)
    assert sim.free["FOO"] == 5
    assert em.inventory(rec) == 0


def test_04_every_submission_has_durable_ids(sim, monkeypatch):
    def before(kind, data):
        records = em.load_trades()
        assert len(records) == 1
        saved = records[0]
        if kind == "order":
            assert any(
                o["cid"] == data["newClientOrderId"] and o["submitted"] for o in saved["orders"]
            )
        elif kind == "oco":
            assert any(o["cid"] == data["listClientOrderId"] for o in saved["oco"])
            assert {data["aboveClientOrderId"], data["belowClientOrderId"]} <= {
                o["cid"] for o in saved["orders"]
            }
        elif kind == "cancel":
            assert saved["cancel_buy_requested"]
        elif kind == "cancel_list":
            assert any(o.get("cancel_requested") for o in saved["oco"])

    sim.prewrite = before
    rec = paused(sim, monkeypatch)
    em.close_bot_trade("FOOUSDT", "CLOSE_FOOUSDT")
    assert rec["approval_id"]


@pytest.mark.parametrize("phase", ["buy", "oco", "emergency"])
def test_05_accepted_timeout_never_duplicates(sim, monkeypatch, phase):
    if phase == "buy":
        sim.buy_behavior = "timeout_accept"
    if phase == "oco":
        sim.oco_behavior[1] = "timeout_accept"
    if phase == "emergency":
        sim.oco_behavior[1] = "rejected"
        sim.sell_behavior = "timeout_accept"
    paused(sim, monkeypatch)
    for _ in range(3):
        em.recover_trades()
    buys = [
        d["newClientOrderId"] for _, p, d in sim.writes if p == "order" and d.get("side") == "BUY"
    ]
    sells = [
        d["newClientOrderId"] for _, p, d in sim.writes if p == "order" and d["side"] == "SELL"
    ]
    lists = [d["listClientOrderId"] for _, p, d in sim.writes if p == "orderList/oco"]
    assert len(buys) == 1
    assert len(sells) == len(set(sells))
    assert len(lists) == len(set(lists))
    assert em.inventory(em.load_trades()[0]) >= 0


@pytest.mark.parametrize("behavior", ["crash", "crash_before_fill"])
def test_06_restart_after_buy_acceptance(sim, monkeypatch, behavior):
    sim.free["FOO"] = D(5)
    sim.buy_behavior = behavior
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    with pytest.raises(Crash):
        execute(ta.build_plan(sig()))
    em.recover_trades()
    assert sum(d.get("side") == "BUY" for _, p, d in sim.writes if p == "order") == 1
    assert sim.free["FOO"] == 5
    assert em.load_trades()[0]["state"] == "closed"


@pytest.mark.parametrize("checkpoint", range(1, 23))
def test_06_crash_after_journal_transition(sim, monkeypatch, checkpoint):
    sim.free["FOO"] = D(5)
    save = em.save_trade
    calls = 0

    def crash(rec):
        nonlocal calls
        save(rec)
        calls += 1
        if calls == checkpoint:
            raise Crash()

    monkeypatch.setattr(em, "save_trade", crash)
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    try:
        execute(ta.build_plan(sig()))
    except Crash:
        pass
    monkeypatch.setattr(em, "save_trade", save)
    for _ in range(2):
        em.recover_trades()
    assert sim.free["FOO"] >= 5
    assert sum(d.get("side") == "BUY" for _, p, d in sim.writes if p == "order") <= 1
    # Unknown pre-transmission intents are deliberately quarantined, never retried.
    assert len({d["newClientOrderId"] for _, p, d in sim.writes if p == "order"}) == len(
        [1 for _, p, d in sim.writes if p == "order"]
    )


def test_06_accounts_and_environments_do_not_share_records(sim, monkeypatch):
    rec = paused(sim, monkeypatch)
    first = em.TRADES_DIR
    monkeypatch.setattr(em, "CONTEXT", None)
    monkeypatch.setattr(em, "TRADES_DIR", None)
    sim.uid = "67890"
    em.initialize()
    assert em.TRADES_DIR != first and not em.load_trades()
    with pytest.raises(ex.OwnershipError):
        em.save_trade(rec)
    monkeypatch.setattr(em, "CONTEXT", None)
    monkeypatch.setattr(em, "TRADES_DIR", None)
    monkeypatch.setattr(em, "USE_TESTNET", False)
    em.initialize()
    assert "live" in str(em.TRADES_DIR)
    assert not em.load_trades()


def test_06_ownership_mismatch_blocks_cancel_and_sell(sim, monkeypatch):
    paused(sim, monkeypatch)
    sim.lists[1]["listClientOrderId"] = "unrelated"
    before = len(sim.writes)
    with pytest.raises(Exception):
        em.close_bot_trade("FOOUSDT", "CLOSE_FOOUSDT")
    assert len(sim.writes) == before


def test_07_missing_accounting_fails_closed(sim, monkeypatch):
    sim.fulfill = True
    rec = execute(ta.build_plan(sig()))
    rec.pop("accounted")
    em.save_trade(rec)
    with pytest.raises(RuntimeError, match="journal invalid"):
        em.todays_loss()


def test_07_exchange_timestamp_and_commission_accounting(sim, monkeypatch):
    sim.clock = int(datetime(2026, 1, 1, 23, 59, tzinfo=timezone.utc).timestamp() * 1000)
    rec = paused(sim, monkeypatch)
    for item in sim.lists.values():
        sim.fill(sim.orders[item["orders"][1]["orderId"]], price="97")
    em.reconcile_trade(rec)
    evidence = em.realized_events(rec)
    assert all(event["time"] == sim.clock for event in evidence)
    buys = list(em.executions(rec, "BUY"))
    sells = list(em.executions(rec, "SELL"))
    expected = sum((em.D(t["quoteQty"]) - em.D(t["commission"]) for t in sells), D(0)) - sum(
        (em.D(t["quoteQty"]) + em.D(t["commission"]) for t in buys), D(0)
    )
    assert sum(em.D(e["pnl"]) for e in evidence) == expected
    # Reconciliation today must not move historic fills into today's loss budget.
    assert em.todays_loss() == 0


def test_07_third_asset_commissions_block_new_entries(sim, monkeypatch):
    rec = paused(sim, monkeypatch)
    sim.fee_asset = "BNB"
    for item in sim.lists.values():
        sim.fill(sim.orders[item["orders"][0]["orderId"]], price="104.5")
    with pytest.raises(ex.Uncertain, match="Third-asset"):
        em.reconcile_trade(rec)
    with pytest.raises(ta.ActiveTradeError):
        ta.build_plan(sig())


def test_07_daily_budget_limits_prospective_risk(sim, monkeypatch):
    monkeypatch.setattr(em, "todays_loss", lambda: D("2.9"))
    plan = ta.build_plan(sig())
    assert plan["loss"] <= D(".1")
    monkeypatch.setattr(em, "todays_loss", lambda: D("2.99999"))
    with pytest.raises(ValueError):
        ta.build_plan(sig())


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1"])
def test_07_invalid_risk_evidence_rejected(sim, monkeypatch, value):
    sim.fulfill = True
    rec = execute(ta.build_plan(sig()))
    rec["baseline_base"] = value
    em.save_trade(rec)
    with pytest.raises(RuntimeError):
        em.todays_loss()


def test_08_skip_when_two_ocos_do_not_fit_cap(sim):
    sim.min_notional = "10"
    with pytest.raises(ValueError, match="Configured size.*minimums"):
        ta.build_plan(sig())
    assert not sim.writes


def test_08_fee_and_rounding_preflight_rejects_before_buy(sim):
    sim.min_notional = "5.6"
    sim.fee_rate = D(".049")
    with pytest.raises(ValueError):
        ta.build_plan(sig())
    assert not sim.writes


def test_08_unknown_symbol_filter_rejected(sim, monkeypatch):
    original = sim.info

    def info():
        result = original()
        result["filters"].append({"filterType": "NEW_UNSUPPORTED_FILTER"})
        return result

    monkeypatch.setattr(sim, "info", info)
    with pytest.raises(ValueError, match="Unsupported"):
        ta.build_plan(sig())


def test_08_liquidation_does_not_add_entry_notional_margin(sim):
    f = em.symbol_filters("FOOUSDT")
    em.validate_qty(".01", f, 100, market=True)
    with pytest.raises(ValueError):
        em.validate_qty(".01", f, 100, market=True, entry=True)


def test_09_monitor_recovers_after_more_than_three_errors(sim, monkeypatch):
    monitor = em.monitor_trade
    rec = paused(sim, monkeypatch)
    sim.read_failures = 5
    original = em.reconcile_trade
    checks = 0

    def check(rec):
        nonlocal checks
        checks += 1
        if checks == 6:
            for item in sim.lists.values():
                sim.fill(sim.orders[item["orders"][0]["orderId"]], price="104.5")
        return original(rec)

    monkeypatch.setattr(em, "reconcile_trade", check)
    monitor(rec, max_checks=8)
    assert checks >= 6 and rec["state"] == "closed"


def test_09_partial_target_fill_is_accounted(sim, monkeypatch):
    rec = paused(sim, monkeypatch)
    first = sim.orders[sim.lists[1]["orders"][0]["orderId"]]
    sim.fill(first, qty=".02", price="104.5", terminal=False)
    em.refresh(rec)
    assert any(t["qty"] == ".02" or D(t["qty"]) == D(".02") for t in em.executions(rec, "SELL"))
    # Cancel only the uncovered list, then unwind its verified remaining quantity.
    em.reconcile_trade(rec)
    assert em.inventory(rec) == em.coverage(rec)
    assert sim.lists[2]["listOrderStatus"] == "EXECUTING"


def test_10_status_before_client_initialization(sim, monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "say", lambda text, **kw: calls.append(text))
    tg.handle_message({"chat": {"id": tg.CHAT_ID}, "from": {"id": tg.CHAT_ID}, "text": "/status"})
    assert "USDT free: 500" in calls[0]
    assert "No active bot positions" in calls[0]


def test_10_cancel_invalidates_pending_proposals(sim, monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "tg", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(tg, "say", lambda text, **kwargs: calls.append(text))
    tg.pending["pending"] = {"mid": 1, "text": "proposal"}
    tg.handle_message({"chat": {"id": tg.CHAT_ID}, "from": {"id": tg.CHAT_ID}, "text": "/cancel"})
    tg.handle_callback(
        {
            "chat": {},
            "from": {"id": tg.CHAT_ID},
            "message": {"chat": {"id": tg.CHAT_ID}},
            "id": "cb",
            "data": "A:pending",
        }
    )
    assert not tg.pending and not sim.writes
    assert "Already submitted orders" in calls[0]


@pytest.mark.parametrize("field", ["qty", "cost", "loss"])
def test_10_approval_never_exceeds_displayed_limits(sim, field):
    plan = ta.build_plan(sig())
    changed = {**plan, field: plan[field] + D(".00001")}
    assert not ta.approval_bounds(plan, changed)


def test_10_no_entry_without_explicit_approval(sim):
    plan = ta.build_plan(sig())
    with pytest.raises(ValueError, match="approval"):
        ta.execute(plan)
    assert not sim.writes


def test_10_approval_replay_and_expiry_rejected(sim):
    sim.fulfill = True
    plan = ta.build_plan(sig())
    approval = approved(plan)
    ta.execute(plan, approval=approval)
    before = len(sim.writes)
    with pytest.raises(ValueError, match="consumed"):
        ta.execute(plan, approval=approval)
    assert len(sim.writes) == before
    approval = approved(plan)
    approval["expires_at"] = time.time() - 1
    with pytest.raises(ValueError):
        ta.execute(plan, approval=approval)


@pytest.mark.parametrize("sender,chat", [("stranger", "owner"), ("owner", "stranger")])
def test_10_unauthorized_telegram_cannot_approve(sim, monkeypatch, sender, chat):
    monkeypatch.setattr(tg, "CHAT_ID", "owner")
    tg.pending["proposal"] = {"created": time.time()}
    tg.handle_callback(
        {
            "from": {"id": sender},
            "message": {"chat": {"id": chat}},
            "data": "A:proposal",
        }
    )
    assert "proposal" in tg.pending and not sim.writes


def test_10_sensitive_errors_are_not_logged(sim, monkeypatch, caplog):
    monkeypatch.setattr(
        sc.os,
        "getenv",
        lambda name, *args: "synthetic-token" if name == "TELEGRAM_TOKEN" else "synthetic-chat",
    )

    def error(*args, **kwargs):
        raise requests_error(
            "https://api.telegram.org/botsynthetic-token/sendMessage?api_key=synthetic-key&signature=synthetic-signature"
        )

    class requests_error(Exception):
        pass

    monkeypatch.setattr(sc.requests, "post", error)
    sc.send("Safe notification")
    assert "synthetic-token" not in caplog.text
    assert "synthetic-key" not in caplog.text
    assert "synthetic-signature" not in caplog.text


def test_scanner_rejects_insufficient_and_stale_candles(monkeypatch):
    monkeypatch.setattr(sc, "get", lambda *a, **kw: [])
    with pytest.raises(ValueError, match="Insufficient"):
        sc.klines("FOOUSDT", "15m")
    rows = [[i * 900000, "100", "101", "99", "100", "100", i * 900000 + 899999] for i in range(120)]
    monkeypatch.setattr(sc, "get", lambda *a, **kw: rows)
    with pytest.raises(ValueError, match="Stale"):
        sc.klines("FOOUSDT", "15m")


def test_entry_is_ioc_with_persisted_price_cap(sim, monkeypatch):
    rec = paused(sim, monkeypatch)
    request = next(d for m, p, d in sim.writes if m == "post" and p == "order")
    assert request["type"] == "LIMIT" and request["timeInForce"] == "IOC"
    assert D(request["price"]) <= D(rec["approved_bounds"]["price"])
    assert D(request["quantity"]) <= D(rec["approved_bounds"]["qty"])
    assert D(request["quantity"]) * D(request["price"]) * (1 + D(rec["plan"]["fee_rate"])) <= D(
        rec["approved_bounds"]["cost"]
    )


def test_changed_account_uid_blocks_new_submission(sim, monkeypatch):
    ta.build_plan(sig())
    sim.uid = "98765"
    with pytest.raises(RuntimeError, match="changed"):
        em.initialize()
    assert not sim.writes


def test_missing_execution_evidence_never_counts_as_zero_loss(sim):
    sim.fulfill = True
    rec = execute(ta.build_plan(sig()))
    rec["orders"][0]["trades"] = []
    em.save_trade(rec)
    with pytest.raises(RuntimeError, match="journal invalid"):
        em.todays_loss()


def test_canceled_oco_partial_execution_is_in_inventory(sim, monkeypatch):
    rec = paused(sim, monkeypatch)
    top = sim.orders[sim.lists[1]["orders"][0]["orderId"]]
    sim.fill(top, qty=".02", price="104.5", terminal=False)
    em.cancel_protection(rec, rec["oco"][0])
    em.refresh(rec)
    assert top["status"] == "CANCELED"
    assert sum((D(t["qty"]) for t in em.executions(rec, "SELL")), D(0)) == D(".02")
    em.reconcile_trade(rec)
    assert em.inventory(rec) == em.coverage(rec)


def test_unconfirmed_emergency_is_not_repeated(sim, monkeypatch):
    sim.oco_behavior[1] = "rejected"
    paused(sim, monkeypatch)
    sim.sell_behavior = "timeout_before"
    for _ in range(3):
        em.recover_trades()
    sells = [d for m, p, d in sim.writes if m == "post" and p == "order" and d["side"] == "SELL"]
    assert len(sells) == 1
    assert em.load_trades()[0]["state"] == "needs_attention"


def test_emergency_partial_fill_is_reconciled_before_any_next_exit(sim, monkeypatch):
    sim.oco_behavior[1] = "rejected"
    paused(sim, monkeypatch)
    sim.sell_behavior = "partial"
    em.recover_trades()
    before = len(sim.writes)
    em.recover_trades()
    assert len(sim.writes) == before
    assert any(
        D(o["executedQty"]) > 0 and o["purpose"] == "emergency"
        for o in em.load_trades()[0]["orders"]
    )


def test_unrelated_inventory_withdrawal_prevents_liquidation(sim, monkeypatch):
    sim.free["FOO"] = D(5)
    paused(sim, monkeypatch)
    # An external actor removes some of the nonbot inventory. The bot must not
    # compensate by selling whatever free balance happens to remain.
    sim.free["FOO"] = D(4)
    with pytest.raises(ex.Uncertain):
        em.close_bot_trade("FOOUSDT", "CLOSE_FOOUSDT")
    assert not any(
        d.get("side") == "SELL" for m, p, d in sim.writes if m == "post" and p == "order"
    )


def test_forged_protective_order_level_blocks_coverage(sim, monkeypatch):
    rec = paused(sim, monkeypatch)
    sim.orders[sim.lists[1]["orders"][1]["orderId"]]["stopPrice"] = "1"
    with pytest.raises(ex.OwnershipError, match="level mismatch"):
        em.refresh(rec)


def test_cancel_while_telegram_send_is_in_flight_invalidates_proposal(sim, monkeypatch):
    plan = ta.build_plan(sig())
    monkeypatch.setattr(tg, "edit", lambda *a: None)

    def send(*args, **kwargs):
        tg.proposal_generation += 1
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(tg, "say", send)
    tg.send_proposal(sig(), plan, "")
    assert not tg.pending


def test_runtime_start_failure_stops_recovery_workers_and_releases_lock(sim, monkeypatch):
    import threading

    monkeypatch.setattr(tg, "TOKEN", "synthetic")
    monkeypatch.setattr(tg, "CHAT_ID", "synthetic")
    worker = threading.Thread(target=lambda: em.STOP.wait(), daemon=False)

    def fail():
        tg.workers.add(worker)
        worker.start()
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(tg, "_run_main", fail)
    with pytest.raises(RuntimeError):
        tg.main()
    assert not worker.is_alive()
    em.instance_lock().unlink()


def test_bnb_discount_skips_before_buy(sim):
    def override(method, path, data):
        if path == "account/commission":
            return {
                "symbol": "FOOUSDT",
                "discount": {"enabledForAccount": True, "enabledForSymbol": True},
            }

    sim.response_override = override
    with pytest.raises(ValueError, match="BNB-discount"):
        ta.build_plan(sig())
    assert not sim.writes


def test_stale_monitor_cannot_overwrite_a_completed_kill(sim, monkeypatch):
    stale = paused(sim, monkeypatch)
    em.close_bot_trade("FOOUSDT", "CLOSE_FOOUSDT")
    before = len(sim.writes)
    assert em.reconcile_trade(stale)
    assert stale["state"] == "closed" and len(sim.writes) == before


def test_two_simultaneous_approvals_cannot_create_two_buys(sim, monkeypatch):
    import threading

    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    plan = ta.build_plan(sig())
    outcomes = []

    def run():
        try:
            outcomes.append(ta.execute(plan, approval=approved(plan)))
        except RuntimeError:
            outcomes.append("blocked")

    workers = [threading.Thread(target=run) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
    assert not any(w.is_alive() for w in workers)
    assert (
        sum(d.get("side") == "BUY" for m, p, d in sim.writes if m == "post" and p == "order") == 1
    )
    assert "blocked" in outcomes


def test_nested_operation_lock_and_crash_release(sim):
    with em.operation_lock(), em.operation_lock():
        assert em.LOCK_STATE.active
    assert not em.LOCK_STATE.active
    with pytest.raises(Crash), em.operation_lock():
        raise Crash()
    with em.operation_lock():
        assert em.LOCK_STATE.active


def test_buy_approval_expires_during_slow_validation(sim, monkeypatch):
    plan = ta.build_plan(sig())
    approval = approved(plan)
    original = em.initialize

    def slow():
        result = original()
        monkeypatch.setattr(em.time, "time", lambda: approval["expires_at"] + 1)
        return result

    monkeypatch.setattr(em, "initialize", slow)
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    ta.execute(plan, approval=approval)
    assert not sim.writes
    assert em.active_trades()


def test_actual_base_buy_fee_leaves_tracked_dust_without_selling_user_holdings(sim, monkeypatch):
    sim.free["FOO"] = D(5)
    sim.fee_asset = "FOO"

    def use_quote_fee_after_buy(kind, data):
        if kind == "oco":
            sim.fee_asset = "USDT"

    sim.prewrite = use_quote_fee_after_buy
    rec = paused(sim, monkeypatch)
    for item in sim.lists.values():
        sim.fill(sim.orders[item["orders"][0]["orderId"]], price="104.5")
    assert em.reconcile_trade(rec)
    assert sim.free["FOO"] > 5
    saved = em.load_trades()[0]
    assert em.inventory(saved) > 0 and em.valid_retained_dust(saved)
    assert saved["state"] == "closed" and not em.active_trades()
    # Owner-authorized dust policy: retain the asset/cost and protect it through
    # the next trade's pre-buy balance floor, rather than clearing ownership.
    next_plan = ta.build_plan(sig())
    assert next_plan["baseline_base"] == sim.free["FOO"]


def test_active_status_reports_coverage_and_unresolved_state(sim, monkeypatch):
    paused(sim, monkeypatch)
    text = em.status_report()
    assert "FOOUSDT: protected" in text and "stop coverage" in text
    sim.read_failures = 1
    text = em.status_report()
    assert "coverage UNKNOWN" in text


def test_manual_check_and_fifteen_minute_recheck_are_preserved(sim, monkeypatch):
    replies = []
    monkeypatch.setattr(
        tg,
        "say",
        lambda text, **kw: replies.append(text) or {"ok": True, "result": {"message_id": 1}},
    )
    monkeypatch.setattr(tg, "tg", lambda *a, **kw: {"ok": True})
    result = {
        "symbol": "FOOUSDT",
        "price": 100,
        "chg": 1,
        "qv": 20_000_000,
        "sig": sig(),
        "reasons": [],
        "status": "SAFE",
        "note": "",
    }
    monkeypatch.setattr(tg, "check_ticker", lambda symbol: {**result, "symbol": symbol})
    message = {"chat": {"id": tg.CHAT_ID}, "from": {"id": tg.CHAT_ID}}
    tg.handle_message({**message, "text": "/check FOO/USDT"})
    assert tg.checks
    cid = next(iter(tg.checks))

    class Timer:
        def __init__(self, interval, fn, args):
            assert interval == 900
            self.fn, self.args = fn, args

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(tg.threading, "Timer", Timer)
    tg.handle_check_callback({"id": "cb"}, "C2", cid)
    timer = next(iter(tg.recheck_timers))
    timer.fn(*timer.args)
    cid = next(iter(tg.checks))
    tg.handle_check_callback({"id": "cb"}, "C3", cid)
    tg.handle_message({**message, "text": "100 97 104.5 107.5"})
    assert tg.pending and not sim.writes
    assert any("MANUAL" in text for text in replies)


def test_callback_requires_new_approval_if_size_grows(sim, monkeypatch):
    plan = ta.build_plan(sig())
    tg.pending["proposal"] = {
        "sig": sig(),
        "plan": plan,
        "created": time.time(),
        "mid": 1,
        "text": "shown",
    }
    monkeypatch.setattr(tg, "tg", lambda *a, **kw: {"ok": True})
    fresh = {**plan, "cost": plan["cost"] + D(".01")}
    monkeypatch.setattr(ta, "build_plan", lambda sig: fresh)
    proposals = []
    monkeypatch.setattr(tg, "send_proposal", lambda *args: proposals.append(args))
    tg.handle_callback(
        {
            "from": {"id": tg.CHAT_ID},
            "message": {"chat": {"id": tg.CHAT_ID}},
            "data": "A:proposal",
            "id": "cb",
        }
    )
    assert proposals and not sim.writes and not tg.trade_lock.locked()


def test_canceled_recheck_generation_does_not_send_new_cards(sim, monkeypatch):
    generation = tg.proposal_generation
    calls = []
    monkeypatch.setattr(tg, "check_ticker", lambda symbol: calls.append(symbol))
    tg.proposal_generation += 1
    tg.run_check("FOO", "UPDATE", generation)
    assert not calls


def test_scanner_indicators_and_closed_candle_validation(monkeypatch):
    assert sc.ema([100] * 60, 20) == [100] * 60
    assert sc.rsi(list(range(1, 61))) == 100
    assert sc.atr([101] * 60, [99] * 60, [100] * 60) == 2
    interval = 900000
    now = int(time.time() * 1000)
    end = now // interval * interval
    rows = [
        [
            end + (i - 120) * interval,
            "100",
            "101",
            "99",
            "100",
            "100",
            end + (i - 119) * interval - 1,
        ]
        for i in range(121)
    ]
    monkeypatch.setattr(sc, "get", lambda *a, **kw: rows)
    data = sc.klines("FOOUSDT", "15m")
    assert len(data["close"]) == 120
    rows[-2][4] = "NaN"
    with pytest.raises(ValueError, match="Invalid candle"):
        sc.klines("FOOUSDT", "15m")


def test_price_filter_and_unsupported_averaging_window_rejected(sim, monkeypatch):
    info = sim.info()
    info["filters"].append(
        {
            "filterType": "PERCENT_PRICE",
            "multiplierUp": "1.01",
            "multiplierDown": ".99",
            "avgPriceMins": 5,
        }
    )
    monkeypatch.setattr(sim, "info", lambda: info)
    with pytest.raises(ValueError, match="percent-price"):
        ta.build_plan(sig())
    info["filters"][-1]["avgPriceMins"] = 1
    with pytest.raises(ValueError, match="averaging window"):
        ta.build_plan(sig())


def test_symbol_order_capacity_checked_before_buy(sim, monkeypatch):
    info = sim.info()
    info["filters"].append({"filterType": "MAX_NUM_ALGO_ORDERS", "maxNumAlgoOrders": 1})
    monkeypatch.setattr(sim, "info", lambda: info)
    with pytest.raises(ValueError, match="capacity"):
        ta.build_plan(sig())
    assert not sim.writes


def test_low_volume_entry_without_explicit_override_is_rejected(sim):
    def override(method, path, data):
        if path == "ticker/24hr":
            return {"symbol": "FOOUSDT", "quoteVolume": "9999999"}

    sim.response_override = override
    with pytest.raises(ValueError, match="10,000,000"):
        ta.build_plan(sig())
    assert not sim.writes


def test_kill_cli_remains_available_while_telegram_instance_is_running(sim, monkeypatch):
    paused(sim, monkeypatch)
    lock = em.instance_lock()
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        ["exit_manager.py", "kill", "--symbol", "FOOUSDT", "--confirm", "CLOSE_FOOUSDT"],
    )
    try:
        em.cli()
    finally:
        lock.unlink()
    assert not em.active_trades()


def test_kill_unwinds_each_released_leg_before_canceling_next(sim, monkeypatch):
    paused(sim, monkeypatch)
    em.close_bot_trade("FOOUSDT", "CLOSE_FOOUSDT")
    actions = [
        ("cancel" if m == "delete" else d.get("side"))
        for m, p, d in sim.writes
        if m == "delete" or p == "order"
    ]
    assert actions == ["BUY", "cancel", "SELL", "cancel", "SELL"]


def test_shutdown_cannot_submit_another_buy(sim, monkeypatch):
    plan = ta.build_plan(sig())
    em.STOP.set()
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    execute(plan)
    assert not sim.writes
