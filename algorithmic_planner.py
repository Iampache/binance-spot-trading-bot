"""Explainable Spot long setups from closed candles; no order submission.

This ruleset is a testable heuristic, not a trained/predictive model or a claim
of positive expected returns. All execution and risk gates remain in build_plan.
"""

from decimal import ROUND_CEILING

import exit_manager as em

VERSION = "atr-structure-v2"
BTC_WARNING = "WARNING: BTC 1h trend is weak/downward. Altcoin longs can fail more often."
D = em.D
DURATIONS = {"15m": 900_000, "1h": 3_600_000}


def ema(values, period):
    weight = D(2) / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * weight + result[-1] * (1 - weight))
    return result


def indicators(frame):
    closes, highs, lows = frame["close"], frame["high"], frame["low"]
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(D(0), value) for value in changes]
    losses = [max(D(0), -value) for value in changes]
    gain, loss = sum(gains[:14]) / 14, sum(losses[:14]) / 14
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    atr = sum(trs[:14]) / 14
    for gained, lost, tr in zip(gains[14:], losses[14:], trs[14:]):
        gain, loss, atr = (gain * 13 + gained) / 14, (loss * 13 + lost) / 14, (atr * 13 + tr) / 14
    rsi = D(50) if gain == loss == 0 else D(100) if loss == 0 else 100 - 100 / (1 + gain / loss)
    average_volume = sum(frame["vol"][-21:-1]) / 20
    if atr <= 0 or average_volume <= 0:
        raise ValueError("Algorithm: insufficient volatility/volume evidence")
    return {
        "ema20": ema(closes, 20),
        "ema50": ema(closes, 50),
        "atr": atr,
        "rsi": rsi,
        "relative_volume": frame["vol"][-1] / average_volume,
    }


def closed_candles(client, symbol, interval, now_ms):
    raw = client.get_klines(symbol=symbol, interval=interval, limit=201)
    duration = DURATIONS[interval]
    if not isinstance(raw, list) or not 120 <= len(raw) <= 201:
        raise ValueError("Algorithm: insufficient candle history")
    rows = []
    previous = None
    for index, row in enumerate(raw):
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            raise ValueError("Algorithm: malformed candle")
        opened, closed = row[0], row[6]
        if (
            type(opened) is not int
            or type(closed) is not int
            or opened < 0
            or closed != opened + duration - 1
            or opened % duration
            or (previous is not None and opened - previous != duration)
        ):
            raise ValueError("Algorithm: missing, unordered or invalid candle timestamps")
        previous = opened
        if closed >= now_ms:
            if index != len(raw) - 1 or opened > now_ms:
                raise ValueError("Algorithm: future/incomplete candle sequence")
            continue  # Never calculate indicators from an open candle.
        o, h, low, close, volume = (D(row[i]) for i in (1, 2, 3, 4, 5))
        if not (0 < low <= min(o, close) <= max(o, close) <= h and volume >= 0):
            raise ValueError("Algorithm: invalid OHLC/volume")
        rows.append((o, h, low, close, volume, closed))
    if len(rows) < 120 or now_ms - rows[-1][5] > duration + 30_000:
        raise ValueError("Algorithm: stale or insufficient closed candles")
    return {
        name: [row[i] for row in rows]
        for i, name in enumerate(("open", "high", "low", "close", "vol", "close_ms"))
    }


def snapshot(symbol):
    client = em.get_client()
    now = client.get_server_time().get("serverTime")
    if type(now) is not int or now <= 0:
        raise ValueError("Algorithm: invalid exchange server time")
    return {
        "now_ms": now,
        "entry": closed_candles(client, symbol, "15m", now),
        "trend": closed_candles(client, symbol, "1h", now),
        "btc": closed_candles(client, "BTCUSDT", "1h", now),
    }


def setup(data):
    trends = {}
    for key in ("trend", "btc"):
        frame = data[key]
        fast, slow = ema(frame["close"], 20), ema(frame["close"], 50)
        trends[key] = frame["close"][-1] > fast[-1] > slow[-1] > slow[-6]
    if not trends["trend"]:
        raise ValueError("Algorithm: 1h trend does not support a long entry")
    frame = data["entry"]
    stats = indicators(frame)
    close, opened = frame["close"][-1], frame["open"][-1]
    fast, rsi, volume = stats["ema20"][-1], stats["rsi"], stats["relative_volume"]
    if close > fast * D("1.025"):
        raise ValueError("Algorithm: price is extended above EMA20; skip chasing")
    pullback = (
        min(frame["low"][-3:]) <= fast * D("1.003")
        and close > fast
        and close > opened
        and D(38) <= rsi <= 60
        and volume >= D(".8")
    )
    breakout = (
        close > max(frame["high"][-21:-1])
        and close > opened
        and volume >= D("1.5")
        and 45 <= rsi <= 68
    )
    if not pullback and not breakout:
        raise ValueError("Algorithm: no confirmed pullback/breakout setup")
    stats["setup"] = "Pullback bounce" if pullback else "Volume breakout"
    stats["market_warning"] = "" if trends["btc"] else BTC_WARNING
    return stats


def quote(symbol):
    book = em.get_client().get_orderbook_ticker(symbol=symbol)
    if book.get("symbol") != symbol:
        raise ValueError("Algorithm: order-book symbol mismatch")
    bid, ask = D(book["bidPrice"]), D(book["askPrice"])
    if not 0 < bid <= ask:
        raise ValueError("Algorithm: invalid order book")
    return bid, ask


def geometry(data, stats, entry, tick, *, approved_stop=None):
    frame, atr = data["entry"], stats["atr"]
    support = min(frame["low"][-12:])
    required_stop = em.round_to(
        min(support - atr / 4, entry - atr * D("1.5"), entry * D(".99")), tick
    )
    stop = required_stop if approved_stop is None else D(approved_stop)
    if stop > required_stop:
        raise ValueError(
            "Algorithm: volatility/support now requires a lower stop; fresh approval required"
        )
    risk = entry - stop
    if not 0 < stop < entry or not D(".01") <= risk / entry <= D(".06"):
        raise ValueError("Algorithm: statistical stop cannot satisfy the 1%-6% distance rule")
    # Only pivots with two CLOSED candles on both sides are confirmed resistance.
    highs = frame["high"]
    resistance = sorted(
        {
            highs[i]
            for i in range(max(2, len(highs) - 96), len(highs) - 2)
            if highs[i] > entry
            and highs[i] == max(highs[i - 2 : i + 3])
            and highs[i] > max(highs[i - 2 : i])
            and highs[i] > max(highs[i + 1 : i + 3])
        }
    )
    first = min(entry + 2 * risk, resistance[0] - atr / 10) if resistance else entry + 2 * risk
    later = [value - atr / 10 for value in resistance if value - atr / 10 > first + tick]
    second = min(entry + 3 * risk, later[0]) if later else entry + 3 * risk
    first, second = em.round_to(first, tick), em.round_to(second, tick)
    if not stop < entry < first < second or (first - entry) / risk < D("1.5"):
        raise ValueError("Algorithm: nearby resistance leaves insufficient reward/risk")
    return stop, first, second, support


def propose(symbol, *, entry=None):
    """Full mode selects a capped current entry; semi mode takes one user limit."""
    data = snapshot(symbol)
    stats = setup(data)
    _, ask = quote(symbol)
    filters = em.symbol_filters(symbol)
    mode = "semi" if entry is not None else "full"
    if entry is None:
        price = (ask * D("1.002") / filters["tick"]).to_integral_value(
            rounding=ROUND_CEILING
        ) * filters["tick"]
    else:
        price = em.round_to(entry, filters["tick"])
        if not ask - 2 * stats["atr"] <= price <= ask:
            raise ValueError(
                "Algorithm: choose a buy price at/below ask and within 2 ATR; no breakout stop-buy is placed"
            )
    stop, t1, t2, support = geometry(data, stats, price, filters["tick"])
    return {
        "symbol": symbol,
        "entry": price,
        "stop": stop,
        "t1": t1,
        "t2": t2,
        "setup": stats["setup"] + " (closed candles)",
        "manual_override": False,
        "entry_mode": "watch" if entry is not None else "now",
        "algorithmic": True,
        "algorithm_version": VERSION,
        "algorithm_mode": mode,
        "analysis_created_at": str(D(data["now_ms"]) / 1000),
        "analysis_candle_close": str(data["entry"]["close_ms"][-1]),
        "market_warning": stats["market_warning"],
        "analysis_summary": f"{stats['setup']}; symbol 1h uptrend; "
        f"BTC {'weak/downward (advisory)' if stats['market_warning'] else 'uptrend'}; "
        f"ATR14 {stats['atr']:.6g}; "
        f"RSI14 {stats['rsi']:.1f}; volume {stats['relative_volume']:.2f}x; "
        f"support {support:.6g}. Targets capped before confirmed resistance, "
        "otherwise 2R/3R projections. No profitability claim.",
    }


def validate_plan(sig, plan):
    if sig.get("algorithm_version") != VERSION or sig.get("algorithm_mode") not in (
        "semi",
        "full",
        "assisted",
    ):
        raise ValueError("Algorithm: missing/unsupported strategy identity")
    if sig.get("manual_override") is not False:
        raise ValueError("Algorithm modes cannot use the manual risk override")
    expected = "watch" if sig["algorithm_mode"] == "semi" else "now"
    if plan["entry_mode"] != expected:
        raise ValueError("Algorithm entry mode changed; fresh approval required")
    data = snapshot(plan["symbol"])
    created = D(sig["analysis_created_at"])
    age = D(data["now_ms"]) / 1000 - created
    asof = D(sig["analysis_candle_close"])
    if (
        not 0 <= age <= 1200
        or asof != asof.to_integral_value()
        or not 0 < created * 1000 - asof <= 930000
        or asof > data["entry"]["close_ms"][-1]
    ):
        raise ValueError("Algorithm: expired or future analysis; request a fresh proposal")
    stats = setup(data)
    _, ask = quote(plan["symbol"])
    close = data["entry"]["close"][-1]
    allowed_drift = min(D(".025"), max(D(".01"), D("1.5") * stats["atr"] / close))
    if abs(ask / close - 1) > allowed_drift:
        raise ValueError("Algorithm: live quote moved too far from closed-candle setup")
    if any(
        low <= D(plan["stop"])
        for low, timestamp in zip(data["entry"]["low"], data["entry"]["close_ms"])
        if timestamp > asof
    ):
        raise ValueError("Algorithm: a closed candle breached the approved stop before entry")
    tick = em.symbol_filters(plan["symbol"])["tick"]
    _, t1, t2, _ = geometry(data, stats, D(plan["price"]), tick, approved_stop=plan["stop"])
    if D(plan["t1"]) > t1 or (em.target_style(plan) == "two" and D(plan["t2"]) > t2):
        raise ValueError("Algorithm: new resistance invalidates approved targets")
    fee, price, stop = (D(plan[k]) for k in ("fee_rate", "price", "stop"))
    net_reward = D(plan["t1"]) - price - (D(plan["t1"]) + price) * fee
    risk = price - stop + (price + stop) * fee
    if risk <= 0 or net_reward / risk < D("1.5"):
        label = "take profit" if em.target_style(plan) == "single" else "TP1"
        raise ValueError(f"Algorithm: {label} reward/risk below 1.5R after reserved fees")

    proceeds = sum(
        (
            D(plan[part]) * D(plan[target])
            for part, target in zip(("half", "rest"), em.target_fields(plan))
        ),
        D(0),
    ) * (1 - fee)
    if proceeds <= D(plan["cost"]):
        raise ValueError(
            "Algorithm: quantity precision/fee reserves leave no positive target-profit scenario"
        )
    return stats["market_warning"]
