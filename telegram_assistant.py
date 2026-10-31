"""
NEXVANT: Telegram scanner proposals + on-demand /check + manual entry
-------------------------------------------------------------------------------
A) AUTOMATIC: scans Binance every 15 min and sends proposals with [Approve] [Reject] buttons.
B) ON DEMAND: send /check (or /check_ticker or /check-ticker) -> it asks for the pair
   (e.g. BTC/USDT) -> it checks the coin and replies with a status:
       SETUP FOUND            -> buttons: 1 Review live plan | 3 Trading options | Cancel
       WARNING - WAIT 15 MIN  -> buttons: 2 Recheck in 15 min | 3 Manual | Cancel
   1 -> shows Entry / SL / TP1 / TP2 / size / $ at risk with [Approve] [Reject]
   2 -> rechecks after 15 minutes and sends you the updated status
   3 -> Manual (ENTRY STOP TP1 TP2), Assisted (bot entry/stop, user targets),
        or Auto Setup (all algorithmic levels); every path requires final approval.
   Approve -> live price re-check -> price-capped IOC buy -> 2 OCO exits (stop + targets), original stops retained after TP1.

Safeguards (apply to ALL paths, including manual):
  - Only taps/messages from YOUR chat ID (TELEGRAM_CHAT_ID) are accepted
  - Proposals expire after 5 minutes (APPROVAL_TTL_SECONDS); /check cards after 10 (CHECK_TTL_SECONDS)
  - Automatic plans retain 1% risk, 3 USDT daily budget and strategy filters.
  - Manual override exceptions are explicit; configured entry caps and exchange rules remain.
  - Enter now freezes the approved ask-based cap; Wait watches locally for up to 15 minutes.
  - Watch restart/read failure cancels authorization; submitted buys always retain recovery.
  - If protection fails, confirmed bot-owned free inventory is reconciled before an exit attempt

Other commands: /discover  /status  /cancel  /help
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

import algorithmic_planner as algo
import entry_scanner as sc
import exit_manager as em
import market_discovery as discovery
import position_manager as positions
import safety
import shutdown_control
import telegram_presentation as presentation
import trade_assistant as ta
import trade_reporting
import trading_settings as settings
from branding import BOT_NAME, BRANDING, WELCOME_IMAGE
from telegram_formatting import spot_url, ticker_link_options

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
APPROVAL_TTL = int(os.getenv("APPROVAL_TTL_SECONDS", "300"))
CHECK_TTL = int(os.getenv("CHECK_TTL_SECONDS", "600"))
AUTO_SCAN = os.getenv("AUTO_SCAN", "true").lower() == "true"
SCAN_CANDIDATE_NOTIFICATIONS = (
    os.getenv("SCAN_CANDIDATE_NOTIFICATIONS", "true").strip().lower() == "true"
)
API = f"https://api.telegram.org/bot{TOKEN}"

last_preview = {}  # read-only snapshot for SHOW PR; never an approval source
pending = {}  # proposal id -> dict
checks = {}  # /check card id -> dict
discovery_views = {}  # ephemeral read-only discovery menus/results
setup_views = {}  # owner-only setting changes awaiting confirmation
position_views = {}  # immutable record IDs; no number ever resolves a fresh ticker list
state = {}  # conversation state for /check and manual entry
recheck_timers = set()
proposal_generation = 0
workers = set()
pend_lock = threading.Lock()
trade_lock = threading.Lock()  # serialize entry submission; monitor positions independently

HELP = (
    f"{BOT_NAME} | Commands\n\n"
    "/start — open your dashboard\n"
    "/help — show this guide\n"
    "/discover — browse High Volume / Top Gainers; New Listings needs a verified source\n"
    "/discover volume or /discover gainers — open a ranking directly\n"
    "/check or /scan — choose a coin\n"
    "/check ZRO or /scan ZRO — USDT is added automatically\n"
    "Full pairs such as ZROUSDT or ZRO/USDT also work.\n"
    "/check_ticker or /check-ticker — aliases for /check\n"
    "/status — balance, bot positions, stop coverage and entry watches\n"
    "/position or /positions — numbered bot positions, details and confirmed exit\n"
    "/setup — change maximum active trades and per-trade investment cap\n"
    "/pnl — today's completed bot-trade profit/loss (Dubai time)\n"
    "/showpr or SHOW PR — latest saved proposal/candidate; view only\n"
    "/cancel — cancel pending proposals, forms, rechecks and untriggered watches. "
    "Does NOT close a position or cancel its protective OCOs.\n\n"
    "Use /position → Exit Trade → Confirm Exit to close a filled trade. "
    "/cancel cannot undo an accepted exit request.\n\n"
    "After /check:\n"
    "1 — scanner plan; 2 — recheck in 15 min; 3 — trading options\n"
    "Manual — type ENTRY STOP TP1 TP2 (explicit risk override)\n"
    "Assisted — bot suggests entry/SL; type TP1 TP2\n"
    "Auto Setup — bot calculates all levels; you still approve\n"
    "Algorithm modes enforce normal risk limits and may skip unsuitable setups.\n"
    "BTC weakness is advisory; the coin's own trend/setup checks still apply.\n"
    "STOP (or EXIT) means the stop-loss trigger, not a guaranteed execution price.\n\n"
    "Proposal buttons: Details (full explanation), Approve or Reject. "
    "Buying always needs approval; configured investment/active-trade limits apply (/setup)."
)


def safe(e):
    return safety.safe_error(e)


def tg(method, *, files=None, _request_timeout=45, **params):
    try:
        options = {}
        if files is not None:
            options["files"] = files
        r = requests.post(f"{API}/{method}", data=params, timeout=_request_timeout, **options)
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
    if "entities" not in extra and "parse_mode" not in extra:
        extra = {**ticker_link_options(text), **extra}
    return tg("sendMessage", chat_id=CHAT_ID, text=text, **extra)


def send_welcome(*, startup=False):
    text = startup_text() if startup else presentation.welcome_text()
    markup = json.dumps(presentation.home_keyboard())
    if len(text.encode("utf-16-le")) // 2 <= 1024:
        try:
            with WELCOME_IMAGE.open("rb") as photo:
                result = tg(
                    "sendPhoto",
                    chat_id=CHAT_ID,
                    caption=text,
                    reply_markup=markup,
                    files={"photo": (WELCOME_IMAGE.name, photo, "image/png")},
                )
            if result and result.get("ok"):
                return result
        except OSError:
            result = None
    return say(text, reply_markup=markup)


def configure_telegram_ui():
    try:
        failures = presentation.configure(
            tg,
            chat_id=CHAT_ID,
            token=TOKEN,
            cache_path=em.TRADES_DIR.parent / "telegram_ui.json",
            atomic_write=em._atomic_write,
            stopped=em.STOP.is_set,
        )
        if failures:
            sc.log.warning(
                "%s Telegram appearance pending: %s. Commands remain available through /help.",
                BOT_NAME,
                ", ".join(failures),
            )
    except Exception as error:
        sc.log.warning("%s Telegram appearance unavailable: %s", BOT_NAME, safe(error))


def edit(item, status):
    text = item["text"] + "\n\n" + status
    tg(
        "editMessageText",
        chat_id=CHAT_ID,
        message_id=item["mid"],
        text=text,
        reply_markup=json.dumps({"inline_keyboard": []}),
        **ticker_link_options(text),
    )


def clear_callback_keyboard(cb):
    mid = cb.get("message", {}).get("message_id")
    if type(mid) is int:
        tg(
            "editMessageReplyMarkup",
            chat_id=CHAT_ID,
            message_id=mid,
            reply_markup=json.dumps({"inline_keyboard": []}),
        )


def finish_mode_menu(cid):
    item = checks.pop(cid, None)
    if item:
        edit(item, "Proposal sent below. Use Approve or Reject on that proposal.")


def mode_label():
    return "Live Spot" if ta.LIVE else "Spot Testnet"


# ---------- proposals (Approve / Reject) ----------
def proposal_text(sig, p, note):
    """Compact approval card; full assumptions remain available through Details."""
    fee = em.D(p["fee_rate"])
    # Conservative scenario: sell only the fee-reserved two exit quantities,
    # reserve full sell commission, subtract the entire approved entry cost.
    proceeds = (em.D(p["half"]) * em.D(p["t1"]) + em.D(p["rest"]) * em.D(p["t2"])) * (1 - fee)
    profit = proceeds - em.D(p["cost"])
    mode = p.get("entry_mode", "legacy")
    if p.get("algorithmic"):
        kind = {"assisted": "Assisted", "semi": "Semi-automatic", "full": "Auto Setup"}[
            p["algorithm_mode"]
        ]
    else:
        kind = "Manual" if p.get("manual_override") is True else "Automated"
    timing = {
        "now": "Enter now",
        "watch": "Wait at my price (15 min)",
        "legacy": "Immediate attempt",
    }[mode]
    lines = [
        "TRADE PROPOSAL",
        f"🔵 Ticker: {p['symbol']}",
        f"Type: {kind} | {timing}",
        "🟡 Status: Awaiting approval",
        "",
        f"Entry (maximum buy price): {sc.fmt(p['price'])}",
        f"SL: {sc.fmt(p['stop'])} (-{p['risk_pct']:.1f}%)",
        f"TP1: {sc.fmt(p['t1'])} — half",
        f"TP2: {sc.fmt(p['t2'])} — remainder",
        f"Size: {em.fmt(p['qty'])} | ~USD {p['cost']:.2f} incl. fee reserve",
        "",
        f"Est. profit if both targets fill: USD {profit:+.2f}",
        f"Est. loss at initial stop: USD {p['loss']:.2f}",
        "Estimates include reserved fees; fills/slippage can change results.",
        "After TP1, the original stop stays; no automatic breakeven.",
    ]
    if mode == "watch":
        lines.append("Watch cancels on observed stop breach or interruption; gaps remain possible.")
    if p.get("manual_override") is True:
        lines += [
            "MANUAL RISK OVERRIDE: liquidity/strategy checks, 1% sizing and the 3 USDT daily-loss gate are bypassed.",
            f"{p['max_position_usdt']} USDT cap, max {p['max_open_trades']} active trades and OCO protection remain mandatory.",
        ]
        count = len(p.get("override_warnings", []))
        if count:
            lines.append(f"Exceptions: {count} checks overridden — see Details.")
    if p.get("algorithmic"):
        lines.append("Data-based suggestion; normal risk limits apply. No guaranteed outcome.")
        if p.get("market_warning"):
            lines.append(p["market_warning"])
    if "WARNING" in note.upper() or "downtrend" in note.lower():
        lines.append("Scanner warning — see Details.")
    lines += [f"Expires in {APPROVAL_TTL // 60} min. Details below."]
    return "\n".join(lines)


def proposal_details_text(sig, p, note):
    lines = [
        "TRADE PROPOSAL",
        f"🔵 {p['symbol']}  |  {sig['setup']}",
        "🟡 Proposal status: AWAITING APPROVAL — no order placed",
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
    mode = p.get("entry_mode", "legacy")
    if mode == "now":
        lines += [
            f"ENTER NOW: maximum buy price {em.fmt(p['price'])} (ask + 0.2%, tick-rounded).",
            "One IOC attempt; no fill means no retry. Price above cap needs a new proposal.",
        ]
    elif mode == "watch":
        lines += [
            f"WAIT AT MY PRICE: maximum buy {em.fmt(p['price'])}; watch lasts 15 minutes after approval.",
            "No resting exchange buy. Stop observed before entry cancels the watch.",
            "Restart or quote-read failure cancels the watch; approve again. Gaps may still occur between checking and execution.",
            "/cancel stops an untriggered watch. One IOC attempt only.",
        ]
    if p.get("algorithmic"):
        lines += [
            "Algorithm: " + p["algorithm_version"],
            p["analysis_summary"],
            "Closed candles only; setup and risk rechecked before submission. Approved levels stay fixed.",
        ]
        if p.get("market_warning"):
            lines.append(p["market_warning"])
    if note:
        lines.append(note)
    if p.get("bnb_discount"):
        reduction = (1 - em.D(p["bnb_discount_multiplier"])) * 100
        lines += [
            f"BNB standard-fee reduction reported by Binance: {reduction:.2f}% (subject to available BNB).",
            "Tax/special fees are not discounted. Sizing reserves full fees for fallback; actual fill fees determine P&L.",
        ]
    if p.get("manual_override") is True:
        lines += [
            "",
            "MANUAL RISK OVERRIDE — approving accepts the risks below.",
            "Volume, spread/depth, trend/setup, reward/risk, stop-distance, 1% sizing and the 3 USDT daily-loss gate are bypassed.",
            f"{p['max_position_usdt']} USDT cap, max {p['max_open_trades']} active trades, exchange rules and OCO protection remain mandatory.",
            "Estimated stop loss includes reserved fees, but slippage can make the actual loss larger.",
        ]
        lines += [f"OVERRIDDEN: {warning}" for warning in p.get("override_warnings", [])]
    lines += [
        "",
        f"Expires in {APPROVAL_TTL // 60} min. Approve only after checking the chart.",
    ]
    return "\n".join(lines)


def send_proposal(sig, plan, note):
    if int(plan.get("settings_revision", 0)) != settings.read()["revision"]:
        return False
    with pend_lock:
        generation = proposal_generation
    pid = uuid.uuid4().hex
    text = proposal_text(sig, plan, note)
    details = proposal_details_text(sig, plan, note)
    kb = {
        "inline_keyboard": [
            [
                {"text": f"Approve (${plan['cost']:.2f})", "callback_data": f"A:{pid}"},
                {"text": "Reject", "callback_data": f"R:{pid}"},
            ],
            [{"text": "Details", "callback_data": f"D:{pid}"}],
        ]
    }
    r = say(text, reply_markup=json.dumps(kb))
    if not r.get("ok"):
        sc.log.warning(f"Could not send proposal: {r.get('description')}")
        return False
    with pend_lock:
        if generation != proposal_generation:
            edit(
                {"mid": r["result"]["message_id"], "text": text},
                "CANCELLED. No approval available.",
            )
            return False
        pending[pid] = dict(
            sig=sig,
            plan=plan,
            created=time.time(),
            mid=r["result"]["message_id"],
            text=text,
            details=details,
        )
        last_preview.update(text=text, saved_at=time.time())
    return True


def show_last_proposal():
    with pend_lock:
        snapshot = dict(last_preview)
    if not snapshot:
        say(
            "No proposal or candidate saved since this bot started. Use /check COIN or wait for a scan."
        )
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(snapshot["saved_at"]))
    # Remove the original relative expiry, which would be misleading on an old snapshot.
    body = "\n".join(
        line for line in snapshot["text"].splitlines() if not line.startswith("Expires in ")
    )
    text = (
        f"LATEST PROPOSAL / CANDIDATE — VIEW ONLY\nSaved {stamp}\n"
        "Historical snapshot; prices may be stale. No order or approval is created.\n\n"
        + body
        + "\n\nUse /check COIN for a fresh proposal. Active-trade limits still apply."
    )
    for start in range(0, len(text), 3500):
        say(text[start : start + 3500])


def expire_pending():
    now = time.time()
    with pend_lock:
        dead = [k for k, v in pending.items() if now - v["created"] > APPROVAL_TTL]
        items = [pending.pop(k) for k in dead]
    for item in items:
        edit(item, "EXPIRED. No order placed.")
    if state and now - state.get("t", now) > 600:
        state.clear()
    for pid, view in list(position_views.items()):
        if now >= view["expires_at"]:
            position_views.pop(pid, None)
            if state.get("position_view_id") == pid:
                state.clear()
            edit(view, "EXPIRED. Open /position again.")
    for sid, view in list(setup_views.items()):
        if now - view["created"] > CHECK_TTL:
            setup_views.pop(sid, None)
            if state.get("setup_id") == sid:
                state.clear()
            edit(view, "EXPIRED. Send /setup again.")
    for vid, view in list(discovery_views.items()):
        if now - view["created"] > CHECK_TTL:
            discovery_views.pop(vid, None)
            edit(view, "EXPIRED. Send /discover again.")
    for cid, card in list(checks.items()):
        if now - card["created"] > CHECK_TTL:
            checks.pop(cid, None)
            if state.get("menu_id") == cid:
                state.clear()
            edit(card, "EXPIRED. Send /check again.")


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
        f"🔵 {title}: {pair}",
        f"Reference price {sc.fmt(res['price'])} | 24h {res['chg']:+.1f}% | volume ${res['qv'] / 1e6:.0f}M",
        "",
    ]
    if res["status"] == "SAFE":
        lines += [
            "🟡 STATUS: Setup found — execution checks pending",
            f"Why: {res['sig']['setup']}, RSI {res['sig']['rsi']:.0f}",
            "Chart conditions match. Request a live plan to check entry, risk, funds and exchange rules.",
        ]
        row = [
            {"text": "1 - Review live plan", "callback_data": f"C1:{cid}"},
            {"text": "3 - Trading options", "callback_data": f"C3:{cid}"},
        ]
    else:
        lines += ["🟡 STATUS: 2 - WARNING - WAIT 15 MINUTES", "Reasons:"] + [
            f"- {x}" for x in res["reasons"]
        ]
        row = [
            {"text": "2 - Recheck in 15 min", "callback_data": f"C2:{cid}"},
            {"text": "3 - Trading options", "callback_data": f"C3:{cid}"},
        ]
    if res["note"]:
        lines += ["", res["note"]]
    text = "\n".join(lines)
    kb = {"inline_keyboard": [row, [{"text": "Cancel", "callback_data": f"CX:{cid}"}]]}
    r = say(text, reply_markup=json.dumps(kb))
    if r.get("ok"):
        with pend_lock:
            if generation is not None and generation != proposal_generation:
                edit(
                    {"mid": r["result"]["message_id"], "text": text},
                    "CANCELLED. This selection can no longer be used.",
                )
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
        say(f"{symbol} was not found on Binance Spot. Send a ticker such as BTC or BTC/USDT.")
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
        clear_callback_keyboard(cb)
        ack("Expired. Send /check again.")
        return
    clicked_mid = cb.get("message", {}).get("message_id")
    if type(clicked_mid) is int and clicked_mid != item.get("mid"):
        clear_callback_keyboard(cb)
        ack("This menu was replaced. Use the latest menu.")
        return
    if time.time() - item["created"] > CHECK_TTL:
        checks.pop(cid, None)
        if state.get("menu_id") == cid:
            state.clear()
        ack("Expired.")
        edit(item, "EXPIRED. Send /check again.")
        return
    res = item["res"]
    if action == "CX":
        checks.pop(cid, None)
        if state.get("menu_id") == cid:
            state.clear()
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
    elif action in ("CN", "CW", "CQ"):
        try:
            ta.ensure_free_trade_slot(res["symbol"])
        except ta.ActiveTradeError as error:
            ack("An existing trade blocks this entry.")
            say("NOT AVAILABLE: " + safety.redact(str(error)))
            return
        ack("Review the calculated proposal before approving.")
        state.clear()
        if action == "CQ":
            if prepare_algorithm_proposal(res["symbol"]):
                finish_mode_menu(cid)
        elif action == "CW":
            try:
                suggestion = algo.propose(res["symbol"])
            except ValueError as error:
                say("No assisted setup: " + safety.redact(str(error)))
                return
            except Exception as error:
                say("Algorithm data unavailable: " + safe(error))
                return
            suggestion["algorithm_mode"] = "assisted"
            state.update(step="algorithm_targets", signal=suggestion, menu_id=cid, t=time.time())
            say(
                f"Assisted setup | {res['symbol'][:-4]}/USDT\n"
                f"Indicative entry cap: {sc.fmt(suggestion['entry'])}\n"
                f"Stop loss: {sc.fmt(suggestion['stop'])}\n\n"
                "Send TWO profit-target prices: TP1 TP2.\n"
                "Targets must satisfy risk/reward and resistance checks. "
                "The final entry quote may update before you approve. /cancel to stop."
                + (
                    "\n\n" + suggestion["market_warning"]
                    if suggestion.get("market_warning")
                    else ""
                )
            )
        else:
            state.update(
                step="manual",
                symbol=res["symbol"],
                status=res["status"],
                entry_mode="watch",
                menu_id=cid,
                t=time.time(),
            )
            say(manual_levels_prompt(res, "watch"))
    elif action == "C3":
        ack("Choose a trading mode.")
        edit(
            item,
            "Choose manual or algorithm-generated levels. Final approval is always required.",
        )
        show_trading_options(res, cid=cid, item=item)


def show_trading_options(res, *, cid=None, item=None, generation=None):
    if generation is None:
        generation = proposal_generation
    if generation != proposal_generation:
        return None
    limits = settings.read()
    cid = cid or uuid.uuid4().hex
    item = item or {"res": res, "created": time.time()}
    state.clear()
    state.update(
        step="manual",
        symbol=res["symbol"],
        status=res["status"],
        entry_mode="watch",
        menu_id=cid,
        t=time.time(),
    )
    text = (
        f"Trading options | {res['symbol'][:-4]}/USDT\n"
        f"Reference price: {sc.fmt(res['price'])}\n\n"
        "Manual — you choose Entry, SL, TP1 and TP2.\n"
        "Assisted — the bot suggests entry/SL; you choose TP1/TP2.\n"
        "Auto Setup — the bot calculates all levels for your approval.\n\n"
        "Algorithm modes enforce normal liquidity, 1% risk and 3 USDT daily-loss limits. "
        "BTC trend is advisory; a weak/downward trend shows a warning. "
        "Manual mode retains its explicit risk override.\n"
        f"All modes: {limits['max_position_usdt']} USDT cap, max {limits['max_open_trades']} active trades, protective OCOs and final approval.\n"
        "/cancel to stop."
    )
    result = say(
        text,
        reply_markup=json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "Manual — set all prices", "callback_data": f"CN:{cid}"}],
                    [
                        {
                            "text": "Assisted — choose profit targets",
                            "callback_data": f"CW:{cid}",
                        }
                    ],
                    [
                        {
                            "text": "Auto Setup — calculate all levels",
                            "callback_data": f"CQ:{cid}",
                        }
                    ],
                    [{"text": "Cancel", "callback_data": f"CX:{cid}"}],
                ]
            }
        ),
    )

    if result and result.get("ok"):
        view = {**item, "mid": result["result"]["message_id"], "text": text}
        with pend_lock:
            if generation != proposal_generation:
                if state.get("menu_id") == cid:
                    state.clear()
                edit(view, "CANCELLED. This selection can no longer be used.")
                return None
            checks[cid] = view
        return cid
    checks.pop(cid, None)
    if state.get("menu_id") == cid:
        state.clear()
    return None


def prepare_algorithm_proposal(symbol, entry=None):
    try:
        ta.ensure_free_trade_slot(symbol)
        sig = algo.propose(symbol, entry=entry)
        plan = ta.build_plan(sig)
    except ValueError as error:
        say("No algorithm proposal: " + safety.redact(str(error)))
        return False
    except Exception as error:
        say("Algorithm data unavailable: " + safe(error) + ". No order submitted.")
        return False
    return send_proposal(
        sig, plan, "Algorithm-calculated levels. Review the chart and Details before approving."
    )


def handle_algorithm_targets(text):
    try:
        values = text.replace(",", " ").split()
        if len(values) != 2:
            raise ValueError("Two prices required")
        t1, t2 = (em.D(value) for value in values)
        suggestion = dict(state["signal"])
        if not em.D(suggestion["entry"]) < t1 < t2:
            raise ValueError("Entry < TP1 < TP2 required")
    except ValueError:
        say("Send TWO prices above entry, with TP1 below TP2. /cancel to stop.")
        return
    suggestion.update(t1=t1, t2=t2)
    suggestion["analysis_summary"] += (
        " User-selected targets; normal algorithm validation still applies."
    )
    menu_id = state.get("menu_id")
    state.clear()
    try:
        plan = ta.build_plan(suggestion)
    except ValueError as error:
        say("No assisted proposal: " + safety.redact(str(error)))
        return
    except Exception as error:
        say("Algorithm data unavailable: " + safe(error))
        return
    if send_proposal(
        suggestion, plan, "Bot-calculated entry/stop; your profit targets. Review before approval."
    ):
        finish_mode_menu(menu_id)


def manual_levels_prompt(res, mode):
    price = em.D(res["price"])
    stops = [price * em.D(multiplier) for multiplier in (".97", "1.04", "1.07")]
    watch = mode == "watch"
    values = [price, *stops] if watch else stops
    return (
        f"{'Manual prices' if watch else 'Manual — current price'} | {res['symbol'][:-4]}/USDT\n\n"
        f"Send {'4' if watch else '3'} prices in ONE message:\n"
        + ("ENTRY STOP TP1 TP2\n" if watch else "STOP TP1 TP2\n")
        + ("ENTRY = your maximum buy price\n" if watch else "")
        + "STOP = stop-loss trigger price\nTP1 / TP2 = first / second profit targets\n\n"
        + "Example format only: "
        + " ".join(sc.fmt(value) for value in values)
        + "\n\n"
        + (
            "After approval, wait up to 15 minutes for your price."
            if watch
            else "A fresh entry quote and maximum buy price appear in the final proposal."
        )
        + "\nReview and approve before buying. /cancel to stop."
    )


def handle_manual(text):
    mode = state.get("entry_mode", "watch")
    count = 3 if mode == "now" else 4
    try:
        nums = [float(x) for x in text.replace(",", " ").split()]
        if len(nums) != count or not all(math.isfinite(x) for x in nums):
            raise ValueError
    except ValueError:
        say(
            "Please send "
            + ("3 numbers: STOP TP1 TP2" if count == 3 else "4 numbers: ENTRY STOP TP1 TP2")
            + " (or /cancel)."
        )
        return
    if mode == "now":
        try:
            _, ask = ta.entry_quote(state["symbol"])
        except Exception as error:
            say(f"Cannot read entry quote: {safe(error)}")
            return
        entry, stop, t1, t2 = float(ask), *nums
    else:
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
        entry_mode=mode,
        manual_override=True,
        rsi=0,
        chg24=0,
    )
    menu_id = state.get("menu_id")
    state.clear()
    try:
        plan = ta.build_plan(sig)
    except ValueError as e:
        say(f"Cannot use these levels: {safety.redact(str(e))}")
        return
    except Exception as e:
        say(f"Error while building the plan: {safe(e)}")
        return
    if send_proposal(
        sig,
        plan,
        f"MANUAL entry. Scanner status was: {status}. The levels are yours, not the scanner's.",
    ):
        finish_mode_menu(menu_id)


# ---------- execution ----------
def run_trade(plan, approval):
    rec = None
    try:
        try:
            rec = ta.execute(plan, approval=approval, monitor=False)
        except ta.PlanChangedError:
            em.alert(
                "NOT SUBMITTED: market/risk checks or settings changed after approval. "
                "No buy was submitted. Request and approve a fresh proposal."
            )
        except BinanceAPIException as e:
            em.alert(safe(e))
        except Exception as e:
            em.alert(f"Error: {safe(e)}. Check Open Orders and Holdings on Binance now.")
        finally:
            trade_lock.release()  # Submission is serialized; position monitoring is separate.
        if rec is not None:
            em.monitor_trade(rec)
    finally:
        workers.discard(threading.current_thread())


def handle_callback(cb):
    sender = cb.get("from", {}).get("id")
    chat = cb.get("message", {}).get("chat", {}).get("id")
    if str(sender) != str(CHAT_ID) or str(chat) != str(CHAT_ID):
        return  # ignore strangers completely
    action, _, pid = cb.get("data", "").partition(":")
    if action == "H":
        command = presentation.HOME_ACTIONS.get(pid)
        tg(
            "answerCallbackQuery",
            callback_query_id=cb["id"],
            text="Opening your dashboard selection..." if command else "Unknown menu option.",
        )
        if command:
            handle_message({"from": cb["from"], "chat": cb["message"]["chat"], "text": command})
        return
    if action.startswith("P"):
        handle_position_callback(cb, action, pid)
        return
    if action.startswith("S"):
        handle_setup_callback(cb, action, pid)
        return
    if action.startswith("V"):
        handle_discovery_callback(cb, action, pid)
        return
    if action.startswith("C"):
        handle_check_callback(cb, action, pid)
        return
    if action == "D":
        with pend_lock:
            item = pending.get(pid)
            details = item.get("details", item["text"]) if item else None
            expired = item is None or time.time() - item["created"] > APPROVAL_TTL
        tg(
            "answerCallbackQuery",
            callback_query_id=cb["id"],
            text="Expired or already used." if expired else "Proposal details",
        )
        if expired:
            clear_callback_keyboard(cb)
        if not expired:
            text = "DETAILS — VIEW ONLY; approve only on the original proposal.\n\n" + details
            for start in range(0, len(text), 3500):
                say(text[start : start + 3500])
        return
    with pend_lock:
        item = pending.pop(pid, None)
    if not item:
        clear_callback_keyboard(cb)
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
        edit(
            item, "NOT PLACED: another entry is being submitted. Request a fresh proposal shortly."
        )
        return
    try:
        old = item["plan"]
        fresh = (
            ta.build_plan(item["sig"], approved_bounds=old)
            if old.get("entry_mode", "legacy") != "legacy"
            else ta.build_plan(item["sig"])
        )
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

    edit(
        item,
        "APPROVAL RECEIVED. Final checks are running; execution is not yet confirmed. "
        "Wait for the entry-result and protection notifications.",
    )
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
    command = low.split(None, 1)[0].split("@", 1)[0] if low else ""
    if " ".join(low.split()) in ("show pr", "/showpr"):
        show_last_proposal()
    elif low.startswith("/status"):
        try:
            say(f"{em.status_report()}\nPending proposals: {len(pending)}")
        except Exception as e:
            say(f"Status error: {safe(e)}")
    elif command == "/start":
        state.clear()
        send_welcome()
    elif command == "/help":
        say(HELP)
    elif low.startswith("/cancel"):
        state.clear()
        with pend_lock:
            proposal_generation += 1
            canceled_checks = (
                list(checks.values())
                + list(discovery_views.values())
                + list(setup_views.values())
                + list(position_views.values())
            )
            checks.clear()
            discovery_views.clear()
            setup_views.clear()
            position_views.clear()
            canceled = list(pending.values())
            pending.clear()
        for card in canceled_checks:
            edit(card, "CANCELLED. This selection can no longer be used.")
        for item in canceled:
            edit(item, "CANCELLED. This proposal can no longer be approved.")
        for timer in list(recheck_timers):
            timer.cancel()
        recheck_timers.clear()
        try:
            watches = ta.cancel_entry_watches()
        except Exception as error:
            say(f"Watch cancellation could not be confirmed: {safe(error)}. Inspect /status.")
            watches = 0
        say(
            f"Cancelled {watches} untriggered entry watch(es), pending proposals, manual entry and scheduled rechecks. "
            "Already submitted orders and protective OCOs remain active; /status shows them."
        )
    elif command in ("/position", "/positions"):
        parts = text.split(None, 1)
        if len(parts) > 1 and state.get("step") == "position_number":
            handle_position_number(parts[1])
        elif len(parts) > 1:
            say("Send /position first, then choose a number from that list.")
        else:
            state.clear()
            try:
                show_position_list()
            except Exception as error:
                say("Positions unavailable: " + safe(error) + ". Use /status to inspect recovery.")
    elif command == "/setup":
        state.clear()
        try:
            send_setup_menu()
        except Exception as error:
            say("Setup unavailable: " + safe(error) + ". No change confirmed.")
    elif command == "/pnl":
        try:
            say(trade_reporting.todays_pnl())
        except Exception as error:
            say("P&L unavailable: verified trade/fee records could not be read. " + safe(error))
    elif command == "/discover":
        state.clear()
        parts = text.split(None, 1)
        if len(parts) == 1:
            send_discovery_menu()
        elif parts[1].lower() in ("volume", "gainers"):
            load_discovery_ranking(parts[1].lower(), generation=proposal_generation)
        elif parts[1].lower() in ("new", "listings"):
            explain_new_listings()
        else:
            say("Use /discover, /discover volume or /discover gainers.")
    elif command in ("/check", "/check_ticker", "/scan"):
        parts = text.split(None, 1)
        state.clear()
        if len(parts) > 1:
            run_check(parts[1])
        else:
            state.update(step="ticker", t=time.time())
            say("Which coin? Send just the ticker, for example ZRO. USDT is added automatically.")
    elif state.get("step") == "position_number" and text:
        handle_position_number(text)
    elif state.get("step") == "setup_value" and text:
        handle_setup_value(text)
    elif state.get("step") == "ticker" and text:
        state.clear()
        run_check(text)
    elif state.get("step") == "algorithm_targets" and text:
        handle_algorithm_targets(text)
    elif state.get("step") == "manual" and text:
        handle_manual(text)


# ---------- bot positions and confirmed closes ----------
def register_position_view(text, buttons, *, kind, context, generation, **extra):
    if generation != proposal_generation:
        return None
    pid = uuid.uuid4().hex
    keyboard = [
        [
            {**button, "callback_data": f"{button['action']}:{pid}" + button.get("suffix", "")}
            for button in row
        ]
        for row in buttons
    ]
    for row in keyboard:
        for button in row:
            button.pop("action", None)
            button.pop("suffix", None)
    result = say(text, reply_markup=json.dumps({"inline_keyboard": keyboard}))
    if not result or not result.get("ok"):
        return None
    now = time.time()
    item = {
        "mid": result["result"]["message_id"],
        "text": text,
        "created": now,
        "expires_at": now + (APPROVAL_TTL if kind == "confirm" else CHECK_TTL),
        "kind": kind,
        "context": context,
        "generation": generation,
        **extra,
    }
    with pend_lock:
        if generation != proposal_generation:
            edit(item, "CANCELLED. Open /position again.")
            return None
        position_views[pid] = item
    return pid


def show_position_list(*, rows=None, context=None, page=0, generation=None):
    generation = proposal_generation if generation is None else generation
    if generation != proposal_generation:
        return None
    if rows is None:
        listing = positions.list_positions()
        rows, context = listing["rows"], listing["context"]
    if not rows:
        say("No active bot positions or entry watches. Use /check COIN to review a new setup.")
        return None
    start, end = page * 10, min((page + 1) * 10, len(rows))
    text = ["Bot positions — latest saved state", ""]
    buttons = []
    for index in range(start, end):
        row = rows[index]
        text.append(f"{index + 1}. {row['symbol']} — {row['state']}")
        buttons.append(
            [{"text": f"{index + 1}. {row['symbol']}", "action": "PS", "suffix": f":{index}"}]
        )
    pages = []
    if page:
        pages.append({"text": "Previous", "action": "PN", "suffix": f":{page - 1}"})
    if end < len(rows):
        pages.append({"text": "Next", "action": "PN", "suffix": f":{page + 1}"})
    if pages:
        buttons.append(pages)
    buttons += [[{"text": "Refresh list", "action": "PR"}, {"text": "Cancel", "action": "PX"}]]
    text += [
        "",
        f"Showing {start + 1}–{end} of {len(rows)}.",
        "Tap a position or send its number for current details. This list contains bot trades only.",
    ]
    pid = register_position_view(
        "\n".join(text), buttons, kind="list", context=context, generation=generation, rows=rows
    )
    if pid:
        state.clear()
        state.update(step="position_number", position_view_id=pid, t=time.time())
    return pid


def show_position_details(trade_id, context, *, generation):
    if generation != proposal_generation:
        return None
    view = positions.snapshot(trade_id, context)
    buttons = []
    if view["action"]:
        buttons.append(
            [
                {
                    "text": "Cancel Entry Watch"
                    if view["action"] == "cancel_watch"
                    else "Exit Trade",
                    "action": "PE",
                }
            ]
        )
    buttons += [
        [{"text": "Refresh", "action": "PR"}, {"text": "Back to positions", "action": "PB"}],
        [{"text": "Cancel", "action": "PX"}],
    ]
    return register_position_view(
        positions.details_text(view),
        buttons,
        kind="detail",
        context=context,
        generation=generation,
        trade_id=trade_id,
        symbol=view["record"]["symbol"],
    )


def consume_position_view(pid, item, status):
    with pend_lock:
        if position_views.pop(pid, None) is not item:
            return False
    if state.get("position_view_id") == pid:
        state.clear()
    edit(item, status)
    return True


def position_view_valid(item):
    return item["generation"] == proposal_generation and time.time() < item["expires_at"]


def handle_position_number(text):
    pid = state.get("position_view_id")
    item = position_views.get(pid)
    if not item or item["kind"] != "list" or not position_view_valid(item):
        state.clear()
        if item:
            consume_position_view(pid, item, "EXPIRED. Open /position again.")
        say("Position list expired or canceled. Send /position again.")
        return
    if not text.isdecimal() or not 1 <= int(text) <= len(item["rows"]):
        say(f"Choose a number from 1 to {len(item['rows'])}, or /cancel to stop.")
        return
    row = item["rows"][int(text) - 1]
    if consume_position_view(pid, item, "Opening the selected trade below."):
        try:
            show_position_details(row["id"], item["context"], generation=item["generation"])
        except Exception as error:
            say("Position details unavailable: " + safe(error) + ". Open /position again.")


def run_position_exit(rec):
    # Uses the existing locked, idempotent cancel/reconcile/sell lifecycle.
    # The close intent was already saved before this worker was launched.
    em.monitor_trade(rec)


def handle_position_callback(cb, action, payload):
    pid, _, selection = payload.partition(":")
    item = position_views.get(pid)

    def ack(text):
        tg("answerCallbackQuery", callback_query_id=cb["id"], text=text)

    if not item:
        clear_callback_keyboard(cb)
        ack("Expired or canceled. Open /position again.")
        return
    if cb.get("message", {}).get("message_id") != item["mid"]:
        clear_callback_keyboard(cb)
        ack("Use the latest position message.")
        return
    if not position_view_valid(item):
        consume_position_view(pid, item, "EXPIRED. Open /position again.")
        ack("Expired.")
        return
    allowed = {
        "list": {"PS", "PN", "PR", "PX"},
        "detail": {"PE", "PR", "PB", "PX"},
        "confirm": {"PF", "PX"},
    }
    if action not in allowed[item["kind"]]:
        ack("Use the available position buttons.")
        return
    if action in ("PS", "PN"):
        maximum = len(item["rows"]) if action == "PS" else (len(item["rows"]) + 9) // 10
        if not selection.isdecimal() or not 0 <= int(selection) < maximum:
            ack("Invalid position selection.")
            return
    if action == "PX":
        ack("Exit not requested." if item["kind"] == "confirm" else "Position menu closed.")
        consume_position_view(pid, item, "CANCELLED. No exit requested by this selection.")
        return
    if action == "PF":
        # Claim before I/O; a second tap cannot start another request.
        with pend_lock:
            if position_views.pop(pid, None) is not item:
                ack("This confirmation was already used.")
                return
        ack("Saving your exit request...")
        try:
            # Serialize cancellation with durable acceptance. No network writes
            # run under this lock: confirmed exchange handling starts afterward.
            with em.operation_lock(), pend_lock:
                if item["generation"] != proposal_generation:
                    raise ValueError("Exit selection canceled. Open /position again")
                rec = em.request_bot_trade_close(
                    item["symbol"],
                    "CLOSE_" + item["symbol"],
                    trade_id=item["trade_id"],
                    context=item["context"],
                    action=item["exit_action"],
                    expires_at=item["expires_at"],
                    actor=CHAT_ID,
                    source="telegram",
                )
        except ValueError as error:
            edit(item, "Exit request not confirmed. " + safety.redact(str(error)))
            return
        except Exception as error:
            edit(
                item,
                "Exit request not confirmed. Refresh /position before retrying. " + safe(error),
            )
            return
        if rec["state"] == "closed":
            edit(
                item,
                "Entry watch closed. No sell order was placed by this confirmation."
                if item["exit_action"] == "cancel_watch" and not rec["orders"]
                else "This trade is already closed. No additional exit requested; see /pnl for completed results.",
            )
            return
        try:
            start_managed_worker(lambda: run_position_exit(rec))
        except Exception as error:
            edit(
                item,
                "Exit request is saved. Worker could not start; existing monitoring/restart recovery will continue it. "
                + safe(error),
            )
            return
        edit(
            item,
            "EXIT REQUEST SAVED. Cancel/market-sale reconciliation was requested for this trade only. "
            "This is not a confirmed fill. Watch for TRADE CLOSED and the final net result; /position shows progress.",
        )
        return
    ack("Loading the latest position details...")
    if not consume_position_view(pid, item, "Selection opened below."):
        return
    try:
        if action == "PS":
            show_position_details(
                item["rows"][int(selection)]["id"], item["context"], generation=item["generation"]
            )
        elif action == "PN":
            show_position_list(
                rows=item["rows"],
                context=item["context"],
                page=int(selection),
                generation=item["generation"],
            )
        elif action == "PB" or (action == "PR" and item["kind"] == "list"):
            show_position_list(generation=item["generation"])
        elif action == "PR":
            show_position_details(item["trade_id"], item["context"], generation=item["generation"])
        elif action == "PE":
            view = positions.snapshot(item["trade_id"], item["context"])
            if not view["action"]:
                show_position_details(
                    item["trade_id"], item["context"], generation=item["generation"]
                )
                return
            label = (
                "Confirm Cancel Watch" if view["action"] == "cancel_watch" else "Confirm Exit Trade"
            )
            keep = "Keep Entry Watch" if view["action"] == "cancel_watch" else "Keep Position"
            register_position_view(
                positions.confirmation_text(view) + f"\n\nExpires in {APPROVAL_TTL // 60} min.",
                [[{"text": label, "action": "PF"}], [{"text": keep, "action": "PX"}]],
                kind="confirm",
                context=item["context"],
                generation=item["generation"],
                trade_id=item["trade_id"],
                symbol=view["record"]["symbol"],
                exit_action=view["action"],
            )
    except Exception as error:
        say("Position action unavailable: " + safe(error) + ". Open /position again.")


# ---------- owner setup ----------
def register_setup_view(text, buttons, *, limits, field=None, value=None):
    sid = uuid.uuid4().hex
    generation = proposal_generation
    kb = [[{"text": label, "callback_data": f"{action}:{sid}"}] for label, action in buttons]
    result = say(text, reply_markup=json.dumps({"inline_keyboard": kb}))
    if result and result.get("ok"):
        item = {
            "mid": result["result"]["message_id"],
            "text": text,
            "created": time.time(),
            "generation": generation,
            "limits": limits,
            "field": field,
            "value": value,
        }
        with pend_lock:
            if generation != proposal_generation:
                edit(item, "CANCELLED. Open /setup again.")
                return None
            setup_views[sid] = item
        return sid
    return None


def send_setup_menu():
    limits = settings.read()
    return register_setup_view(
        "Trading setup\n\n"
        f"Maximum active trades: {limits['max_open_trades']}\n"
        f"Maximum investment per trade: {limits['max_position_usdt']} USDT (including fee reserve)\n\n"
        "The investment is a cap; available funds, risk sizing and exchange rules can reduce it. "
        "Different pairs may run concurrently; duplicate pairs and unresolved positions block entry.\n"
        "Normal entries retain 1% risk sizing and the 3 USDT daily risk budget. "
        "Every buy requires Telegram approval. Choose what to change:",
        [("Change max trades", "ST"), ("Change max investment", "SI"), ("Cancel", "SX")],
        limits=limits,
    )


def handle_setup_value(text):
    sid = state.get("setup_id")
    item = setup_views.get(sid)
    if (
        not item
        or item["generation"] != proposal_generation
        or time.time() - item["created"] > CHECK_TTL
    ):
        state.clear()
        say("Setup expired. Send /setup again.")
        return
    field = state["field"]
    try:
        value = settings.parse_value(field, text)
    except ValueError as error:
        say(str(error) + " /cancel to stop.")
        return
    state.clear()
    setup_views.pop(sid, None)
    edit(item, "Value entered. Review the confirmation below.")
    limits = {**item["limits"], field: value}
    register_setup_view(
        "Confirm trading limits\n\n"
        f"Maximum active trades: {limits['max_open_trades']}\n"
        f"Investment cap per trade: {limits['max_position_usdt']} USDT including fee reserve\n"
        f"Combined allocation ceiling: {em.D(limits['max_position_usdt']) * limits['max_open_trades']:f} USDT, subject to funds/risk checks.\n\n"
        "Applies to new entries. Existing positions and protective OCOs stay in place. "
        "Pending proposals and untriggered watches will require fresh approval. "
        "Confirm to save these account settings.",
        [("Confirm change", "SA"), ("Cancel", "SX")],
        limits=item["limits"],
        field=field,
        value=value,
    )


def invalidate_for_settings_change():
    global proposal_generation
    with pend_lock:
        proposal_generation += 1
        items = [
            *pending.values(),
            *checks.values(),
            *discovery_views.values(),
            *setup_views.values(),
            *position_views.values(),
        ]
        pending.clear()
        checks.clear()
        discovery_views.clear()
        setup_views.clear()
        position_views.clear()
        state.clear()
    for item in items:
        edit(item, "SETTINGS CHANGED. Request a fresh selection/proposal before approval.")
    for timer in list(recheck_timers):
        timer.cancel()
    recheck_timers.clear()


def handle_setup_callback(cb, action, sid):
    item = setup_views.get(sid)

    def ack(text):
        tg("answerCallbackQuery", callback_query_id=cb["id"], text=text)

    if not item:
        clear_callback_keyboard(cb)
        ack("Expired or canceled. Open /setup again.")
        return
    if cb.get("message", {}).get("message_id") != item["mid"]:
        clear_callback_keyboard(cb)
        ack("Use the latest setup message.")
        return
    if item["generation"] != proposal_generation or time.time() - item["created"] > CHECK_TTL:
        setup_views.pop(sid, None)
        edit(item, "EXPIRED. Open /setup again.")
        ack("Expired.")
        return
    if action == "SX":
        setup_views.pop(sid, None)
        if state.get("setup_id") == sid:
            state.clear()
        edit(item, "CANCELLED. No setting changed.")
        ack("Cancelled.")
    elif action in ("ST", "SI") and item["field"] is None:
        field = "max_open_trades" if action == "ST" else "max_position_usdt"
        state.clear()
        state.update(step="setup_value", setup_id=sid, field=field, t=time.time())
        ack("Enter the new value in chat.")
        say(
            "Send the maximum number of active trades (whole number, 1–100). /cancel to stop."
            if action == "ST"
            else "Send the maximum investment per trade in USDT, for example 25 or 25.50. "
            "This includes the buy fee reserve. /cancel to stop."
        )
    elif action == "SA" and item["field"] is not None:
        setup_views.pop(sid, None)  # A confirmation is single use even if saving fails.
        ack("Saving settings...")
        try:
            with em.operation_lock():
                saved = settings.update(
                    item["field"],
                    item["value"],
                    expected_revision=item["limits"]["revision"],
                    actor=CHAT_ID,
                )
                if saved["revision"] != item["limits"]["revision"]:
                    canceled = ta.cancel_entry_watches()
                else:
                    canceled = 0
            if saved["revision"] != item["limits"]["revision"]:
                invalidate_for_settings_change()
        except Exception as error:
            edit(
                item,
                "Change could not be confirmed. Open /setup to check current settings. "
                + safe(error),
            )
            return
        edit(item, "SAVED. Limits apply now and remain available after restart.")
        say(
            f"Settings saved\nMaximum active trades: {saved['max_open_trades']}\n"
            f"Maximum investment per trade: {saved['max_position_usdt']} USDT\n"
            f"Untriggered watches canceled: {canceled}. Existing positions remain protected.\n"
            "Every new buy still requires approval. Use /setup to view or change limits."
        )
    else:
        ack("Choose a valid setup option.")


# ---------- read-only public discovery ----------
def discovery_buttons(vid):
    return [
        [
            {"text": "High Volume", "callback_data": f"VV:{vid}"},
            {"text": "Top Gainers", "callback_data": f"VG:{vid}"},
        ],
        [{"text": "New Listings", "callback_data": f"VN:{vid}"}],
        [{"text": "Cancel", "callback_data": f"VX:{vid}"}],
    ]


def register_discovery_view(text, rows, category, *, generation):
    if generation != proposal_generation:
        return None
    vid = uuid.uuid4().hex
    buttons = []
    for index, row in enumerate(rows):
        base = row["symbol"][:-4]
        label = base if len(base) <= 12 else base[:11] + "…"
        buttons.append(
            [
                {"text": f"{label} · Spot", "url": spot_url(row["symbol"])},
                {"text": "Check setup", "callback_data": f"VC:{vid}:{index}"},
                {"text": "Trading options", "callback_data": f"VM:{vid}:{index}"},
            ]
        )
    buttons += discovery_buttons(vid)
    result = say(text, reply_markup=json.dumps({"inline_keyboard": buttons}))
    if not result or not result.get("ok"):
        return None
    item = {
        "text": text,
        "mid": result["result"]["message_id"],
        "created": time.time(),
        "rows": rows,
        "category": category,
        "generation": generation,
    }
    with pend_lock:
        if generation != proposal_generation:
            edit(item, "CANCELLED. Send /discover again.")
            return None
        discovery_views[vid] = item
    return vid


def send_discovery_menu():
    return register_discovery_view(
        "Discover tokens\n\nChoose High Volume or Top Gainers.\n"
        "USDT Spot only · at least 10M USDT 24h volume · OCO support.\n"
        "Rankings are for research; setup and execution checks follow.\n"
        "New Listings requires a verified listing source and is not available yet.",
        [],
        "menu",
        generation=proposal_generation,
    )


def explain_new_listings():
    say(
        "New Listings is not available yet: a verified listing source is required. "
        "The Spot symbol API does not establish an official listing date, and dashboard assets "
        "may not have a tradable USDT Spot pair. Use High Volume or Top Gainers for now."
    )


def load_discovery_ranking(category, *, generation):
    try:
        ranking = discovery.rank_pairs(category)
    except ValueError as error:
        say(safety.redact(str(error)) + ". No order submitted.")
        return None
    except Exception as error:
        say("Discovery data unavailable: " + safe(error) + ". Try /discover later.")
        return None
    label = "High Volume" if category == "volume" else "Top Gainers"
    lines = [f"Discover tokens · {label}", "USDT Spot · OCO support · 24h volume ≥10M USDT", ""]
    for index, row in enumerate(ranking["rows"], 1):
        lines += [
            f"{index}. {row['symbol']} · reference price {sc.fmt(row['price'])}",
            f"24h {row['change']:+.2f}% | volume {row['quote_volume'] / 1000000:.1f}M USDT",
        ]
        lines += row["flags"]
        lines.append("")
    if not ranking["rows"]:
        lines.append("No qualifying pairs in this snapshot.")
    if ranking["invalid_count"]:
        lines.append(f"Incomplete ticker entries skipped: {ranking['invalid_count']}.")
    lines += [
        "Discovery only; no setup/risk approval. Prices can change.",
        "Use Check setup or Trading options. Every buy still requires approval.",
    ]
    return register_discovery_view(
        "\n".join(lines), ranking["rows"], category, generation=generation
    )


def handle_discovery_callback(cb, action, payload):
    vid, _, selection = payload.partition(":")
    item = discovery_views.get(vid)

    def ack(text):
        tg("answerCallbackQuery", callback_query_id=cb["id"], text=text)

    if not item:
        clear_callback_keyboard(cb)
        ack("Expired or canceled. Send /discover again.")
        return
    mid = cb.get("message", {}).get("message_id")
    if type(mid) is int and mid != item["mid"]:
        clear_callback_keyboard(cb)
        ack("Use the latest discovery menu.")
        return
    if time.time() - item["created"] > CHECK_TTL or item["generation"] != proposal_generation:
        discovery_views.pop(vid, None)
        edit(item, "EXPIRED. Send /discover again.")
        ack("Expired.")
        return
    if action == "VN":
        ack("Verified listing source required.")
        explain_new_listings()
        return
    if action in ("VC", "VM"):
        if not selection.isdecimal() or not 0 <= int(selection) < len(item["rows"]):
            ack("Invalid token selection.")
            return
        row = item["rows"][int(selection)]
    elif action not in ("VV", "VG", "VX"):
        ack("Unknown discovery action.")
        return
    discovery_views.pop(vid, None)
    if action == "VX":
        ack("Discovery closed.")
        edit(item, "CANCELLED. Discovery closed; existing trades are unaffected.")
    elif action in ("VV", "VG"):
        ack("Loading public Spot rankings...")
        edit(item, "Loading the selected ranking below.")
        load_discovery_ranking(
            "volume" if action == "VV" else "gainers", generation=item["generation"]
        )
    elif action == "VC":
        ack("Checking the latest setup...")
        edit(item, f"Checking {row['symbol']}; use the new check card below.")
        run_check(row["symbol"], generation=item["generation"])
    else:
        ack("Choose a trading mode, then review the proposal.")
        edit(item, f"Trading options for {row['symbol']} opened below.")
        res = {
            "symbol": row["symbol"],
            "price": row["price"],
            "status": "DISCOVERY",
            "sig": None,
            "reasons": [],
            "note": "Discovery ranking; chart setup not checked.",
        }
        show_trading_options(res, generation=item["generation"])


def candidate_card(symbol, sig, status, reason, note):
    marker = "🔴" if status in ("Blocked", "Not delivered / canceled") else "🟡"
    lines = [
        "Potential Token Detected",
        f"🔵 Ticker: {symbol}",
        f"{marker} Current status: {status}",
        f"Reason for rejection: {reason}",
        "",
    ]
    try:
        ta.require_finite_levels(sig)
        lines += [
            f"{label}: {sc.fmt(sig[key])}"
            for label, key in (("Entry", "entry"), ("SL", "stop"), ("TP1", "t1"), ("TP2", "t2"))
        ]
    except (ValueError, KeyError, TypeError):
        lines.append("Levels unavailable: invalid or incomplete signal.")
    lines.append("Preview only; fresh approval required to trade.")
    if note:
        lines.append(note)
    return "\n".join(lines)


def scan_once():
    cands, note = ta.scan_candidates()
    names = ", ".join(cands) or "none"
    sc.log.info("Scan candidates (%s): %s", len(cands), names)
    results, cards, sent = [], [], 0
    for sym, sig in cands.items():
        if em.STOP.is_set():
            break
        delivered = False
        status, reason = "Blocked", ""
        badge = "🔴 Proposal status: BLOCKED — no actionable approval"
        remaining = sc.COOLDOWN_SECONDS - (time.time() - sc.last_alert.get(sym, 0))
        if remaining > 0:
            result = f"{sym}: cooldown, {int(remaining)} seconds remaining"
            status, reason = "Cooldown", f"Cooldown: {int(remaining)} seconds remaining"
            badge = "🟡 Proposal status: COOLDOWN — preview only"
        else:
            try:
                plan = ta.build_plan(sig)
                if send_proposal(sig, plan, note):
                    delivered = True
                    status, reason = "Awaiting approval", "None — proposal sent separately"
                    badge = "🟡 Proposal status: AWAITING APPROVAL — no order placed"
                    sc.last_alert[sym] = time.time()
                    sent += 1
                    result = f"{sym}: proposal sent; awaiting your approval"
                else:
                    result = f"{sym}: proposal not delivered or canceled; no approval available"
                    badge = "🔴 Proposal status: NOT DELIVERED / CANCELED"
                    status, reason = (
                        "Not delivered / canceled",
                        "Proposal not delivered or canceled",
                    )
            except ValueError as error:
                reason = safety.redact(str(error))
                result = f"{sym}: blocked — {reason}"
            except Exception as error:
                reason = safe(error)
                result = f"{sym}: blocked — {reason}"
        result = f"🔵 {result}\n{badge}"
        try:
            # Display scanner levels without bypassing execution planning or creating
            # an actionable proposal. These are signal prices, not rounded order levels.
            ta.require_finite_levels(sig)
            result += (
                "\nCandidate levels — PREVIEW ONLY (scanner reference, not an approved order):"
                f"\nEntry: {sc.fmt(sig['entry'])} | SL: {sc.fmt(sig['stop'])}"
                f"\nTP1: {sc.fmt(sig['t1'])} | TP2: {sc.fmt(sig['t2'])}"
                "\nA fresh executable proposal and approval are required before buying. "
                "Configured active-trade limits remain enforced."
            )
        except (ValueError, KeyError, TypeError):
            result += "\nCandidate levels unavailable: invalid or incomplete scanner signal."
        card = candidate_card(sym, sig, status, reason, note)
        cards.append(card)
        if not delivered:
            with pend_lock:
                last_preview.update(text=card, saved_at=time.time())
        results.append(result)
        sc.log.info("%s", result)
    sc.log.info(
        "Scan done: %s candidate(s) [%s], %s proposal(s) sent. %s", len(cands), names, sent, note
    )
    if cands and SCAN_CANDIDATE_NOTIFICATIONS:
        lines = [card + "\n" for card in cards]
        chunk = ""
        for line in lines:
            # Bound Telegram messages, retaining every candidate/reason across chunks.
            for start in range(0, len(line) or 1, 3000):
                piece = line[start : start + 3000]
                if len(chunk) + len(piece) + 1 > 3500:
                    say(chunk)
                    chunk = ""
                chunk += piece + "\n"
        if chunk:
            say(chunk.rstrip())
    return results


def scanner_loop():
    while not em.STOP.is_set():
        try:
            scan_once()
        except Exception as e:
            sc.log.warning(f"Scan error: {safe(e)}")
        em.STOP.wait(900 - (time.time() % 900) + 10)


def startup_status():
    connection = "Connected to Binance Spot" + ("" if ta.LIVE else " Testnet")
    scanning = "Active" if AUTO_SCAN else "Disabled — use /check"
    return f"{connection}\nMarket scanning: {scanning}\nTrade execution: Requires Telegram approval"


def startup_text():
    return (
        f"{BRANDING}\n\n{startup_status()}\n\n"
        "/discover — browse High Volume and Top Gainers\n"
        "/check COIN — review a trading setup\n"
        "/setup — change trading limits\n"
        "/pnl — today's completed trade results\n"
        "/status — view positions and protection\n"
        "/position — view a bot trade or request an exit\n"
        "/help — view all commands"
    )


# ---------- main ----------
def _run_main():
    if not TOKEN or not CHAT_ID:
        raise SystemExit("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
    print("[4/5] Reconciling saved positions...", flush=True)
    unresolved = em.recover_trades()
    if em.STOP.is_set():
        return
    active = sum(record["state"] != "closed" for record in unresolved)
    print(
        f"[4/5] Reconciliation pass complete | {active} active/unresolved record(s); "
        + ("monitoring continues." if active else "no active positions."),
        flush=True,
    )
    for record in unresolved:
        if record["state"] != "closed":
            worker = threading.Thread(target=em.monitor_trade, args=(record,), daemon=False)
            workers.add(worker)
            worker.start()
    print("=" * 60)
    print(" Trading configuration")
    print(
        f" BNB fee accounting: {'ON' if em.ALLOW_BNB_FEES else 'OFF'} | candidate notifications: {'ON' if SCAN_CANDIDATE_NOTIFICATIONS else 'OFF'}"
    )
    print(
        f" Auto risk {sc.RISK_PCT}% | auto daily loss limit ${em.MAX_DAILY_LOSS_USD} | "
        f"auto minimum volume {sc.MIN_QUOTE_VOLUME:,.0f} USDT"
    )
    print(
        " Manual override: liquidity/strategy, 1% sizing and daily-loss gate bypassed after approval"
    )
    try:
        limits = settings.read()
        print(
            f" All entries: max {limits['max_position_usdt']} USDT per trade | "
            f"max {limits['max_open_trades']} active trades | change with /setup"
        )
    except Exception:
        print(" Trading settings unavailable; new entries blocked. Recovery monitoring continues.")
    print(
        f" Automatic scanning: {'ON' if AUTO_SCAN else 'OFF'} | Telegram approval required for every new buy"
    )
    print(" Press Ctrl+C to stop the assistant. Exchange orders remain active.")
    print("=" * 60)

    # ignore any old taps that piled up while the program was off
    print("[5/5] Connecting to Telegram...", flush=True)
    offset = 0
    r = tg("getUpdates", timeout=0)
    if not r.get("ok"):
        raise SystemExit(
            f"Telegram error: {r.get('description')} "
            f"(409 = another program is reading this bot; 401 = wrong token)"
        )
    if r.get("result"):
        offset = r["result"][-1]["update_id"] + 1

    print("[5/5] Telegram connection verified.", flush=True)
    print(f"[5/5] Applying {BOT_NAME} Telegram appearance...", flush=True)
    configure_telegram_ui()
    if em.STOP.is_set():
        return
    send_welcome(startup=True)
    start_managed_worker(em.result_notice_loop)
    if AUTO_SCAN:
        start_managed_worker(scanner_loop)
    print(startup_status(), flush=True)

    while not em.STOP.is_set():
        try:
            r = tg(
                "getUpdates",
                timeout=30,
                offset=offset,
                allowed_updates=json.dumps(["callback_query", "message"]),
            )
            if em.STOP.is_set():
                break
            if not r.get("ok"):
                sc.log.warning(f"Telegram: {r.get('description')}")
                em.STOP.wait(10)
                continue
            for u in r.get("result", []):
                if em.STOP.is_set():
                    break
                offset = u["update_id"] + 1
                if "callback_query" in u:
                    handle_callback(u["callback_query"])
                elif "message" in u:
                    handle_message(u["message"])
            expire_pending()
        except KeyboardInterrupt:
            request_shutdown()
            break
        except Exception as e:
            sc.log.warning(f"Loop error: {safe(e)}")
            em.STOP.wait(5)


def start_managed_worker(target):
    def run():
        try:
            target()
        finally:
            workers.discard(threading.current_thread())

    worker = threading.Thread(target=run, daemon=False)
    workers.add(worker)
    try:
        worker.start()
    except Exception:
        workers.discard(worker)
        raise
    return worker


def request_shutdown():
    if not em.STOP.is_set():
        em.STOP.set()
        print(
            "\nPlease wait while I finish saving the current session details...\n"
            "Waiting for pending operations. Existing Binance orders remain active.",
            flush=True,
        )


def main():
    if not TOKEN or not CHAT_ID:
        raise SystemExit("Telegram configuration missing")
    em.STOP.clear()
    restore_signals = shutdown_control.install(request_shutdown)
    lock = None
    try:
        print(BRANDING, flush=True)
        print(f"Environment: {mode_label()}", flush=True)
        print("[1/5] Scanner module ready; initial market scan pending.", flush=True)
        print("[2/5] Verifying Spot exchange connection...", flush=True)
        em.initialize()
        if em.STOP.is_set():
            return
        print("[2/5] Spot exchange connection verified.", flush=True)
        print("[3/5] Acquiring account instance lock...", flush=True)
        lock = em.instance_lock()
        print("[3/5] Instance lock acquired.", flush=True)
        _run_main()
    finally:
        em.STOP.set()
        for timer in recheck_timers:
            timer.cancel()
        # Hold the process/account lock until in-flight durable transitions finish.
        for worker in list(workers):
            worker.join()
        workers.clear()
        if lock is not None:
            lock.unlink()
        restore_signals()
        print(
            "Session records preserved. Assistant stopped; Binance orders remain active.\n"
            "Monitoring resumes after restart.",
            flush=True,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(safety.safe_error(error))
        raise SystemExit(1) from None
