"""Approval, order acceptance, actual fills and protective coverage are distinct."""

from decimal import Decimal as D

from test_bot import approved, sig

import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta


def entry(sim, monkeypatch, messages):
    monkeypatch.setattr(em, "alert", messages.append)
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    plan = ta.build_plan(sig())
    return ta.execute(plan, approval=approved(plan))


def test_zero_fill_ioc_expiry_reports_no_buy_and_retains_last_result(sim, monkeypatch):
    sim.buy_behavior = "expired_empty"
    before = dict(sim.free)
    messages = []
    rec = entry(sim, monkeypatch, messages)
    assert rec["state"] == "closed" and not em.active_trades()
    assert sim.free == before and not sim.lists
    assert any("NO BUY FILLED" in m and "EXPIRED" in m and "executed 0" in m for m in messages)
    assert not any("BUY FILLED / PROTECTION CONFIRMED" in m for m in messages)
    report = em.status_report()
    assert "No active bot positions" in report
    assert "Latest saved entry: FOOUSDT: buy EXPIRED; executed 0" in report
    assert "Binance order" in report
    em.recover_trades()
    assert len([d for m, p, d in sim.writes if m == "post" and p == "order"]) == 1


def test_rejected_buy_reports_and_persists_safe_api_metadata(sim, monkeypatch):
    sim.buy_behavior = "rejected"
    messages = []
    rec = entry(sim, monkeypatch, messages)
    assert rec["state"] == "closed"
    assert any("NO BUY FILLED" in m and "REJECTED" in m and "API code -2010" in m for m in messages)
    assert "API code -2010" in em.status_report()
    assert not sim.orders and not sim.lists


def test_confirmed_fill_message_requires_verified_remaining_stop_coverage(sim, monkeypatch):
    messages = []

    def before(kind, data):
        if kind == "oco":
            assert not any("PROTECTION CONFIRMED" in m for m in messages)

    sim.prewrite = before
    rec = entry(sim, monkeypatch, messages)
    assert rec["state"] == "protected"
    assert em.coverage(rec) == em.inventory(rec) > 0
    confirmed = [m for m in messages if "BUY FILLED / PROTECTION CONFIRMED" in m]
    assert len(confirmed) == 1 and "2 OCO lists verified" in confirmed[0]
    assert not any("NO BUY FILLED" in m for m in messages)


def test_partial_entry_recovery_is_not_reported_as_zero_fill(sim, monkeypatch):
    sim.buy_behavior = "expired"
    messages = []
    rec = entry(sim, monkeypatch, messages)
    assert rec["state"] == "closed" and em.inventory(rec) == 0
    assert any("PARTIAL BUY" in m for m in messages)
    assert any("PARTIAL ENTRY RECONCILED" in m for m in messages)
    assert not any("NO BUY FILLED" in m for m in messages)
    assert "executed 0.059" in em.status_report()


def test_unknown_buy_cannot_be_reported_as_no_fill_or_protected(sim, monkeypatch):
    sim.buy_behavior = "timeout_before"
    messages = []
    rec = entry(sim, monkeypatch, messages)
    assert rec["state"] == "needs_attention"
    assert not any("NO BUY FILLED" in m or "PROTECTION CONFIRMED" in m for m in messages)
    assert "buy UNKNOWN" in em.status_report()


def test_failed_final_approval_bounds_reports_no_submission(sim, monkeypatch):
    plan = ta.build_plan(sig())
    fresh = {**plan, "cost": plan["cost"] + D(".01")}
    monkeypatch.setattr(ta, "build_plan", lambda signal: fresh)
    messages = []
    monkeypatch.setattr(em, "alert", messages.append)
    tg.trade_lock.acquire()
    tg.run_trade(plan, approved(plan))
    assert any("NOT SUBMITTED" in m and "No buy was submitted" in m for m in messages)
    assert not sim.writes and not em.load_trades()
    assert not tg.trade_lock.locked()


def test_already_exited_position_is_not_reported_as_active_protection(sim, monkeypatch):
    sim.fulfill = True
    messages = []
    entry(sim, monkeypatch, messages)
    assert any("exits already executed" in m for m in messages)
    assert not any("PROTECTION CONFIRMED" in m for m in messages)
