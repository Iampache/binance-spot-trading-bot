"""Telegram-approved Spot entry planning and durable execution."""

import math
import time
from decimal import ROUND_CEILING, Decimal

import entry_scanner as sc
import exit_manager as em
import safety

MAX_POSITION_USDT = safety.MAX_POSITION_USDT
MAX_OPEN_TRADES = safety.MAX_OPEN_TRADES
MIN_RR = Decimal("1.5")
MAX_SPREAD_BPS = Decimal("30")
MIN_DEPTH_MULTIPLE = Decimal("5")
MAX_PRICE_DRIFT_PCT = Decimal("0.5")
LIVE = not em.USE_TESTNET


class PlanChangedError(ValueError):
    """Final approval bounds changed before any buy was submitted."""


def scan_candidates():
    note = sc.btc_note()
    candidates = {}
    for symbol, _, change in sc.universe():
        try:
            signal = sc.analyze(symbol, change)
            if signal:
                candidates[symbol] = signal
        except Exception as error:
            sc.log.warning("%s: %s", symbol, safety.safe_error(error))
        time.sleep(0.15)
    return candidates, note


def require_finite_levels(sig):
    values = [em.D(sig[k]) for k in ("entry", "stop", "t1", "t2")]
    if not 0 < values[1] < values[0] < values[2] < values[3]:
        raise ValueError("STOP < ENTRY < TP1 < TP2 required, all finite and positive")


def market_liquidity(symbol, estimated_cost, *, enforce=True):
    c = em.get_client()
    book = c.get_orderbook_ticker(symbol=symbol)
    bid, ask = em.D(book["bidPrice"]), em.D(book["askPrice"])
    if not 0 < bid <= ask:
        raise ValueError("Invalid best bid/ask")
    spread = (ask - bid) / ask * 10000
    depths = c.get_order_book(symbol=symbol, limit=20)
    amounts = []
    for side in ("bids", "asks"):
        # Only count depth near the spread; remote book levels cannot protect slippage.
        center = bid if side == "bids" else ask
        rows = [(em.D(p), em.D(q)) for p, q in depths[side]]
        if any(p <= 0 or q < 0 for p, q in rows):
            raise ValueError("Invalid depth")
        amounts.append(
            sum(
                (p * q for p, q in rows if abs(p / center - 1) <= Decimal(".005")),
                Decimal(0),
            )
        )
    depth = min(amounts)
    if enforce and (spread > MAX_SPREAD_BPS or depth < em.D(estimated_cost) * MIN_DEPTH_MULTIPLE):
        raise ValueError("Spread too wide or order-book depth insufficient")
    return spread, depth


def risk_amount(qty, entry, stop, fee):
    # Reserve full-position downside plus undiscounted buy/sell commission.
    return em.D(qty) * (em.D(entry) - em.D(stop) + (em.D(entry) + em.D(stop)) * em.D(fee))


def build_plan(sig, *, approved_bounds=None, watch_id=None):
    require_finite_levels(sig)
    manual = sig.get("manual_override", False)
    if type(manual) is not bool:
        raise ValueError("Manual override must be explicitly enabled with a boolean flag")
    warnings = []
    em.require_trade_permission()
    if sc.RISK_PCT != 1 or MAX_POSITION_USDT != 12 or MAX_OPEN_TRADES != 1:
        raise ValueError("Required limits: 1% risk, 12 USDT position, one open trade")
    if any(not empty_watch(r, watch_id) for r in em.active_trades()):
        raise ValueError("Open or unresolved bot trade exists")
    c = em.get_client()
    symbol = sig["symbol"]
    ticker = c.get_ticker(symbol=symbol)
    if ticker.get("symbol") != symbol:
        raise ValueError("Ticker identity mismatch")
    quote_volume = em.D(ticker["quoteVolume"])
    if quote_volume < 0:
        raise ValueError("Invalid quote volume")
    if quote_volume < safety.MIN_QUOTE_VOLUME:
        if not manual:
            raise ValueError("Minimum 24-hour trading volume is 10,000,000 USDT; skip trade")
        warnings.append(f"24h volume {quote_volume:,.0f} USDT is below 10,000,000 USDT")
    price = em.D(c.get_symbol_ticker(symbol=symbol)["price"])
    mode = sig.get("entry_mode", "legacy")
    if mode not in ("legacy", "now", "watch"):
        raise ValueError("Unknown entry mode")
    f = em.symbol_filters(symbol)
    if mode == "legacy":
        if price <= 0 or abs(price / em.D(sig["entry"]) - 1) > Decimal(".01"):
            raise ValueError("Entry moved more than 1% from signal")
    else:
        bid, ask = entry_quote(symbol)
        if price <= em.D(sig["stop"]) or bid <= em.D(sig["stop"]):
            raise ValueError("Stop already reached; entry invalidated")
        if approved_bounds is not None:
            price = em.D(approved_bounds["price"])
        elif mode == "now":
            # Fixed 0.2% ask allowance, rounded up to an exchange-valid tick.
            price = (ask * Decimal("1.002") / f["tick"]).to_integral_value(
                rounding=ROUND_CEILING
            ) * f["tick"]
        else:
            price = em.round_to(sig["entry"], f["tick"])
        if mode == "now" and ask > price:
            raise PlanChangedError(
                "Ask exceeds approved maximum buy price; approve a fresh proposal"
            )
    stop, t1, t2 = (em.round_to(sig[k], f["tick"]) for k in ("stop", "t1", "t2"))
    if not 0 < stop < price < t1 < t2:
        raise ValueError("Rounded protective levels invalid")
    risk = price - stop
    if not Decimal(".01") <= risk / price <= Decimal(".06"):
        if not manual:
            raise ValueError("Stop must be 1%-6% below entry and TP1 at least 1.5R")
        warnings.append(
            f"Stop distance {risk / price * 100:.2f}% is outside the 1%-6% strategy range"
        )
    if (t1 - price) / risk < MIN_RR:
        if not manual:
            raise ValueError("Stop must be 1%-6% below entry and TP1 at least 1.5R")
        warnings.append(f"TP1 reward/risk {(t1 - price) / risk:.2f}R is below 1.5R")
    if c.get_open_orders():
        raise ValueError("Existing exchange orders: cannot establish isolated ownership")
    balance = em.D(c.get_asset_balance(asset="USDT")["free"])
    if balance <= 0:
        raise ValueError("No available USDT")
    fee_schedule = em.commission_schedule(symbol)
    fee = fee_schedule["reserve_rate"]
    daily_loss = em.todays_loss(watch_id=watch_id) if watch_id is not None else em.todays_loss()
    remaining = em.D(3) - daily_loss
    # Manual approval accepts the chosen levels at the position cap, instead of
    # reducing quantity to the automatic 1%/daily-risk budget.
    max_cost = min(em.D(12), balance if manual else balance * Decimal(".95"))
    budget = max_cost if manual else min(balance / 100, remaining)
    if not manual and budget <= 0:
        raise ValueError("Daily bot-trade loss limit reached")
    risk_per_unit = risk + (price + stop) * fee
    cost_limit_units = max_cost / (price * (1 + fee))
    qty = em.round_to(
        cost_limit_units if manual else min(budget / risk_per_unit, cost_limit_units),
        f["step"],
    )
    # Compute a common exact step instead of repeatedly rounding incompatible lattices.
    if f["market_step"] > 0:
        from fractions import Fraction

        a, b = Fraction(f["step"]), Fraction(f["market_step"])
        common = Decimal(math.lcm(a.numerator, b.numerator)) / Decimal(
            math.gcd(a.denominator, b.denominator)
        )
        qty = em.round_to(qty, common)
    if approved_bounds is not None:
        qty = min(qty, em.D(approved_bounds["qty"]))
    baseline = c.get_asset_balance(asset=f["base"])
    baseline = em.D(baseline["free"]) + em.D(baseline["locked"])
    loss = risk_amount(qty, price, stop, fee)
    if manual and loss > balance / 100:
        warnings.append(f"Planned stop loss {loss:.2f} USDT exceeds 1% of available USDT")
    if manual and loss > remaining:
        warnings.append(
            f"Daily realized loss {daily_loss:.2f} USDT plus planned risk {loss:.2f} USDT "
            "exceeds the automatic 3 USDT daily budget"
        )
    spread, depth = market_liquidity(symbol, qty * price, enforce=not manual)
    if manual and spread > MAX_SPREAD_BPS:
        warnings.append(f"Spread {spread:.2f} bps exceeds {MAX_SPREAD_BPS} bps")
    if manual and depth < qty * price * MIN_DEPTH_MULTIPLE:
        warnings.append(f"Nearby order-book depth {depth:.2f} USDT is below the strategy minimum")
    half = em.round_to(qty * (1 - fee) / 2, f["step"])
    rest = em.round_to(qty * (1 - fee), f["step"]) - half
    plan = {
        "symbol": symbol,
        "entry_mode": mode,
        "price": price,
        "stop": stop,
        "t1": t1,
        "t2": t2,
        "qty": qty,
        "half": half,
        "rest": rest,
        "step": f["step"],
        "fee_rate": fee,
        "discounted_fee_rate": fee_schedule["discounted_rate"],
        "bnb_discount": fee_schedule["bnb_discount"],
        "bnb_discount_multiplier": fee_schedule["discount_multiplier"],
        "bnb_fee_support": fee_schedule["bnb_fee_support"],
        "baseline_base": baseline,
        "cost": qty * price * (1 + fee),
        "balance": balance,
        "loss": loss,
        "risk_budget": budget,
        "gain1": half * (t1 - price),
        "gain2": rest * (t2 - price),
        "risk_pct": risk / price * 100,
        "rr": (t1 - price) / risk,
        "spread_bps": spread,
        "depth_usdt": depth,
        "manual_override": manual,
        "override_warnings": warnings,
    }
    em.preflight(plan, f)
    return plan


def approval_bounds(old, fresh):
    if old["symbol"] != fresh["symbol"] or str(old.get("manual_override", False)) != str(
        fresh.get("manual_override", False)
    ):
        return False
    if old.get("entry_mode", "legacy") != fresh.get("entry_mode", "legacy"):
        return False
    # Changing quantity, cost, risk or any exit level requires a NEW approval.
    return (
        all(em.D(fresh[k]) <= em.D(old[k]) for k in ("qty", "cost", "loss"))
        and all(em.D(fresh[k]) == em.D(old[k]) for k in ("stop", "t1", "t2"))
        and abs(em.D(fresh["price"]) / em.D(old["price"]) - 1) <= Decimal(".005")
    )


def execute(plan, *, approval=None):
    """No entry without explicit displayed bounds supplied by the Telegram handler."""
    if (
        approval is None
        or not approval.get("approved")
        or not approval.get("approval_id")
        or time.time() > approval.get("expires_at", 0)
        or not approval_bounds(approval, plan)
    ):
        raise ValueError("Explicit unchanged Telegram approval bounds required")
    with em.operation_lock():
        em.require_trade_permission()
        if em.active_trades():
            raise RuntimeError("Another tracked trade is active or unresolved")
        if any(r.get("approval_id") == approval["approval_id"] for r in em.load_trades()):
            raise ValueError("Approval already consumed")
        # Rebuild immediately under account operation lock to enforce risk, filters,
        # balances, commission schedule and approval bounds at transmission time.
        sig = {
            "symbol": plan["symbol"],
            "entry": plan["price"],
            "stop": plan["stop"],
            "t1": plan["t1"],
            "t2": plan["t2"],
            "manual_override": plan.get("manual_override", False),
        }
        sig["entry_mode"] = plan.get("entry_mode", "legacy")
        fresh = (
            build_plan(sig, approved_bounds=approval)
            if sig["entry_mode"] != "legacy"
            else build_plan(sig)
        )
        if not approval_bounds(approval, fresh):
            raise PlanChangedError("Market/risk changed; send a new proposal for approval")
        plan = fresh
        if plan.get("entry_mode") == "watch":
            armed = em.D(time.time())
            if armed + 15 >= em.D(approval["expires_at"]):
                raise PlanChangedError("Approval expired before watch could be armed")
            rec = em.new_trade(
                plan,
                approval=approval,
                entry_watch={
                    "armed_at": str(armed),
                    "expires_at": str(armed + 900),
                    "active": True,
                },
            )
            em.alert(
                f"ENTRY WATCH ARMED: {plan['symbol']}; maximum buy {plan['price']}. "
                "Expires in 15 minutes. No exchange buy is resting. /cancel stops the watch. Restart or read failure cancels it."
            )
        else:
            rec = em.new_trade(plan, approval=approval)
            submit_entry(rec, plan)
    em.monitor_trade(rec)
    return rec


def submit_entry(rec, plan):
    """Called under account operation lock with durable approval and ownership record."""
    f = em.symbol_filters(plan["symbol"])
    rec["state"] = "buy_submitting"
    item = em.intent(rec, "BUY", plan["qty"], "buy")
    item["price_cap"] = em.fmt(em.round_to(plan["price"], f["tick"]))
    em.save_trade(rec)
    try:
        em.submit_order(rec, item)
        if item["status"] != "FILLED":
            if em.D(item["executedQty"]) > 0:
                em.alert(
                    "PARTIAL BUY REQUIRES RECONCILIATION. "
                    + em.entry_result(rec)
                    + ". Confirmed acquired quantity is being reconciled; do not retry the buy."
                )
            # Reconcile partial fills or cancel a still-open buy before
            # reporting a final outcome. Never infer zero fill from balance.
            em.reconcile_trade(rec)
            buy_result = next(order for order in rec["orders"] if order["purpose"] == "buy")
            if buy_result["status"] in em.TERMINAL and em.D(buy_result["executedQty"]) == 0:
                explanation = (
                    "The price-capped IOC did not execute; no coins were acquired."
                    if buy_result["status"] in ("EXPIRED", "CANCELED", "EXPIRED_IN_MATCH")
                    else "The buy was rejected with zero confirmed execution."
                )
                em.alert(
                    "NO BUY FILLED. "
                    + em.entry_result(rec)
                    + ". "
                    + explanation
                    + " No automatic buy retry; a fresh proposal needs approval."
                )
            elif rec["state"] == "closed":
                em.alert(
                    "PARTIAL ENTRY RECONCILED. "
                    + em.entry_result(rec)
                    + ". Confirmed acquired inventory has been closed and accounted for."
                )

        else:
            rec["state"] = "bought"
            em.save_trade(rec)
            em.submit_protection(rec, em.inventory(rec), plan["t1"], plan["t2"], plan["stop"], f)
            remaining, covered = em.inventory(rec), em.coverage(rec)
            if remaining > 0 and covered >= remaining:
                em.alert(
                    "BUY FILLED / PROTECTION CONFIRMED. "
                    + em.entry_result(rec)
                    + f". Remaining bot quantity {remaining}; observed stop coverage {covered}; "
                    f"{len(rec['oco'])} OCO lists verified. Use /status for current state."
                )
            elif remaining == 0:
                em.alert(
                    "BUY FILLED; exits already executed. "
                    + em.entry_result(rec)
                    + ". Position accounting is being reconciled."
                )
            else:
                em.alert(
                    "BUY FILLED; COVERAGE INCOMPLETE. "
                    + em.entry_result(rec)
                    + f". Remaining {remaining}; observed stop coverage {covered}. "
                    "Recovery is reconciling the uncovered inventory; inspect Binance."
                )
    except Exception as error:
        if not item["submitted"]:
            # Validation failed before the transport boundary: no request could exist.
            item.update(status="REJECTED", local_rejection=True, rejection=safety.safe_error(error))
        rec["state"] = "needs_attention"
        em.save_trade(rec)
        em.alert(
            safety.safe_error(error) + ". Durable recovery will reconcile before any further write."
        )


def empty_watch(rec, watch_id):
    return (
        watch_id is not None
        and rec["id"] == watch_id
        and rec.get("entry_watch", {}).get("active") is True
        and not rec["orders"]
        and not rec["oco"]
    )


def entry_quote(symbol):
    book = em.get_client().get_orderbook_ticker(symbol=symbol)
    bid, ask = em.D(book["bidPrice"]), em.D(book["askPrice"])
    if not 0 < bid <= ask:
        raise ValueError("Invalid best bid/ask")
    return bid, ask


def watch_deadline(rec):
    """Validate the persisted, finite 15-minute authorization, never renew it."""
    watch, bounds = rec["entry_watch"], rec["approved_bounds"]
    armed, deadline = em.D(watch["armed_at"]), em.D(watch["expires_at"])
    if (
        bounds.get("entry_mode") != "watch"
        or rec["plan"].get("entry_mode") != "watch"
        or not rec.get("approval_id")
        or armed <= 0
        or armed + 15 >= em.D(bounds["expires_at"])
        or deadline != armed + 900
    ):
        raise ValueError("Invalid entry-watch authorization")
    return deadline


def close_watch(rec, reason):
    if rec["orders"] or rec["oco"]:
        raise ValueError("Cannot cancel an entry watch after submission has started")
    rec["entry_watch"].update(active=False, result=reason)
    rec.update(state="closed", accounted=True, realized_events=[])
    em.save_trade(rec)
    em.alert(f"ENTRY WATCH CLOSED: {rec['symbol']}: {reason}. No buy submitted.")
    return True


def advance_watch(rec):
    """One locked step; one attempt only, with confirmed fills handled normally."""
    if rec["orders"] or rec["oco"]:
        raise ValueError("Active watch contains order intents; refusing new buy")
    deadline = watch_deadline(rec)
    if em.D(time.time()) + 15 >= deadline:
        return close_watch(rec, "expired")
    if em.STOP.is_set():
        return False
    bid, ask = entry_quote(rec["symbol"])
    last = em.D(em.get_client().get_symbol_ticker(symbol=rec["symbol"])["price"])
    if min(bid, last) <= em.D(rec["plan"]["stop"]):
        return close_watch(rec, "stop reached before entry")
    if ask > em.D(rec["plan"]["price"]):
        return False
    plan = rec["plan"]
    sig = {
        "symbol": rec["symbol"],
        "entry": plan["price"],
        "stop": plan["stop"],
        "t1": plan["t1"],
        "t2": plan["t2"],
        "entry_mode": "watch",
        "manual_override": plan["manual_override"] == "True",
    }
    try:
        fresh = build_plan(sig, approved_bounds=rec["approved_bounds"], watch_id=rec["id"])
        if not approval_bounds(rec["approved_bounds"], fresh):
            return close_watch(rec, "approved risk/quantity bounds no longer valid")
    except ValueError as error:
        return close_watch(rec, safety.redact(str(error)))
    # Persist disarming BEFORE any order intent: a crash here closes without buying.
    rec["entry_watch"].update(active=False, result="triggered")
    rec["plan"] = {k: str(v) for k, v in fresh.items()}
    rec["baseline_base"] = em.fmt(fresh["baseline_base"])
    em.save_trade(rec)
    submit_entry(rec, fresh)
    return rec["state"] == "closed"


def cancel_entry_watches():
    if em.CONTEXT is None:
        return 0
    with em.operation_lock():
        count = 0
        for rec in em.active_trades():
            if empty_watch(rec, rec["id"]):
                close_watch(rec, "cancelled by user")
                count += 1
        return count
