"""Telegram-approved Spot entry planning and durable execution."""

import math
import time
from decimal import ROUND_CEILING, Decimal

import entry_scanner as sc
import exit_manager as em
import safety
import trading_settings as settings

MAX_POSITION_USDT = safety.MAX_POSITION_USDT
MAX_OPEN_TRADES = safety.MAX_OPEN_TRADES
MIN_RR = Decimal("1.5")
MAX_SPREAD_BPS = Decimal("30")
MIN_DEPTH_MULTIPLE = Decimal("5")
MAX_PRICE_DRIFT_PCT = Decimal("0.5")
LIVE = not em.USE_TESTNET
ALGORITHM_FIELDS = (
    "algorithmic",
    "algorithm_version",
    "algorithm_mode",
    "analysis_created_at",
    "analysis_candle_close",
    "analysis_summary",
)


class PlanChangedError(ValueError):
    """Final approval bounds changed before any buy was submitted."""


class ActiveTradeError(ValueError):
    """The account's existing durable trade slot prevents a new entry."""


def same_trading_limits(left, right):
    """A session can reuse an ordinal; compare the actual approved limits too."""
    return str(left.get("settings_revision", left.get("revision", 0))) == str(
        right.get("settings_revision", right.get("revision", 0))
    ) and all(
        em.D(left.get(field, default)) == em.D(right.get(field, default))
        for field, default in (
            ("max_open_trades", safety.MAX_OPEN_TRADES),
            ("max_position_usdt", safety.MAX_POSITION_USDT),
        )
    )


def active_trade_reason(symbol, records, limit=1):
    if len(records) != 1:
        names = ", ".join(sorted({record["symbol"] for record in records}))
        label = (
            "Active trades"
            if all(record["state"] == "protected" for record in records)
            else "Active or unresolved trades"
        )
        return f"{label}: {names}; active trade limit is {limit}."
    record = records[0]
    name = record["symbol"]
    if record.get("entry_watch", {}).get("active"):
        return f"{name} entry watch is already active; new entries are blocked."
    flat = False
    try:
        flat = em.inventory(record) == 0 and any(
            order["side"] == "BUY" and em.D(order["executedQty"]) > 0 for order in record["orders"]
        )
    except (ValueError, RuntimeError):
        pass  # The record still blocks entry; missing evidence cannot free the slot.
    if record["state"] == "needs_attention" or flat:
        return f"{name} trade is awaiting reconciliation; new entries are blocked."
    if record["state"] in ("intent", "buy_submitting"):
        return f"{name} entry is pending confirmation; duplicate entries are blocked."
    if name == symbol:
        return f"{name} trade already in progress; duplicate entry blocked."
    if limit == 1:
        return f"{name} trade is in progress; only one active trade is allowed."
    return f"{name} trade is in progress; active trade limit {limit} reached."


def ensure_free_trade_slot(symbol, *, watch_id=None, limits=None):
    limits = limits or settings.read()
    records = [record for record in em.active_trades() if not empty_watch(record, watch_id)]
    if len({record["symbol"] for record in records}) != len(records):
        raise ActiveTradeError(
            "Duplicate pair records require reconciliation; new entries blocked."
        )
    if len(records) >= limits["max_open_trades"]:
        raise ActiveTradeError(active_trade_reason(symbol, records, limits["max_open_trades"]))
    for record in records:
        if record["symbol"] == symbol:
            raise ActiveTradeError(active_trade_reason(symbol, [record], limits["max_open_trades"]))
        if empty_watch(record, record["id"]):
            continue
        if record["state"] != "protected" or not em.protection_sufficient(record):
            raise ActiveTradeError(
                f"{record['symbol']} trade requires reconciliation/protection; new entries are blocked."
            )
    return records


def verify_parallel_positions(records):
    """Read/revalidate other pairs, never cancel or trade them during planning."""
    known = {}
    for rec in records:
        if empty_watch(rec, rec["id"]):
            continue
        em.refresh(rec)
        if rec["state"] != "protected" or not em.protection_sufficient(rec):
            raise ActiveTradeError(
                f"{rec['symbol']} protection/accounting changed; reconciliation required."
            )
        if any(em.executions(rec, "SELL")):
            em.attach_fee_valuations(rec)
        for order in rec["orders"]:
            if order["status"] not in em.TERMINAL:
                if order["purpose"] not in ("stop", "target"):
                    raise ActiveTradeError("A bot order still requires reconciliation.")
                key = (rec["symbol"], order["orderId"])
                if key in known:
                    raise ActiveTradeError("Overlapping order ownership requires reconciliation.")
                known[key] = (rec, order)
    opened = em.get_client().get_open_orders()
    if not isinstance(opened, list):
        raise ValueError("Invalid exchange open-order evidence")
    seen = set()
    for row in opened:
        key = (row["symbol"], row["orderId"])
        if key in seen or key not in known:
            raise ValueError("Existing exchange orders: cannot establish isolated ownership")
        seen.add(key)
        rec, order = known[key]
        parent = next(item for item in rec["oco"] if order["cid"] in item["order_cids"])
        target = order["purpose"] == "target"
        if (
            row["clientOrderId"] not in {order["cid"], order.get("cancel_cid")}
            or row["side"] != "SELL"
            or row["type"] != ("LIMIT_MAKER" if target else "STOP_LOSS")
            or row["status"] != order["status"]
            or row["orderListId"] != parent["orderListId"]
            or em.D(row["origQty"]) != em.D(order["qty"])
            or em.D(row["executedQty"]) != em.D(order["executedQty"])
            or em.D(row["price" if target else "stopPrice"])
            != em.D(parent["tp" if target else "stop"])
        ):
            raise ActiveTradeError("Existing protective order changed; reconciliation required.")
    if seen != set(known):
        raise ActiveTradeError("Existing protective orders changed; reconciliation required.")
    return opened


def scan_candidates():
    if em.STOP.is_set():
        return {}, ""
    note = sc.btc_note()
    candidates = {}
    for symbol, _, change in sc.universe():
        if em.STOP.is_set():
            break
        try:
            signal = sc.analyze(symbol, change)
            if signal:
                candidates[symbol] = signal
        except Exception as error:
            sc.log.warning("%s: %s", symbol, safety.safe_error(error))
        em.STOP.wait(0.15)
    return candidates, note


def require_finite_levels(sig):
    fields = em.target_fields(sig)
    if em.target_style(sig) == "single" and "t2" in sig:
        raise ValueError("Single target requires one take-profit level only")
    values = [em.D(sig[k]) for k in ("stop", "entry", *fields)]
    if values[0] <= 0 or any(a >= b for a, b in zip(values, values[1:])):
        raise ValueError(
            "STOP < ENTRY < TP required"
            if len(fields) == 1
            else "STOP < ENTRY < TP1 < TP2 required, all finite and positive"
        )


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
    with em.operation_lock():
        return _build_plan(sig, approved_bounds=approved_bounds, watch_id=watch_id)


def _build_plan(sig, *, approved_bounds=None, watch_id=None):
    require_finite_levels(sig)
    manual = sig.get("manual_override", False)
    if type(manual) is not bool:
        raise ValueError("Manual override must be explicitly enabled with a boolean flag")
    algorithmic = sig.get("algorithmic", False)
    if type(algorithmic) is not bool or (algorithmic and manual):
        raise ValueError("Algorithm mode requires normal risk checks; manual override is separate")
    warnings = []
    em.require_trade_permission()
    if sc.RISK_PCT != 1:
        raise ValueError("Automatic risk must remain 1%")
    limits = settings.read()
    if approved_bounds is not None and not same_trading_limits(approved_bounds, limits):
        raise PlanChangedError(
            "Trading settings changed; a fresh proposal and approval are required"
        )
    records = ensure_free_trade_slot(sig["symbol"], watch_id=watch_id, limits=limits)
    opened = verify_parallel_positions(records)
    c = em.get_client()
    symbol = sig["symbol"]
    ticker = c.get_ticker(symbol=symbol)
    if ticker.get("symbol") != symbol:
        raise ValueError("Ticker identity mismatch")
    if algorithmic and not em.D(-15) <= em.D(ticker["priceChangePercent"]) <= em.D(25):
        raise ValueError("Algorithm: 24h move outside strategy limits (-15% to +25%)")
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
    fields = em.target_fields(sig)
    levels = {k: em.round_to(sig[k], f["tick"]) for k in ("stop", *fields)}
    target_label = "Take profit" if em.target_style(sig) == "single" else "TP1"
    stop, t1 = levels["stop"], levels["t1"]
    if sig.get("target_policy") == "scanner_r_multiple_v1" and not manual and not algorithmic:
        # Compile only NEW generated suggestions against the original signal entry.
        # Never chase a newer quote, change manual levels or rewrite approved exits.
        reference = em.D(sig["entry"])
        reference_risk = reference - stop
        compiled = tuple(
            ((reference + multiple * reference_risk) / f["tick"]).to_integral_value(
                rounding=ROUND_CEILING
            )
            * f["tick"]
            for multiple in (MIN_RR, Decimal("2.5"))[: len(fields)]
        )
        levels.update(zip(fields, compiled))
        t1 = levels["t1"]
        ceiling = sig.get("resistance_ceiling")
        if ceiling is not None and t1 > em.D(ceiling):
            raise ValueError("Rounded scanner TP1 exceeds observed resistance; skip trade")
    ordered = [stop, price, *(levels[k] for k in fields)]
    if stop <= 0 or any(a >= b for a, b in zip(ordered, ordered[1:])):
        raise ValueError("Rounded protective levels invalid")
    risk = price - stop
    if not Decimal(".01") <= risk / price <= Decimal(".06"):
        if not manual:
            raise ValueError(
                f"Stop distance {risk / price * 100:.4f}% is outside 1%-6%. "
                f"Checked entry {em.fmt(price)}, SL {em.fmt(stop)}; signal entry {em.fmt(sig['entry'])}."
            )
        warnings.append(
            f"Stop distance {risk / price * 100:.2f}% is outside the 1%-6% strategy range"
        )
    if t1 - price < MIN_RR * risk:
        if not manual:
            minimum = ((price + MIN_RR * risk) / f["tick"]).to_integral_value(
                rounding=ROUND_CEILING
            ) * f["tick"]
            raise ValueError(
                f"{target_label} reward/risk is below 1.5R after price/tick checks. "
                f"Checked entry {em.fmt(price)}, SL {em.fmt(stop)}, {target_label} {em.fmt(t1)}; "
                f"minimum {target_label} {em.fmt(minimum)}. Signal entry {em.fmt(sig['entry'])}."
            )
        warnings.append(f"{target_label} reward/risk {(t1 - price) / risk:.2f}R is below 1.5R")
    watch_costs = [
        em.D(rec["approved_bounds"]["cost"]) for rec in records if empty_watch(rec, rec["id"])
    ]
    risks = [em.D(rec["plan"]["loss"]) for rec in records]
    if any(value <= 0 for value in watch_costs + risks):
        raise ValueError("Invalid active-trade reservation")
    watch_reserve = sum(watch_costs, Decimal(0))
    retained_cost = sum(
        (
            em.D(rec["retained_dust"]["cost_basis_usdt"])
            for rec in em.load_trades()
            if rec["state"] == "closed" and rec.get("retained_dust")
        ),
        Decimal(0),
    )
    # A freed slot does not turn an unsold asset into free risk capacity.
    committed_risk = sum(risks, Decimal(0)) + retained_cost
    balance = em.D(c.get_asset_balance(asset="USDT")["free"]) - watch_reserve
    if balance <= 0:
        raise ValueError("No available USDT")
    fee_schedule = em.commission_schedule(symbol)
    fee = fee_schedule["reserve_rate"]
    daily_loss = em.todays_loss(watch_id=watch_id) if watch_id is not None else em.todays_loss()
    remaining = em.D(3) - daily_loss - committed_risk
    # Manual approval accepts the chosen levels at the position cap, instead of
    # reducing quantity to the automatic 1%/daily-risk budget.
    max_cost = min(
        em.D(limits["max_position_usdt"]), balance if manual else balance * Decimal(".95")
    )
    budget = max_cost if manual else min(balance / 100, remaining)
    if not manual and budget <= 0:
        raise ValueError(
            "Daily bot-trade loss limit reached or risk budget committed to active trades"
        )
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
            f"Daily realized loss {daily_loss:.2f} USDT plus committed risk {committed_risk:.2f} "
            f"and planned risk {loss:.2f} USDT "
            "exceeds the automatic 3 USDT daily budget"
        )
    spread, depth = market_liquidity(symbol, qty * price, enforce=not manual)
    if manual and spread > MAX_SPREAD_BPS:
        warnings.append(f"Spread {spread:.2f} bps exceeds {MAX_SPREAD_BPS} bps")
    if manual and depth < qty * price * MIN_DEPTH_MULTIPLE:
        warnings.append(f"Nearby order-book depth {depth:.2f} USDT is below the strategy minimum")
    portions = em.target_quantities(em.round_to(qty * (1 - fee), f["step"]), f["step"], sig)
    half, rest = portions[0], portions[1] if len(portions) == 2 else Decimal(0)
    plan = {
        "symbol": symbol,
        "entry_mode": mode,
        "exit_style": em.target_style(sig),
        "price": price,
        "stop": stop,
        "t1": t1,
        **({"t2": levels["t2"]} if len(fields) == 2 else {}),
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
        "gain2": rest * (levels["t2"] - price) if len(fields) == 2 else Decimal(0),
        "risk_pct": risk / price * 100,
        "rr": (t1 - price) / risk,
        "spread_bps": spread,
        "depth_usdt": depth,
        "manual_override": manual,
        "override_warnings": warnings,
        "settings_revision": limits["revision"],
        "max_open_trades": limits["max_open_trades"],
        "max_position_usdt": limits["max_position_usdt"],
        "committed_risk": committed_risk,
        "watch_reserve": watch_reserve,
        "retained_dust_cost": retained_cost,
    }
    if algorithmic:
        import algorithmic_planner as algo

        plan.update({key: sig[key] for key in ALGORITHM_FIELDS})
        plan["market_warning"] = algo.validate_plan(sig, plan)
    em.preflight(plan, f, open_orders=opened)
    return plan


def approval_bounds(old, fresh):
    if not same_trading_limits(old, fresh):
        return False
    if em.target_style(old) != em.target_style(fresh):
        return False
    if old["symbol"] != fresh["symbol"] or str(old.get("manual_override", False)) != str(
        fresh.get("manual_override", False)
    ):
        return False
    if old.get("entry_mode", "legacy") != fresh.get("entry_mode", "legacy"):
        return False
    if any(
        str(old.get(key, False if key == "algorithmic" else ""))
        != str(fresh.get(key, False if key == "algorithmic" else ""))
        for key in ALGORITHM_FIELDS
    ):
        return False
    # Changing quantity, cost, risk or any exit level requires a NEW approval.
    return (
        all(em.D(fresh[k]) <= em.D(old[k]) for k in ("qty", "cost", "loss"))
        and all(em.D(fresh[k]) == em.D(old[k]) for k in ("stop", *em.target_fields(old)))
        and all(em.D(fresh[k]) <= em.D(old[k]) for k in ("half", "rest"))
        and abs(em.D(fresh["price"]) / em.D(old["price"]) - 1) <= Decimal(".005")
    )


def execute(plan, *, approval=None, monitor=True):
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
        try:
            ensure_free_trade_slot(plan["symbol"])
        except ActiveTradeError as error:
            raise RuntimeError(str(error)) from None
        if not same_trading_limits(plan, settings.read()):
            raise PlanChangedError("Trading settings changed; fresh approval required")
        if any(r.get("approval_id") == approval["approval_id"] for r in em.load_trades()):
            raise ValueError("Approval already consumed")
        # Rebuild immediately under account operation lock to enforce risk, filters,
        # balances, commission schedule and approval bounds at transmission time.
        sig = {
            "symbol": plan["symbol"],
            "entry": plan["price"],
            "stop": plan["stop"],
            "t1": plan["t1"],
            **({"t2": plan["t2"]} if em.target_style(plan) == "two" else {}),
            "exit_style": em.target_style(plan),
            "manual_override": plan.get("manual_override", False),
        }
        sig.update({key: plan[key] for key in ALGORITHM_FIELDS if key in plan})
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
    if monitor:
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
                    + ". "
                    + (
                        em.retained_dust_text(rec)
                        if rec.get("retained_dust")
                        else "Confirmed acquired inventory has been closed and accounted for."
                    )
                )

        else:
            rec["state"] = "bought"
            em.save_trade(rec)
            em.submit_protection(
                rec, em.inventory(rec), plan["t1"], plan.get("t2"), plan["stop"], f
            )
            remaining, covered = em.inventory(rec), em.coverage(rec)
            if em.protection_sufficient(rec, f):
                em.alert(
                    "BUY FILLED / PROTECTION CONFIRMED. "
                    + em.entry_result(rec)
                    + f". Remaining bot quantity {remaining}; observed stop coverage {covered}; "
                    f"{len(rec['oco'])} OCO lists verified. "
                    + (em.protection_dust_text(rec) + " " if rec.get("protection_dust") else "")
                    + "Use /status for current state."
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


def close_watch(rec, reason, *, notify=True):
    if rec["orders"] or rec["oco"]:
        raise ValueError("Cannot cancel an entry watch after submission has started")
    rec["entry_watch"].update(active=False, result=reason)
    rec.update(state="closed", accounted=True, realized_events=[])
    em.save_trade(rec)
    if notify:
        em.alert(f"ENTRY WATCH CLOSED: {rec['symbol']}: {reason}. No buy submitted.")
    return True


def advance_watch(rec):
    """One locked step; one attempt only, with confirmed fills handled normally."""
    if rec["orders"] or rec["oco"]:
        raise ValueError("Active watch contains order intents; refusing new buy")
    if rec.get("close_requested"):
        return close_watch(rec, "cancelled by saved exit request")
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
        **({"t2": plan["t2"]} if em.target_style(plan) == "two" else {}),
        "exit_style": em.target_style(plan),
        "entry_mode": "watch",
        "manual_override": plan["manual_override"] == "True",
    }
    sig.update({key: plan[key] for key in ALGORITHM_FIELDS if key in plan})
    if "algorithmic" in sig:
        sig["algorithmic"] = sig["algorithmic"] == "True"
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
