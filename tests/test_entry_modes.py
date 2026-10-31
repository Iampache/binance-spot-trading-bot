"""Offline entry-mode regressions through the installed SDK transport interface."""

import json
import time
from decimal import Decimal as D

import pytest
from simulator import Crash
from test_bot import approved, sig

import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta


def signal(mode="watch"):
    return {**sig(), "entry_mode": mode, "manual_override": True}


def arm(sim, monkeypatch, **levels):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    sim.price = D(101)
    plan = ta.build_plan({**signal(), **levels})
    rec = ta.execute(plan, approval=approved(plan))
    assert not sim.writes
    return rec


def buys(sim):
    return [data for _, path, data in sim.writes if path == "order" and data.get("side") == "BUY"]


def test_now_uses_ask_and_fixed_ceiling_despite_typing_delay(sim, monkeypatch):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    plan = ta.build_plan(signal("now"))
    assert plan["price"] == D("100.20")
    sim.price = D("100.15")
    rec = ta.execute(plan, approval=approved(plan))
    assert len(buys(sim)) == 1
    assert D(buys(sim)[0]["price"]) == plan["price"]
    assert D(buys(sim)[0]["quantity"]) <= plan["qty"]
    assert em.coverage(rec) >= em.inventory(rec) > 0


def test_now_above_cap_never_buys(sim, monkeypatch):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    plan = ta.build_plan(signal("now"))
    sim.price = D("100.21")
    with pytest.raises(ta.PlanChangedError, match="maximum"):
        ta.execute(plan, approval=approved(plan))
    assert not sim.writes


def test_falling_price_cannot_increase_approved_quantity(sim, monkeypatch):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    plan = ta.build_plan(signal("now"))
    sim.price = D(99)
    rec = ta.execute(plan, approval=approved(plan))
    assert D(buys(sim)[0]["quantity"]) <= plan["qty"]
    assert em.D(rec["plan"]["cost"]) <= plan["cost"]


def test_watch_below_market_waits_then_executes_once_with_protection(sim, monkeypatch):
    rec = arm(sim, monkeypatch)
    assert rec["state"] == "watching_entry"
    assert not em.reconcile_trade(rec) and not sim.writes
    assert "ENTRY WATCH" in em.status_report() and not sim.writes
    with pytest.raises(ValueError, match="unresolved"):
        ta.build_plan(signal("now"))
    sim.price = D(100)
    assert not em.reconcile_trade(rec)
    assert len(buys(sim)) == 1 and len(sim.lists) == 2
    assert em.coverage(rec) >= em.inventory(rec) > 0
    em.reconcile_trade(rec)
    assert len(buys(sim)) == 1


@pytest.mark.parametrize("price", ["97", "96", "90"])
def test_watch_gap_to_or_below_stop_cancels_without_buy(sim, monkeypatch, price):
    rec = arm(sim, monkeypatch)
    sim.price = D(price)
    assert em.reconcile_trade(rec)
    assert rec["state"] == "closed" and not sim.writes
    assert "stop reached" in rec["entry_watch"]["result"]


def test_watch_expiry_not_proposal_expiry_controls_armed_attempt(sim, monkeypatch):
    rec = arm(sim, monkeypatch)
    clock = D(rec["entry_watch"]["armed_at"])
    monkeypatch.setattr(time, "time", lambda: float(clock + 350))
    sim.price = D(100)
    em.reconcile_trade(rec)
    assert len(buys(sim)) == 1


def test_watch_expired_never_submits(sim, monkeypatch):
    rec = arm(sim, monkeypatch)
    deadline = D(rec["entry_watch"]["expires_at"])
    monkeypatch.setattr(time, "time", lambda: float(deadline + 1))
    sim.price = D(100)
    assert em.reconcile_trade(rec) and not sim.writes


def test_cancel_watch_is_persistent_and_does_not_cancel_exchange_orders(sim, monkeypatch):
    rec = arm(sim, monkeypatch)
    assert ta.cancel_entry_watches() == 1
    sim.price = D(100)
    assert em.reconcile_trade(rec) and not sim.writes
    assert ta.cancel_entry_watches() == 0


def test_watch_restart_cancels_untriggered_authorization(sim, monkeypatch):
    rec = arm(sim, monkeypatch)
    sim.price = D(100)
    em.recover_trades()
    assert em.load_trades()[0]["state"] == "closed"
    assert not sim.writes and rec["orders"] == []


def test_watch_read_failure_cancels_without_late_buy(sim, monkeypatch):
    rec = arm(sim, monkeypatch)
    monkeypatch.setattr(ta, "entry_quote", lambda symbol: (_ for _ in ()).throw(TimeoutError()))
    # Exercise the real loop rather than the stub used while arming.
    REAL_MONITOR(rec, max_checks=1)
    assert em.load_trades()[0]["state"] == "closed" and not sim.writes


REAL_MONITOR = em.monitor_trade


@pytest.mark.parametrize(
    "behavior",
    ["partial", "expired", "expired_empty", "rejected", "timeout_accept", "timeout_before"],
)
def test_watch_order_results_never_repeat_buy_or_sell_unrelated(sim, monkeypatch, behavior):
    sim.free["FOO"] = D(8)
    rec = arm(sim, monkeypatch)
    sim.price = D(100)
    sim.buy_behavior = behavior
    em.reconcile_trade(rec)
    try:
        em.reconcile_trade(rec)
    except Exception:
        assert behavior == "timeout_before"
    assert len(buys(sim)) == 1
    assert sim.free["FOO"] >= D(8)
    assert not rec["entry_watch"]["active"]


def test_watch_crash_after_accepted_buy_recovers_without_rebuy(sim, monkeypatch):
    sim.free["FOO"] = D(8)
    rec = arm(sim, monkeypatch)
    sim.price = D(100)
    sim.buy_behavior = "crash"
    with pytest.raises(Crash):
        em.reconcile_trade(rec)
    sim.buy_behavior = "filled"
    em.recover_trades()
    assert len(buys(sim)) == 1
    assert sim.free["FOO"] == D(8)
    assert em.load_trades()[0]["state"] == "closed"


def test_watch_disarmed_crash_before_intent_does_not_later_buy(sim, monkeypatch):
    rec = arm(sim, monkeypatch)
    sim.price = D(100)
    monkeypatch.setattr(ta, "submit_entry", lambda *a: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        em.reconcile_trade(rec)
    em.recover_trades()
    assert not sim.writes and em.load_trades()[0]["state"] == "closed"


def test_watch_authorization_cannot_extend_deadline(sim, monkeypatch):
    rec = arm(sim, monkeypatch)
    rec["entry_watch"]["expires_at"] = str(D(rec["entry_watch"]["expires_at"]) + 900)
    em.save_trade(rec)
    sim.price = D(100)
    with pytest.raises(ValueError, match="authorization"):
        em.reconcile_trade(rec)
    assert not sim.writes


def test_entry_mode_change_requires_new_approval(sim):
    plan = ta.build_plan(signal("now"))
    assert not ta.approval_bounds(plan, {**plan, "entry_mode": "watch"})


def test_telegram_fast_flow_and_presets_still_require_approval(sim, monkeypatch):
    messages = []
    monkeypatch.setattr(
        tg,
        "say",
        lambda text, **kw: messages.append((text, kw)) or {"ok": True, "result": {"message_id": 1}},
    )
    monkeypatch.setattr(tg, "tg", lambda *a, **kw: {})
    monkeypatch.setattr(tg, "edit", lambda *a: None)
    tg.checks["x"] = {
        "created": time.time(),
        "res": {"symbol": "FOOUSDT", "price": 100, "status": "WARNING"},
    }
    tg.handle_check_callback({"id": "cb"}, "C3", "x")
    keyboard = json.loads(messages[-1][1]["reply_markup"])
    assert any(b["callback_data"] == "CN:x" for row in keyboard["inline_keyboard"] for b in row)
    labels = {b["text"] for row in keyboard["inline_keyboard"] for b in row}
    assert {"Manual — set all prices", "Assisted — choose profit targets"} <= labels
    assert "Send 4 numbers" not in messages[-1][0]
    tg.handle_check_callback({"id": "cb"}, "CN", "x")
    assert "Send 4 prices in ONE message" in messages[-1][0]
    assert "STOP = stop-loss trigger price" in messages[-1][0]
    tg.handle_manual("100 97 105 108")
    assert next(iter(tg.pending.values()))["plan"]["entry_mode"] == "watch"
    assert not sim.writes


@pytest.mark.parametrize("checkpoint", range(1, 13))
def test_watch_crashes_at_journal_boundaries_never_duplicate_buy(sim, monkeypatch, checkpoint):
    sim.free["FOO"] = D(8)
    rec = arm(sim, monkeypatch)
    sim.price = D(100)
    original = em.save_trade
    calls = 0

    def crash_after_save(record):
        nonlocal calls
        original(record)
        calls += 1
        if calls == checkpoint:
            raise Crash()

    monkeypatch.setattr(em, "save_trade", crash_after_save)
    try:
        em.reconcile_trade(rec)
    except Crash:
        pass
    monkeypatch.setattr(em, "save_trade", original)
    em.recover_trades()
    assert len(buys(sim)) <= 1
    assert sim.free["FOO"] >= D(8)
    for record in em.active_trades():
        # Any unknown request keeps its journal and blocks another entry.
        with pytest.raises(ValueError, match="unresolved"):
            ta.build_plan(signal("now"))
        if not any(o["status"] == "UNKNOWN" for o in record["orders"]):
            assert em.coverage(record) >= em.inventory(record)


def test_stop_gap_during_final_validation_prevents_transport(sim, monkeypatch):
    rec = arm(sim, monkeypatch)
    sim.price = D(100)
    original = em.submit_order

    def gap_before_submit(record, order):
        sim.price = D(96)
        return original(record, order)

    monkeypatch.setattr(em, "submit_order", gap_before_submit)
    em.reconcile_trade(rec)
    assert not sim.writes
    assert em.reconcile_trade(rec)
    assert rec["state"] == "closed"


def test_gap_after_request_unwinds_only_confirmed_bot_inventory(sim, monkeypatch):
    sim.free["FOO"] = D(8)
    rec = arm(sim, monkeypatch)
    sim.price = D(100)
    original = em.submit_order

    def gap_after_fill(record, order):
        result = original(record, order)
        if order["side"] == "BUY":
            sim.price = D(96)
        return result

    monkeypatch.setattr(em, "submit_order", gap_after_fill)
    em.reconcile_trade(rec)
    em.reconcile_trade(rec)
    assert len(buys(sim)) == 1
    assert sim.free["FOO"] == D(8)
    assert rec["state"] == "closed"


def test_cancel_does_not_remove_confirmed_protection(sim, monkeypatch):
    rec = arm(sim, monkeypatch)
    sim.price = D(100)
    em.reconcile_trade(rec)
    covered = em.coverage(rec)
    count = len(sim.writes)
    assert ta.cancel_entry_watches() == 0
    assert len(sim.writes) == count and em.coverage(rec) == covered > 0


def test_watch_invalid_levels_still_enforce_exchange_minimums(sim, monkeypatch):
    sim.min_notional = "10"
    with pytest.raises(ValueError, match="notional|minimum"):
        ta.build_plan(signal())
    assert not sim.writes


def test_watch_refreshes_unrelated_balance_baseline_before_buy(sim, monkeypatch):
    rec = arm(sim, monkeypatch)
    sim.free["FOO"] = D(8)  # unrelated deposit during the watch, before entry
    sim.price = D(100)
    em.reconcile_trade(rec)
    assert D(rec["baseline_base"]) == D(8)
    assert D(rec["plan"]["baseline_base"]) == D(8)
    assert em.inventory(rec) > 0 and em.coverage(rec) >= em.inventory(rec)
    assert sim.free["FOO"] >= D(8)
