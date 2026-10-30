"""Original safety scenarios retained using actual SDK transport simulation."""

import time
from decimal import Decimal

import pytest

import entry_scanner as sc
import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta


def sig():
    return {
        "symbol": "FOOUSDT",
        "entry": 100,
        "stop": 97,
        "t1": 104.5,
        "t2": 107.5,
        "setup": "MANUAL entry (your levels)",
        "rsi": 49,
        "chg24": 1,
    }


def approved(plan):
    import uuid

    return {
        **plan,
        "approved": True,
        "approval_id": uuid.uuid4().hex,
        "expires_at": time.time() + 300,
    }


def execute(plan):
    return ta.execute(plan, approval=approved(plan))


def test_15r_alignment(sim):
    p = ta.build_plan(sig())
    assert p["rr"] == Decimal("1.5")
    assert p["cost"] <= 12
    assert p["loss"] <= Decimal(3)
    assert p["half"] + p["rest"] <= p["qty"]


def test_10m_volume_stays_default():
    assert sc.MIN_QUOTE_VOLUME == 10_000_000
    assert sc.RISK_PCT == 1 and ta.MAX_POSITION_USDT == 12
    assert ta.MAX_OPEN_TRADES == 1 and em.MAX_DAILY_LOSS_USD == 3


def test_manual_nan_inf_negative_rejected():
    for bad in (float("nan"), float("inf"), -1, 0):
        s = sig()
        s["entry"] = bad
        with pytest.raises(ValueError):
            ta.require_finite_levels(s)


def test_order_filters_and_split(sim):
    f = em.symbol_filters("FOOUSDT")
    with pytest.raises(ValueError):
        em.validate_qty(".1005", f, 100)
    with pytest.raises(ValueError):
        em.validate_qty(".001", f, 100)
    with pytest.raises(ValueError):
        em.place_oco("FOOUSDT", ".03", 104.5, 101, f["tick"], f)


def test_buy_then_two_protected_ocos_and_journal(sim):
    sim.fulfill = True
    rec = execute(ta.build_plan(sig()))
    assert len(sim.lists) == 2
    assert rec["state"] == "closed"
    assert not em.active_trades()
    assert sum(em.D(e["pnl"]) for e in rec["realized_events"]) > 0
    assert all(o["belowType"] == "STOP_LOSS" for _, p, o in sim.writes if p == "orderList/oco")


def test_unknown_buy_blocks_retry_and_does_not_liquidate(sim, monkeypatch):
    sim.buy_behavior = "timeout_before"
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    rec = execute(ta.build_plan(sig()))
    assert rec["state"] == "needs_attention"
    assert len(sim.writes) == 1
    em.recover_trades()
    assert len(sim.writes) == 1
    with pytest.raises(ValueError, match="Open or unresolved"):
        ta.build_plan(sig())


def test_second_oco_fail_leaves_first_protection_in_place(sim, monkeypatch):
    sim.oco_behavior[2] = "rejected"
    monkeypatch.setattr(em, "monitor_trade", lambda rec: em.reconcile_trade(rec))
    rec = execute(ta.build_plan(sig()))
    assert len(sim.lists) == 1
    assert not any(m == "delete" for m, _, _ in sim.writes)
    assert any(p == "order" and d["side"] == "SELL" for _, p, d in sim.writes)
    assert em.coverage(rec) == em.inventory(rec)


def test_restart_recovery_protected_ocos_and_refuses_duplicate(sim, monkeypatch):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    rec = execute(ta.build_plan(sig()))
    assert rec["state"] == "protected"
    writes = len(sim.writes)
    em.recover_trades()
    assert len(sim.writes) == writes
    with pytest.raises(ValueError, match="Open or unresolved"):
        ta.build_plan(sig())


def test_closed_pnl_daily_loss_guard(sim, monkeypatch):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    rec = execute(ta.build_plan(sig()))
    # A severe stop-market gap can realize more than the reserved stop risk.
    for item in sim.lists.values():
        sim.fill(sim.orders[item["orders"][1]["orderId"]], price=".01")
    em.reconcile_trade(rec)
    assert em.todays_loss() > 3
    with pytest.raises(ValueError, match="Daily bot-trade loss limit"):
        ta.build_plan(sig())


def test_live_disabled_without_explicit_optin(sim, monkeypatch):
    monkeypatch.setattr(em, "USE_TESTNET", False)
    with pytest.raises(RuntimeError, match="LIVE TRADING BLOCKED"):
        em.require_trade_permission()


def test_kill_requires_confirm_and_closes_bot_owned_only(sim, monkeypatch):
    sim.free["FOO"] = Decimal(5)
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    execute(ta.build_plan(sig()))
    with pytest.raises(ValueError):
        em.close_bot_trade("FOOUSDT", "BAD")
    em.close_bot_trade("FOOUSDT", "CLOSE_FOOUSDT")
    assert sim.free["FOO"] == 5
    assert em.load_trades()[0]["state"] == "closed"


def test_replay_telegram_proposal_rejected(sim, monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "tg", lambda method, **kw: calls.append(method) or {"ok": True})
    tg.handle_callback(
        {
            "from": {"id": tg.CHAT_ID},
            "message": {"chat": {"id": tg.CHAT_ID}},
            "data": "A:deadbeef",
            "id": "cb",
        }
    )
    assert calls == ["answerCallbackQuery"]
    assert not sim.writes


def test_lock_prevents_two_instances(sim):
    lock = em.instance_lock()
    try:
        with pytest.raises(FileExistsError):
            em.instance_lock()
    finally:
        lock.unlink()
    em.instance_lock().unlink()
