"""Synthetic closed-candle markets behind the actual pinned SDK transport."""

import copy
import math
import time
from decimal import Decimal as D

import pytest
from test_bot import approved

import algorithmic_planner as ap
import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta


@pytest.fixture
def market(sim):
    now_ms = int(time.time() * 1000)
    histories = {}
    for symbol, interval in (("FOOUSDT", "15m"), ("FOOUSDT", "1h"), ("BTCUSDT", "1h")):
        duration = ap.DURATIONS[interval]
        current_open = now_ms // duration * duration
        rows = []
        for i in range(201):
            opened = current_open - (200 - i) * duration
            if interval == "1h":
                close = D(90) + D(i) / 20
                opening = close - D(".03")
                high, low = close + D(".2"), opening - D(".2")
            else:
                close = D(str(100 + 0.45 * math.sin(i * 0.7)))
                opening = close - D(".03")
                high, low = close + D(".1"), opening - D(".1")
                if i == 199:
                    opening, close, high, low = D("100.1"), D("100.85"), D("100.95"), D("100.0")
            volume = "250" if i == 199 else "100"
            rows.append(
                [
                    opened,
                    str(opening),
                    str(high),
                    str(low),
                    str(close),
                    volume,
                    opened + duration - 1,
                    "0",
                    1,
                    "0",
                    "0",
                    "0",
                ]
            )
        histories[(symbol, interval)] = rows
    original = sim.client._request_api

    def request(method, path, signed=False, version=None, **kwargs):
        if method == "get" and path == "time":
            return {"serverTime": now_ms}  # Deterministic candle-close boundary.
        if method == "get" and path == "klines":
            data = kwargs["data"]
            return copy.deepcopy(histories[(data["symbol"], data["interval"])])
        return original(method, path, signed, version, **kwargs)

    sim.client._request_api = request
    sim.price = D("100.9")
    return histories


def test_algorithm_calculates_closed_candle_setup_and_preserves_limits(sim, market):
    sig = ap.propose("FOOUSDT")
    p = ta.build_plan(sig)
    assert sig["algorithmic"] and not sig["manual_override"]
    assert sig["stop"] < sig["entry"] < sig["t1"] < sig["t2"]
    assert p["cost"] <= 12 and p["loss"] <= min(p["balance"] / 100, D(3))
    assert "ATR14" in p["analysis_summary"]
    assert p["algorithm_version"] == ap.VERSION
    assert not sim.writes


@pytest.mark.parametrize(
    "field,value",
    [("algorithmic", False), ("algorithm_mode", "assisted"), ("algorithm_version", "other")],
)
def test_algorithm_identity_cannot_change_under_approval(sim, market, field, value):
    p = ta.build_plan(ap.propose("FOOUSDT"))
    assert not ta.approval_bounds(p, {**p, field: value})


def test_closed_candle_indicators_ignore_unfinished_price_spike(sim, market):
    first = ap.propose("FOOUSDT")
    market[("FOOUSDT", "15m")][-1][1:6] = ["nan", "9999999", "0", "999999", "9999999"]
    second = ap.propose("FOOUSDT")
    assert all(
        first[key] == second[key] for key in ("entry", "stop", "t1", "t2", "analysis_summary")
    )


@pytest.mark.parametrize(
    "kind", ["gap", "reversed", "nan", "negative_volume", "bad_ohlc", "future", "short", "stale"]
)
def test_bad_or_stale_market_data_never_produces_proposal(sim, market, kind):
    rows = market[("FOOUSDT", "15m")]
    if kind == "gap":
        del rows[80]
    elif kind == "reversed":
        rows.reverse()
    elif kind == "nan":
        rows[-2][4] = "NaN"
    elif kind == "negative_volume":
        rows[-2][5] = "-1"
    elif kind == "bad_ohlc":
        rows[-2][3] = "999"
    elif kind == "future":
        rows[-1][0] += 900000
        rows[-1][6] += 900000
    elif kind == "short":
        del rows[:100]
    else:
        for row in rows:
            row[0] -= 3600000
            row[6] -= 3600000
    with pytest.raises(ValueError):
        ap.propose("FOOUSDT")
    assert not sim.writes


def downtrend(market, symbol):
    rows = market[(symbol, "1h")]
    for i, row in enumerate(rows):
        close = D(110) - D(i) / 20
        row[1:5] = [str(close), str(close + 1), str(close - 1), str(close)]


def test_symbol_downtrend_still_blocks_algorithm(sim, market):
    downtrend(market, "FOOUSDT")
    with pytest.raises(ValueError, match="trend|regime"):
        ap.propose("FOOUSDT")
    assert not sim.writes


def test_btc_downtrend_is_visible_advisory_with_all_risk_limits(sim, market):
    downtrend(market, "BTCUSDT")
    sig = ap.propose("FOOUSDT")
    plan = ta.build_plan(sig)
    assert sig["market_warning"] == plan["market_warning"] == ap.BTC_WARNING
    assert "BTC weak/downward (advisory)" in sig["analysis_summary"]
    assert "1h/BTC uptrend" not in sig["analysis_summary"]
    assert ap.BTC_WARNING in tg.proposal_text(sig, plan, "")
    assert ap.BTC_WARNING in tg.proposal_details_text(sig, plan, "")
    assert plan["cost"] <= 12 and plan["loss"] <= min(plan["balance"] / 100, D(3))
    with pytest.raises(ValueError, match="approval"):
        ta.execute(plan)
    assert not sim.writes


def test_btc_change_during_approval_remains_advisory_and_keeps_approved_exits(
    sim, market, monkeypatch
):
    plan = ta.build_plan(ap.propose("FOOUSDT"))
    assert not plan["market_warning"]
    downtrend(market, "BTCUSDT")
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    rec = ta.execute(plan, approval=approved(plan))
    assert rec["state"] == "protected" and em.coverage(rec) >= em.inventory(rec) > 0
    assert rec["plan"]["market_warning"] == ap.BTC_WARNING
    assert all(D(rec["plan"][key]) == plan[key] for key in ("stop", "t1", "t2"))


def test_missing_btc_evidence_still_blocks_instead_of_inventing_an_advisory(sim, market):
    market[("BTCUSDT", "1h")].clear()
    with pytest.raises(ValueError, match="history"):
        ap.propose("FOOUSDT")
    assert not sim.writes


def test_near_resistance_cannot_be_ignored_to_offer_attractive_targets(sim, market):
    market[("FOOUSDT", "15m")][120][2] = "101.4"
    with pytest.raises(ValueError, match="resistance"):
        ap.propose("FOOUSDT")


def test_wilder_indicators_on_flat_series_are_defined():
    frame = {
        "close": [D(100)] * 120,
        "high": [D(101)] * 120,
        "low": [D(99)] * 120,
        "vol": [D(100)] * 119 + [D(200)],
    }
    stats = ap.indicators(frame)
    assert stats["atr"] == 2 and stats["rsi"] == 50
    assert stats["ema20"][-1] == 100 and stats["relative_volume"] == 2


def test_net_reward_risk_includes_full_fee_reserves(sim, market):
    sig = ap.propose("FOOUSDT")
    sim.fee_rate = D(".02")
    with pytest.raises(ValueError, match="after reserved fees"):
        ta.build_plan(sig)
    assert not sim.writes


@pytest.mark.parametrize("gate", ["volume", "daily_loss", "minimums", "manual_override"])
def test_algorithm_cannot_bypass_existing_risk_gates(sim, market, monkeypatch, gate):
    downtrend(market, "BTCUSDT")
    sig = ap.propose("FOOUSDT")
    if gate == "volume":
        sim.response_override = lambda method, path, data: (
            {"symbol": "FOOUSDT", "quoteVolume": "9000000", "priceChangePercent": "1"}
            if path == "ticker/24hr"
            else None
        )
    elif gate == "daily_loss":
        monkeypatch.setattr(em, "todays_loss", lambda: D(3))
    elif gate == "minimums":
        sim.min_notional = "10"
    else:
        sig["manual_override"] = True
    with pytest.raises(ValueError):
        ta.build_plan(sig)
    assert not sim.writes


def test_algorithm_full_entry_preserves_metadata_and_protection(sim, market, monkeypatch):
    p = ta.build_plan(ap.propose("FOOUSDT"))
    assert not sim.writes
    with pytest.raises(ValueError, match="approval"):
        ta.execute(p)
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    rec = ta.execute(p, approval=approved(p))
    assert rec["state"] == "protected" and len(sim.lists) == 2
    assert rec["plan"]["algorithm_version"] == ap.VERSION
    assert rec["plan"]["algorithmic"] == "True"
    assert em.coverage(rec) >= em.inventory(rec) > 0
    assert D(rec["plan"]["qty"]) <= p["qty"]


def test_protected_v1_record_recovers_after_v2_policy_upgrade(sim, market, monkeypatch):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    plan = ta.build_plan(ap.propose("FOOUSDT"))
    rec = ta.execute(plan, approval=approved(plan))
    rec["plan"]["algorithm_version"] = "atr-structure-v1"
    em.save_trade(rec)
    quantity, writes = em.inventory(rec), len(sim.writes)
    em.recover_trades()
    saved = em.load_trades()[0]
    assert saved["plan"]["algorithm_version"] == "atr-structure-v1"
    assert saved["state"] == "protected" and em.inventory(saved) == quantity
    assert em.coverage(saved) >= quantity > 0 and len(sim.writes) == writes


def test_deterioration_between_display_and_approval_never_moves_stop_or_buys(
    sim, market, monkeypatch
):
    p = ta.build_plan(ap.propose("FOOUSDT"))
    market[("FOOUSDT", "15m")][-2][3] = str(p["stop"] - 1)
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    with pytest.raises(ValueError, match="stop|volatility|setup"):
        ta.execute(p, approval=approved(p))
    assert not sim.writes and not em.active_trades()


def test_algorithm_limit_watch_revalidates_after_persistence(sim, market, monkeypatch):
    p = ta.build_plan(ap.propose("FOOUSDT", entry=D("100.8")))
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    rec = ta.execute(p, approval=approved(p))
    assert rec["state"] == "watching_entry" and not sim.writes
    sim.price = D("100.8")
    em.reconcile_trade(rec)
    assert len(sim.lists) == 2
    assert rec["plan"]["algorithm_mode"] == "semi"
    assert rec["state"] == "protected"


def test_algorithm_watch_invalidated_by_support_change_does_not_buy(sim, market, monkeypatch):
    p = ta.build_plan(ap.propose("FOOUSDT", entry=D("100.8")))
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    rec = ta.execute(p, approval=approved(p))
    sim.price = D("100.8")
    market[("FOOUSDT", "15m")][-2][3] = str(p["stop"] - 1)
    assert em.reconcile_trade(rec)
    assert rec["state"] == "closed" and not sim.writes


def test_expired_analysis_and_unsupported_version_are_rejected(sim, market):
    sig = ap.propose("FOOUSDT")
    for change in ({"analysis_created_at": "1"}, {"algorithm_version": "unreviewed"}):
        with pytest.raises(ValueError):
            ta.build_plan({**sig, **change})
    assert not sim.writes


def telegram_message(text):
    return {"chat": {"id": tg.CHAT_ID}, "from": {"id": tg.CHAT_ID}, "text": text}


@pytest.fixture
def messages(monkeypatch):
    out = []
    monkeypatch.setattr(
        tg,
        "say",
        lambda text, **kw: out.append((text, kw)) or {"ok": True, "result": {"message_id": 1}},
    )
    monkeypatch.setattr(tg, "tg", lambda *args, **kwargs: {})
    monkeypatch.setattr(tg, "edit", lambda *args: None)
    tg.checks["test"] = {
        "created": time.time(),
        "stage": "modes",
        "exit_style": "two",
        "res": {"symbol": "FOOUSDT", "price": 100.9, "status": "WARNING"},
    }
    return out


def test_auto_setup_calculates_all_levels_but_cannot_purchase_without_approval(
    sim, market, messages
):
    tg.handle_check_callback({"id": "cb"}, "CQ", "test")
    assert len(tg.pending) == 1 and not sim.writes
    item = next(iter(tg.pending.values()))
    assert item["plan"]["algorithm_mode"] == "full" and not item["plan"]["manual_override"]
    assert "Auto Setup" in item["text"]
    assert "ATR14" in item["details"]


@pytest.mark.parametrize("action", ["CQ", "CW"])
def test_algorithm_modes_show_btc_warning_without_buying(sim, market, messages, action):
    downtrend(market, "BTCUSDT")
    tg.handle_check_callback({"id": "cb"}, action, "test")
    assert ap.BTC_WARNING in messages[-1][0] and not sim.writes
    if action == "CQ":
        assert len(tg.pending) == 1
    else:
        assert tg.state["step"] == "algorithm_targets" and not tg.pending


@pytest.mark.parametrize("action", ["CN", "CW", "CQ"])
def test_active_trade_block_comes_before_algorithm_or_manual_input(
    sim, market, messages, monkeypatch, action
):
    monkeypatch.setattr(em, "active_trades", lambda: [{"symbol": "UNIUSDT", "state": "protected"}])
    monkeypatch.setattr(em, "inventory", lambda rec: D(1))

    def no_analysis(*args, **kwargs):
        pytest.fail("Occupied slot should be checked before algorithm data is read")

    monkeypatch.setattr(ap, "propose", no_analysis)
    tg.handle_check_callback({"id": "cb"}, action, "test")
    assert "UNIUSDT trade is in progress" in messages[-1][0]
    assert not tg.pending and not tg.state and not sim.writes


def test_direct_algorithm_proposal_also_checks_active_slot_first(sim, messages, monkeypatch):
    monkeypatch.setattr(em, "active_trades", lambda: [{"symbol": "FOOUSDT", "state": "protected"}])
    monkeypatch.setattr(em, "inventory", lambda rec: D(1))
    assert not tg.prepare_algorithm_proposal("FOOUSDT")
    assert "FOOUSDT trade already in progress" in messages[-1][0]
    assert not tg.pending and not sim.writes


def test_assisted_mode_gets_entry_stop_from_algorithm_and_targets_from_user(sim, market, messages):
    tg.handle_check_callback({"id": "cb"}, "CW", "test")
    assert tg.state["step"] == "algorithm_targets" and not tg.pending
    suggestion = tg.state["signal"]
    tg.handle_message(telegram_message(f"{suggestion['t1']} {suggestion['t2']}"))
    assert len(tg.pending) == 1 and not sim.writes
    item = next(iter(tg.pending.values()))
    assert item["plan"]["algorithm_mode"] == "assisted" and not item["plan"]["manual_override"]
    assert item["plan"]["stop"] == suggestion["stop"]
    assert "Assisted" in item["text"]


def test_assisted_rejects_bad_targets_and_cancel_prevents_late_action(sim, market, messages):
    tg.handle_check_callback({"id": "cb"}, "CW", "test")
    tg.handle_message(telegram_message("1 2"))
    assert tg.state["step"] == "algorithm_targets" and not tg.pending
    tg.handle_message(telegram_message("/cancel"))
    tg.handle_message(telegram_message("105 108"))
    assert not tg.pending and not sim.writes and not tg.state


def test_assisted_approved_order_keeps_user_targets_and_normal_policy(sim, market, monkeypatch):
    signal = ap.propose("FOOUSDT")
    signal["algorithm_mode"] = "assisted"
    risk = signal["entry"] - signal["stop"]
    signal["t1"] = em.round_to(signal["entry"] + D("1.9") * risk, D(".01"))
    p = ta.build_plan(signal)
    monkeypatch.setattr(em, "monitor_trade", lambda record: None)
    rec = ta.execute(p, approval=approved(p))
    assert rec["state"] == "protected" and len(sim.lists) == 2
    assert D(rec["plan"]["t1"]) == p["t1"]
    assert rec["plan"]["manual_override"] == "False"
    assert rec["plan"]["algorithm_mode"] == "assisted"
    assert D(rec["plan"]["loss"]) <= p["risk_budget"]


def test_assisted_cannot_approve_targets_above_model_bounds(sim, market, messages):
    tg.handle_check_callback({"id": "cb"}, "CW", "test")
    suggestion = tg.state["signal"]
    tg.handle_message(telegram_message(f"{suggestion['t1']} {suggestion['t2'] + 10}"))
    assert not tg.pending and not sim.writes
    assert any("Why no entry is ready" in text and "1." in text for text, _ in messages)
    assert "reply_markup" in messages[-1][1]  # Rejected targets now have recovery choices.


def test_coarse_quantity_steps_cannot_hide_negative_conservative_profit(sim, market, monkeypatch):
    original = sim.info

    def coarse_info():
        data = original()
        for rule in data["filters"]:
            if rule["filterType"] == "LOT_SIZE":
                rule.update(stepSize=".01", minQty=".01")
        return data

    monkeypatch.setattr(sim, "info", coarse_info)
    with pytest.raises(ValueError, match="precision/fee reserves"):
        ta.build_plan(ap.propose("FOOUSDT"))
    assert not sim.writes
