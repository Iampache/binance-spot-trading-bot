"""Generated targets must satisfy 1.5R AFTER exchange rounding, without chasing."""

import math
from decimal import Decimal as D

import pytest
from test_bot import approved, sig

import entry_scanner as sc
import exit_manager as em
import trade_assistant as ta


def generated():
    # Exact 1.5R at the unrounded stop, below 1.5R after flooring both exits.
    return {
        **sig(),
        "stop": D("97.003"),
        "t1": D("104.4955"),
        "t2": D("107.4925"),
        "target_policy": "scanner_r_multiple_v1",
        "resistance_ceiling": None,
    }


def test_generated_targets_compile_up_from_original_entry_and_rounded_stop(sim):
    p = ta.build_plan(generated())
    assert p["stop"] == 97 and p["t1"] == D("104.50") and p["t2"] == D("107.50")
    assert p["rr"] == D("1.5") and not sim.writes


def test_unmarked_or_user_specified_targets_are_never_raised_to_force_pass(sim):
    s = generated()
    del s["target_policy"]
    with pytest.raises(ValueError, match="TP1 reward/risk.*below 1.5R") as caught:
        ta.build_plan(s)
    assert "Checked entry 100" in str(caught.value)
    assert "SL 97" in str(caught.value) and "minimum TP1 104.50" in str(caught.value)
    assert not sim.writes


def test_live_price_increase_still_rejects_and_explains_both_entries(sim):
    sim.price = D("100.01")
    with pytest.raises(ValueError, match="TP1 reward/risk.*below 1.5R") as caught:
        ta.build_plan(generated())
    assert "Checked entry 100.01" in str(caught.value)
    assert "Signal entry 100" in str(caught.value)
    assert not sim.writes


def test_manual_targets_stay_user_selected_even_if_sig_carries_scanner_marker(sim):
    p = ta.build_plan({**generated(), "manual_override": True})
    assert p["t1"] == D("104.49") and p["rr"] < D("1.5")
    assert any("reward/risk" in value for value in p["override_warnings"])
    assert not sim.writes


def test_generated_rounding_cannot_ignore_pullback_resistance_ceiling(sim):
    with pytest.raises(ValueError, match="observed resistance"):
        ta.build_plan({**generated(), "resistance_ceiling": D("104.496")})
    assert not sim.writes


def test_rounded_targets_freeze_on_approval_and_oco_submission(sim, monkeypatch):
    p = ta.build_plan(generated())
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    rec = ta.execute(p, approval=approved(p))
    assert rec["state"] == "protected"
    targets = [D(data["abovePrice"]) for _, path, data in sim.writes if path == "orderList/oco"]
    assert targets == [p["t1"], p["t2"]]
    assert D(rec["plan"]["qty"]) <= p["qty"]


def test_scanner_emits_explicit_generated_target_policy(sim, monkeypatch):
    closes = [100 + 0.45 * math.sin(i * 0.7) for i in range(120)]
    opens = [value - 0.03 for value in closes]
    highs = [value + 0.1 for value in closes]
    lows = [value - 0.13 for value in closes]
    opens[-1], closes[-1], highs[-1], lows[-1] = 100.1, 100.85, 100.95, 100.0
    frame = {"open": opens, "close": closes, "high": highs, "low": lows, "vol": [100] * 119 + [250]}
    monkeypatch.setattr(sc, "hourly_uptrend", lambda symbol: True)
    monkeypatch.setattr(sc, "klines", lambda *args: frame)
    signal = sc.analyze("FOOUSDT", 1)
    assert signal is not None and signal["target_policy"] == "scanner_r_multiple_v1"
    sim.price = D(str(signal["entry"]))
    p = ta.build_plan(signal)
    assert p["rr"] >= D("1.5")


def test_stop_distance_failure_is_distinct_from_reward_failure(sim):
    with pytest.raises(ValueError, match="Stop distance .*outside 1%-6%") as caught:
        ta.build_plan({**sig(), "stop": 99.5})
    assert "TP1 reward/risk" not in str(caught.value)
    assert not sim.writes
