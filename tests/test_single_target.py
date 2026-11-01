"""One OCO must protect all tradable bot inventory with replayable accounting."""

import copy
from decimal import Decimal as D

import pytest
from simulator import Crash
from test_bot import approved, sig
from test_position_exit_lifecycle import request

import entry_readiness as ready
import exit_manager as em
import portfolio_reporting
import position_manager as pm
import trade_assistant as ta


def signal(**extra):
    return em.select_targets({**sig(), **extra}, "single")


def buy(sim, **extra):
    plan = ta.build_plan(signal(**extra))
    return ta.execute(plan, approval=approved(plan), monitor=False)


@pytest.mark.parametrize("asset", ["USDT", "FOO"])
def test_full_net_inventory_uses_exactly_one_oco_and_one_slot(sim, asset):
    sim.free["FOO"], sim.fee_asset = D(8), asset
    rec = buy(sim)
    assert rec["state"] == "protected" and len(rec["oco"]) == len(sim.lists) == 1
    assert len(em.active_trades()) == 1 and "t2" not in rec["plan"]
    assert D(rec["plan"]["rest"]) == 0
    assert em.coverage(rec) == em.round_to(em.inventory(rec), D(".001"))
    assert sim.free["FOO"] >= 8
    oco = [data for _, path, data in sim.writes if path == "orderList/oco"]
    assert len(oco) == 1 and D(oco[0]["quantity"]) == em.coverage(rec)
    assert D(oco[0]["abovePrice"]) == D(rec["plan"]["t1"])
    assert D(oco[0]["belowStopPrice"]) == D(rec["plan"]["stop"])


@pytest.mark.parametrize("purpose", ["target", "stop"])
@pytest.mark.parametrize("base_fee", [False, True])
def test_one_target_or_stop_closes_the_position_with_actual_pnl_and_retained_dust(
    sim, purpose, base_fee
):
    initial_cash = sim.free["USDT"]
    sim.fee_asset = "FOO" if base_fee else "USDT"
    rec = buy(sim)
    sim.fee_asset = "USDT"
    leg = next(o for o in rec["orders"] if o["purpose"] == purpose)
    sim.fill(sim.orders[leg["orderId"]], price=rec["plan"]["t1" if purpose == "target" else "stop"])
    assert em.reconcile_trade(rec) and rec["state"] == "closed"
    assert not em.active_trades() and len(sim.lists) == 1
    pnl = sum((D(e["pnl"]) for e in em.realized_events(rec)), D(0))
    retained = D(rec.get("retained_dust", {}).get("cost_basis_usdt", 0))
    # Cash spent on unsold dust is excluded from realized sales, never lost twice.
    assert abs(pnl - retained - (sim.free["USDT"] - initial_cash)) < D(".00000001")
    assert bool(rec.get("retained_dust")) == base_fee
    assert em.load_trades()[0]["state"] == "closed"
    text = em.trade_result_text(rec)
    assert "TRADE CLOSED" in text and "Take Profit-2" not in text
    assert (
        "Take profit executed successfully"
        if purpose == "target"
        else "Stop loss executed successfully"
    ) in text


def test_six_dollars_can_support_one_five_dollar_exit_but_not_two(sim):
    sim.min_notional, sim.free["USDT"] = "5", D("6.1")
    result = ready.check("FOOUSDT", mode="manual", signal=signal(manual_override=True))
    assert result["minimum"] < D("6.1")
    plan = ta.build_plan(signal(manual_override=True))
    assert plan["cost"] <= D("6.1") and plan["rest"] == 0
    with pytest.raises(ready.Unavailable) as caught:
        ready.check("FOOUSDT", mode="manual", signal={**sig(), "manual_override": True})
    assert caught.value.code == "FUNDS"
    with pytest.raises(ValueError):
        ta.build_plan({**sig(), "manual_override": True})
    assert not sim.writes


@pytest.mark.parametrize("normal", [False, True])
def test_minimum_funding_uses_one_stop_valid_leg_and_common_quantity_steps(sim, normal):
    sim.min_notional = "5"
    f = em.symbol_filters("FOOUSDT")
    f.update(step=D(".003"), market_step=D(".002"))
    minimum = ready.minimum_funding(f, 100, ".001", stop=97, normal=normal, exit_style="single")
    assert minimum["rest"] == 0 and minimum["quantity"] % D(".006") == 0
    threshold = D(5) * (D("1.05") if normal else 1)
    assert minimum["half"] * 97 >= threshold
    smaller = em.round_to((minimum["quantity"] - D(".006")) * D(".999"), f["step"])
    assert smaller * 97 < threshold


@pytest.mark.parametrize("scope", ["symbol", "exchange"])
def test_capacity_reserves_one_list_two_orders_and_one_stop_algo(sim, monkeypatch, scope):
    original = sim.info

    def info():
        item = original()
        if scope == "symbol":
            item["filters"] += [
                {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 2},
                {"filterType": "MAX_NUM_ALGO_ORDERS", "maxNumAlgoOrders": 1},
                {"filterType": "MAX_NUM_ORDER_LISTS", "maxNumOrderLists": 1},
            ]
        return item

    monkeypatch.setattr(sim, "info", info)
    if scope == "exchange":
        sim.response_override = lambda m, path, data: (
            {
                "symbols": [info()],
                "exchangeFilters": [
                    {"filterType": "EXCHANGE_MAX_NUM_ORDERS", "maxNumOrders": 2},
                    {"filterType": "EXCHANGE_MAX_NUM_ALGO_ORDERS", "maxNumAlgoOrders": 1},
                    {"filterType": "EXCHANGE_MAX_NUM_ORDER_LISTS", "maxNumOrderLists": 1},
                ],
            }
            if path == "exchangeInfo"
            else None
        )
    assert ta.build_plan(signal())["exit_style"] == "single"
    with pytest.raises(ValueError, match="capacity"):
        ta.build_plan(sig())
    assert not sim.writes


def test_approval_cannot_switch_target_count_or_level_or_allocation(sim):
    one, two = ta.build_plan(signal()), ta.build_plan(sig())
    assert not ta.approval_bounds(one, two) and not ta.approval_bounds(two, one)
    assert not ta.approval_bounds(one, {**one, "t1": one["t1"] + 1})
    assert not ta.approval_bounds(two, {**two, "half": two["half"] + D(".001")})
    with pytest.raises(ValueError, match="approval"):
        ta.execute(one, approval=approved(two), monitor=False)
    assert not sim.writes


@pytest.mark.parametrize("value", ["bad", "", 1, True])
def test_unknown_target_structure_never_creates_an_order(sim, value):
    with pytest.raises(ValueError):
        ta.build_plan({**sig(), "exit_style": value})
    assert not sim.writes


def test_hidden_second_target_is_rejected_for_single_target(sim):
    with pytest.raises(ValueError, match="one take-profit"):
        ta.build_plan({**sig(), "exit_style": "single"})
    assert not sim.writes


@pytest.mark.parametrize("kind", ["protected", "confirmed_exit", "legacy_two"])
def test_restart_retains_target_structure_without_duplicate_exchange_writes(sim, kind):
    sim.free["FOO"] = D(8)
    if kind == "legacy_two":
        p = ta.build_plan(sig())
        rec = ta.execute(p, approval=approved(p), monitor=False)
        rec["plan"].pop("exit_style")
        rec["approved_bounds"].pop("exit_style")
        em.save_trade(rec)
    else:
        rec = buy(sim)
    if kind == "confirmed_exit":
        request(rec)
    before = len(sim.writes)
    em.recover_trades()
    saved = em.load_trades()[0]
    if kind == "confirmed_exit":
        assert saved["state"] == "closed" and not em.active_trades()
        assert sim.free["FOO"] == 8
        count = len(sim.writes)
        em.recover_trades()
        assert len(sim.writes) == count
    else:
        assert saved["state"] == "protected" and len(sim.writes) == before
        assert len(saved["oco"]) == (2 if kind == "legacy_two" else 1)


@pytest.mark.parametrize("fault", ["style", "target", "oco_count"])
def test_journal_target_tampering_blocks_new_entries(sim, fault):
    rec = buy(sim)
    if fault == "style":
        rec["plan"]["exit_style"] = "two"
    elif fault == "target":
        rec["oco"][0]["tp"] = str(D(rec["plan"]["t1"]) + 1)
    else:
        rec["oco"].append(copy.deepcopy(rec["oco"][0]))
    em.save_trade(rec)
    with pytest.raises(RuntimeError, match="journal invalid"):
        em.load_trades()


@pytest.mark.parametrize("behavior", ["partial", "expired", "expired_empty", "timeout_accept"])
def test_partial_or_uncertain_buy_does_not_repeat_or_sell_unrelated_inventory(sim, behavior):
    sim.free["FOO"], sim.buy_behavior = D(8), behavior
    rec = buy(sim)
    em.reconcile_trade(rec)
    assert len([1 for _, path, d in sim.writes if path == "order" and d.get("side") == "BUY"]) == 1
    assert sim.free["FOO"] >= 8 and len(rec["oco"]) <= 1
    if behavior in ("partial", "expired", "expired_empty"):
        assert rec["state"] == "closed"


@pytest.mark.parametrize("behavior", ["timeout_accept", "rejected", "crash"])
def test_single_oco_submission_fault_recovers_without_rebuying_or_a_second_list(sim, behavior):
    sim.free["FOO"] = D(8)
    sim.oco_behavior[1] = behavior
    try:
        buy(sim)
    except Crash:
        assert behavior == "crash"
    em.recover_trades()
    saved = em.load_trades()[0]
    assert len([1 for _, path, d in sim.writes if path == "order" and d.get("side") == "BUY"]) == 1
    assert len([1 for _, path, _ in sim.writes if path == "orderList/oco"]) == 1
    assert sim.free["FOO"] >= 8
    assert saved["state"] == ("closed" if behavior == "rejected" else "protected")


def test_single_position_views_and_portfolio_have_no_phantom_second_target(sim):
    rec = buy(sim)
    count = len(sim.writes)
    view = pm.snapshot(rec["id"], rec["context"])
    text = pm.details_text(view)
    assert "Single target" in text and "TP:" in text and "TP2" not in text
    assert set(view["performance"]["distances"]) == {"SL", "TP"}
    assert "Trade slots: 1 / 1" in portfolio_reporting.report()
    assert len(sim.writes) == count


def test_approved_single_entry_watch_preserves_choice_when_it_triggers(sim):
    sim.price = D(101)
    rec = buy(sim, entry_mode="watch", manual_override=True)
    assert rec["state"] == "watching_entry" and not sim.writes
    sim.price = D(100)
    em.reconcile_trade(rec)
    assert rec["state"] == "protected" and len(rec["oco"]) == 1
    assert "t2" not in rec["plan"] and em.protection_sufficient(rec)


def test_partial_single_take_profit_unwinds_only_the_uncovered_bot_remainder(sim):
    sim.free["FOO"] = D(8)
    rec = buy(sim)
    top = next(o for o in rec["orders"] if o["purpose"] == "target")
    part = em.round_to(D(top["qty"]) / 2, D(".001"))
    sim.fill(sim.orders[top["orderId"]], qty=part, price=rec["plan"]["t1"], terminal=False)
    assert em.reconcile_trade(rec)
    assert rec["state"] == "closed" and sim.free["FOO"] == 8
    assert len(rec["oco"]) == 1 and not em.active_trades()
    count = len(sim.writes)
    em.recover_trades()
    assert len(sim.writes) == count


@pytest.mark.parametrize("checkpoint", range(1, 15))
def test_crash_at_single_target_journal_boundaries_never_repeats_a_buy_or_list(
    sim, monkeypatch, checkpoint
):
    sim.free["FOO"] = D(8)
    original = em.save_trade
    count = 0

    def crash(record):
        nonlocal count
        original(record)
        count += 1
        if count == checkpoint:
            raise Crash()

    monkeypatch.setattr(em, "save_trade", crash)
    try:
        buy(sim)
    except Crash:
        pass
    monkeypatch.setattr(em, "save_trade", original)
    em.recover_trades()
    assert len([1 for _, path, d in sim.writes if path == "order" and d.get("side") == "BUY"]) <= 1
    assert len([1 for _, path, _ in sim.writes if path == "orderList/oco"]) <= 1
    assert sim.free["FOO"] >= 8
    for rec in em.active_trades():
        assert rec["state"] == "needs_attention" or em.protection_sufficient(rec)
