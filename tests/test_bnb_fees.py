"""Actual BNB fill commissions, fallback reserves and persistent valuation evidence."""

from decimal import Decimal as D

import pytest
from test_bot import approved, sig

import exchange as ex
import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta


def enabled(sim, monkeypatch):
    monkeypatch.setattr(em, "ALLOW_BNB_FEES", True)
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    sim.bnb_discount = True
    sim.fee_asset = "BNB"


def buy(sim):
    plan = ta.build_plan(sig())
    return ta.execute(plan, approval=approved(plan))


def fill_exits(sim):
    for item in sim.lists.values():
        sim.fill(sim.orders[item["orders"][0]["orderId"]], price="104.5")


def test_bnb_fee_support_is_explicitly_enabled(sim, monkeypatch):
    sim.bnb_discount = True
    with pytest.raises(ValueError, match="ALLOW_BNB_FEES=true"):
        ta.build_plan(sig())
    enabled(sim, monkeypatch)
    plan = ta.build_plan(sig())
    assert plan["bnb_discount"] and plan["bnb_fee_support"]
    assert plan["fee_rate"] == D(".001")
    assert plan["discounted_fee_rate"] == D(".00075")
    text = tg.proposal_details_text(sig(), plan, "")
    assert "25.00%" in text and "reserves full fees" in text
    assert not sim.writes


def test_discount_applies_only_to_standard_fees(sim, monkeypatch):
    enabled(sim, monkeypatch)

    def response(method, path, data):
        if path == "account/commission":
            return {
                "symbol": "FOOUSDT",
                "standardCommission": {
                    "maker": ".001",
                    "taker": ".001",
                    "buyer": "0",
                    "seller": "0",
                },
                "taxCommission": {"maker": ".0002", "taker": ".0002", "buyer": "0", "seller": "0"},
                "specialCommission": {
                    "maker": ".0003",
                    "taker": ".0003",
                    "buyer": "0",
                    "seller": "0",
                },
                "discount": {
                    "enabledForAccount": True,
                    "enabledForSymbol": True,
                    "discountAsset": "BNB",
                    "discount": ".75",
                },
            }

    sim.response_override = response
    schedule = em.commission_schedule("FOOUSDT")
    assert schedule["reserve_rate"] == D(".0015")
    assert schedule["discounted_rate"] == D(".00125")


def test_actual_bnb_commissions_are_accounted_once_without_reapplying_discount(sim, monkeypatch):
    enabled(sim, monkeypatch)
    initial_usdt, initial_bnb = sim.free["USDT"], sim.free["BNB"]
    rec = buy(sim)
    assert rec["state"] == "protected" and len(sim.lists) == 2
    fill_exits(sim)
    assert em.reconcile_trade(rec)
    charged = sum((em.D(row["commission"]) for row in sim.trades), D(0))
    assert abs(initial_bnb - sim.free["BNB"] - charged) < D("1e-23")
    expected = sim.free["USDT"] - initial_usdt - charged * sim.bnb_price
    assert abs(sum(em.D(e["pnl"]) for e in rec["realized_events"]) - expected) < D("1e-23")
    assert "preceding BNBUSDT trade" in rec["pnl_valuation"]
    assert all("fee_valuation" in row for order in rec["orders"] for row in order["trades"])
    assert em.load_trades()[0]["state"] == "closed"


def test_bnb_shortfall_falls_back_to_actual_usdt_fees(sim, monkeypatch):
    enabled(sim, monkeypatch)
    sim.free["BNB"] = D(0)
    before = sim.free["USDT"]
    rec = buy(sim)
    fill_exits(sim)
    em.reconcile_trade(rec)
    assert all(row["commissionAsset"] == "USDT" for row in sim.trades)
    assert abs(
        sum(em.D(e["pnl"]) for e in rec["realized_events"]) - (sim.free["USDT"] - before)
    ) < D("1e-23")
    assert not any("fee_valuation" in row for order in rec["orders"] for row in order["trades"])
    assert rec["state"] == "closed"


def test_bnb_buy_fee_then_usdt_exit_fallback_is_supported(sim, monkeypatch):
    enabled(sim, monkeypatch)
    rec = buy(sim)
    assert list(em.executions(rec, "BUY"))[0]["commissionAsset"] == "BNB"
    sim.free["BNB"] = D(0)
    fill_exits(sim)
    em.reconcile_trade(rec)
    assert rec["state"] == "closed"
    assert all(row["commissionAsset"] == "USDT" for row in em.executions(rec, "SELL"))


def test_reference_uses_fill_time_not_reconciliation_price(sim, monkeypatch):
    enabled(sim, monkeypatch)
    rec = buy(sim)
    prices = {sim.clock: D(600)}
    for price, item in zip((D(700), D(800)), sim.lists.values()):
        sim.clock += 2000
        sim.bnb_price = price
        prices[sim.clock] = price
        sim.fill(sim.orders[item["orders"][0]["orderId"]], price="104.5")
    sim.bnb_price = D(9999)
    em.reconcile_trade(rec)
    for order in rec["orders"]:
        for row in order["trades"]:
            assert em.D(row["fee_valuation"]["price"]) == prices[row["time"]]
    assert rec["state"] == "closed"


def test_missing_conversion_history_does_not_delay_initial_ocos_or_unwind(sim, monkeypatch):
    enabled(sim, monkeypatch)

    def missing(method, path, data):
        if path == "aggTrades":
            return []

    sim.response_override = missing
    rec = buy(sim)
    assert rec["state"] == "protected" and len(sim.lists) == 2
    with pytest.raises(ex.Uncertain, match="history missing"):
        em.close_bot_trade("FOOUSDT", "CLOSE_FOOUSDT")
    saved = em.load_trades()[0]
    assert em.inventory(saved) == 0  # Exit completed; only accounting remains unresolved.
    assert all(order["status"] in em.TERMINAL for order in saved["orders"])
    assert em.active_trades()
    sim.response_override = None
    assert em.reconcile_trade(saved)
    assert saved["state"] == "closed"


def test_stored_bnb_opt_in_survives_restart_with_environment_setting_off(sim, monkeypatch):
    enabled(sim, monkeypatch)
    buy(sim)
    fill_exits(sim)
    monkeypatch.setattr(em, "ALLOW_BNB_FEES", False)
    saved = em.load_trades()[0]
    assert saved["plan"]["bnb_fee_support"] == "True"
    assert em.reconcile_trade(saved)
    assert saved["state"] == "closed"
    assert em.load_trades()[0]["state"] == "closed"


def test_closed_bnb_record_with_missing_reference_fails_closed(sim, monkeypatch):
    enabled(sim, monkeypatch)
    rec = buy(sim)
    fill_exits(sim)
    em.reconcile_trade(rec)
    next(row for order in rec["orders"] for row in order["trades"]).pop("fee_valuation")
    em.save_trade(rec)
    with pytest.raises(RuntimeError, match="journal invalid"):
        em.todays_loss()


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"a": 1, "T": 999999, "p": "600"}],
        [{"a": 1, "T": 1000, "p": "NaN"}],
        [{"a": 1, "T": 1000, "p": "600"}] * 1000,
        [{"a": 1, "T": 1000, "p": "600"}, {"a": 1, "T": 1000, "p": "601"}],
    ],
)
def test_invalid_or_incomplete_reference_does_not_become_zero_fee(sim, rows):
    def response(method, path, data):
        if path == "aggTrades":
            return rows

    sim.response_override = response
    with pytest.raises((ex.Uncertain, ValueError)):
        em.bnb_fee_reference(1000)


def test_partial_buy_can_be_unwound_even_when_bnb_reference_is_unavailable(sim, monkeypatch):
    enabled(sim, monkeypatch)
    sim.buy_behavior = "expired"

    def missing(method, path, data):
        if path == "aggTrades":
            return []

    sim.response_override = missing
    rec = buy(sim)
    assert em.inventory(rec) == 0
    assert rec["state"] == "needs_attention"
    order_requests = [d for m, p, d in sim.writes if m == "post" and p == "order"]
    assert [d["side"] for d in order_requests] == ["BUY", "SELL"]
    sim.response_override = None
    assert em.reconcile_trade(rec)
    assert rec["state"] == "closed"
    assert len([d for m, p, d in sim.writes if m == "post" and p == "order"]) == 2


def test_bnbusdt_fee_inventory_edge_case_is_explicitly_blocked(sim, monkeypatch):
    enabled(sim, monkeypatch)
    profile = sim.client.v3_get_account_commission(symbol="FOOUSDT")
    profile["symbol"] = "BNBUSDT"

    def response(method, path, data):
        if path == "account/commission":
            return profile

    sim.response_override = response
    with pytest.raises(ValueError, match="base-fee inventory reserve"):
        em.commission_schedule("BNBUSDT")
    assert not sim.writes


def test_fee_reserve_covers_maker_target_when_maker_fee_exceeds_taker(sim, monkeypatch):
    enabled(sim, monkeypatch)
    profile = sim.client.v3_get_account_commission(symbol="FOOUSDT")
    profile["standardCommission"]["maker"] = ".002"

    def response(method, path, data):
        if path == "account/commission":
            return profile

    sim.response_override = response
    schedule = em.commission_schedule("FOOUSDT")
    assert schedule["reserve_rate"] == D(".002")
    assert schedule["discounted_rate"] == D(".0015")
