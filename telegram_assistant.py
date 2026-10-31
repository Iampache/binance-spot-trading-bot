"""
Telegram Trade Assistant: scanner proposals + on-demand /check + manual entry
-------------------------------------------------------------------------------
A) AUTOMATIC: scans Binance every 15 min and sends proposals with [Approve] [Reject] buttons.
B) ON DEMAND: send /check (or /check_ticker or /check-ticker) -> it asks for the pair
   (e.g. BTC/USDT) -> it checks the coin and replies with a status:
       SAFE TO ENTER          -> buttons: 1 Enter (shows the plan) | 3 Manual | Cancel
       WARNING - WAIT 15 MIN  -> buttons: 2 Recheck in 15 min | 3 Manual | Cancel
   1 -> shows Entry / SL / TP1 / TP2 / size / $ at risk with [Approve] [Reject]
   2 -> rechecks after 15 minutes and sends you the updated status
   3 -> asks you to send:  ENTRY STOP TP1 TP2   (example: 0.0777 0.0766 0.0795 0.0806)
        then shows the same plan with [Approve] [Reject]
   Approve -> live price re-check -> price-capped IOC buy -> 2 OCO exits (stop + targets), original stops retained after TP1.

Safeguards (apply to ALL paths, including manual):
  - Only taps/messages from YOUR chat ID (TELEGRAM_CHAT_ID) are accepted
  - Proposals expire after 5 minutes (APPROVAL_TTL_SECONDS); /check cards after 10 (CHECK_TTL_SECONDS)
  - On Approve the live price is rechecked: moved >0.5% since the proposal -> refused
  - Size cap, open-trade limit, daily loss limit, reward/risk >= 1.5 to TP1, stop below price
  - Manual levels must be within 1% of the live price (entries are price-capped IOC orders)
  - If protection fails, confirmed bot-owned free inventory is reconciled before an exit attempt

Other commands: /status  /cancel  /help
Env extras: AUTO_SCAN=false  -> disable automatic proposals (keep only /check)

Files needed in the SAME folder: entry_scanner.py, exit_manager.py, trade_assistant.py,
telegram_assistant.py, .env
Run:  python telegram_assistant.py       (or double-click start_telegram_assistant.bat)
Only ONE program may read this bot's updates at a time (409 error otherwise).
"""

import json
import math
import os
import threading
import time
import uuid

import requests
from binance.exceptions import BinanceAPIException

import entry_scanner as sc
import exit_manager as em
import safety
import trade_assistant as ta

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
APPROVAL_TTL = int(os.getenv("APPROVAL_TTL_SECONDS", "300"))
CHECK_TTL = int(os.getenv("CHECK_TTL_SECONDS", "600"))
AUTO_SCAN = os.getenv("AUTO_SCAN", "true").lower() == "true"
API = f"https://api.telegram.org/bot{TOKEN}"

pending = {}  # proposal id -> dict
checks = {}  # /check card id -> dict
state = {}  # conversation state for /check and manual entry
recheck_timers = set()
proposal_generation = 0
workers = set()
pend_lock = threading.Lock()
trade_lock = threading.Lock()  # one live trade being executed/managed at a time

HELP = (
    "Commands:\n"
    "/check  - check any coin (then send the pair, e.g. BTC/USDT)\n"
    "/check BTC/USDT - check directly\n"
    "/status - balance and open orders\n"
    "/cancel - cancel what you were doing\n\n"
    "After /check: 1 = enter with the scanner plan, 2 = recheck in 15 min, 3 = manual levels."
)


def safe(e):
    return safety.safe_error(e)


def tg(method, **params):
    try:
        r = requests.post(f"{API}/{method}", data=params, timeout=45)
        result = r.json()
        if not result.get("ok"):
            return {
                "ok": False,
                "description": "Telegram rejected request; check bot configuration",
            }
        return result
    except Exception as e:
        return {"ok": False, "description": safe(e)}


def say(text, **extra):
    return tg("sendMessage", chat_id=CHAT_ID, text=text, **extra)


def edit(item, status):
    tg(
        "editMessageText",
        chat_id=CHAT_ID,
        message_id=item["mid"],
        text=item["text"] + "\n\n" + status,
    )


def mode_label():
    return "LIVE - REAL MONEY" if ta.LIVE else "TESTNET - fake money"


# ---------- proposals (Approve / Reject) ----------
def proposal_text(sig, p, note):
    lines = [
        f"TRADE PROPOSAL [{mode_label()}]",
        f"{p['symbol']}  |  {sig['setup']}",
        "",
        f"Estimated stop loss: ~${p['loss']:.2f} including reserved fees; slippage can increase loss.",
        f"Entry ~ {sc.fmt(p['price'])}",
        f"Stop: {sc.fmt(p['stop'])} (-{p['risk_pct']:.1f}%)",
        f"TP1: {sc.fmt(p['t1'])} (sells half)",
        f"TP2: {sc.fmt(p['t2'])} (sells rest)",
        f"Size: {em.fmt(p['qty'])} (~${p['cost']:.2f} of ${p['balance']:.2f} USDT)",
        f"Gross TP1 / TP2 gains before fees: about +${p['gain1']:.2f} / +${p['gain2']:.2f}",
        "After TP1 the ORIGINAL OCO2 stop remains active; breakeven is NOT automatic.",
    ]
    if note:
        lines.append(note)
    if p.get("manual_override") is True:
        lines += [
            "",
            "MANUAL RISK OVERRIDE — approving accepts the risks below.",
            "Volume, spread/depth, trend/setup, reward/risk, stop-distance, 1% sizing and the 3 USDT daily-loss gate are bypassed.",
            "12 USDT cap, one active/unresolved trade, exchange rules and OCO protection remain mandatory.",
            "Estimated stop loss includes reserved fees, but slippage can make the actual loss larger.",
        ]
        lines += [f"OVERRIDDEN: {warning}" for warning in p.get("override_warnings", [])]
    lines += [
        "",
        f"Expires in {APPROVAL_TTL // 60} min. Approve only after checking the chart.",
    ]
    return "\n".join(lines)


def send_proposal(sig, plan, note):
    with pend_lock:
        generation = proposal_generation
    pid = uuid.uuid4().hex
    text = proposal_text(sig, plan, note)
    kb = {
        "inline_keyboard": [
            [
                {"text": f"Approve (${plan['cost']:.2f})", "callback_data": f"A:{pid}"},
                {"text": "Reject", "callback_data": f"R:{pid}"},
            ]
        ]
    }
    r = say(text, reply_markup=json.dumps(kb))
    if not r.get("ok"):
        sc.log.warning(f"Could not send proposal: {r.get('description')}")
        return
    with pend_lock:
        if generation != proposal_generation:
            edit(
                {"mid": r["result"]["message_id"], "text": text},
                "CANCELLED. No approval available.",
            )
            return
        pending[pid] = dict(
            sig=sig,
            plan=plan,
            created=time.time(),
            mid=r["result"]["message_id"],
            text=text,
        )


def expire_pending():
    now = time.time()
    with pend_lock:
        dead = [k for k, v in pending.items() if now - v["created"] > APPROVAL_TTL]
        items = [pending.pop(k) for k in dead]
    for item in items:
        edit(item, "EXPIRED. No order placed.")
    if state and now - state.get("t", now) > 600:
        state.clear()
    for cid, card in list(checks.items()):
        if now - card["created"] > CHECK_TTL:
            checks.pop(cid, None)


# ---------- /check analysis ----------
def parse_symbol(raw):
    s = raw.upper().replace("/", "").replace("-", "").replace("_", "").replace(" ", "")
    return s if s.endswith("USDT") else s + "USDT"


def diagnose(symbol):
    """Explain why no entry setup is active right now."""
    h1 = sc.klines(symbol, "1h", 120)["close"]
    e20h, e50h = sc.ema(h1, 20), sc.ema(h1, 50)
    if not (h1[-1] > e50h[-1] and e20h[-1] > e50h[-1] and e50h[-1] > e50h[-6]):
        return ["1h trend is not up (price, EMA20 and EMA50 are not aligned upward)"]
    k = sc.klines(symbol, "15m", 120)
    c, h, lows, o, v = k["close"], k["high"], k["low"], k["open"], k["vol"]
    e20 = sc.ema(c, 20)
    r = sc.rsi(c)
    entry = c[-1]
    touched = min(lows[-3:]) <= e20[-1] * 1.003
    near = entry <= e20[-1] * 1.025
    pull = touched and near and entry > e20[-1] and c[-1] > o[-1] and 38 <= r <= 58
    avg_vol = sum(v[-21:-1]) / 20
    brk = entry > max(h[-21:-1]) and v[-1] > 1.5 * avg_vol and r < 72 and near
    if pull or brk:
        return [
            "A setup is forming, but the stop distance (must be 1-6%) or the room to the "
            "recent high does not pass the risk rules"
        ]
    out = []
    ext = entry / e20[-1] - 1
    if ext > 0.025:
        out.append(f"Extended: price is {ext * 100:.1f}% above the 15m EMA20 (wait for a pullback)")
    elif not touched:
        out.append("Has not pulled back to the 15m EMA20 yet")
    elif entry <= e20[-1]:
        out.append("Price is below the 15m EMA20 (needs to close back above it)")
    elif c[-1] <= o[-1]:
        out.append("Last 15m candle is red: waiting for a green bounce candle")
    if not 38 <= r <= 58:
        out.append(f"RSI {r:.0f} is outside the 38-58 pullback range")
    return out or ["No active pullback or breakout setup right now"]


def check_ticker(symbol):
    t = sc.get("/api/v3/ticker/24hr", symbol=symbol)  # raises HTTPError for unknown pairs
    price, chg, qv = (
        float(t["lastPrice"]),
        float(t["priceChangePercent"]),
        float(t["quoteVolume"]),
    )
    reasons = []
    if qv < sc.MIN_QUOTE_VOLUME:
        reasons.append(
            f"Low liquidity: 24h volume ${qv / 1e6:.1f}M (minimum ${sc.MIN_QUOTE_VOLUME / 1e6:.0f}M)"
        )
    if chg > sc.MAX_24H_PUMP:
        reasons.append(f"Already up {chg:+.1f}% in 24h: chasing a pump")
    if chg < sc.MAX_24H_DUMP:
        reasons.append(f"Down {chg:+.1f}% in 24h: falling hard")
    sig = sc.analyze(symbol, chg) if not reasons else None
    if sig is None:
        reasons += diagnose(symbol)
    status = "SAFE" if (sig and not reasons) else "WARNING"
    return dict(
        symbol=symbol,
        price=price,
        chg=chg,
        qv=qv,
        sig=sig,
        reasons=reasons,
        status=status,
        note=sc.btc_note(),
    )


def send_check_card(res, title="CHECK", generation=None):
    cid = uuid.uuid4().hex
    pair = f"{res['symbol'][:-4]}/USDT"
    lines = [
        f"{title}: {pair}  [{mode_label()}]",
        f"Price {sc.fmt(res['price'])} | 24h {res['chg']:+.1f}% | volume ${res['qv'] / 1e6:.0f}M",
        "",
    ]
    if res["status"] == "SAFE":
        lines += [
            "STATUS: 1 - SAFE TO ENTER",
            f"Why: {res['sig']['setup']}, RSI {res['sig']['rsi']:.0f}",
            "(Technical rules met. Execution checks may still reject. NOT a safety guarantee.)",
        ]
        row = [
            {"text": "1 - Enter (show plan)", "callback_data": f"C1:{cid}"},
            {"text": "3 - Manual override", "callback_data": f"C3:{cid}"},
        ]
    else:
        lines += ["STATUS: 2 - WARNING - WAIT 15 MINUTES", "Reasons:"] + [
            f"- {x}" for x in res["reasons"]
        ]
        row = [
            {"text": "2 - Recheck in 15 min", "callback_data": f"C2:{cid}"},
            {"text": "3 - Manual override", "callback_data": f"C3:{cid}"},
        ]
    if res["note"]:
        lines += ["", res["note"]]
    text = "\n".join(lines)
    kb = {"inline_keyboard": [row, [{"text": "Cancel", "callback_data": f"CX:{cid}"}]]}
    r = say(text, reply_markup=json.dumps(kb))
    if r.get("ok"):
        with pend_lock:
            if generation is not None and generation != proposal_generation:
                return
            checks[cid] = dict(
                res=res, created=time.time(), mid=r["result"]["message_id"], text=text
            )


def run_check(raw, title="CHECK", generation=None):
    with pend_lock:
        if generation is None:
            generation = proposal_generation
        if generation != proposal_generation:
            return
    symbol = parse_symbol(raw)
    try:
        res = check_ticker(symbol)
    except requests.HTTPError:
        say(f"{symbol} was not found on Binance Spot. Use the format BTC/USDT.")
        return
    except Exception as e:
        say(f"Check failed: {safe(e)}")
        return
    send_check_card(res, title, generation)


def handle_check_callback(cb, action, cid):
    def ack(t):
        tg("answerCallbackQuery", callback_query_id=cb["id"], text=t)

    item = checks.get(cid)
    if not item:
        ack("Expired. Send /check again.")
        return
    if time.time() - item["created"] > CHECK_TTL:
        checks.pop(cid, None)
        ack("Expired.")
        edit(item, "EXPIRED. Send /check again.")
        return
    res = item["res"]
    if action == "CX":
        checks.pop(cid, None)
        ack("Cancelled.")
        edit(item, "CANCELLED.")
    elif action == "C1":
        if not res["sig"] or res["status"] != "SAFE":
            ack("No valid setup right now.")
            return
        ack("Building the plan...")
        try:
            plan = ta.build_plan(res["sig"])
        except ValueError as e:
            edit(item, f"NOT AVAILABLE: {safety.redact(str(e))}")
            checks.pop(cid, None)
            return
        except Exception as e:
            edit(item, f"Error while building the plan: {safe(e)}")
            checks.pop(cid, None)
            return
        checks.pop(cid, None)
        edit(item, "Plan sent below. Tap Approve or Reject.")
        send_proposal(res["sig"], plan, res["note"])
    elif action == "C2":
        checks.pop(cid, None)
        ack("I'll recheck in 15 minutes.")
        edit(item, "Rechecking in 15 minutes. You'll get an update here.")
        timer = threading.Timer(
            900,
            run_check,
            args=(res["symbol"], "UPDATE after 15 min", proposal_generation),
        )
        timer.daemon = True
        recheck_timers.add(timer)
        timer.start()
    elif action == "C3":
        checks.pop(cid, None)
        ack("Manual override mode.")
        edit(
            item,
            "MANUAL risk override selected. Strategy/liquidity and account-risk gates are bypassed.",
        )
        state.clear()
        state.update(step="manual", symbol=res["symbol"], status=res["status"], t=time.time())
        say(
            f"MANUAL entry for {res['symbol'][:-4]}/USDT. Current price: {sc.fmt(res['price'])}\n"
            f"Send 4 numbers in one message:\nENTRY STOP TP1 TP2\n"
            f"Example: {sc.fmt(res['price'])} {sc.fmt(res['price'] * 0.97)} "
            f"{sc.fmt(res['price'] * 1.04)} {sc.fmt(res['price'] * 1.07)}\n"
            "Manual override bypasses liquidity/strategy checks, 1% sizing and the 3 USDT daily-loss gate. "
            "The 12 USDT cap, one active trade and valid protective OCOs remain mandatory. "
            "You must approve the final risk-override proposal before any buy.\n"
            f"(Entry must be within 1% of the live price. /cancel to stop.)"
        )


def handle_manual(text):
    try:
        nums = [float(x) for x in text.replace(",", " ").split()]
        if len(nums) != 4 or not all(math.isfinite(x) for x in nums):
            raise ValueError
    except ValueError:
        say("Please send 4 numbers: ENTRY STOP TP1 TP2 (or /cancel).")
        return
    entry, stop, t1, t2 = nums
    if not (0 < stop < entry < t1 < t2):
        say("The order must be STOP < ENTRY < TP1 < TP2. Try again or /cancel.")
        return
    symbol, status = state["symbol"], state.get("status", "?")
    sig = dict(
        symbol=symbol,
        entry=entry,
        stop=stop,
        t1=t1,
        t2=t2,
        setup="MANUAL entry (your levels)",
        manual_override=True,
        rsi=0,
        chg24=0,
    )
    state.clear()
    try:
        plan = ta.build_plan(sig)
    except ValueError as e:
        say(f"Cannot use these levels: {safety.redact(str(e))}")
        return
    except Exception as e:
        say(f"Error while building the plan: {safe(e)}")
        return
    send_proposal(
        sig,
        plan,
        f"MANUAL entry. Scanner status was: {status}. The levels are yours, not the scanner's.",
    )


# ---------- execution ----------
def run_trade(plan, approval):
    try:
        ta.execute(plan, approval=approval)
    except BinanceAPIException as e:
        em.alert(safe(e))
    except Exception as e:
        em.alert(f"Error: {safe(e)}. Check Open Orders and Holdings on Binance now.")
    finally:
        trade_lock.release()
        workers.discard(threading.current_thread())


def handle_callback(cb):
    sender = cb.get("from", {}).get("id")
    chat = cb.get("message", {}).get("chat", {}).get("id")
    if str(sender) != str(CHAT_ID) or str(chat) != str(CHAT_ID):
        return  # ignore strangers completely
    action, _, pid = cb.get("data", "").partition(":")
    if action.startswith("C"):
        handle_check_callback(cb, action, pid)
        return
    with pend_lock:
        item = pending.pop(pid, None)
    if not item:
        tg(
            "answerCallbackQuery",
            callback_query_id=cb["id"],
            text="Expired or already used.",
        )
        return
    if time.time() - item["created"] > APPROVAL_TTL:
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Expired.")
        edit(item, "EXPIRED. No order placed.")
        return
    if action == "R":
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Rejected.")
        edit(item, "REJECTED. No order placed.")
        return
    if action != "A":
        return

    tg("answerCallbackQuery", callback_query_id=cb["id"], text="Checking live price...")
    if not trade_lock.acquire(blocking=False):
        edit(item, "NOT PLACED: another trade is still being managed.")
        return
    try:
        old, fresh = item["plan"], ta.build_plan(item["sig"])
    except ValueError as e:
        trade_lock.release()
        edit(item, f"NOT PLACED: {safety.redact(str(e))}")
        return
    except Exception as e:
        trade_lock.release()
        edit(item, f"NOT PLACED: error while checking ({safe(e)})")
        return

    if not ta.approval_bounds(old, fresh):
        trade_lock.release()
        edit(
            item,
            "NOT PLACED: quantity, cost, risk or levels changed. A new approval is required.",
        )
        send_proposal(item["sig"], fresh, "Updated plan; previous approval was not used.")
        return

    edit(item, "APPROVED. Placing the order and the exits now...")
    approval = {
        **old,
        "approval_id": pid,
        "expires_at": item["created"] + APPROVAL_TTL,
        "approved": True,
    }
    try:
        worker = threading.Thread(target=run_trade, args=(fresh, approval), daemon=False)
        workers.add(worker)
        worker.start()
    except Exception:
        trade_lock.release()
        raise


def handle_message(msg):
    global proposal_generation
    if str(msg.get("chat", {}).get("id")) != str(CHAT_ID) or str(
        msg.get("from", {}).get("id")
    ) != str(CHAT_ID):
        return  # strangers get no reply at all
    text = msg.get("text", "").strip()
    low = text.lower().replace("-", "_")
    if low.startswith("/status"):
        try:
            say(f"[{mode_label()}]\n{em.status_report()}\nPending proposals: {len(pending)}")
        except Exception as e:
            say(f"Status error: {safe(e)}")
    elif low.startswith(("/help", "/start")):
        say(HELP)
    elif low.startswith("/cancel"):
        state.clear()
        checks.clear()
        with pend_lock:
            proposal_generation += 1
            canceled = list(pending.values())
            pending.clear()
        for item in canceled:
            edit(item, "CANCELLED. This proposal can no longer be approved.")
        for timer in list(recheck_timers):
            timer.cancel()
        recheck_timers.clear()
        say(
            "Cancelled pending proposals, manual entry and scheduled rechecks. "
            "Already submitted orders and protective OCOs remain active; /status shows them."
        )
    elif low.startswith("/check"):
        parts = text.split(None, 1)
        state.clear()
        if len(parts) > 1:
            run_check(parts[1])
        else:
            state.update(step="ticker", t=time.time())
            say("Which coin? Send the pair, for example BTC/USDT")
    elif state.get("step") == "ticker" and text:
        state.clear()
        run_check(text)
    elif state.get("step") == "manual" and text:
        handle_manual(text)


# ---------- scanner thread ----------
def scanner_loop():
    while not em.STOP.is_set():
        try:
            cands, note = ta.scan_candidates()
            sent = 0
            for sym, sig in cands.items():
                if time.time() - sc.last_alert.get(sym, 0) < sc.COOLDOWN_SECONDS:
                    continue
                try:
                    plan = ta.build_plan(sig)
                except ValueError as e:
                    sc.log.info("%s skipped: %s", sym, safety.redact(str(e)))
                    continue
                sc.last_alert[sym] = time.time()
                send_proposal(sig, plan, note)
                sent += 1
            sc.log.info(f"Scan done: {len(cands)} candidate(s), {sent} proposal(s) sent. {note}")
        except Exception as e:
            sc.log.warning(f"Scan error: {safe(e)}")
        em.STOP.wait(900 - (time.time() % 900) + 10)


# ---------- main ----------
def _run_main():
    if not TOKEN or not CHAT_ID:
        raise SystemExit("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
    unresolved = em.recover_trades()
    for record in unresolved:
        if record["state"] != "closed":
            worker = threading.Thread(target=em.monitor_trade, args=(record,), daemon=False)
            workers.add(worker)
            worker.start()
    print("=" * 60)
    print(f" Telegram Trade Assistant - {mode_label()}")
    print(
        f" Auto risk {sc.RISK_PCT}% | auto daily loss limit ${em.MAX_DAILY_LOSS_USD} | "
        f"auto minimum volume {sc.MIN_QUOTE_VOLUME:,.0f} USDT"
    )
    print(
        " Manual override: liquidity/strategy, 1% sizing and daily-loss gate bypassed after approval"
    )
    print(
        f" All entries: max ${ta.MAX_POSITION_USDT} per trade | max {ta.MAX_OPEN_TRADES} active/unresolved"
    )
    print(
        f" Auto proposals: {'ON' if AUTO_SCAN else 'OFF'} | Send /check in Telegram to check any coin"
    )
    print(" Ctrl+C to stop (open orders stay active on Binance).")
    print("=" * 60)

    # ignore any old taps that piled up while the program was off
    offset = 0
    r = tg("getUpdates", timeout=0)
    if not r.get("ok"):
        raise SystemExit(
            f"Telegram error: {r.get('description')} "
            f"(409 = another program is reading this bot; 401 = wrong token)"
        )
    if r.get("result"):
        offset = r["result"][-1]["update_id"] + 1

    say(
        f"Assistant started [{mode_label()}]. "
        f"{'Scanning every 15 min. ' if AUTO_SCAN else ''}Send /check to check a coin, /help for commands."
    )
    if AUTO_SCAN:
        threading.Thread(target=scanner_loop, daemon=True).start()

    while True:
        try:
            r = tg(
                "getUpdates",
                timeout=30,
                offset=offset,
                allowed_updates=json.dumps(["callback_query", "message"]),
            )
            if not r.get("ok"):
                sc.log.warning(f"Telegram: {r.get('description')}")
                time.sleep(10)
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                if "callback_query" in u:
                    handle_callback(u["callback_query"])
                elif "message" in u:
                    handle_message(u["message"])
            expire_pending()
        except KeyboardInterrupt:
            print("\nStopped. Any open orders stay active on Binance.")
            break
        except Exception as e:
            sc.log.warning(f"Loop error: {safe(e)}")
            time.sleep(5)


def main():
    if not TOKEN or not CHAT_ID:
        raise SystemExit("Telegram configuration missing")
    em.initialize()
    em.STOP.clear()
    lock = em.instance_lock()
    try:
        _run_main()
    finally:
        em.STOP.set()
        for timer in recheck_timers:
            timer.cancel()
        # Hold the process/account lock until in-flight durable transitions finish.
        for worker in list(workers):
            worker.join()
        workers.clear()
        lock.unlink()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(safety.safe_error(error))
        raise SystemExit(1) from None
