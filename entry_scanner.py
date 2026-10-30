"""
Binance Entry Scanner - alerts only, NO trading, NO API key needed
------------------------------------------------------------------
Scans liquid USDT pairs every 15 minutes (after each 15m candle closes) and alerts you
on Telegram when a coin matches a pullback or breakout setup in an uptrend.
Each alert includes entry, stop, 2 targets, reward/risk and a suggested position size,
so you can place the OCO immediately after buying.

It uses only public endpoints (no keys, cannot trade or withdraw).

Setup:
    pip install requests python-dotenv
    .env (optional, for Telegram alerts; otherwise alerts print in the console):
        TELEGRAM_TOKEN=...
        TELEGRAM_CHAT_ID=...
        ACCOUNT_USDT=500        # for position-size suggestion
        RISK_PCT=1              # % of account risked per trade
        MIN_QUOTE_VOLUME=10000000
        TOP_N=100

Run:
    python entry_scanner.py            # loop forever
    python entry_scanner.py --once     # one scan, then exit (good for testing)

IMPORTANT: this is a FILTER, not a prediction. Backtest or paper-trade the alerts
before using real money, and always place the stop immediately after entry.
"""
import argparse
import logging
from pathlib import Path
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.binance.com"
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "10000000"))  # 24h USDT volume
TOP_N = int(os.getenv("TOP_N", "100"))
ACCOUNT_USDT = float(os.getenv("ACCOUNT_USDT", "0"))
RISK_PCT = float(os.getenv("RISK_PCT", "1"))
MAX_24H_PUMP = 25.0      # skip coins already up more than this % in 24h (avoid chasing pumps)
MAX_24H_DUMP = -15.0     # skip coins crashing
MIN_RISK_PCT = 0.01      # stop at least 1% away (tighter = noise)
MAX_RISK_PCT = 0.06      # stop no more than 6% away
COOLDOWN_SECONDS = 4 * 3600
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDE", "EUR", "EURI", "AEUR", "XUSD", "BFUSD"}
BAD_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.FileHandler(str(Path(__file__).resolve().parent / "scanner.log")), logging.StreamHandler()])
log = logging.getLogger("scanner")
last_alert = {}


# ---------- indicators ----------
def ema(values, n):
    k = 2 / (n + 1)
    e = values[0]
    out = [e]
    for v in values[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr(highs, lows, closes, n=14):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


# ---------- data ----------
def get(path, **params):
    r = requests.get(BASE + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def klines(symbol, interval, limit=120):
    raw = get("/api/v3/klines", symbol=symbol, interval=interval, limit=limit)
    raw = raw[:-1]  # drop the still-open candle: only closed candles are used
    return {
        "open": [float(x[1]) for x in raw],
        "high": [float(x[2]) for x in raw],
        "low": [float(x[3]) for x in raw],
        "close": [float(x[4]) for x in raw],
        "vol": [float(x[5]) for x in raw],
    }


def universe():
    out = []
    for t in get("/api/v3/ticker/24hr"):
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in STABLES or base.endswith(BAD_SUFFIX):
            continue
        qv = float(t["quoteVolume"])
        chg = float(t["priceChangePercent"])
        if qv < MIN_QUOTE_VOLUME or chg > MAX_24H_PUMP or chg < MAX_24H_DUMP:
            continue
        out.append((s, qv, chg))
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:TOP_N]


def fmt(x):
    return f"{x:.6g}"


# ---------- strategy ----------
def hourly_uptrend(symbol):
    h = klines(symbol, "1h", 120)
    c = h["close"]
    e20, e50 = ema(c, 20), ema(c, 50)
    return c[-1] > e50[-1] and e20[-1] > e50[-1] and e50[-1] > e50[-6]


def analyze(symbol, chg24):
    if not hourly_uptrend(symbol):
        return None
    k = klines(symbol, "15m", 120)
    c, h, l, o, v = k["close"], k["high"], k["low"], k["open"], k["vol"]
    e20 = ema(c, 20)
    r = rsi(c)
    a = atr(h, l, c)
    entry = c[-1]

    setup = None
    # A) pullback to EMA20 in an uptrend, with a green bounce candle
    touched = min(l[-3:]) <= e20[-1] * 1.003
    near = entry <= e20[-1] * 1.025
    if touched and near and entry > e20[-1] and c[-1] > o[-1] and 38 <= r <= 58:
        setup = "Pullback bounce (15m, 1h uptrend)"
    # B) breakout of the 20-candle high on strong volume, not overextended
    avg_vol = sum(v[-21:-1]) / 20
    if (setup is None and entry > max(h[-21:-1]) and v[-1] > 1.5 * avg_vol
            and r < 72 and entry <= e20[-1] * 1.025):
        setup = "Breakout on volume (15m, 1h uptrend)"
    if not setup:
        return None

    stop = min(l[-12:]) - 0.2 * a
    risk = entry - stop
    risk_pct = risk / entry
    if not (MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT):
        return None
    t1, t2 = entry + 1.5 * risk, entry + 2.5 * risk
    # Do not reject confirmed breakouts for exceeding the PREVIOUS high.
    # For pullbacks, require a preceding recent high at/above TP1.
    if setup.startswith("Pullback") and max(h[-96:]) < t1:
        return None

    qty = (ACCOUNT_USDT * RISK_PCT / 100) / risk if ACCOUNT_USDT else None
    return dict(symbol=symbol, setup=setup, entry=entry, stop=stop, t1=t1, t2=t2,
                risk_pct=risk_pct * 100, rsi=r, chg24=chg24, qty=qty)


def btc_note():
    try:
        c = klines("BTCUSDT", "1h", 120)["close"]
        e50 = ema(c, 50)
        if c[-1] < e50[-1] and e50[-1] < e50[-6]:
            return "WARNING: BTC 1h is in a downtrend. Altcoin longs fail more often now."
        return "BTC 1h trend OK."
    except Exception:
        return ""


# ---------- alerts ----------
def send(msg):
    log.info("\n" + msg)
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": msg}, timeout=10)
        except Exception as e:
            log.warning(f"Telegram failed: {e}")


def build_msg(s, note):
    base = s["symbol"][:-4]
    size = ""
    if s["qty"]:
        size = (f"\nSize for {RISK_PCT}% risk of ${ACCOUNT_USDT:.0f}: {s['qty']:.6g} {base} "
                f"(~${s['qty'] * s['entry']:.2f})")
    return (f"ENTRY CANDIDATE: {s['symbol']}\n{s['setup']}\n"
            f"Entry ~{fmt(s['entry'])} | Stop {fmt(s['stop'])} (-{s['risk_pct']:.1f}%)\n"
            f"Target 1 {fmt(s['t1'])} (1.5R) | Target 2 {fmt(s['t2'])} (2.5R)\n"
            f"RSI {s['rsi']:.0f} | 24h {s['chg24']:+.1f}%{size}\n{note}\n"
            f"Place the stop/OCO the moment you buy. Skip it if price already moved >1% from entry.\n"
            f"https://www.binance.com/en/trade/{base}_USDT")


# ---------- main ----------
def scan():
    note = btc_note()
    found = 0
    coins = universe()
    log.info(f"Scanning {len(coins)} pairs...")
    for symbol, _, chg in coins:
        try:
            if time.time() - last_alert.get(symbol, 0) < COOLDOWN_SECONDS:
                continue
            res = analyze(symbol, chg)
            if res:
                last_alert[symbol] = time.time()
                send(build_msg(res, note))
                found += 1
            time.sleep(0.15)  # stay well under rate limits
        except Exception as e:
            log.warning(f"{symbol}: {e}")
    log.info(f"Scan done: {found} candidate(s). {note}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    a = p.parse_args()
    if a.once:
        scan()
        return
    send("Entry scanner started. Alerts only, no trading.")
    while True:
        try:
            scan()
        except Exception as e:
            log.warning(f"Scan error: {e}")
        time.sleep(900 - (time.time() % 900) + 10)  # wake 10s after each 15m close


if __name__ == "__main__":
    main()
