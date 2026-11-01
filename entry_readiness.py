"""Early, read-only entry gates; a successful check never authorizes a buy."""

import math
from decimal import ROUND_CEILING, Decimal
from fractions import Fraction
from pathlib import Path

import bot_health
import exit_manager as em
import safety
import trade_assistant as ta
import trading_settings as settings


class Unavailable(ValueError):
    def __init__(self, code, reason, *, actions=("health",), modes=False):
        super().__init__(reason)
        self.code, self.reason = code, reason
        self.actions, self.modes = actions, modes
        self.checks = {}


def minimum_funding(filters, entry, fee, *, stop=None, normal=False, exit_style="two"):
    """Find the smallest step-valid buy supporting both conservative exit legs.

    Before levels are chosen, entry is only a necessary lower-bound reference.
    Given a stop, both legs must meet the stop's stricter order-value minimum.
    Full order/price/filter and strategy checks still belong to build_plan.
    """
    entry, fee = em.D(entry), em.D(fee)
    reference = min(entry, em.D(stop)) if stop is not None else entry
    if entry <= 0 or reference <= 0 or not 0 <= fee < em.D(".05"):
        raise ValueError("Invalid funding reference")
    step = filters["step"]
    structure = {"exit_style": em.target_style({"exit_style": exit_style})}
    count = len(em.target_fields(structure))
    margin = em.D("1.05") if normal else em.D(1)
    leg = max(filters["min_qty"], filters["min_notional"] * margin / reference)
    leg = (leg / step).to_integral_value(rounding=ROUND_CEILING) * step
    common = step
    if filters["market_step"] > 0:
        a, b = Fraction(step), Fraction(filters["market_step"])
        common = Decimal(math.lcm(a.numerator, b.numerator)) / Decimal(
            math.gcd(a.denominator, b.denominator)
        )
    needed = max(count * leg / (1 - fee), filters["min_qty"], filters["market_min_qty"])
    quantity = (needed / common).to_integral_value(rounding=ROUND_CEILING) * common
    net = em.round_to(quantity * (1 - fee), step)
    portions = em.target_quantities(net, step, structure)
    if min(portions) < leg:
        quantity += common  # Correct a Decimal division exactly on a step boundary.
        net = em.round_to(quantity * (1 - fee), step)
        portions = em.target_quantities(net, step, structure)
    em.validate_qty(quantity, filters, entry, market=True, entry=normal)
    for part in portions:
        em.validate_qty(part, filters, reference, entry=normal)
    return {
        "quantity": quantity,
        "cost": quantity * entry * (1 + fee),
        "half": portions[0],
        "rest": portions[1] if count == 2 else em.D(0),
    }


def check(symbol, *, mode=None, signal=None, exit_style="two"):
    """Check capacity, ownership/system evidence and pair-specific funding.

    mode=None is a preliminary all-mode check, not normal strategy validation.
    Existing Manual risk exceptions apply only to explicit mode='manual'.
    """
    stage = "runtime configuration"
    checks = {
        "Capacity": "not checked",
        "Funding": "not checked",
        "Normal risk": "not checked",
        "System/ownership": "not checked",
    }
    try:
        with em.operation_lock():
            if not em.USE_TESTNET and not em.ALLOW_LIVE:
                raise Unavailable(
                    "LIVE_DISABLED", "Live entries are disabled in the runtime configuration."
                )
            em.require_trade_permission()
            if em.STOP.is_set():
                raise Unavailable(
                    "SHUTDOWN", "The assistant is shutting down. Restart before preparing an entry."
                )
            stage = "saved trading settings"
            limits = settings.read()
            stage = "trade journal"
            records = em.load_trades()
            active = [rec for rec in records if rec["state"] != "closed"]
            for rec in active:
                if ta.empty_watch(rec, rec["id"]):
                    continue
                if rec["state"] != "protected" or rec.get("close_requested"):
                    raise Unavailable(
                        "RECONCILIATION",
                        f"{rec['symbol']} has a pending entry/exit or unresolved trade. Resolve it before another entry.",
                        actions=("positions", "health"),
                    )
                em.refresh(rec, persist=False)
                if not em.protection_sufficient(rec):
                    raise Unavailable(
                        "PROTECTION",
                        f"{rec['symbol']} does not have verified coverage for all tradable units.",
                        actions=("positions", "health"),
                    )
                if any(em.executions(rec, "SELL")):
                    em.attach_fee_valuations(rec, persist=False)
            if any(rec["symbol"] == symbol for rec in active):
                raise Unavailable(
                    "DUPLICATE",
                    f"{symbol} trade already in progress, or an entry watch exists for this pair.",
                    actions=("positions",),
                )
            capacity_reason = (
                f"Your trade limit is {limits['max_open_trades']}; all slots are occupied "
                f"({len(active)}/{limits['max_open_trades']}) by "
                + ", ".join(rec["symbol"] for rec in active)
                + ". Wait for a slot or review your session limit."
            )
            try:
                ta.ensure_free_trade_slot(symbol, limits=limits)
            except ta.ActiveTradeError as error:
                raise Unavailable(
                    "CAPACITY",
                    capacity_reason
                    if len(active) >= limits["max_open_trades"]
                    else safety.redact(str(error)),
                    actions=("positions", "setup"),
                ) from None
            if len(active) >= limits["max_open_trades"]:
                raise Unavailable(
                    "CAPACITY",
                    capacity_reason,
                    actions=("positions", "setup"),
                )
            checks["Capacity"] = "passed"
            stage = "exchange account and order ownership"
            can_trade, free, opened = bot_health.exchange_check()
            if not can_trade:
                raise Unavailable(
                    "PERMISSION", "Binance reports that trading is disabled for this account."
                )
            mismatches = bot_health.order_issues(active, opened)
            if mismatches:
                raise Unavailable(
                    "OWNERSHIP",
                    "Exchange open orders do not match isolated bot ownership. " + mismatches[0],
                )
            # This also requires actual execution/fee evidence before Manual.
            stage = "execution/fee accounting and risk reservations"
            remaining_risk = bot_health.normal_budget(
                active + [rec for rec in records if rec["state"] == "closed"]
            )
            reserve = sum(
                (
                    em.D(rec["approved_bounds"]["cost"])
                    for rec in active
                    if ta.empty_watch(rec, rec["id"])
                ),
                em.D(0),
            )
            available = free - reserve
            stage = "pair price, exchange minimums and commission"
            quote = em.get_client().get_symbol_ticker(symbol=symbol)
            if quote.get("symbol") != symbol or em.D(quote["price"]) <= 0:
                raise ValueError("Invalid entry reference quote")
            filters = em.symbol_filters(symbol)
            fee = em.commission_schedule(symbol)["reserve_rate"]
            checks["System/ownership"] = "passed"
            price = em.D(quote["price"])
            stop = None
            if signal is not None:
                stop = em.round_to(signal["stop"], filters["tick"])
                if signal.get("entry_mode") == "watch":
                    price = em.round_to(signal["entry"], filters["tick"])
                elif signal.get("entry_mode") == "now":
                    _, ask = ta.entry_quote(symbol)
                    price = (ask * em.D("1.002") / filters["tick"]).to_integral_value(
                        rounding=ROUND_CEILING
                    ) * filters["tick"]
            normal = mode is not None and mode != "manual"
            exit_style = em.target_style(
                signal if signal is not None else {"exit_style": exit_style}
            )
            minimum = minimum_funding(
                filters, price, fee, stop=stop, normal=normal, exit_style=exit_style
            )
            exit_description = "both exit legs" if exit_style == "two" else "the full-position exit"
            cap = em.D(limits["max_position_usdt"])
            if minimum["cost"] > cap:
                raise Unavailable(
                    "INVESTMENT_CAP",
                    f"This pair needs at least {minimum['cost']:.4f} USDT for {exit_description} and the buy-fee reserve at the checked levels. Your cap is {cap:f} USDT.",
                    actions=("setup", "portfolio"),
                    modes=normal,
                )
            allocation = available * em.D(".95") if normal else available
            if minimum["cost"] > allocation:
                raise Unavailable(
                    "FUNDS",
                    f"{exit_description.capitalize()} and fees need at least {minimum['cost']:.4f} USDT. Available after entry-watch reservations: {available:.4f} USDT"
                    + (
                        f"; normal allocation after the 5% cash buffer: {allocation:.4f} USDT."
                        if normal
                        else "."
                    )
                    + " Increasing the investment cap does not add funds.",
                    actions=("portfolio",),
                    modes=normal,
                )
            if normal and remaining_risk <= 0:
                raise Unavailable(
                    "NORMAL_RISK",
                    f"The normal daily risk budget is exhausted or reserved ({remaining_risk:.4f} USDT remaining).",
                    actions=("portfolio",),
                    modes=True,
                )
            if normal and stop is not None and 0 < stop < price:
                allowance = min(available / 100, remaining_risk)
                required_risk = ta.risk_amount(minimum["quantity"], price, stop, fee)
                if required_risk > allowance:
                    raise Unavailable(
                        "NORMAL_RISK",
                        f"The minimum exchange-valid size needs {required_risk:.4f} USDT of planned risk; normal allowance is {allowance:.4f} USDT. Wait or review portfolio risk.",
                        actions=("portfolio",),
                        modes=True,
                    )
            checks["Funding"] = "preliminary minimum checked" if signal is None else "passed"
            checks["Normal risk"] = (
                "Manual override"
                if mode == "manual"
                else "mode not selected"
                if mode is None
                else "budget available; sizing pending"
                if signal is None
                else "passed"
            )
            return {
                "context": dict(em.CONTEXT),
                "revision": limits["revision"],
                "price": price,
                "minimum": minimum["cost"],
                "available": available,
                "remaining_risk": remaining_risk,
                "provisional": signal is None,
                "checks": dict(checks),
            }
    except Unavailable as error:
        category = (
            "Capacity"
            if error.code in {"CAPACITY", "DUPLICATE"}
            else "Normal risk"
            if error.code == "NORMAL_RISK"
            else "Funding"
            if error.code in {"FUNDS", "INVESTMENT_CAP"}
            else "System/ownership"
        )
        checks[category] = "blocked"
        error.checks = dict(checks)
        raise
    except Exception as error:
        checks["System/ownership"] = "unverified"
        problem = Unavailable("SYSTEM", f"Could not verify {stage}. " + safety.safe_error(error))
        problem.checks = dict(checks)
        raise problem from None


def problem(error):
    if isinstance(error, Unavailable):
        return error
    if isinstance(error, ta.ActiveTradeError):
        return Unavailable("CAPACITY", safety.redact(str(error)), actions=("positions", "health"))
    if isinstance(error, ValueError):
        code = safety.recovery_code(error)
        detail = safety.safe_error(error)
        if type(error) is not ValueError and not code:
            return Unavailable("SYSTEM", detail)
        if type(error) is ValueError:
            trace = error.__traceback__
            while trace and trace.tb_next:
                trace = trace.tb_next
            trusted = {
                str(Path(__file__).with_name(name))
                for name in (
                    "trade_assistant.py",
                    "algorithmic_planner.py",
                    "exit_manager.py",
                    "entry_readiness.py",
                )
            }
            if trace and trace.tb_frame.f_code.co_filename in trusted:
                detail = safety.redact(str(error))
            else:
                return Unavailable("SYSTEM", detail)
        if code in {"ORDER_MIN_NOTIONAL", "ORDER_LOT_SIZE"}:
            return Unavailable("EXCHANGE_SIZE", detail, actions=("setup", "portfolio"), modes=True)
        if code or any(
            word in detail.lower()
            for word in ("unsupported", "commission", "exchange orders", "accounting")
        ):
            return Unavailable("SYSTEM", safety.safe_error(error), actions=("health",))
        return Unavailable("SETUP", detail, actions=(), modes=True)
    return Unavailable("SYSTEM", safety.safe_error(error))
