"""Early alerts observe public markets without granting entry authority."""

import json
import time

import pytest
from test_setup_watches import callback, message

import entry_scanner as sc
import momentum_watch as mw
import telegram_assistant as tg


def candles():
    rows = []
    price = 100
    for index in range(23):
        previous = price
        if index >= 20:
            price += 2
        opened = index * 300_000
        rows.append(
            [
                opened,
                previous,
                price,
                previous,
                price,
                1,
                opened + 299_999,
                90_000 if index >= 20 else 10_000,
            ]
        )
    return rows


def ticker(symbol="ONEUSDT", price=100, **extra):
    return dict(
        symbol=symbol,
        lastPrice=price,
        quoteVolume=2_000_000,
        priceChangePercent=71,
        closeTime=0,
        **extra,
    )


def feed(watch, minute, price=100, *, symbols=None):
    symbols = symbols or {"ONEUSDT"}
    return watch.shortlist(
        [{**ticker(symbol, price), "closeTime": minute * 60_000} for symbol in symbols],
        symbols,
        minute * 60,
        minute * 60_000,
    )


def signal():
    return dict(
        symbol="ONEUSDT",
        price=106,
        chg=71,
        qv=2_000_000,
        change5=2,
        change15=6,
        relative_volume=9,
        recent_volume=270_000,
        closed_at=int(time.time() * 1000) - 10_000,
    )


def test_prices_need_warmup_then_include_daily_pumps_and_lower_volume():
    watch = mw.MomentumWatch()
    for minute in range(5):
        assert feed(watch, minute) == []
    found = feed(watch, 5, 103)
    assert [row["symbol"] for row in found] == ["ONEUSDT"]
    assert found[0]["chg"] > sc.MAX_24H_PUMP
    assert found[0]["qv"] < sc.MIN_QUOTE_VOLUME


def test_gradual_fifteen_minute_growth_is_detected():
    watch = mw.MomentumWatch()
    for minute in range(15):
        assert feed(watch, minute, 100 + minute * 0.25) == []
    assert feed(watch, 15, 103.75)


def test_missing_samples_reset_warmup_and_stale_or_invalid_tickers_are_ignored():
    watch = mw.MomentumWatch()
    for minute in range(5):
        feed(watch, minute)
    assert feed(watch, 9, 120) == []  # cannot measure a five-minute move over a data gap
    rows = [
        ticker(),
        {**ticker("BADUSDT"), "lastPrice": "NaN"},
        {**ticker("LOWUSDT"), "quoteVolume": 999_999},
        ticker("INACTIVEUSDT"),
    ]
    assert watch.shortlist(rows, {"ONEUSDT", "BADUSDT", "LOWUSDT"}, 600, 600_000) == []
    assert watch.history == {}


def test_duplicates_fail_and_shortlist_work_is_bounded():
    watch = mw.MomentumWatch()
    with pytest.raises(ValueError, match="duplicate"):
        watch.shortlist([ticker(), ticker()], {"ONEUSDT"}, 0, 0)
    symbols = {f"COIN{i}USDT" for i in range(20)}
    for minute in range(5):
        feed(watch, minute, symbols=symbols)
    assert len(feed(watch, 5, 110, symbols=symbols)) == mw.MAX_CONFIRMATIONS


def test_confirmation_requires_closed_price_and_volume_surge():
    rows = candles()
    found = mw.confirm("ONEUSDT", rows, 23 * 300_000)
    assert found["change15"] == pytest.approx(6)
    assert found["relative_volume"] == 9
    # An enormous unfinished candle cannot change the measured signal.
    forming = [23 * 300_000, 106, 500, 106, 500, 1, 24 * 300_000 - 1, 100_000_000]
    assert mw.confirm("ONEUSDT", rows + [forming], 23 * 300_000) == found
    for row in rows:
        row[7] = 10_000
    assert mw.confirm("ONEUSDT", rows, 23 * 300_000) is None
    for row in rows:
        row[1:5] = [100, 100, 100, 100]
        row[7] = 90_000 if row[0] >= 20 * 300_000 else 10_000
    assert mw.confirm("ONEUSDT", rows, 23 * 300_000) is None


@pytest.mark.parametrize("damage", ["stale", "gap", "nonfinite", "negative", "bad_interval"])
def test_bad_candles_never_produce_momentum(damage):
    rows = candles()
    now = 23 * 300_000
    if damage == "stale":
        now += 330_001
    elif damage == "gap":
        rows.pop(5)
    elif damage == "nonfinite":
        rows[-1][7] = "NaN"
    elif damage == "negative":
        rows[-1][7] = -1
    else:
        rows[-1][6] += 1
    with pytest.raises(ValueError):
        mw.confirm("ONEUSDT", rows, now)


def test_per_pair_and_global_notification_limits():
    watch = mw.MomentumWatch()
    for symbol in ("ONEUSDT", "TWOUSDT", "THREEUSDT"):
        assert watch.can_notify(symbol, 300)
        watch.attempted(symbol, 300)
    assert not watch.can_notify("FOURUSDT", 301)
    assert watch.can_notify("FOURUSDT", 1200)
    assert not watch.can_notify("ONEUSDT", 1200)
    assert watch.can_notify("ONEUSDT", 300 + mw.PAIR_COOLDOWN)


@pytest.fixture
def ui(sim, monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "momentum_enabled", True)
    monkeypatch.setattr(tg, "AUTO_SCAN", True)

    def transport(method, **params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": len(calls)}}

    monkeypatch.setattr(tg, "tg", transport)
    return calls


def test_alert_has_actions_and_no_approval_or_buy_authority(sim, ui):
    cid = tg.send_momentum_alert(signal(), tg.proposal_generation)
    card = tg.checks[cid]
    assert "Early momentum" in card["text"]
    assert "+25%" in card["text"] and "$10M" in card["text"]
    buttons = [
        button for row in json.loads(ui[-1][1]["reply_markup"])["inline_keyboard"] for button in row
    ]
    assert {"Watch 1 min", "Watch 3 min", "Watch 15 min", "Trading options", "Ignore"} <= {
        b["text"] for b in buttons
    }
    assert not any(b["callback_data"].startswith(("A:", "C1:")) for b in buttons)
    assert not tg.pending and not sim.writes
    tg.handle_callback(callback("C1", cid, card["mid"]))  # fabricated review cannot authorize
    assert not tg.pending and not sim.writes
    tg.handle_callback(callback("CX", cid, card["mid"], sender="stranger"))
    assert cid in tg.checks
    tg.handle_callback(callback("CX", cid, card["mid"]))
    assert cid not in tg.checks


def test_momentum_watch_button_uses_existing_read_only_watch(sim, ui, monkeypatch):
    jobs = []
    monkeypatch.setattr(tg, "start_managed_worker", jobs.append)
    cid = tg.send_momentum_alert(signal(), tg.proposal_generation)
    tg.handle_callback(callback("CM15", cid, tg.checks[cid]["mid"]))
    assert len(jobs) == len(tg.setup_watches) == 1
    assert not tg.pending and not sim.writes


def test_notifications_are_deduplicated_and_failed_delivery_also_has_cooldown(sim, ui, monkeypatch):
    watch = mw.MomentumWatch()
    monkeypatch.setattr(watch, "scan", lambda *args: [signal()])
    monkeypatch.setattr(tg, "tg", lambda *args, **kwargs: {"ok": False})
    tg.momentum_once(watch)
    assert "ONEUSDT" in watch.notified and not tg.checks
    monkeypatch.setattr(tg, "send_momentum_alert", lambda *args: pytest.fail("Do not retry"))
    tg.momentum_once(watch)
    assert not sim.writes


@pytest.mark.parametrize("block", ["off", "stop", "cancel", "stale", "proposal", "watch"])
def test_suppressed_notifications_have_no_side_effects(sim, ui, monkeypatch, block):
    watch = mw.MomentumWatch()

    def scan(*args):
        row = signal()
        if block == "cancel":
            tg.handle_message(message("/cancel"))
        elif block == "stale":
            row["closed_at"] -= 400_000
        return [row]

    monkeypatch.setattr(watch, "scan", scan)
    if block == "off":
        tg.handle_message(message("/momentum off"))
    elif block == "stop":
        tg.em.STOP.set()
    elif block == "proposal":
        monkeypatch.setattr(tg, "pending_pair", lambda symbol: True)
    elif block == "watch":
        tg.setup_watches["watch"] = {"symbol": "ONEUSDT"}
    tg.momentum_once(watch)
    assert not watch.notified and not tg.checks and not sim.writes


def test_mute_is_owner_only_and_reversible(sim, ui):
    tg.handle_message({**message("/momentum off"), "from": {"id": "stranger"}})
    assert tg.momentum_enabled
    tg.handle_message(message("/momentum off"))
    assert not tg.momentum_enabled
    tg.handle_message(message("/momentum on"))
    assert tg.momentum_enabled


def test_public_scan_validates_candidates_without_account_calls(monkeypatch):
    watch = mw.MomentumWatch()
    monkeypatch.setattr(mw, "eligible_symbols", lambda info: {"ONEUSDT"})
    monkeypatch.setattr(watch, "shortlist", lambda *args: [dict(symbol="ONEUSDT", qv=2e6, chg=71)])
    calls = []

    def public(path, **params):
        calls.append(path)
        return candles() if path.endswith("klines") else []

    monkeypatch.setattr(sc, "get", public)
    result = watch.scan(300, 23 * 300_000, lambda: False)
    assert len(result) == 1 and result[0]["symbol"] == "ONEUSDT"
    assert calls == ["/api/v3/exchangeInfo", "/api/v3/ticker/24hr", "/api/v3/klines"]
    calls.clear()
    assert watch.scan(360, 23 * 300_000, lambda: True) == [] and not calls


def test_daily_cross_is_an_additional_trigger_not_a_startup_pump_alert():
    watch = mw.MomentumWatch()

    def sample(minute, change):
        row = {**ticker(), "priceChangePercent": change, "closeTime": minute * 60_000}
        return watch.shortlist([row], {"ONEUSDT"}, minute * 60, minute * 60_000)

    assert sample(0, 7) == []
    assert sample(1, 8.1)[0]["daily_cross"]
    assert sample(2, 8.2) == []
    rows = candles()
    for i, row in enumerate(rows):
        price = 100 + max(0, i - 19) * 0.1
        row[1:5] = [price, price, price, price]
    assert mw.confirm("ONEUSDT", rows, 23 * 300_000) is None
    assert mw.confirm("ONEUSDT", rows, 23 * 300_000, daily_cross=True)
