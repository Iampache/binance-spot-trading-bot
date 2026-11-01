"""Persistent limits, isolated parallel positions and unchanged approval/risk gates."""

import json
import threading
from decimal import Decimal as D

import pytest
from test_bot import approved, sig

import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta
import trading_settings as settings


def change(field, value):
    return settings.update(
        field, value, expected_revision=settings.read()["revision"], actor="test-owner"
    )


@pytest.fixture
def parallel(sim):
    sim.symbols += ["BARUSDT", "BAZUSDT"]
    for base in ("BAR", "BAZ"):
        sim.free[base] = D(0)
        sim.locked[base] = D(0)
    change("max_open_trades", 2)
    return sim


def signal(symbol="FOOUSDT", **fields):
    return {**sig(), "symbol": symbol, **fields}


def buy(symbol="FOOUSDT", **fields):
    plan = ta.build_plan(signal(symbol, **fields))
    return ta.execute(plan, approval=approved(plan), monitor=False)


def test_defaults_persistence_and_atomic_audit_history(sim):
    assert settings.read()["max_open_trades"] == 1
    assert settings.read()["max_position_usdt"] == "12"
    change("max_open_trades", 3)
    saved = change("max_position_usdt", "25.50")
    em.CONTEXT = em.TRADES_DIR = None
    assert settings.read() == saved
    assert [e["revision"] for e in saved["changes"]] == [1, 2]
    assert saved["changes"][1]["old"] == "12" and saved["changes"][1]["new"] == "25.50"
    assert saved["changes"][1]["actor"] == "test-owner"


def test_settings_are_isolated_by_verified_account_and_environment(sim):
    change("max_open_trades", 3)
    sim.uid = "6789"
    em.CONTEXT = em.TRADES_DIR = None
    assert settings.read()["max_open_trades"] == 1
    sim.uid = "12345"
    em.CONTEXT = em.TRADES_DIR = None
    assert settings.read()["max_open_trades"] == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_open_trades", 0),
        ("max_open_trades", 101),
        ("max_open_trades", "2.5"),
        ("max_open_trades", True),
        ("max_position_usdt", "NaN"),
        ("max_position_usdt", "Infinity"),
        ("max_position_usdt", "-12"),
        ("max_position_usdt", "0"),
        ("max_position_usdt", "12.001"),
        ("max_position_usdt", "1e8"),
        ("risk_pct", "10"),
    ],
)
def test_invalid_settings_cannot_change_defaults_or_submit_orders(sim, field, value):
    with pytest.raises(ValueError):
        change(field, value)
    assert settings.read()["revision"] == 0 and not sim.writes


def test_stale_revision_and_failed_disk_write_preserve_the_last_confirmed_settings(
    sim, monkeypatch
):
    saved = change("max_open_trades", 2)
    with pytest.raises(ValueError, match="changed"):
        settings.update("max_position_usdt", "30", expected_revision=0, actor="test")

    def failed(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(em, "_replace_journal", failed)
    with pytest.raises(OSError):
        change("max_position_usdt", "30")
    assert settings.read() == saved and not sim.writes


def test_configured_amount_is_used_by_both_planner_and_exchange_preflight(sim):
    change("max_position_usdt", "25")
    plan = ta.build_plan(sig())
    assert 24 < plan["cost"] <= 25 and plan["loss"] <= plan["balance"] / 100
    forged = {**plan, "qty": D(1)}
    with pytest.raises(ValueError, match="configured 25 USDT cap"):
        em.preflight(forged, em.symbol_filters("FOOUSDT"))
    assert not sim.writes


def test_settings_change_invalidates_old_approval_without_any_buy(sim):
    old = ta.build_plan(sig())
    change("max_position_usdt", "25")
    fresh = ta.build_plan(sig())
    assert not ta.approval_bounds(old, fresh)
    with pytest.raises(ta.PlanChangedError, match="settings changed"):
        ta.execute(old, approval=approved(old), monitor=False)
    assert not sim.writes and not em.active_trades()


def test_two_different_pairs_are_independently_protected_recoverable_and_counted(parallel):
    first = buy()
    second = buy("BARUSDT")
    assert len(em.active_trades()) == 2 and len(parallel.lists) == 4
    assert all(em.coverage(rec) >= em.inventory(rec) > 0 for rec in (first, second))
    count = len(parallel.writes)
    em.recover_trades()
    assert len(parallel.writes) == count and len(em.active_trades()) == 2
    with pytest.raises(ta.ActiveTradeError, match="limit is 2"):
        ta.build_plan(signal("BAZUSDT"))
    assert em.todays_loss() == 0


def test_closing_one_parallel_pair_never_cancels_or_sells_the_other(parallel):
    first, second = buy(), buy("BARUSDT")
    other_ids = {item["orderListId"] for item in second["oco"]}
    assert em.close_bot_trade("FOOUSDT", "CLOSE_FOOUSDT")
    saved = em.active_trades()[0]
    assert saved["id"] == second["id"] and em.coverage(saved) >= em.inventory(saved) > 0
    assert all(parallel.lists[oid]["listOrderStatus"] == "EXECUTING" for oid in other_ids)
    assert next(r for r in em.load_trades() if r["id"] == first["id"])["state"] == "closed"


def test_bnb_fee_account_can_open_another_protected_pair_before_any_exit(parallel, monkeypatch):
    monkeypatch.setattr(em, "ALLOW_BNB_FEES", True)
    parallel.bnb_discount = True
    parallel.fee_asset = "BNB"
    buy()
    second = buy("BARUSDT")
    assert second["state"] == "protected" and len(em.active_trades()) == 2


def test_duplicate_pair_and_unresolved_position_still_block_with_free_slots(parallel):
    first = buy()
    with pytest.raises(ta.ActiveTradeError, match="duplicate entry"):
        ta.build_plan(sig())
    em.mark_attention(first)
    with pytest.raises(ta.ActiveTradeError, match="reconciliation"):
        ta.build_plan(signal("BARUSDT"))


def test_unowned_exchange_order_cannot_be_whitelisted_by_parallel_mode(parallel):
    buy()
    count = len(parallel.writes)
    parallel.make_order(
        {"symbol": "BAZUSDT", "quantity": "1", "newClientOrderId": "external"}, "BUY"
    )
    with pytest.raises(ValueError, match="isolated ownership"):
        ta.build_plan(signal("BARUSDT"))
    assert len(parallel.writes) == count


def test_changed_existing_stop_blocks_new_entry_without_removing_protection(parallel):
    buy()
    stop = next(o for o in parallel.orders.values() if o["type"] == "STOP_LOSS")
    stop["stopPrice"] = "96"
    count = len(parallel.writes)
    with pytest.raises(RuntimeError, match="level mismatch"):
        ta.build_plan(signal("BARUSDT"))
    assert len(parallel.writes) == count


def test_existing_orders_count_toward_exchange_wide_capacity(parallel):
    buy()
    count = len(parallel.writes)
    info = {
        "symbols": [parallel.info(), {**parallel.info(), "symbol": "BARUSDT", "baseAsset": "BAR"}],
        "exchangeFilters": [{"filterType": "EXCHANGE_MAX_NUM_ORDERS", "maxNumOrders": 6}],
    }
    parallel.response_override = lambda method, path, data: info if path == "exchangeInfo" else None
    with pytest.raises(ValueError, match="exchange-wide"):
        ta.build_plan(signal("BARUSDT"))
    assert len(parallel.writes) == count


def test_other_positions_risk_is_reserved_from_daily_budget(parallel, monkeypatch):
    first = buy()
    monkeypatch.setattr(em, "todays_loss", lambda: D("2.5"))
    plan = ta.build_plan(signal("BARUSDT"))
    assert plan["committed_risk"] == D(first["plan"]["loss"])
    assert plan["loss"] + plan["committed_risk"] <= D(".5")
    monkeypatch.setattr(em, "todays_loss", lambda: D("2.9"))
    with pytest.raises(ValueError, match="risk budget"):
        ta.build_plan(signal("BARUSDT"))


def test_untriggered_watches_reserve_cash_before_another_watch(parallel):
    change("max_position_usdt", "300")
    parallel.price = D(101)
    first = buy(entry_mode="watch", manual_override=True)
    second_plan = ta.build_plan(signal("BARUSDT", entry_mode="watch", manual_override=True))
    assert second_plan["watch_reserve"] == D(first["approved_bounds"]["cost"])
    assert second_plan["cost"] + second_plan["watch_reserve"] <= parallel.free["USDT"]
    assert not parallel.writes


def test_lowering_limits_does_not_close_or_reprice_existing_positions(parallel):
    first, second = buy(), buy("BARUSDT")
    count = len(parallel.writes)
    change("max_open_trades", 1)
    change("max_position_usdt", "5")
    em.recover_trades()
    assert len(em.active_trades()) == 2 and len(parallel.writes) == count
    assert all(em.coverage(rec) >= em.inventory(rec) > 0 for rec in (first, second))
    with pytest.raises(ta.ActiveTradeError):
        ta.build_plan(signal("BAZUSDT"))


def test_corrupt_settings_block_new_buys_but_not_existing_trade_recovery(parallel):
    buy()
    count = len(parallel.writes)
    (em.TRADES_DIR.parent / "settings.json").write_text(json.dumps({"version": 1}))
    with pytest.raises(RuntimeError, match="settings invalid"):
        ta.build_plan(signal("BARUSDT"))
    em.recover_trades()
    assert em.active_trades()[0]["state"] == "protected" and len(parallel.writes) == count


def test_settings_values_must_match_the_atomic_audit_history(sim):
    saved = change("max_position_usdt", "25")
    saved["max_position_usdt"] = "250"
    (em.TRADES_DIR.parent / "settings.json").write_text(json.dumps(saved))
    with pytest.raises(RuntimeError, match="settings invalid"):
        ta.build_plan(sig())
    assert not sim.writes


def test_stopping_scan_prevents_another_symbol_analysis(sim, monkeypatch):
    seen = []
    monkeypatch.setattr(ta.sc, "btc_note", lambda: "")
    monkeypatch.setattr(
        ta.sc, "universe", lambda: [("FOOUSDT", 20000000, 1), ("BARUSDT", 20000000, 1)]
    )

    def analyze(symbol, change):
        seen.append(symbol)
        em.STOP.set()
        return sig()

    monkeypatch.setattr(ta.sc, "analyze", analyze)
    ta.scan_candidates()
    assert seen == ["FOOUSDT"] and not sim.writes


def test_concurrent_approvals_cannot_exceed_configured_slot_count(parallel):
    plans = [ta.build_plan(signal(symbol)) for symbol in parallel.symbols]
    outcomes = []

    def submit(plan):
        try:
            outcomes.append(ta.execute(plan, approval=approved(plan), monitor=False))
        except RuntimeError:
            outcomes.append("blocked")

    threads = [threading.Thread(target=submit, args=(plan,)) for plan in plans]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not any(t.is_alive() for t in threads)
    assert outcomes.count("blocked") == 1 and len(em.active_trades()) == 2
    assert sum(path == "order" and data["side"] == "BUY" for _, path, data in parallel.writes) == 2


def test_telegram_submission_lock_is_released_before_position_monitoring(sim, monkeypatch):
    rec = buy()
    monkeypatch.setattr(ta, "execute", lambda *args, **kwargs: rec)
    seen = []
    monkeypatch.setattr(em, "monitor_trade", lambda r: seen.append(tg.trade_lock.locked()))
    tg.trade_lock.acquire()
    tg.run_trade({}, {})
    assert seen == [False]
