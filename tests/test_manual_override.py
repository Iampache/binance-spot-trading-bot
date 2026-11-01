"""Explicit human-approved strategy overrides stay separate from automatic entries."""

import json
import time
from decimal import Decimal as D

import pytest
from test_bot import approved, sig

import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta


def manual_signal(**changes):
    return {**sig(), "manual_override": True, **changes}


def low_volume(sim, amount="800000"):
    def response(method, path, data):
        if path == "ticker/24hr":
            return {"symbol": "FOOUSDT", "quoteVolume": amount}

    sim.response_override = response


def test_manual_bypasses_low_volume_while_automatic_still_rejects(sim):
    low_volume(sim)
    with pytest.raises(ValueError, match="10,000,000"):
        ta.build_plan(sig())
    plan = ta.build_plan(manual_signal())
    assert plan["manual_override"] is True
    assert any("800,000" in warning for warning in plan["override_warnings"])
    assert plan["cost"] <= 12 and not sim.writes


@pytest.mark.parametrize(
    "levels", [{"stop": 99.5, "t1": 100.2, "t2": 101}, {"stop": 80, "t1": 105, "t2": 110}]
)
def test_manual_can_choose_stop_distance_and_rr_outside_strategy(sim, levels):
    with pytest.raises(ValueError, match="Stop distance"):
        ta.build_plan({**sig(), **levels})
    plan = ta.build_plan(manual_signal(**levels))
    assert plan["rr"] < D("1.5")
    assert any("Stop distance" in warning for warning in plan["override_warnings"])
    assert any("reward/risk" in warning for warning in plan["override_warnings"])


def test_manual_bypasses_spread_and_depth_thresholds_but_records_them(sim):
    def response(method, path, data):
        if path == "ticker/bookTicker":
            return {"bidPrice": "99", "askPrice": "101"}
        if path == "depth":
            return {"bids": [["99", ".01"]], "asks": [["101", ".01"]]}

    sim.response_override = response
    with pytest.raises(ValueError, match="Spread too wide"):
        ta.build_plan(sig())
    plan = ta.build_plan(manual_signal())
    assert any("Spread" in warning for warning in plan["override_warnings"])
    assert any("order-book depth" in warning for warning in plan["override_warnings"])


def test_manual_uses_position_cap_instead_of_one_percent_risk_sizing(sim):
    sim.free["USDT"] = D(33)
    auto = ta.build_plan(sig())
    manual = ta.build_plan(manual_signal())
    assert auto["loss"] <= D(".33")
    assert manual["loss"] > D(".33")
    assert manual["cost"] <= 12 and manual["qty"] > auto["qty"]
    assert any("1% of available" in warning for warning in manual["override_warnings"])


def test_manual_bypasses_daily_loss_gate_and_prospective_budget(sim, monkeypatch):
    monkeypatch.setattr(em, "todays_loss", lambda: D(4))
    with pytest.raises(ValueError, match="Daily bot-trade loss limit"):
        ta.build_plan(sig())
    plan = ta.build_plan(manual_signal())
    assert plan["qty"] > 0 and plan["cost"] <= 12
    assert any("Daily realized loss 4.00" in warning for warning in plan["override_warnings"])


def test_manual_with_low_volume_and_exceeded_budget_survives_execute_revalidation(sim, monkeypatch):
    low_volume(sim)
    monkeypatch.setattr(em, "todays_loss", lambda: D(4))
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    plan = ta.build_plan(manual_signal())
    rec = ta.execute(plan, approval=approved(plan))
    assert rec["state"] == "protected" and len(sim.lists) == 2
    assert rec["plan"]["manual_override"] == "True"
    assert rec["approved_bounds"]["manual_override"] == "True"
    assert (
        sum(d.get("side") == "BUY" for m, p, d in sim.writes if m == "post" and p == "order") == 1
    )


def test_override_is_not_inferred_from_a_manual_setup_label(sim):
    low_volume(sim)
    assert sig()["setup"].startswith("MANUAL")
    with pytest.raises(ValueError, match="10,000,000"):
        ta.build_plan(sig())


@pytest.mark.parametrize("flag", ["true", 1, None])
def test_override_requires_explicit_boolean_not_truthy_payload(sim, flag):
    with pytest.raises(ValueError, match="boolean"):
        ta.build_plan({**sig(), "manual_override": flag})
    assert not sim.writes


def test_manual_cannot_bypass_exchange_minimums(sim):
    sim.min_notional = "10"
    with pytest.raises(ValueError, match="order minimums"):
        ta.build_plan(manual_signal())
    assert not sim.writes


def test_manual_cannot_bypass_nonfinite_data_or_invalid_levels(sim):
    for bad in (float("nan"), float("inf"), 0, -1):
        with pytest.raises(ValueError):
            ta.build_plan(manual_signal(stop=bad))
    with pytest.raises(ValueError):
        ta.build_plan(manual_signal(stop=105))
    assert not sim.writes


def test_manual_cannot_trade_with_existing_or_uncertain_inventory(sim, monkeypatch):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    first = ta.build_plan(sig())
    ta.execute(first, approval=approved(first))
    with pytest.raises(ta.ActiveTradeError):
        ta.build_plan(manual_signal())


def test_manual_cannot_bypass_invalid_identity(sim):
    def response(method, path, data):
        if path == "ticker/24hr":
            return {"symbol": "OTHERUSDT", "quoteVolume": "800000"}

    sim.response_override = response
    with pytest.raises(ValueError, match="identity mismatch"):
        ta.build_plan(manual_signal())
    assert not sim.writes


def test_automatic_approval_cannot_be_escalated_to_manual_override(sim):
    auto = ta.build_plan(sig())
    manual = ta.build_plan(manual_signal())
    assert not ta.approval_bounds(auto, manual)
    assert not ta.approval_bounds(manual, auto)
    with pytest.raises(ValueError, match="approval bounds"):
        ta.execute(manual, approval=approved(auto))
    assert not sim.writes


def test_manual_still_requires_fresh_explicit_approval(sim):
    plan = ta.build_plan(manual_signal())
    with pytest.raises(ValueError, match="approval"):
        ta.execute(plan)
    expired = approved(plan)
    expired["expires_at"] = time.time() - 1
    with pytest.raises(ValueError, match="approval"):
        ta.execute(plan, approval=expired)
    assert not sim.writes


def test_manual_proposal_discloses_overrides_before_approval(sim, monkeypatch):
    low_volume(sim)
    monkeypatch.setattr(em, "todays_loss", lambda: D(4))
    plan = ta.build_plan(manual_signal())
    text = tg.proposal_text(manual_signal(), plan, "Scanner status was WARNING")
    assert "MANUAL RISK OVERRIDE" in text
    assert "1% sizing" in text and "3 USDT daily-loss gate are bypassed" in text
    assert "liquidity/strategy checks" in text
    assert "12 USDT cap" in text and "OCO protection remain mandatory" in text
    assert "slippage" in text and "Exceptions:" in text
    details = tg.proposal_details_text(manual_signal(), plan, "Scanner status was WARNING")
    assert "800,000" in details
    assert "Volume, spread/depth, trend/setup, reward/risk, stop-distance" in details
    assert "daily-loss gate are bypassed" in details


def test_manual_warning_card_uses_override_label_and_collects_levels_without_buy(sim, monkeypatch):
    messages = []

    def say(text, **kwargs):
        messages.append((text, kwargs))
        return {"ok": True, "result": {"message_id": len(messages)}}

    monkeypatch.setattr(tg, "say", say)
    monkeypatch.setattr(tg, "tg", lambda *a, **k: {"ok": True})
    result = {
        "symbol": "FOOUSDT",
        "price": 100,
        "chg": 1,
        "qv": 800000,
        "sig": None,
        "reasons": ["Low liquidity", "Trend is not up"],
        "status": "WARNING",
        "note": "BTC downtrend",
    }
    tg.send_check_card(result)
    kb = json.loads(messages[-1][1]["reply_markup"])
    assert any(
        button["text"] == "Trading options" for row in kb["inline_keyboard"] for button in row
    )
    card = next(iter(tg.checks))
    tg.handle_check_callback({"id": "cb"}, "C3", card)
    tg.handle_check_callback({"id": "cb"}, "CT2", card)
    assert any("Manual mode retains its explicit risk override" in text for text, _ in messages)
    low_volume(sim)
    tg.handle_manual("100 99.5 100.2 101")
    assert tg.pending
    item = next(iter(tg.pending.values()))
    assert item["sig"]["manual_override"] is True
    assert item["plan"]["manual_override"] is True
    assert "3 USDT daily-loss gate" in item["text"]
    assert not sim.writes


def test_manual_cannot_bypass_unknown_third_asset_fee_valuation(sim):
    def response(method, path, data):
        if path == "account/commission":
            return {
                "symbol": "FOOUSDT",
                "discount": {"enabledForAccount": True, "enabledForSymbol": True},
            }

    sim.response_override = response
    with pytest.raises(ValueError, match="BNB-discount"):
        ta.build_plan(manual_signal())
    assert not sim.writes


@pytest.mark.parametrize("field", ["qty", "cost", "loss"])
def test_manual_approval_still_bounds_quantity_cost_and_risk(sim, field):
    plan = ta.build_plan(manual_signal())
    changed = {**plan, field: plan[field] + D(".001")}
    assert not ta.approval_bounds(plan, changed)
    assert not sim.writes


def test_manual_trade_losses_still_block_future_automatic_entries(sim, monkeypatch):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    plan = ta.build_plan(manual_signal())
    rec = ta.execute(plan, approval=approved(plan))
    for item in sim.lists.values():
        sim.fill(sim.orders[item["orders"][1]["orderId"]], price="60")
    em.reconcile_trade(rec)
    assert em.todays_loss() > 3
    with pytest.raises(ValueError, match="Daily bot-trade loss limit"):
        ta.build_plan(sig())
    assert ta.build_plan(manual_signal())["manual_override"] is True


def test_manual_uses_actual_notional_minimum_without_optional_strategy_padding(sim):
    sim.min_notional = "5.6"
    with pytest.raises(ValueError, match="order minimums"):
        ta.build_plan(sig())
    plan = ta.build_plan(manual_signal())
    assert plan["cost"] <= 12
    assert plan["half"] * plan["stop"] >= D("5.6")


def test_manual_uses_available_funds_without_automatic_five_percent_balance_reserve(sim):
    sim.free["USDT"] = D("12.01")
    plan = ta.build_plan(manual_signal())
    assert D("11.9") < plan["cost"] <= 12
    assert plan["cost"] <= plan["balance"]
