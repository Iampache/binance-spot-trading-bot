"""Durable exact-record exit requests survive races, faults and restart."""

import copy
import time

import pytest
from test_bot import approved, sig

import exchange as ex
import exit_manager as em
import trade_assistant as ta


def buy(sim, **fields):
    plan = ta.build_plan({**sig(), **fields})
    return ta.execute(plan, approval=approved(plan), monitor=False)


def request(rec, **changes):
    kwargs = {
        "trade_id": rec["id"],
        "context": dict(rec["context"]),
        "action": "exit",
        "expires_at": time.time() + 300,
        "actor": "test-owner",
        "source": "telegram",
        **changes,
    }
    return em.request_bot_trade_close(rec["symbol"], "CLOSE_" + rec["symbol"], **kwargs)


def test_close_intent_is_saved_before_any_exchange_mutation_and_recovers_after_restart(sim):
    rec = buy(sim)
    count = len(sim.writes)
    saved = request(rec)
    assert saved["close_requested"] and saved["state"] == "closing" and len(sim.writes) == count
    assert em.load_trades()[0]["close_request"]["actor"] == "test-owner"
    em.CONTEXT = em.TRADES_DIR = None
    em.initialize()
    em.recover_trades()
    assert not em.active_trades() and em.load_trades()[0]["accounted"]
    count = len(sim.writes)
    request(rec)
    em.recover_trades()
    assert len(sim.writes) == count


@pytest.mark.parametrize(
    "changes",
    [
        {"trade_id": "does-not-exist"},
        {"context": {"environment": "live", "account_uid": "other"}},
        {"action": "cancel_watch"},
        {"action": "anything"},
        {"expires_at": 1},
    ],
)
def test_identity_action_and_expiry_checks_precede_any_exit_side_effect(sim, changes):
    rec = buy(sim)
    count = len(sim.writes)
    with pytest.raises((ValueError, ex.OwnershipError)):
        request(rec, **changes)
    assert len(sim.writes) == count and not em.active_trades()[0]["close_requested"]


def test_shutdown_prevents_accepting_a_new_telegram_exit(sim):
    rec = buy(sim)
    count = len(sim.writes)
    em.STOP.set()
    with pytest.raises(ValueError, match="shutdown"):
        request(rec)
    assert len(sim.writes) == count and not em.active_trades()[0]["close_requested"]


def test_failed_close_journal_write_cannot_cancel_orders_or_launch_a_sale(sim, monkeypatch):
    rec = buy(sim)
    count = len(sim.writes)

    def fail(*args):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(em, "_atomic_write", fail)
    with pytest.raises(OSError):
        request(rec)
    assert not em.active_trades()[0]["close_requested"] and len(sim.writes) == count


def test_overlapping_pair_records_are_not_treated_as_separate_sellable_ownership(sim):
    rec = buy(sim)
    duplicate = copy.deepcopy(rec)
    duplicate["id"] = "f" * 32
    em.save_trade(duplicate)
    count = len(sim.writes)
    with pytest.raises(ex.OwnershipError, match="Overlapping"):
        request(rec)
    assert len(sim.writes) == count and not any(r["close_requested"] for r in em.active_trades())


def test_accepted_sell_timeout_remains_pending_then_reconciles_without_duplicate_sale(sim):
    rec = buy(sim)
    sim.sell_behavior = "timeout_accept"
    first = True

    def prewrite(kind, data):
        nonlocal first
        if first and kind == "order" and data.get("side") == "SELL":
            first = False
            sim.read_failures = 1

    sim.prewrite = prewrite
    saved = request(rec)
    em.monitor_trade(saved, max_checks=1)
    pending = em.active_trades()[0]
    assert pending["close_requested"] and pending["state"] == "needs_attention"
    assert sum(path == "order" and d["side"] == "SELL" for _, path, d in sim.writes) == 1
    em.recover_trades()
    assert not em.active_trades()
    sells = [d for _, path, d in sim.writes if path == "order" and d.get("side") == "SELL"]
    assert len(sells) == 2
    assert sum(em.D(d["quantity"]) for d in sells) == em.D(rec["orders"][0]["executedQty"])


def test_stop_fill_during_cancel_reduces_the_market_exit_quantity(sim):
    rec = buy(sim)
    first = next(iter(sim.lists.values()))
    stop = sim.orders[first["orders"][1]["orderId"]]
    filled = False

    def prewrite(kind, data):
        nonlocal filled
        if kind == "cancel_list" and not filled:
            filled = True
            sim.fill(stop, price="97")

    sim.prewrite = prewrite
    saved = request(rec)
    em.monitor_trade(saved, max_checks=2)
    assert not em.active_trades()
    sells = [d for _, path, d in sim.writes if path == "order" and d.get("side") == "SELL"]
    assert len(sells) == 1
    assert em.D(sells[0]["quantity"]) + em.D(stop["executedQty"]) == em.D(
        rec["orders"][0]["executedQty"]
    )


def test_pending_partial_buy_is_canceled_and_only_confirmed_acquired_quantity_is_sold(
    sim, monkeypatch
):
    original = em.reconcile_trade
    sim.buy_behavior = "new"
    with monkeypatch.context() as temporary:
        temporary.setattr(em, "reconcile_trade", lambda rec: False)
        rec = buy(sim)
    assert rec["state"] == "buy_submitting"
    order = next(iter(sim.orders.values()))
    sim.fill(order, qty="0.06", terminal=False)
    saved = request(rec)
    assert original(saved)
    assert order["status"] == "CANCELED" and not em.active_trades()
    sells = [d for _, path, d in sim.writes if path == "order" and d.get("side") == "SELL"]
    assert len(sells) == 1 and em.D(sells[0]["quantity"]) == em.D("0.06")
    assert sum(path == "order" and d.get("side") == "BUY" for _, path, d in sim.writes) == 1


def test_cli_close_on_unfilled_watch_cancels_without_triggering_a_buy(sim):
    sim.price = em.D(101)
    rec = buy(sim, entry_mode="watch", manual_override=True)
    sim.price = em.D(100)
    assert em.close_bot_trade(rec["symbol"], "CLOSE_" + rec["symbol"])
    assert not sim.writes and not em.active_trades()


def test_preexisting_saved_close_flag_on_watch_is_never_allowed_to_buy(sim):
    sim.price = em.D(101)
    rec = buy(sim, entry_mode="watch", manual_override=True)
    rec["close_requested"] = True
    rec["state"] = "closing"
    em.save_trade(rec)
    sim.price = em.D(100)
    assert em.reconcile_trade(rec)
    assert not sim.writes and rec["state"] == "closed"
