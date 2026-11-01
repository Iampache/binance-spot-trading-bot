"""Independent cash-flow checks for live valuation; reading can never trade."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from test_bnb_fees import enabled
from test_bot import approved, sig
from test_fractional_dust import fill_pepe_exits, replay_pepe
from test_fractional_dust import market as market

import exit_manager as em
import position_manager as pm
import position_performance as performance
import trade_assistant as ta


def buy():
    plan = ta.build_plan(sig())
    return ta.execute(plan, approval=approved(plan), monitor=False)


def files():
    return {str(path): path.read_bytes() for path in em.STATE_DIR.rglob("*.json")}


@pytest.mark.parametrize("price", [D(110), D(90)])
def test_remaining_pnl_matches_cash_cost_and_estimated_net_value_without_touching_holdings(
    sim, price
):
    sim.free["FOO"] = D(5)  # Unrelated holdings must not enter bot valuation.
    initial_cash = sim.free["USDT"]
    rec = buy()
    cost = initial_cash - sim.free["USDT"]
    owned = sim.free["FOO"] + sim.locked["FOO"] - 5
    sim.price = price
    before = files(), len(sim.writes)
    now = datetime.fromtimestamp(sim.clock / 1000, timezone.utc) + timedelta(hours=2, minutes=15)
    view = pm.snapshot(rec["id"], rec["context"], now=now)
    result = view["performance"]
    expected = owned * price * D(".999") - cost
    assert result["remaining_cost"] == cost and result["quantity"] == owned
    assert result["unrealized"] == expected and result["realized"] == 0
    assert result["unrealized_pct"] == expected / cost * 100
    assert result["age"] == 8100 and "Time in trade: 0d 2h 15m" in pm.details_text(view)
    assert (files(), len(sim.writes)) == before


def test_partial_tp_realized_plus_unsold_estimate_reconciles_whole_cash_flow_once(sim):
    initial_cash = sim.free["USDT"]
    rec = buy()
    entry_cost = initial_cash - sim.free["USDT"]
    first = next(iter(sim.lists.values()))
    sim.fill(sim.orders[first["orders"][0]["orderId"]], price="104.5")
    sim.price = D(110)
    view = pm.snapshot(rec["id"], rec["context"])
    result = view["performance"]
    remaining = sim.free["FOO"] + sim.locked["FOO"]
    expected_whole = sim.free["USDT"] - initial_cash + remaining * D(110) * D(".999")
    assert abs(result["total"] - expected_whole) < D("1e-24")
    assert result["realized"] > 0 and result["remaining_cost"] < entry_cost
    assert result["total"] == result["realized"] + result["unrealized"]
    assert "TP1: 104.5 — Executed" in pm.details_text(view)


def test_actual_base_fee_is_accounted_as_fewer_units_without_double_charging_cash(sim):
    sim.fee_asset = "FOO"
    initial_cash = sim.free["USDT"]
    rec = buy()
    cost = initial_cash - sim.free["USDT"]
    owned = sim.free["FOO"] + sim.locked["FOO"]
    result = pm.snapshot(rec["id"], rec["context"])["performance"]
    assert result["remaining_cost"] == cost
    assert result["unrealized"] == owned * sim.price * D(".999") - cost
    assert result["quantity"] < D(rec["orders"][0]["executedQty"])


def test_bnb_entry_fee_uses_historical_fill_reference_even_when_current_bnb_changes(
    sim, monkeypatch
):
    enabled(sim, monkeypatch)
    cash, bnb = sim.free["USDT"], sim.free["BNB"]
    rec = buy()
    cost = cash - sim.free["USDT"] + (bnb - sim.free["BNB"]) * D(600)
    sim.bnb_price = D(9999)
    sim.price = D(110)
    before = files(), len(sim.writes)
    result = pm.snapshot(rec["id"], rec["context"])["performance"]
    assert abs(result["remaining_cost"] - cost) < D("1e-24")
    assert abs(
        result["unrealized"] - ((sim.free["FOO"] + sim.locked["FOO"]) * D(110) * D(".999") - cost)
    ) < D("1e-24")
    assert (files(), len(sim.writes)) == before
    assert all("fee_valuation" not in row for rec in em.load_trades() for row in em.executions(rec))
    assert result["bnb_reference"] and "historical USDT reference values" in "\n".join(
        performance.lines(result)
    )


def test_missing_bnb_history_is_unavailable_without_inventing_zero_fees(sim, monkeypatch):
    enabled(sim, monkeypatch)
    rec = buy()
    sim.response_override = lambda m, path, data: [] if path in {"aggTrades", "klines"} else None
    before = files(), len(sim.writes)
    view = pm.snapshot(rec["id"], rec["context"])
    assert view["performance"]["status"] == "unavailable"
    assert "Performance unavailable" in pm.details_text(view)
    assert "Estimated unrealized P&L: +0.0000" not in pm.details_text(view)
    assert (files(), len(sim.writes)) == before


@pytest.mark.parametrize(
    "quote",
    [
        {"symbol": "OTHERUSDT", "price": "100"},
        {"symbol": "FOOUSDT", "price": "0"},
        {"symbol": "FOOUSDT", "price": "NaN"},
    ],
)
def test_invalid_reference_quote_withholds_unrealized_pnl_but_keeps_known_cost(sim, quote):
    rec = buy()
    sim.response_override = lambda m, path, data: quote if path == "ticker/price" else None
    view = pm.snapshot(rec["id"], rec["context"])
    assert view["price"] is None and view["performance"]["unrealized"] is None
    assert view["performance"]["remaining_cost"] > 0
    assert "Estimated unrealized P&L: unavailable" in pm.details_text(view)


def test_missing_current_fee_schedule_is_not_replaced_with_old_plan_rate(sim):
    rec = buy()

    def unavailable(m, path, data):
        if path == "account/commission":
            raise TimeoutError("API_SECRET=private")

    sim.response_override = unavailable
    view = pm.snapshot(rec["id"], rec["context"])
    assert view["performance"]["market_value"] > 0 and view["performance"]["unrealized"] is None
    text = pm.details_text(view)
    assert "Estimated sell fees unavailable" in text and "private" not in text


def test_failed_order_refresh_never_renders_saved_fills_as_current_performance(sim):
    rec = buy()
    sim.read_failures = 1
    before = files(), len(sim.writes)
    view = pm.snapshot(rec["id"], rec["context"])
    text = pm.details_text(view)
    assert view["error"] and "Performance unavailable" in text
    assert "Estimated unrealized P&L:" not in text
    assert (files(), len(sim.writes)) == before


def test_crossing_target_price_does_not_invent_a_target_fill(sim):
    rec = buy()
    sim.price = D(120)
    view = pm.snapshot(rec["id"], rec["context"])
    assert view["performance"]["distances"]["TP1"] < 0
    assert "TP1: 104.5 — Pending" in pm.details_text(view)
    assert "Move from reference to level" in pm.details_text(view)


def test_entry_watch_has_no_invented_position_pnl(sim):
    sim.price = D(101)
    plan = ta.build_plan({**sig(), "entry_mode": "watch", "manual_override": True})
    rec = ta.execute(plan, approval=approved(plan), monitor=False)
    view = pm.snapshot(rec["id"], rec["context"])
    assert view["performance"]["status"] == "pending"
    assert "No buy filled" in pm.details_text(view) and not sim.writes


def test_fully_sold_trade_pending_monitor_closure_has_realized_pnl_without_a_sell_fee_crash(sim):
    initial_cash = sim.free["USDT"]
    rec = buy()
    for parent in sim.lists.values():
        sim.fill(sim.orders[parent["orders"][0]["orderId"]], price="104.5")
    view = pm.snapshot(rec["id"], rec["context"])
    result = view["performance"]
    assert result["status"] == "flat" and result["unrealized"] == 0
    assert abs(result["realized"] - (sim.free["USDT"] - initial_cash)) < D("1e-24")
    assert "No remaining bot quantity" in pm.details_text(view)
    assert em.load_trades()[0]["state"] == "protected"  # View cannot close the ledger.


def test_closed_dust_is_never_included_in_active_marked_profit(sim, market):
    rec = replay_pepe(sim)
    fill_pepe_exits(sim, rec)
    em.reconcile_trade(rec)
    view = pm.snapshot(rec["id"], rec["context"])
    assert view["performance"]["status"] == "closed"
    assert view["performance"]["remaining_cost"] == view["performance"]["market_value"] == 0
    assert em.inventory(view["record"]) == D(
        ".05"
    ) and "Retained dust: 0.05 PEPE" in pm.details_text(view)


def test_no_reporting_path_can_write_journals_or_submit_cancel_sell(sim, monkeypatch):
    rec = buy()

    def forbidden(*args, **kwargs):
        pytest.fail("Valuation may not write or trade")

    for name in (
        "_atomic_write",
        "submit_order",
        "cancel_protection",
        "reconcile_trade",
        "emergency_sell_free",
    ):
        monkeypatch.setattr(em, name, forbidden)
    view = pm.snapshot(rec["id"], rec["context"])
    assert view["performance"]["status"] == "ready"


def test_duration_does_not_show_a_false_zero_if_entry_time_is_in_the_future(sim):
    rec = buy()
    now = datetime.fromtimestamp(sim.clock / 1000, timezone.utc) - timedelta(seconds=1)
    view = pm.snapshot(rec["id"], rec["context"], now=now)
    assert view["performance"]["age"] is None and "timestamp is ahead" in pm.details_text(view)
    with pytest.raises(ValueError, match="timezone"):
        performance.calculate(rec, D(100), D(".001"), now=now.replace(tzinfo=None))
