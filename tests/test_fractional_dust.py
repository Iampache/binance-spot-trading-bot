"""Replay the reported PEPE 0.05-unit fee tail through the pinned SDK transport."""

import copy
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from test_bot import approved

import exchange as ex
import exit_manager as em
import position_manager as pm
import safety
import trade_assistant as ta
import trade_reporting
import trading_settings as settings


@pytest.fixture
def market(sim, monkeypatch):
    original = sim.info()
    pepe, uni = copy.deepcopy(original), copy.deepcopy(original)
    pepe.update(symbol="PEPEUSDT", baseAsset="PEPE", baseCommissionPrecision=2)
    uni.update(symbol="UNIUSDT", baseAsset="UNI")
    for rule in pepe["filters"]:
        if rule["filterType"] == "LOT_SIZE":
            rule.update(stepSize="1", minQty="1", maxQty="1000000000")
        elif rule["filterType"] == "MARKET_LOT_SIZE":
            rule.update(maxQty="1000000000")
        elif rule["filterType"] == "PRICE_FILTER":
            rule.update(tickSize="0.00000001", minPrice="0.00000001")
        elif rule["filterType"] == "MIN_NOTIONAL":
            rule["minNotional"] = "5"
    for rule in uni["filters"]:
        if rule["filterType"] == "LOT_SIZE":
            rule.update(stepSize=".01", minQty=".01")
        elif rule["filterType"] == "PRICE_FILTER":
            rule["tickSize"] = ".001"
    prices = {"PEPEUSDT": D(".00000694"), "UNIUSDT": D("6.986")}
    ticks = {"PEPEUSDT": D(".00000001"), "UNIUSDT": D(".001")}
    sim.symbols = list(prices)
    sim.free.update(PEPE=D(7), UNI=D(0))
    sim.locked.update(PEPE=D(0), UNI=D(0))

    def response(method, path, data):
        if path == "exchangeInfo":
            return {"symbols": [copy.deepcopy(pepe), copy.deepcopy(uni)]}
        symbol = data.get("symbol")
        if symbol not in prices:
            return None
        price = prices[symbol]
        if path == "ticker/price":
            return {"symbol": symbol, "price": str(price)}
        if path == "ticker/24hr":
            return {
                "symbol": symbol,
                "lastPrice": str(price),
                "quoteVolume": "50000000",
                "priceChangePercent": "1",
            }
        if path == "ticker/bookTicker":
            return {
                "symbol": symbol,
                "bidPrice": str(price - ticks[symbol]),
                "askPrice": str(price),
            }
        if path == "depth":
            return {
                "bids": [[str(price - ticks[symbol]), "1000000000"]],
                "asks": [[str(price), "1000000000"]],
            }
        return None

    sim.response_override = response
    original_fill = sim.fill

    def fill(order, qty=None, price=None, terminal=True):
        return original_fill(
            order,
            qty=qty,
            price=prices[order["symbol"]] if price is None else price,
            terminal=terminal,
        )

    monkeypatch.setattr(sim, "fill", fill)
    settings.update("max_open_trades", 2, expected_revision=0, actor="test-owner")
    return prices


def signal(symbol):
    if symbol == "UNIUSDT":
        levels = ("6.986", "6.916", "7.114", "7.193")
    else:
        levels = (".00000694", ".00000680", ".00000715", ".00000729")
    return {
        "symbol": symbol,
        **dict(zip(("entry", "stop", "t1", "t2"), levels)),
        "setup": "test replay",
    }


def replay_pepe(sim):
    """Use valid fixed order bounds to replay the user's exact fill and base fee."""
    plan = ta.build_plan(signal("PEPEUSDT"))
    f = em.symbol_filters("PEPEUSDT")
    plan["qty"] = D(1724953)
    plan["cost"] = plan["qty"] * plan["price"] * (1 + plan["fee_rate"])
    plan["loss"] = ta.risk_amount(plan["qty"], plan["price"], plan["stop"], plan["fee_rate"])
    net = em.round_to(plan["qty"] * (1 - plan["fee_rate"]), f["step"])
    plan["half"] = em.round_to(net / 2, f["step"])
    plan["rest"] = net - plan["half"]
    em.preflight(plan, f)
    rec = em.new_trade(plan, approval=approved(plan))
    item = em.intent(rec, "BUY", plan["qty"], "buy")
    item["price_cap"] = em.fmt(plan["price"])
    sim.fee_asset, sim.base_fee_step = "PEPE", D(".01")
    em.submit_order(rec, item)
    assert item["status"] == "FILLED" and D(item["trades"][0]["commission"]) == D("1724.95")
    sim.fee_asset = "USDT"
    em.submit_protection(rec, em.inventory(rec), plan["t1"], plan["t2"], plan["stop"], f)
    return rec


def buy_uni():
    plan = ta.build_plan(signal("UNIUSDT"))
    return ta.execute(plan, approval=approved(plan), monitor=False)


def fill_pepe_exits(sim, rec):
    for parent in rec["oco"]:
        order = next(
            o
            for o in rec["orders"]
            if o["cid"] in parent["order_cids"] and o["purpose"] == "target"
        )
        sim.fill(sim.orders[order["orderId"]], price=parent["tp"])


def test_exact_reported_gap_heals_without_cancelling_stops_or_selling_anything(sim, market):
    uni = buy_uni()
    rec = replay_pepe(sim)
    assert em.inventory(rec) == D("1723228.05") and em.coverage(rec) == D("1723228")
    assert em.inventory(uni) == D("1.71") and em.coverage(uni) == D("1.71")
    em.mark_attention(rec, ValueError("old generic failure"))
    count = len(sim.writes)
    em.monitor_trade(rec, max_checks=1)
    saved = next(r for r in em.load_trades() if r["symbol"] == "PEPEUSDT")
    assert saved["state"] == "protected"
    assert D(saved["protection_dust"]["quantity"]) == D(".05")
    assert em.inventory(saved) == D("1723228.05") and em.coverage(saved) == D("1723228")
    assert len(sim.writes) == count and "recovery_reason_code" not in saved
    report = em.status_report()
    assert (
        "PEPEUSDT: protected" in report and "UNIUSDT: protected" in report and "0.05 PEPE" in report
    )
    with pytest.raises(ta.ActiveTradeError):
        ta.build_plan(signal("PEPEUSDT"))  # Two genuine positions still consume both slots.
    with pytest.raises(ta.ActiveTradeError, match="Active trades: PEPEUSDT, UNIUSDT"):
        ta.build_plan(signal("UNIUSDT"))


def test_manual_exit_retains_exact_dust_and_cost_and_frees_only_its_trade_slot(sim, market):
    uni = buy_uni()
    rec = replay_pepe(sim)
    before = sim.free["USDT"]
    assert em.close_bot_trade("PEPEUSDT", "CLOSE_PEPEUSDT")
    saved = next(r for r in em.load_trades() if r["symbol"] == "PEPEUSDT")
    assert saved["id"] == rec["id"] and saved["state"] == "closed" and em.valid_retained_dust(saved)
    assert em.inventory(saved) == D(".05") and sim.free["PEPE"] == D("7.05")
    acquired, cost = em.acquisition_basis(saved)
    dust_cost = D(saved["retained_dust"]["cost_basis_usdt"])
    assert dust_cost == cost * D(".05") / acquired
    proceeds = sim.free["USDT"] - before
    realized = sum((D(e["pnl"]) for e in saved["realized_events"]), D(0))
    assert abs(realized - (proceeds - cost + dust_cost)) < D("1e-24")
    assert em.active_trades()[0]["id"] == uni["id"] and em.coverage(uni) == D("1.71")
    text = em.trade_result_text(saved)
    assert "TRADE CLOSED — dust retained" in text and "Remaining bot quantity: 0.05" in text
    assert "cost basis" in text and "excluded from realized P&L" in text
    assert "Retained dust ledger" in em.status_report()
    next_plan = ta.build_plan(signal("PEPEUSDT"))
    assert next_plan["baseline_base"] == D("7.05")
    assert next_plan["retained_dust_cost"] == dust_cost
    assert next_plan["committed_risk"] == D(uni["plan"]["loss"]) + dust_cost


def test_take_profits_close_tradable_position_and_pnl_lists_unsold_cost_separately(sim, market):
    rec = replay_pepe(sim)
    fill_pepe_exits(sim, rec)
    count = len(sim.writes)
    assert em.reconcile_trade(rec)
    assert len(sim.writes) == count and not em.active_trades()
    report = trade_reporting.todays_pnl(datetime.fromtimestamp(sim.clock / 1000, timezone.utc))
    assert "Completed: 1" in report and "0.05 PEPE | retained cost" in report
    assert "not realized P&L" in report
    view = pm.snapshot(rec["id"], rec["context"])
    assert view["action"] is None and "dust retained" in pm.details_text(view)


def test_new_same_pair_trade_preserves_old_dust_and_old_exit_cannot_target_new_trade(sim, market):
    old = replay_pepe(sim)
    fill_pepe_exits(sim, old)
    em.reconcile_trade(old)
    plan = ta.build_plan(signal("PEPEUSDT"))
    new = ta.execute(plan, approval=approved(plan), monitor=False)
    assert new["baseline_base"] == "7.05"
    count = len(sim.writes)
    assert em.close_bot_trade("PEPEUSDT", "CLOSE_PEPEUSDT", trade_id=old["id"])
    assert len(sim.writes) == count and em.active_trades()[0]["id"] == new["id"]
    assert em.inventory(next(r for r in em.load_trades() if r["id"] == old["id"])) == D(".05")


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", "0"),
        ("quantity", "1"),
        ("asset", "UNI"),
        ("lot_step", ".01"),
        ("cost_basis_usdt", "0"),
        ("cost_basis_usdt", ".001"),
    ],
)
def test_dust_evidence_is_replayed_not_blindly_trusted(sim, market, field, value):
    rec = replay_pepe(sim)
    fill_pepe_exits(sim, rec)
    em.reconcile_trade(rec)
    rec["retained_dust"][field] = value
    em.save_trade(rec)
    count = len(sim.writes)
    with pytest.raises(RuntimeError, match="journal invalid"):
        em.load_trades()
    assert len(sim.writes) == count


def test_a_whole_lot_of_unprotected_inventory_is_not_waived_as_dust(sim, market):
    rec = replay_pepe(sim)
    first = rec["oco"][0]
    target = next(
        o for o in rec["orders"] if o["cid"] in first["order_cids"] and o["purpose"] == "target"
    )
    sim.fill(sim.orders[target["orderId"]], qty=D(first["qty"]) - 1, terminal=False)
    em.monitor_trade(rec, max_checks=1)
    saved = em.active_trades()[0]
    assert (
        saved["state"] == "needs_attention"
        and saved["recovery_reason_code"] == "ORDER_MIN_NOTIONAL"
    )
    assert em.inventory(saved) - em.coverage(saved) == D("1.05")
    assert "retained_dust" not in saved
    assert not any(path == "order" and d.get("side") == "SELL" for _, path, d in sim.writes)


def test_unknown_exchange_evidence_is_not_excused_by_a_fractional_tail(sim, market):
    rec = replay_pepe(sim)
    count = len(sim.writes)
    sim.read_failures = 1
    em.monitor_trade(rec, max_checks=1)
    assert em.active_trades()[0]["state"] == "needs_attention" and len(sim.writes) == count
    assert "retained_dust" not in em.active_trades()[0]


def test_dust_cannot_close_when_the_recorded_balance_floor_was_removed(sim, market):
    rec = replay_pepe(sim)
    fill_pepe_exits(sim, rec)
    sim.free["PEPE"] = D(7)
    with pytest.raises(ex.OwnershipError):
        em.reconcile_trade(rec)
    assert em.active_trades() and "retained_dust" not in em.active_trades()[0]


def test_missing_bnb_fee_valuation_still_blocks_dust_closure_until_evidence_returns(
    sim, market, monkeypatch
):
    monkeypatch.setattr(em, "ALLOW_BNB_FEES", True)
    rec = replay_pepe(sim)
    sim.fee_asset = "BNB"
    fill_pepe_exits(sim, rec)
    original = sim.response_override
    sim.server_time_ms = sim.clock + 61000
    sim.response_override = lambda m, path, data: (
        [] if path == "aggTrades" else original(m, path, data)
    )
    em.monitor_trade(rec, max_checks=1)
    saved = em.active_trades()[0]
    assert saved["recovery_reason_code"] == "BNB_REFERENCE_MISSING" and "retained_dust" not in saved
    sim.response_override = original
    em.recover_trades()
    assert not em.active_trades() and em.valid_retained_dust(em.load_trades()[0])


def test_local_validation_codes_are_safe_and_sdk_value_errors_cannot_supply_them():
    class PrivateError(safety.RecoveryValidationError):
        def __str__(self):
            raise AssertionError("Do not render exception text")

    assert "ORDER_MIN_NOTIONAL" in safety.safe_error(
        PrivateError("private", reason_code="ORDER_MIN_NOTIONAL")
    )
    spoof = ValueError("API_SECRET=private")
    spoof.reason_code = "ORDER_MIN_NOTIONAL"
    assert safety.safe_error(spoof) == "ValueError: operation failed; reconcile exchange state"


def test_dust_closure_write_failure_remains_recoverable_without_another_sell(
    sim, market, monkeypatch
):
    rec = replay_pepe(sim)
    fill_pepe_exits(sim, rec)
    original_write = em._atomic_write

    def fail_closed(path, item):
        if item.get("state") == "closed":
            raise OSError("synthetic closure write failure")
        return original_write(path, item)

    count = len(sim.writes)
    with monkeypatch.context() as temporary:
        temporary.setattr(em, "_atomic_write", fail_closed)
        em.monitor_trade(rec, max_checks=1)
    saved = em.active_trades()[0]
    assert saved["state"] == "needs_attention" and "retained_dust" not in saved
    em.recover_trades()
    assert not em.active_trades() and em.valid_retained_dust(em.load_trades()[0])
    assert len(sim.writes) == count


def test_actionable_error_names_pair_and_safe_code_without_private_exception_text(
    sim, market, monkeypatch
):
    rec = replay_pepe(sim)
    messages = []

    def invalid_rules(symbol):
        raise safety.RecoveryValidationError("API_SECRET=private", reason_code="ORDER_LOT_SIZE")

    monkeypatch.setattr(em, "symbol_filters", invalid_rules)
    monkeypatch.setattr(em, "alert", lambda text: messages.append(text) or True)
    em.monitor_trade(rec, max_checks=1)
    saved = em.active_trades()[0]
    assert saved["recovery_reason_code"] == "ORDER_LOT_SIZE"
    assert any("PEPEUSDT | Trade" in text and "ORDER_LOT_SIZE" in text for text in messages)
    assert not any("API_SECRET" in text or "private" in text for text in messages)


@pytest.mark.parametrize("closure_write_fails", [False, True])
def test_tiny_partial_ioc_fill_is_costed_without_a_fictitious_sale_or_invalid_error_record(
    sim, monkeypatch, closure_write_fails
):
    sim.free["FOO"] = D(5)
    sim.fee_asset = "FOO"
    sim.buy_behavior = "expired"
    original_fill, original_write = sim.fill, em._atomic_write

    def small_partial(order, qty=None, price=None, terminal=True):
        return original_fill(
            order, qty=".001" if order["side"] == "BUY" else qty, price=price, terminal=terminal
        )

    def fail_closed(path, item):
        if item.get("state") == "closed" and closure_write_fails:
            raise OSError("synthetic closure disk failure")
        return original_write(path, item)

    monkeypatch.setattr(sim, "fill", small_partial)
    with monkeypatch.context() as temporary:
        temporary.setattr(em, "_atomic_write", fail_closed)
        plan = ta.build_plan(
            {"symbol": "FOOUSDT", "entry": 100, "stop": 97, "t1": 104.5, "t2": 107.5}
        )
        rec = ta.execute(plan, approval=approved(plan), monitor=False)
    if closure_write_fails:
        saved = em.active_trades()[0]
        assert saved["state"] == "needs_attention" and "retained_dust" not in saved
        em.recover_trades()
    saved = em.load_trades()[0]
    assert em.valid_retained_dust(saved) and not em.active_trades()
    assert D(saved["retained_dust"]["quantity"]) == D(".000999")
    assert D(saved["retained_dust"]["cost_basis_usdt"]) == D(".1")
    assert saved["realized_events"] == [] and sim.free["FOO"] == D("5.000999")
    assert saved["id"] == rec["id"]
    assert not any(path == "order" and d.get("side") == "SELL" for _, path, d in sim.writes)
