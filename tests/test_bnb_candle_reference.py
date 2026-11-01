"""Recover genuinely empty 1s references with replayable closed-minute evidence."""

import copy
from decimal import Decimal as D

import pytest
from test_bnb_fees import buy, enabled

import exchange as ex
import exit_manager as em


def closed_minute(sim, fill_time=None):
    fill_time = sim.clock if fill_time is None else fill_time
    start = fill_time // 60000 * 60000
    end = start + 59999
    row = [start, "598", "610", "590", "602", "50", end, "30000", 100, "25", "15000", "0"]
    sim.bnb_candles = [row]
    sim.server_time_ms = end + 2000
    sim.response_override = lambda method, path, data: [] if path == "aggTrades" else None
    return row


def test_closed_minute_reference_uses_high_and_exact_containing_interval(sim):
    row = closed_minute(sim)
    calls = []

    def response(method, path, data):
        calls.append((method, path, dict(data)))
        return [] if path == "aggTrades" else None

    sim.response_override = response
    ref = em.bnb_fee_reference(sim.clock)
    assert ref["method"] == "closed_1m_high_at_fill" and D(ref["price"]) == D(610)
    assert em.valid_bnb_candle_reference(ref, sim.clock)
    params = next(data for method, path, data in calls if path == "klines")
    assert params == {
        "symbol": "BNBUSDT",
        "interval": "1m",
        "startTime": row[0],
        "endTime": row[6],
        "limit": 1,
    }
    assert ref["observed_server_time"] > row[6]
    assert not sim.writes


def test_flat_bnb_trade_recovers_without_orders_and_persists_conservative_pnl(sim, monkeypatch):
    enabled(sim, monkeypatch)
    sim.free["FOO"] = D(5)
    initial_usdt = sim.free["USDT"]
    rec = buy(sim)
    for item in sim.lists.values():
        stop = sim.orders[item["orders"][1]["orderId"]]
        sim.fill(stop, price="97")
    closed_minute(sim)
    writes = len(sim.writes)
    messages = []
    monkeypatch.setattr(em, "alert", lambda text: messages.append(text) or True)
    assert em.reconcile_trade(rec)
    assert rec["state"] == "closed" and not em.active_trades()
    assert sim.free["FOO"] == 5 and len(sim.writes) == writes
    fees_bnb = sum(D(row["commission"]) for row in sim.trades)
    expected = sim.free["USDT"] - initial_usdt - fees_bnb * 610
    net = sum(D(event["pnl"]) for event in rec["realized_events"])
    assert abs(net - expected) < D("1e-23")
    assert net <= sim.free["USDT"] - initial_usdt - fees_bnb * 600
    assert abs(em.todays_loss() + net) < D("1e-23")
    assert "closed fill-minute highs" in rec["pnl_valuation"]
    assert any("reference-valued" in text for text in messages)
    saved = copy.deepcopy(em.load_trades()[0])
    sim.bnb_candles = []
    sim.bnb_price = D(9999)
    assert em.reconcile_trade(rec)
    assert em.load_trades()[0] == saved  # No revaluation at the current price after restart.


def test_mixed_old_trade_and_new_candle_references_remain_replayable(sim, monkeypatch):
    enabled(sim, monkeypatch)
    rec = buy(sim)
    first = next(em.executions(rec, "BUY"))
    first["fee_valuation"] = em.bnb_fee_reference(first["time"])
    em.save_trade(rec)
    for item in sim.lists.values():
        sim.fill(sim.orders[item["orders"][0]["orderId"]], price="104.5")
    closed_minute(sim)
    assert em.reconcile_trade(rec)
    methods = {row["fee_valuation"]["method"] for row in em.executions(rec)}
    assert methods == {"last_aggregate_trade_at_or_before_fill", "closed_1m_high_at_fill"}
    assert em.load_trades()[0]["state"] == "closed"
    assert D(next(em.executions(rec, "BUY"))["fee_valuation"]["price"]) == 600


@pytest.mark.parametrize(
    "problem",
    [
        "wrong_minute",
        "wrong_close",
        "nan",
        "zero_volume",
        "zero_quote",
        "zero_trades",
        "high_below_open",
        "negative_low",
    ],
)
def test_invalid_candle_never_unblocks_fee_accounting(sim, problem):
    row = closed_minute(sim)
    if problem == "wrong_minute":
        row[0] -= 60000
        row[6] -= 60000
    elif problem == "wrong_close":
        row[6] -= 1
    else:
        index, value = {
            "nan": (2, "NaN"),
            "zero_volume": (5, "0"),
            "zero_quote": (7, "0"),
            "zero_trades": (8, 0),
            "high_below_open": (2, "597"),
            "negative_low": (3, "-1"),
        }[problem]
        row[index] = value
    with pytest.raises(ex.Uncertain) as caught:
        em.bnb_fee_reference(sim.clock)
    assert caught.value.reason_code == "BNB_CANDLE_INVALID"
    assert not sim.writes


def test_minute_must_be_closed_on_verified_exchange_time(sim):
    row = closed_minute(sim)
    sim.server_time_ms = row[6]
    with pytest.raises(ex.Uncertain) as caught:
        em.bnb_fee_reference(sim.clock)
    assert caught.value.reason_code == "BNB_CANDLE_NOT_CLOSED"
    sim.server_time_ms += 1
    assert em.bnb_fee_reference(sim.clock)["price"] == "610"


@pytest.mark.parametrize("boundary", [0, 59999, 60000])
def test_fill_timestamp_binds_correct_minute_including_boundaries(sim, boundary):
    fill_time = 180000 + boundary
    closed_minute(sim, fill_time)
    ref = em.bnb_fee_reference(fill_time)
    assert ref["open_time"] <= fill_time <= ref["close_time"]
    assert ref["open_time"] == fill_time // 60000 * 60000


def test_timeout_is_not_treated_as_empty_trade_history(sim, monkeypatch):
    closed_minute(sim)

    def broken(method, path, data):
        if path == "aggTrades":
            raise TimeoutError("synthetic read timeout")

    sim.response_override = broken
    monkeypatch.setattr(
        sim.client, "get_klines", lambda **kw: pytest.fail("No fallback after uncertain read")
    )
    with pytest.raises(TimeoutError):
        em.bnb_fee_reference(sim.clock)


@pytest.mark.parametrize(
    "field,value", [("price", "500"), ("fill_time", 1), ("observed_server_time", 0), ("trades", 0)]
)
def test_modified_persisted_candle_reference_cannot_reduce_fee_or_pass_validation(
    sim, field, value
):
    closed_minute(sim)
    ref = em.bnb_fee_reference(sim.clock)
    ref[field] = value
    row = {"commission": "0.001", "commissionAsset": "BNB", "time": sim.clock, "fee_valuation": ref}
    with pytest.raises(ex.Uncertain):
        em.fee_in_quote(row, "FOO")
