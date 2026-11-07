"""Readiness is a necessary gate, never approval or a weaker sizing policy."""

from decimal import Decimal as D

import pytest
from test_bot import approved, sig

import entry_readiness as ready
import exit_manager as em
import trade_assistant as ta
import trading_settings as settings


def buy(symbol="FOOUSDT", **extra):
    plan = ta.build_plan({**sig(), "symbol": symbol, **extra})
    return ta.execute(plan, approval=approved(plan), monitor=False)


def files():
    return {str(path): path.read_bytes() for path in em.STATE_DIR.rglob("*.json")}


def second_pair(sim):
    sim.symbols.append("BARUSDT")
    sim.free["BAR"] = sim.locked["BAR"] = D(0)
    settings.update("max_open_trades", 2, expected_revision=0, actor="test-owner")


@pytest.mark.parametrize(
    "normal,quantity,cost", [(False, ".105", "10.5105"), (True, ".111", "11.1111")]
)
def test_funding_requires_two_stop_valid_legs_after_quantity_and_fee_rounding(
    sim, normal, quantity, cost
):
    sim.min_notional = "5"
    rules = em.symbol_filters("FOOUSDT")
    result = ready.minimum_funding(rules, 100, ".001", stop=97, normal=normal)
    assert result["quantity"] == D(quantity) and result["cost"] == D(cost)
    floor = D(5) * (D("1.05") if normal else 1)
    assert result["half"] * 97 >= floor and result["rest"] * 97 >= floor
    previous_net = em.round_to((result["quantity"] - rules["step"]) * D(".999"), rules["step"])
    previous_half = em.round_to(previous_net / 2, rules["step"])
    assert min(previous_half, previous_net - previous_half) * 97 < floor


def test_incompatible_market_and_lot_steps_use_a_common_valid_lattice(sim):
    f = em.symbol_filters("FOOUSDT")
    f.update(step=D(".003"), market_step=D(".002"))
    result = ready.minimum_funding(f, 100, ".001")
    assert result["quantity"] == D(".030")
    assert result["quantity"] % f["market_step"] == result["quantity"] % f["step"] == 0
    assert result["half"] % f["step"] == result["rest"] % f["step"] == 0


def test_initial_funding_is_explicitly_provisional_until_selected_levels_are_checked(sim):
    sim.min_notional = "5"
    before = files(), len(sim.writes)
    preliminary = ready.check("FOOUSDT")
    selected = ready.check("FOOUSDT", mode="manual", signal={**sig(), "entry_mode": "watch"})
    assert preliminary["provisional"] and not selected["provisional"]
    assert selected["minimum"] > preliminary["minimum"]
    assert (files(), len(sim.writes)) == before


def test_six_dollars_free_is_not_enough_for_two_five_dollar_legs(sim):
    sim.min_notional, sim.free["USDT"] = "5", D("6.1")
    with pytest.raises(ready.Unavailable) as caught:
        ready.check("FOOUSDT", mode="manual")
    assert caught.value.code == "FUNDS" and "6.1000" in caught.value.reason
    assert "Increasing the investment cap does not add funds" in caught.value.reason
    assert caught.value.actions == ("portfolio",) and not sim.writes


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("style", ["single", "two"])
def test_planned_cost_includes_fees_and_cannot_exceed_spendable_cash(sim, manual, style):
    sim.free["USDT"] = D("11.50")
    signal = em.select_targets({**sig(), "manual_override": manual}, style)
    plan = ta.build_plan(signal)
    assert plan["cost"] == plan["qty"] * plan["price"] * (1 + plan["fee_rate"])
    assert 0 < plan["cost"] <= plan["balance"] == sim.free["USDT"]
    assert not sim.writes


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("style", ["single", "two"])
def test_balance_lost_after_proposal_is_rechecked_before_any_buy(sim, manual, style):
    plan = ta.build_plan(em.select_targets({**sig(), "manual_override": manual}, style))
    approval = approved(plan)
    sim.free["USDT"] = D("0")
    with pytest.raises(ValueError, match="No available USDT"):
        ta.execute(plan, approval=approval, monitor=False)
    assert not sim.writes and not em.load_trades()


def test_six_dollar_exit_minimum_needs_more_than_twelve_including_reserve(sim):
    sim.min_notional = "6"
    with pytest.raises(ready.Unavailable) as caught:
        ready.check("FOOUSDT")
    assert caught.value.code == "INVESTMENT_CAP" and "12.1121" in caught.value.reason
    assert "setup" in caught.value.actions and not sim.writes


def test_current_count_equal_to_limit_blocks_one_more_trade_without_writing(sim):
    buy()
    before = files(), len(sim.writes)
    with pytest.raises(ready.Unavailable) as caught:
        ready.check("BARUSDT", mode="manual")
    assert caught.value.code == "CAPACITY" and "FOOUSDT" in caught.value.reason
    assert "(1/1)" in caught.value.reason and "limit is 1" in caught.value.reason
    assert "setup" in caught.value.actions and (files(), len(sim.writes)) == before


def test_same_pair_remains_blocked_even_with_a_free_parallel_slot(sim):
    second_pair(sim)
    buy()
    with pytest.raises(ready.Unavailable) as caught:
        ready.check("FOOUSDT")
    assert caught.value.code == "DUPLICATE" and "setup" not in caught.value.actions


def test_unresolved_trade_requires_health_not_a_limit_increase(sim):
    rec = buy()
    em.mark_attention(rec, ValueError("synthetic unresolved state"))
    with pytest.raises(ready.Unavailable) as caught:
        ready.check("BARUSDT", mode="manual")
    assert caught.value.code == "RECONCILIATION" and "health" in caught.value.actions
    assert "setup" not in caught.value.actions


def test_manual_does_not_bypass_changed_or_external_exchange_orders(sim):
    external = dict(
        symbol="FOOUSDT",
        status="NEW",
        orderId=99999,
        origQty="1",
        executedQty="0",
        clientOrderId="external",
        side="SELL",
        type="LIMIT",
        price="100",
        orderListId=-1,
    )
    sim.response_override = lambda m, path, data: [external] if path == "openOrders" else None
    before = len(sim.writes)
    with pytest.raises(ready.Unavailable) as caught:
        ready.check("FOOUSDT", mode="manual")
    assert caught.value.code == "OWNERSHIP" and len(sim.writes) == before


def test_normal_daily_budget_blocks_algorithm_modes_but_retains_explicit_manual_policy(sim):
    second_pair(sim)
    settings.update("max_position_usdt", "200", expected_revision=1, actor="test-owner")
    buy(manual_override=True)
    with pytest.raises(ready.Unavailable) as caught:
        ready.check("BARUSDT", mode="normal")
    assert caught.value.code == "NORMAL_RISK"
    assert ready.check("BARUSDT", mode="manual")["remaining_risk"] < 0


def test_entry_watch_cash_is_reserved_even_though_it_has_no_buy_fill(sim):
    second_pair(sim)
    settings.update("max_position_usdt", "300", expected_revision=1, actor="test-owner")
    sim.price = D(101)
    buy(entry_mode="watch", manual_override=True)
    sim.free["USDT"] = D(100)
    with pytest.raises(ready.Unavailable) as caught:
        ready.check("BARUSDT", mode="manual")
    assert caught.value.code == "FUNDS" and "reservations" in caught.value.reason
    assert not sim.writes


@pytest.mark.parametrize("gate", ["permission", "shutdown", "live", "settings"])
def test_runtime_failures_block_every_mode_and_are_not_strategy_warnings(sim, monkeypatch, gate):
    if gate == "permission":
        sim.response_override = lambda m, path, data: (
            {"uid": 12345, "accountType": "SPOT", "canTrade": False} if path == "account" else None
        )
    elif gate == "shutdown":
        em.STOP.set()
    elif gate == "live":
        monkeypatch.setattr(em, "USE_TESTNET", False)
    else:
        path = em.TRADES_DIR.parent / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"private":"do not render"}')
    with pytest.raises(ready.Unavailable) as caught:
        ready.check("FOOUSDT", mode="manual")
    assert caught.value.code in {"PERMISSION", "SHUTDOWN", "LIVE_DISABLED", "SYSTEM"}
    assert "do not render" not in caught.value.reason and not sim.writes


def test_readiness_cannot_write_or_run_recovery(sim, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Readiness may not write, recover or trade")

    for name in ("_atomic_write", "reconcile_trade", "submit_order", "cancel_protection"):
        monkeypatch.setattr(em, name, forbidden)
    assert ready.check("FOOUSDT")["minimum"] > 0 and not sim.writes


def test_arbitrary_sdk_value_errors_cannot_supply_public_diagnostics_or_mode_override():
    class PrivateValueError(ValueError):
        def __str__(self):
            raise AssertionError("Do not stringify a remote exception")

    problem = ready.problem(PrivateValueError())
    assert problem.code == "SYSTEM" and not problem.modes


@pytest.mark.parametrize("mode", ["legacy", "now", "watch"])
def test_approved_cost_above_remaining_cash_blocks_instead_of_downsizing(sim, mode):
    plan = ta.build_plan({**sig(), "manual_override": True, "entry_mode": mode})
    sim.free["USDT"] = D("8")
    with pytest.raises(ValueError, match="Insufficient available funds"):
        ta.execute(plan, approval=approved(plan), monitor=False)
    assert not sim.writes
