"""Verified exit reports and durable delivery never initiate exchange orders."""

import pytest
from simulator import Crash
from test_bot import approved, sig

import exit_manager as em
import trade_assistant as ta


def buy(sim, monkeypatch):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    return ta.execute(ta.build_plan(sig()), approval=approved(ta.build_plan(sig())))


def exit_leg(sim, item, *, stop=False):
    leg = item["orders"][1 if stop else 0]
    sim.fill(sim.orders[leg["orderId"]], price="97" if stop else "104.5")


def delivery(monkeypatch, *, success=True):
    messages = []
    monkeypatch.setattr(em, "alert", lambda text: messages.append(text) or success)
    return messages


@pytest.mark.parametrize("stop", [False, True])
def test_final_result_uses_verified_net_fees_and_actual_entry_cost(sim, monkeypatch, stop):
    before = sim.free["USDT"]
    rec = buy(sim, monkeypatch)
    messages = delivery(monkeypatch)
    for item in sim.lists.values():
        exit_leg(sim, item, stop=stop)
    assert em.reconcile_trade(rec)
    net = sim.free["USDT"] - before
    cost = sum(em.D(t["quoteQty"]) + em.D(t["commission"]) for t in sim.trades if t["isBuyer"])
    report = next(text for text in messages if "TRADE CLOSED" in text)
    assert f"{net:+.4f} USDT" in report
    assert f"{net / cost * 100:+.2f}% of entry cost" in report
    assert "Remaining bot quantity: 0" in report
    assert (net < 0) == stop
    assert "Stop loss" in report if stop else "Take profit" in report
    writes = len(sim.writes)
    em.reconcile_trade(rec)
    em.retry_result_notices()
    assert len(messages) == 1 and len(sim.writes) == writes
    assert em.load_trades()[0]["result_notices"]["final"]["sent"] is True


def test_tp1_reports_partial_then_tp2_reports_final(sim, monkeypatch):
    rec = buy(sim, monkeypatch)
    messages = delivery(monkeypatch)
    lists = list(sim.lists.values())
    exit_leg(sim, lists[0])
    assert not em.reconcile_trade(rec)
    assert len(messages) == 1 and "PARTIAL EXIT" in messages[0]
    assert "Net realized so far" in messages[0] and "Not a final result" in messages[0]
    assert em.inventory(rec) > 0 and em.coverage(rec) >= em.inventory(rec)
    em.reconcile_trade(rec)
    assert len(messages) == 1
    exit_leg(sim, lists[1])
    assert em.reconcile_trade(rec)
    assert len(messages) == 2 and "TRADE CLOSED" in messages[1]


def test_tp1_then_stop_is_net_mixed_outcome_not_full_target_projection(sim, monkeypatch):
    before = sim.free["USDT"]
    rec = buy(sim, monkeypatch)
    messages = delivery(monkeypatch)
    lists = list(sim.lists.values())
    exit_leg(sim, lists[0])
    exit_leg(sim, lists[1], stop=True)
    assert em.reconcile_trade(rec)
    text = messages[-1]
    assert "Stop loss" in text and "Take profit" in text
    assert f"{sim.free['USDT'] - before:+.4f} USDT" in text


def test_zero_fill_does_not_generate_profit_report(sim, monkeypatch):
    sim.buy_behavior = "expired_empty"
    rec = buy(sim, monkeypatch)
    messages = delivery(monkeypatch)
    assert em.reconcile_trade(rec)
    em.retry_result_notices()
    assert not messages and "result_notices" not in em.load_trades()[0]


def test_failed_delivery_retries_closed_trade_from_persistent_outbox(sim, monkeypatch):
    rec = buy(sim, monkeypatch)
    for item in sim.lists.values():
        exit_leg(sim, item)
    messages = delivery(monkeypatch, success=False)
    assert em.reconcile_trade(rec)
    saved = em.load_trades()[0]
    assert saved["result_notice_due"] and not saved["result_notices"]["final"]["sent"]
    writes = len(sim.writes)
    monkeypatch.setattr(em, "alert", lambda text: messages.append(text) or True)
    em.retry_result_notices()  # process-local memory not required
    em.retry_result_notices()
    assert len(messages) == 2 and messages[0] == messages[1]
    assert not em.load_trades()[0]["result_notice_due"]
    assert len(sim.writes) == writes


def test_crash_after_close_before_notification_preserves_due_flag(sim, monkeypatch):
    rec = buy(sim, monkeypatch)
    for item in sim.lists.values():
        exit_leg(sim, item)
    original = em.save_trade

    def crash_after_close(record):
        original(record)
        if record["state"] == "closed":
            raise Crash()

    monkeypatch.setattr(em, "save_trade", crash_after_close)
    with pytest.raises(Crash):
        em.reconcile_trade(rec)
    monkeypatch.setattr(em, "save_trade", original)
    assert em.load_trades()[0]["result_notice_due"]
    messages = delivery(monkeypatch)
    em.retry_result_notices()
    assert len(messages) == 1 and "TRADE CLOSED" in messages[0]


def test_delivery_exception_does_not_reopen_closed_position(sim, monkeypatch):
    rec = buy(sim, monkeypatch)
    for item in sim.lists.values():
        exit_leg(sim, item)
    monkeypatch.setattr(em, "alert", lambda text: (_ for _ in ()).throw(TimeoutError()))
    assert em.reconcile_trade(rec)
    assert em.load_trades()[0]["state"] == "closed"
    assert not em.active_trades()


def test_missing_bnb_valuation_never_reports_final_estimate(sim, monkeypatch):
    monkeypatch.setattr(em, "ALLOW_BNB_FEES", True)
    sim.bnb_discount = True
    sim.fee_asset = "BNB"
    rec = buy(sim, monkeypatch)
    messages = delivery(monkeypatch)
    lists = list(sim.lists.values())
    exit_leg(sim, lists[0])
    em.reconcile_trade(rec)
    assert "pending verified fee valuation" in messages[-1]
    exit_leg(sim, lists[1])
    monkeypatch.setattr(
        em, "bnb_fee_reference", lambda *a: (_ for _ in ()).throw(ValueError("missing reference"))
    )
    with pytest.raises(ValueError):
        em.reconcile_trade(rec)
    assert not any("TRADE CLOSED" in text for text in messages)
    assert em.load_trades()[0]["state"] != "closed"


def test_bnb_final_result_includes_reference_valuation_label(sim, monkeypatch):
    monkeypatch.setattr(em, "ALLOW_BNB_FEES", True)
    sim.bnb_discount = True
    sim.fee_asset = "BNB"
    rec = buy(sim, monkeypatch)
    for item in sim.lists.values():
        exit_leg(sim, item)
    messages = delivery(monkeypatch)
    assert em.reconcile_trade(rec)
    assert "BNB fees use fill-time USDT reference" in messages[-1]
    expected = sum(em.D(e["pnl"]) for e in rec["realized_events"])
    assert f"{expected:+.4f} USDT" in messages[-1]


def test_result_delivery_defers_when_account_lock_is_already_held(sim, monkeypatch):
    rec = buy(sim, monkeypatch)
    for item in sim.lists.values():
        exit_leg(sim, item)
    messages = delivery(monkeypatch)
    with em.operation_lock():
        assert em.reconcile_trade(rec)
        em.notify_trade_result(rec)
        assert not messages
    em.retry_result_notices()
    assert len(messages) == 1
