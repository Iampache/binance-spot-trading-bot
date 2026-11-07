"""Read-only momentum discovery. No account, approval or order authority."""

import math
from collections import deque

import entry_scanner as sc
import safety
from market_discovery import eligible_symbols

POLL_SECONDS = 60
MIN_QUOTE_VOLUME = 1_000_000
MIN_RECENT_VOLUME = 100_000
MIN_5M_CHANGE = 2.0
MIN_15M_CHANGE = 3.0
MIN_RELATIVE_VOLUME = 2.0
DAILY_CROSS_PERCENT = 8.0
MAX_CONFIRMATIONS = 8
MAX_ALERTS = 3  # total in any rolling 15 minutes
ALERT_WINDOW = 900
PAIR_COOLDOWN = 4 * 3600


def number(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Momentum: non-finite market data")
    return value


def confirm(symbol, raw, now_ms, *, daily_cross=False):
    """Validate contiguous closed 5m candles, then price AND quote-volume growth."""
    if not isinstance(raw, list):
        raise ValueError("Momentum: invalid candle response")
    candles = []
    try:
        for row in raw:
            opened, closed = int(row[0]), int(row[6])
            if opened % 300_000 or closed != opened + 299_999:
                raise ValueError("Momentum: invalid candle interval")
            if closed >= now_ms:
                continue
            o, h, low, close, quote = (number(row[i]) for i in (1, 2, 3, 4, 7))
            if not (0 < low <= min(o, close) <= max(o, close) <= h) or quote < 0:
                raise ValueError("Momentum: invalid candle values")
            candles.append((opened, closed, close, quote))
    except (IndexError, KeyError, TypeError, OverflowError) as error:
        raise ValueError("Momentum: incomplete candle data") from error
    if (
        len(candles) < 23
        or now_ms - candles[-1][1] > 330_000
        or any(b[0] - a[0] != 300_000 for a, b in zip(candles, candles[1:]))
    ):
        raise ValueError("Momentum: stale or missing closed candles")
    candles = candles[-23:]
    change5 = (candles[-1][2] / candles[-2][2] - 1) * 100
    change15 = (candles[-1][2] / candles[-4][2] - 1) * 100
    recent = sum(row[3] for row in candles[-3:])
    baseline = sum(row[3] for row in candles[:-3]) / 20
    relative = recent / 3 / baseline if baseline > 0 else 0
    if not all(math.isfinite(value) for value in (change5, change15, recent, baseline, relative)):
        raise ValueError("Momentum: invalid derived candle values")
    if (
        (
            change5 < MIN_5M_CHANGE
            and change15 < MIN_15M_CHANGE
            and not (daily_cross and change5 > 0 and change15 > 0)
        )
        or recent < MIN_RECENT_VOLUME
        or relative < MIN_RELATIVE_VOLUME
    ):
        return None
    return dict(
        symbol=symbol,
        price=candles[-1][2],
        change5=change5,
        change15=change15,
        relative_volume=relative,
        recent_volume=recent,
        closed_at=candles[-1][1],
    )


class MomentumWatch:
    """Session-local samples and notification limits, owned by one worker."""

    def __init__(self):
        self.history = {}
        self.last_sample = None
        self.daily_changes = {}
        self.symbols = set()
        self.metadata_at = None
        self.notified = {}
        self.alerts = deque()

    def can_notify(self, symbol, now):
        while self.alerts and now - self.alerts[0] >= ALERT_WINDOW:
            self.alerts.popleft()
        return (
            len(self.alerts) < MAX_ALERTS
            and now - self.notified.get(symbol, -PAIR_COOLDOWN) >= PAIR_COOLDOWN
        )

    def attempted(self, symbol, now):
        # Reserve before Telegram: an ambiguous send must not produce a retry storm.
        self.notified[symbol] = now
        self.alerts.append(now)

    def shortlist(self, tickers, symbols, now, now_ms):
        if not isinstance(tickers, list):
            raise ValueError("Momentum: invalid market response")
        current = {}
        seen = set()
        for row in tickers:
            if not isinstance(row, dict) or row.get("symbol") not in symbols:
                continue
            symbol = row["symbol"]
            if symbol in seen:
                raise ValueError("Momentum: duplicate ticker")
            seen.add(symbol)
            try:
                price, volume, change = (
                    number(row[key]) for key in ("lastPrice", "quoteVolume", "priceChangePercent")
                )
                age = now_ms - number(row["closeTime"])
                if price <= 0 or volume < MIN_QUOTE_VOLUME or not -5000 <= age <= 120_000:
                    continue
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
            current[symbol] = dict(symbol=symbol, price=price, qv=volume, chg=change)
        if self.last_sample is not None and not 0 < now - self.last_sample <= 150:
            self.history.clear()
            self.daily_changes.clear()
        self.last_sample = now
        self.history = {s: h for s, h in self.history.items() if s in current}
        candidates = []
        for symbol, ticker in current.items():
            history = self.history.setdefault(symbol, deque(maxlen=18))
            moves = []
            previous_daily = self.daily_changes.get(symbol)
            crossed = (
                bool(history)
                and previous_daily is not None
                and previous_daily < DAILY_CROSS_PERCENT <= ticker["chg"]
            )
            ticker["daily_cross"] = crossed
            if crossed:
                moves.append(DAILY_CROSS_PERCENT)
            for seconds, threshold in ((300, MIN_5M_CHANGE), (900, MIN_15M_CHANGE)):
                samples = [(t, p) for t, p in history if seconds <= now - t <= seconds + 75]
                if samples:
                    _, previous = max(samples)
                    change = (ticker["price"] / previous - 1) * 100
                    if change >= threshold:
                        moves.append(change)
            history.append((now, ticker["price"]))
            if moves and self.can_notify(symbol, now):
                candidates.append((max(moves), ticker))
        self.daily_changes = {s: row["chg"] for s, row in current.items()}
        candidates.sort(key=lambda item: (-item[0], item[1]["symbol"]))
        return [ticker for _, ticker in candidates[:MAX_CONFIRMATIONS]]

    def scan(self, now, now_ms, stopped):
        if stopped():
            return []
        if self.metadata_at is None or now - self.metadata_at >= 900:
            self.symbols = eligible_symbols(sc.get("/api/v3/exchangeInfo"))
            self.metadata_at = now
        candidates = self.shortlist(sc.get("/api/v3/ticker/24hr"), self.symbols, now, now_ms)
        results = []
        for ticker in candidates:
            if stopped():
                break
            try:
                signal = confirm(
                    ticker["symbol"],
                    sc.get("/api/v3/klines", symbol=ticker["symbol"], interval="5m", limit=24),
                    now_ms,
                    daily_cross=ticker.get("daily_cross", False),
                )
            except Exception as error:
                sc.log.warning("Momentum %s: %s", ticker["symbol"], safety.safe_error(error))
                continue
            if signal:
                results.append({**ticker, **signal})
        return results
