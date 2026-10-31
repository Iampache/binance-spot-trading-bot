"""Durable Spot inventory ledger. Unknown outcomes never authorize another write."""

import argparse
import json
import logging
import os
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv

import exchange as ex
import safety
import trading_settings as settings
from file_locking import ProcessLock
from telegram_formatting import ticker_link_options

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "runtime_state"
TRADES_DIR = None
CONTEXT = None
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
ALLOW_LIVE = os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"
ALLOW_BNB_FEES = os.getenv("ALLOW_BNB_FEES", "false").strip().lower() == "true"
MAX_DAILY_LOSS_USD = safety.MAX_DAILY_LOSS_USD
POLL_SECONDS = 5
client = None
STOP = threading.Event()
MUTEX = threading.RLock()
NOTICE_LOCK = threading.Lock()
LOCK_STATE = threading.local()
WINDOWS = sys.platform == "win32"
JOURNAL_REPLACE_ATTEMPTS = 8
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}
log = logging.getLogger(__name__)
safety.secure_logging()


def D(value):
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("Numeric values must be finite")
    return value


def fmt(value):
    return format(D(value), "f")


def round_to(value, step):
    value, step = D(value), D(step)
    if value < 0 or step <= 0:
        raise ValueError("Invalid value/step")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def get_client():
    global client
    if client is None:
        ex.verify_sdk()
        from binance.client import Client

        # Read only when explicitly starting an authenticated runtime. Never log.
        key, secret = os.getenv("API_KEY"), os.getenv("API_SECRET")
        if not key or not secret:
            raise RuntimeError("Runtime Binance credentials are not configured")
        client = Client(
            key,
            secret,
            testnet=USE_TESTNET,
            ping=False,
            requests_params={"timeout": 15},
        )
    ex.synchronize_clock(client)
    return client


def initialize():
    """Account UID is verified by Binance; aliases/keys are not account identity."""
    global CONTEXT, TRADES_DIR
    account = ex.signed_read(get_client(), "get_account")
    if account.get("uid") is None or account.get("accountType") != "SPOT":
        raise RuntimeError("Verified Spot account UID required; no identity fallback")
    uid = str(account["uid"])
    if not uid.isdigit():
        raise RuntimeError("Invalid account UID")
    context = {"environment": "testnet" if USE_TESTNET else "live", "account_uid": uid}
    if CONTEXT is not None and CONTEXT != context:
        raise RuntimeError("Account/environment changed in running process")
    # Older journals cannot be safely migrated without explicit reconciliation.
    if (BASE_DIR / "state").exists():
        raise RuntimeError("Legacy state directory requires separate manual reconciliation")
    CONTEXT = context
    TRADES_DIR = STATE_DIR / context["environment"] / uid / "trades"
    return context


def ensure_context():
    if CONTEXT is None or TRADES_DIR is None:
        initialize()
    if CONTEXT["environment"] != ("testnet" if USE_TESTNET else "live"):
        raise RuntimeError("Journal environment mismatch")


def require_trade_permission():
    if not USE_TESTNET and not ALLOW_LIVE:
        raise RuntimeError("LIVE TRADING BLOCKED")
    if MAX_DAILY_LOSS_USD != 3:
        raise ValueError("Daily loss threshold must remain 3 USDT")
    ensure_context()


def alert(message):
    message = safety.redact(message)
    log.warning("%s", message)
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat, "text": message, **ticker_link_options(message)},
                timeout=12,
            )
            response.raise_for_status()
            return response.json().get("ok") is True
        except Exception as error:
            log.error("%s", safety.safe_error(error))
    return False


def _atomic_write(path, item):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(item, fp, indent=2, allow_nan=False)
            fp.flush()
            os.fsync(fp.fileno())
        _replace_journal(tmp, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _replace_journal(source, destination):
    """Retry transient Windows denial without ever truncating the old journal.

    Scanners/readers can temporarily prevent replacement on Windows. Error 5
    also covers permanent ACL failures, so retries are bounded and exhaustion
    propagates the original error. Nothing may submit an order until this returns.
    """
    for attempt in range(JOURNAL_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            if (
                not WINDOWS
                or getattr(error, "winerror", None) not in {5, 32, 33}
                or attempt == JOURNAL_REPLACE_ATTEMPTS - 1
            ):
                raise
            time.sleep(min(0.025 * 2**attempt, 0.2))


def _sync_directory(path):
    # Python cannot open/fsync directory handles on Windows. The journal itself
    # is flushed before atomic replacement on both platforms. Retain directory
    # fsync on POSIX and propagate real IO failures rather than hiding them.
    if WINDOWS:
        return
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def save_trade(rec):
    ensure_context()
    if rec["context"] != CONTEXT:
        raise ex.OwnershipError("Journal account mismatch")
    rec["updated_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(TRADES_DIR / (rec["id"] + ".json"), rec)


def load_trades():
    ensure_context()
    records = []
    for path in sorted(TRADES_DIR.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            if (
                rec["version"] != 3
                or rec["context"] != CONTEXT
                or rec["id"] != path.stem
                or rec["state"]
                not in {
                    "intent",
                    "watching_entry",
                    "buy_submitting",
                    "bought",
                    "protection_submitting",
                    "protected",
                    "closing",
                    "needs_attention",
                    "closed",
                }
            ):
                raise ValueError("Invalid journal schema/identity")
            for field in ("orders", "oco", "plan", "baseline_base"):
                if field not in rec:
                    raise ValueError("Incomplete journal")
            if D(rec["baseline_base"]) < 0:
                raise ValueError("Negative baseline balance")
            for event in rec["orders"]:
                if event["status"] not in TERMINAL | {
                    "NEW",
                    "PARTIALLY_FILLED",
                    "PENDING_NEW",
                    "UNKNOWN",
                    "PENDING_CANCEL",
                }:
                    raise ValueError("Unknown order status")
                executed = D(event["executedQty"])
                if not 0 <= executed <= D(event["qty"]) or event["side"] not in (
                    "BUY",
                    "SELL",
                ):
                    raise ValueError("Invalid execution quantity/side")
                if "trades" not in event:
                    raise ValueError("Missing execution evidence")
                if rec["state"] == "closed" and (
                    event["status"] not in TERMINAL or event.get("evidence_pending")
                ):
                    raise ValueError("Unresolved order in closed journal")
                seen = set()
                total = D(0)
                for row in event["trades"]:
                    if (
                        row["id"] in seen
                        or row["orderId"] != event["orderId"]
                        or row["symbol"] != rec["symbol"]
                        or row["isBuyer"] != (event["side"] == "BUY")
                        or int(row["time"]) <= 0
                        or D(row["qty"]) <= 0
                        or D(row["quoteQty"]) <= 0
                        or D(row["price"]) <= 0
                        or D(row["commission"]) < 0
                    ):
                        raise ValueError("Invalid execution evidence")
                    seen.add(row["id"])
                    total += D(row["qty"])
                if (
                    not event.get("evidence_pending")
                    and event["status"] != "UNKNOWN"
                    and total != executed
                ):
                    raise ValueError("Missing fills")
            if len({o["cid"] for o in rec["orders"]}) != len(rec["orders"]):
                raise ValueError("Duplicate order client IDs")
            if sum(o["purpose"] == "buy" for o in rec["orders"]) > 1:
                raise ValueError("Multiple buys in one journal")
            if rec["state"] == "closed" and not rec.get("accounted"):
                raise ValueError("Closed record missing execution accounting")
            if rec["state"] == "closed":
                if inventory(rec) != 0 or rec.get("realized_events") != realized_events(rec):
                    raise ValueError("Closed record does not reconcile")
            records.append(rec)
        except Exception:
            raise RuntimeError("Trade journal invalid; new trades blocked") from None
    return records


def active_trades():
    return [rec for rec in load_trades() if rec["state"] != "closed"]


def new_trade(plan, approval=None, entry_watch=None):
    ensure_context()
    rec = {
        "version": 3,
        "id": uuid.uuid4().hex,
        "context": dict(CONTEXT),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "state": "intent",
        "symbol": plan["symbol"],
        "plan": {k: str(v) for k, v in plan.items()},
        "baseline_base": fmt(plan["baseline_base"]),
        "orders": [],
        "oco": [],
        "accounted": False,
        "close_requested": False,
    }
    if entry_watch is not None:
        rec.update(state="watching_entry", entry_watch=entry_watch)
    if approval is not None:
        rec["approval_id"] = approval["approval_id"]
        rec["approved_bounds"] = {k: str(v) for k, v in approval.items()}
    save_trade(rec)
    return rec


def intent(rec, side, qty, purpose):
    item = {
        "cid": "bs3" + uuid.uuid4().hex[:26],
        "side": side,
        "purpose": purpose,
        "qty": fmt(qty),
        "status": "UNKNOWN",
        "executedQty": "0",
        "trades": [],
        "submitted": False,
        "orderId": None,
    }
    rec["orders"].append(item)
    save_trade(rec)
    return item


def symbol_filters(symbol):
    data = get_client().get_symbol_info(symbol)
    if (
        not data
        or data.get("symbol") != symbol
        or data.get("status") != "TRADING"
        or data.get("quoteAsset") != "USDT"
        or not data.get("isSpotTradingAllowed")
        or not data.get("ocoAllowed")
        or not {"LIMIT", "MARKET", "LIMIT_MAKER", "STOP_LOSS"} <= set(data.get("orderTypes", []))
    ):
        raise ValueError("Symbol does not support required Spot LIMIT/MARKET/OCO orders")
    raw = {f["filterType"]: f for f in data["filters"]}
    lot, price = raw["LOT_SIZE"], raw["PRICE_FILTER"]
    market = raw.get("MARKET_LOT_SIZE", lot)
    notional = raw.get("NOTIONAL", raw.get("MIN_NOTIONAL"))
    if not notional or D(notional["minNotional"]) <= 0:
        raise ValueError("Missing exchange notional constraints")
    return {
        "step": D(lot["stepSize"]),
        "tick": D(price["tickSize"]),
        "min_price": D(price["minPrice"]),
        "max_price": D(price["maxPrice"]),
        "min_qty": D(lot["minQty"]),
        "max_qty": D(lot["maxQty"]),
        "market_step": D(market["stepSize"]),
        "market_min_qty": D(market["minQty"]),
        "market_max_qty": D(market["maxQty"]),
        "min_notional": D(notional["minNotional"]),
        "max_notional": D(notional.get("maxNotional", "1e30")),
        "base": data["baseAsset"],
        "raw": raw,
    }


def validate_qty(qty, f, reference_price, *, market=False, entry=False):
    qty, price = D(qty), D(reference_price)
    if qty <= 0 or price <= 0:
        raise ValueError("Quantity and price must be positive")
    # LOT_SIZE always applies. Zero market constraints mean disabled, not zero maximum.
    for prefix in ("", "market_") if market else ("",):
        step = f[prefix + "step"] if prefix else f["step"]
        mn, mx = f[prefix + "min_qty"], f[prefix + "max_qty"]
        if (
            (mn > 0 and qty < mn)
            or (mx > 0 and qty > mx)
            or (step > 0 and round_to(qty, step) != qty)
        ):
            raise ValueError("Quantity violates exchange LOT_SIZE/MARKET_LOT_SIZE")
    margin = D("1.05") if entry else D("1")
    if not f["min_notional"] * margin <= qty * price <= f["max_notional"]:
        raise ValueError(
            "Configured size cannot satisfy required order minimums/fee buffer; skip trade"
        )


def commission_schedule(symbol):
    info = get_client().v3_get_account_commission(symbol=symbol)
    if info.get("symbol") != symbol:
        raise ValueError("Commission symbol mismatch")
    # Reserve undiscounted entry/stop taker fees and target maker fees, including
    # tax/special components. A BNB shortfall must not invalidate the reserve.
    discount = info.get("discount", {})
    enabled = discount.get("enabledForAccount") is True and discount.get("enabledForSymbol") is True
    if enabled and not ALLOW_BNB_FEES:
        raise ValueError("BNB-discount fees require ALLOW_BNB_FEES=true in this checkout's .env")
    multiplier = D(discount.get("discount", "1")) if enabled else D(1)
    if enabled and (discount.get("discountAsset") != "BNB" or not 0 <= multiplier <= 1):
        raise ValueError("Unsupported or invalid commission discount metadata")
    if enabled and symbol == "BNBUSDT":
        raise ValueError(
            "BNB fee payment on BNBUSDT needs a separate base-fee inventory reserve; skip trade"
        )
    rates, discounted = [], []
    for role, side in (("taker", "buyer"), ("taker", "seller"), ("maker", "seller")):
        components = [
            D(info[name][role]) + D(info[name][side])
            for name in ("standardCommission", "taxCommission", "specialCommission")
        ]
        if any(rate < 0 for rate in components):
            raise ValueError("Invalid negative commission schedule")
        rates.append(sum(components))
        discounted.append(components[0] * multiplier + sum(components[1:]))
    rate = max(rates)
    if not 0 <= rate < D(".05"):
        raise ValueError("Invalid/unsupported commission schedule")
    if enabled:
        reference = get_client().get_symbol_ticker(symbol="BNBUSDT")
        if reference.get("symbol") != "BNBUSDT" or D(reference["price"]) <= 0:
            raise ValueError("BNB fee conversion market unavailable")
    return {
        "reserve_rate": rate,
        "discounted_rate": max(discounted),
        "bnb_discount": enabled,
        "discount_multiplier": multiplier,
        "bnb_fee_support": ALLOW_BNB_FEES,
    }


def commission_rate(symbol):
    return commission_schedule(symbol)["reserve_rate"]


def validate_price(price, f, side):
    price = D(price)
    if not f["min_price"] <= price <= f["max_price"] or round_to(price, f["tick"]) != price:
        raise ValueError("Price violates PRICE_FILTER")
    # Dynamic percent-price filters use Binance weighted average, not last trade.
    avg_info = (
        get_client().get_avg_price(symbol=f["symbol"])
        if any(k in f["raw"] for k in ("PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE"))
        else None
    )
    avg = D(avg_info["price"]) if avg_info is not None else None
    if avg is not None:
        for name in ("PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE"):
            rule = f["raw"].get(name)
            if rule:
                if int(rule["avgPriceMins"]) != int(avg_info["mins"]):
                    raise ValueError("Unsupported dynamic price averaging window; skip trade")
                prefix = "ask" if side == "SELL" else "bid"
                lo = D(rule.get(prefix + "MultiplierDown", rule.get("multiplierDown")))
                hi = D(rule.get(prefix + "MultiplierUp", rule.get("multiplierUp")))
                if not avg * lo <= price <= avg * hi:
                    raise ValueError("Price violates dynamic percent-price filter")


def preflight(plan, f, *, open_orders=None):
    f["symbol"] = plan["symbol"]
    for key in ("stop", "t1", "t2"):
        validate_price(plan[key], f, "SELL")
    price = D(plan["price"])
    validate_price(round_to(price, f["tick"]), f, "BUY")
    if not D(plan["stop"]) < price < D(plan["t1"]) < D(plan["t2"]):
        raise ValueError("Rounded protective levels invalid")
    qty = D(plan["qty"])
    entry_buffer = plan.get("manual_override") is not True
    validate_qty(qty, f, price, market=True, entry=entry_buffer)
    # Reserve worst-side base commission before sizing the two protective lists.
    net = round_to(qty * (1 - D(plan["fee_rate"])), f["step"])
    half = round_to(net / 2, f["step"])
    for part, target in ((half, plan["t1"]), (net - half, plan["t2"])):
        validate_qty(part, f, plan["stop"], entry=entry_buffer)
        validate_qty(part, f, target, entry=entry_buffer)
    cap = D(settings.read()["max_position_usdt"])
    if qty * price * (1 + D(plan["fee_rate"])) > cap:
        raise ValueError(f"Position including commission exceeds configured {cap} USDT cap")
    opened = get_client().get_open_orders() if open_orders is None else open_orders

    def usage(rows):
        return (
            len(rows),
            sum(
                row["type"] in {"STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"}
                for row in rows
            ),
            len({row["orderListId"] for row in rows if row.get("orderListId", -1) >= 0}),
        )

    symbol_usage = usage([row for row in opened if row["symbol"] == plan["symbol"]])
    exchange_usage = usage(opened)
    # Check symbol order/list capacity before buying; unknown exotic constraints fail closed.
    supported = {
        "PRICE_FILTER",
        "LOT_SIZE",
        "MARKET_LOT_SIZE",
        "MIN_NOTIONAL",
        "NOTIONAL",
        "PERCENT_PRICE",
        "PERCENT_PRICE_BY_SIDE",
        "ICEBERG_PARTS",
        "MAX_NUM_ORDERS",
        "MAX_NUM_ALGO_ORDERS",
        "MAX_NUM_ORDER_LISTS",
        "MAX_POSITION",
        "TRAILING_DELTA",
        "MAX_NUM_ICEBERG_ORDERS",
        "MAX_NUM_ORDER_AMENDS",
    }
    if set(f["raw"]) - supported:
        raise ValueError("Unsupported exchange filter; skip trade")
    for index, (name, field, needed) in enumerate(
        [
            ("MAX_NUM_ORDERS", "maxNumOrders", 4),
            ("MAX_NUM_ALGO_ORDERS", "maxNumAlgoOrders", 2),
            ("MAX_NUM_ORDER_LISTS", "maxNumOrderLists", 2),
        ]
    ):
        rule = f["raw"].get(name)
        if rule and int(rule[field]) and int(rule[field]) < needed + symbol_usage[index]:
            raise ValueError("Insufficient symbol capacity for two protective OCOs")
    exchange_info = get_client().get_exchange_info()
    for rule in exchange_info.get("exchangeFilters", []):
        limits = {
            "EXCHANGE_MAX_NUM_ORDERS": ("maxNumOrders", 4),
            "EXCHANGE_MAX_NUM_ALGO_ORDERS": ("maxNumAlgoOrders", 2),
            "EXCHANGE_MAX_NUM_ORDER_LISTS": ("maxNumOrderLists", 2),
        }
        if rule["filterType"] not in limits:
            raise ValueError("Unsupported exchange-wide filter")
        field, needed = limits[rule["filterType"]]
        index = list(limits).index(rule["filterType"])
        if int(rule[field]) and int(rule[field]) < needed + exchange_usage[index]:
            raise ValueError("Insufficient exchange-wide protective order capacity")
    limit = f["raw"].get("MAX_POSITION")
    if limit and D(plan["baseline_base"]) + qty > D(limit["maxPosition"]):
        raise ValueError("Exchange MAX_POSITION exceeded")


def trades_for(order, symbol):
    rows = get_client().get_my_trades(symbol=symbol, orderId=order["orderId"], limit=1000)
    if len(rows) >= 1000:
        raise ex.Uncertain(
            "Execution pagination requires manual reconciliation",
            reason_code="EXECUTION_HISTORY_TRUNCATED",
        )
    seen = set()
    total = D(0)
    cleaned = []
    for row in rows:
        if (
            row["orderId"] != order["orderId"]
            or row["symbol"] != symbol
            or bool(row["isBuyer"]) != (order["side"] == "BUY")
            or row["id"] in seen
            or int(row["time"]) <= 0
        ):
            raise ex.OwnershipError("Execution identity mismatch")
        seen.add(row["id"])
        qty, quote, fee = D(row["qty"]), D(row["quoteQty"]), D(row["commission"])
        if qty <= 0 or quote <= 0 or fee < 0:
            raise ex.Uncertain("Invalid execution evidence")
        total += qty
        cleaned.append(
            {
                k: row[k]
                for k in (
                    "id",
                    "orderId",
                    "symbol",
                    "isBuyer",
                    "time",
                    "qty",
                    "quoteQty",
                    "price",
                    "commission",
                    "commissionAsset",
                )
            }
        )
    if sum((D(row["quoteQty"]) for row in cleaned), D(0)) != D(order["cummulativeQuoteQty"]):
        raise ex.Uncertain(
            "Execution quote totals do not reconcile", reason_code="EXECUTION_TOTALS_MISMATCH"
        )
    if total != D(order["executedQty"]):
        raise ex.Uncertain(
            "Execution history incomplete; no liquidation authorized",
            reason_code="EXECUTION_HISTORY_INCOMPLETE",
        )
    return cleaned


def refresh_order(rec, item):
    if item.get("local_rejection"):
        return item
    result = ex.order(
        get_client(),
        rec["symbol"],
        item["cid"],
        item["side"],
        item.get("orderId"),
        [item["cancel_cid"]] if item.get("cancel_cid") else [],
    )
    if D(result["origQty"]) != D(item["qty"]) or (
        item["orderId"] is not None and item["orderId"] != result["orderId"]
    ):
        raise ex.OwnershipError("Order quantity/ID mismatch")
    if result["status"] not in TERMINAL | {
        "NEW",
        "PARTIALLY_FILLED",
        "PENDING_NEW",
        "PENDING_CANCEL",
    }:
        raise ex.Uncertain("Unsupported order state", reason_code="ORDER_STATE_UNSUPPORTED")
    expected_type = {
        "buy": "LIMIT",
        "target": "LIMIT_MAKER",
        "stop": "STOP_LOSS",
        "emergency": "MARKET",
    }[item["purpose"]]
    if result["type"] != expected_type:
        raise ex.OwnershipError("Order type mismatch")
    if item["purpose"] in ("target", "stop"):
        parent = next(o for o in rec["oco"] if item["cid"] in o["order_cids"])
        field = "price" if item["purpose"] == "target" else "stopPrice"
        level = parent["tp"] if item["purpose"] == "target" else parent["stop"]
        if result.get("orderListId") != parent["orderListId"] or D(result[field]) != D(level):
            raise ex.OwnershipError("Protective order list/level mismatch")
    if item["side"] == "BUY" and (
        result.get("type") != "LIMIT"
        or result.get("timeInForce") != "IOC"
        or D(result["price"]) > D(item["price_cap"])
    ):
        raise ex.OwnershipError("Buy does not match approved IOC price ceiling")
    # Persist known acquired quantity even if fill/fee history is temporarily unavailable.
    item.update(
        status=result["status"],
        executedQty=fmt(result["executedQty"]),
        orderId=result["orderId"],
        evidence_pending=True,
    )
    save_trade(rec)
    rows = trades_for(result, rec["symbol"])
    # Retain already-recorded historical reference evidence across read retries.
    previous = {row["id"]: row for row in item["trades"]}
    for row in rows:
        old = previous.get(row["id"], {})
        if old.get("fee_valuation") and all(old.get(key) == value for key, value in row.items()):
            row["fee_valuation"] = old["fee_valuation"]
    item.update(trades=rows, evidence_pending=False)
    save_trade(rec)
    return item


def submit_order(rec, item):
    require_trade_permission()
    if item["submitted"]:
        return refresh_order(rec, item)  # never resubmit an ambiguous request
    initialize()  # Reverify authenticated UID before a new submission.
    if item["side"] == "BUY":
        if STOP.is_set():
            raise ValueError("Shutdown in progress; no entry submitted")
        bounds = rec.get("approved_bounds", {})
        deadline = D(bounds.get("expires_at", 0))
        if rec.get("entry_watch"):
            from trade_assistant import watch_deadline

            deadline = watch_deadline(rec)
            if rec["entry_watch"].get("active") or rec["entry_watch"].get("result") != "triggered":
                raise ValueError("Watch not authorized to submit")
        if not rec.get("approval_id") or D(time.time()) + 15 >= deadline:
            raise ValueError("Approval expired during validation; no entry submitted")
        if D(item["qty"]) > D(bounds["qty"]) or D(item["price_cap"]) > D(bounds["price"]):
            raise ValueError("Buy would exceed approved quantity or price ceiling")
        if rec["plan"].get("entry_mode") in ("now", "watch"):
            from trade_assistant import entry_quote

            bid, ask = entry_quote(rec["symbol"])
            last = D(get_client().get_symbol_ticker(symbol=rec["symbol"])["price"])
            if min(bid, last) <= D(rec["plan"]["stop"]) or ask > D(item["price_cap"]):
                raise ValueError("Entry quote invalidated before submission; no buy submitted")
        if D(item["qty"]) * D(item["price_cap"]) * (1 + D(rec["plan"]["fee_rate"])) > D(
            bounds["cost"]
        ):
            raise ValueError("Buy would exceed approved cost ceiling")
    if item["side"] == "BUY" and (STOP.is_set() or D(time.time()) + 15 >= deadline):
        raise ValueError("Entry authorization expired or shutdown requested; no buy submitted")
    item["submitted"] = True
    save_trade(rec)
    try:
        if item["side"] == "BUY":
            get_client().create_order(
                symbol=rec["symbol"],
                side="BUY",
                type="LIMIT",
                timeInForce="IOC",
                price=item["price_cap"],
                quantity=item["qty"],
                newClientOrderId=item["cid"],
                newOrderRespType="FULL",
            )
        else:
            get_client().order_market_sell(
                symbol=rec["symbol"],
                quantity=item["qty"],
                newClientOrderId=item["cid"],
                newOrderRespType="FULL",
            )
    except Exception as error:
        if ex.definitely_rejected(error):
            item.update(status="REJECTED", local_rejection=True, rejection=safety.safe_error(error))
            save_trade(rec)
            return item
        # Lookup even when the write timed out; query failures remain uncertain.
    return refresh_order(rec, item)


def executions(rec, side=None):
    for order in rec["orders"]:
        if order.get("evidence_pending") or (
            not order.get("local_rejection") and order["status"] == "UNKNOWN"
        ):
            raise ex.Uncertain("Order execution evidence uncertain")
        if side is None or side == order["side"]:
            for row in order["trades"]:
                yield row


def inventory(rec):
    base = rec["symbol"][:-4]
    acquired = sum(
        (
            D(t["qty"]) - (D(t["commission"]) if t["commissionAsset"] == base else 0)
            for t in executions(rec, "BUY")
        ),
        D(0),
    )
    sold = sum(
        (
            D(t["qty"]) + (D(t["commission"]) if t["commissionAsset"] == base else 0)
            for t in executions(rec, "SELL")
        ),
        D(0),
    )
    if sold > acquired or acquired < 0:
        raise ex.OwnershipError("Sell executions exceed bot-owned inventory")
    return acquired - sold


def refresh_lists(rec):
    for item in rec["oco"]:
        if item.get("local_rejection"):
            continue
        result = ex.order_list(
            get_client(),
            rec["symbol"],
            item["cid"],
            item.get("orderListId"),
            [item["cancel_cid"]] if item.get("cancel_cid") else [],
        )
        if item.get("orderListId") is not None and item["orderListId"] != result["orderListId"]:
            raise ex.OwnershipError("OCO numeric ID changed")
        reports = result["orders"]
        if len(reports) != 2 or {r["clientOrderId"] for r in reports} != set(item["order_cids"]):
            raise ex.OwnershipError("OCO leg identity mismatch")
        item["orderListId"] = result["orderListId"]
        if result["listOrderStatus"] not in ("EXECUTING", "ALL_DONE"):
            raise ex.Uncertain("Unsupported OCO list state", reason_code="ORDER_STATE_UNSUPPORTED")
        item["list_status"] = result["listOrderStatus"]
        for report in reports:
            order = next((o for o in rec["orders"] if o["cid"] == report["clientOrderId"]), None)
            if order is None:
                raise ex.OwnershipError("Missing persisted OCO leg")
            refresh_order(rec, order)
        save_trade(rec)


def coverage(rec):
    qty = D(0)
    for item in rec["oco"]:
        if item.get("local_rejection"):
            continue
        legs = [o for o in rec["orders"] if o["cid"] in item["order_cids"]]
        if len(legs) != 2 or any(
            o["status"] == "UNKNOWN" or o.get("evidence_pending") for o in legs
        ):
            raise ex.Uncertain("OCO coverage unknown")
        remaining = D(item["qty"]) - sum((D(o["executedQty"]) for o in legs), D(0))
        stop_leg = next(o for o in legs if o["purpose"] == "stop")
        if stop_leg["status"] in {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}:
            qty += max(D(0), remaining)
        # Partially executing TP cancels its stop sibling: residual is uncovered.
    return qty


def place_oco(symbol, qty, tp, stop, tick, f=None, client_id=None, order_cids=None):
    try:
        f = f or symbol_filters(symbol)
        tp, stop = round_to(tp, tick), round_to(stop, tick)
        validate_qty(qty, f, stop)
        live = D(get_client().get_symbol_ticker(symbol=symbol)["price"])
        if not 0 < stop < live < tp:
            raise ValueError("Market moved outside protective levels")
    except Exception as error:
        raise ex.NotSubmitted(safety.safe_error(error)) from None
    return get_client().create_oco_order(
        symbol=symbol,
        side="SELL",
        quantity=fmt(qty),
        aboveType="LIMIT_MAKER",
        abovePrice=fmt(tp),
        belowType="STOP_LOSS",
        belowStopPrice=fmt(stop),
        listClientOrderId=client_id,
        aboveClientOrderId=order_cids[0],
        belowClientOrderId=order_cids[1],
    )


def submit_protection(rec, qty, t1, t2, stop, f):
    require_trade_permission()
    initialize()
    qty = round_to(qty, f["step"])
    half = round_to(qty / 2, f["step"])
    for part, tp in ((half, t1), (qty - half, t2)):
        validate_qty(part, f, stop)
        validate_qty(part, f, tp)
    for part, tp in ((half, t1), (qty - half, t2)):
        top, bottom = (
            intent(rec, "SELL", part, "target"),
            intent(rec, "SELL", part, "stop"),
        )
        item = {
            "cid": "bs3o" + uuid.uuid4().hex[:25],
            "qty": fmt(part),
            "tp": fmt(tp),
            "stop": fmt(stop),
            "order_cids": [top["cid"], bottom["cid"]],
            "orderListId": None,
            "submitted": True,
        }
        # Atomically persist both leg IDs and list ID before transmission.
        rec["oco"].append(item)
        top["submitted"] = bottom["submitted"] = True
        rec["state"] = "protection_submitting"
        save_trade(rec)
        try:
            place_oco(
                rec["symbol"],
                part,
                tp,
                stop,
                f["tick"],
                f,
                item["cid"],
                item["order_cids"],
            )
        except Exception as error:
            if isinstance(error, ex.NotSubmitted) or ex.definitely_rejected(error):
                item["local_rejection"] = True
                for order in (top, bottom):
                    order.update(status="REJECTED", local_rejection=True)
                save_trade(rec)
            else:
                # Unknown acceptance must be resolved before ANY liquidation.
                refresh_lists(rec)
                raise ex.Uncertain(
                    "OCO submission interrupted; reconciled before next action"
                ) from None
            raise RuntimeError("OCO rejected; remaining inventory requires safe recovery") from None
        refresh_lists(rec)
    rec["state"] = "protected"
    save_trade(rec)


def refresh(rec):
    initialize()
    if rec["context"] != CONTEXT:
        raise ex.OwnershipError("Account mismatch")
    for item in rec["orders"]:
        if item["purpose"] not in ("target", "stop"):
            refresh_order(rec, item)
    refresh_lists(rec)
    # Orphaned intentions from a crash while preparing an OCO were never submitted.
    listed = {cid for item in rec["oco"] for cid in item["order_cids"]}
    for item in rec["orders"]:
        if item["purpose"] in ("target", "stop") and item["cid"] not in listed:
            if item["submitted"]:
                raise ex.Uncertain("Orphaned submitted OCO leg")
            item.update(status="REJECTED", local_rejection=True)
    save_trade(rec)


def valid_bnb_candle_reference(reference, fill_time):
    """Pure replay validation of persisted public-candle evidence."""
    try:
        start = fill_time // 60000 * 60000
        opened, high, low, close, volume, quote_volume = (
            D(reference[key]) for key in ("open", "high", "low", "close", "volume", "quote_volume")
        )
        return (
            reference.get("interval") == "1m"
            and type(reference.get("open_time")) is int
            and reference["open_time"] == start
            and type(reference.get("close_time")) is int
            and reference["close_time"] == start + 59999
            and type(reference.get("observed_server_time")) is int
            and reference["observed_server_time"] > reference["close_time"]
            and type(reference.get("trades")) is int
            and reference["trades"] > 0
            and 0 < low <= min(opened, close) <= max(opened, close) <= high
            and volume > 0
            and quote_volume > 0
            and D(reference["price"]) == high
        )
    except (KeyError, TypeError, ValueError):
        return False


def bnb_candle_fee_reference(fill_time):
    """Closed containing-minute high: conservative traded-price reference, not an exact fee conversion."""
    start = fill_time // 60000 * 60000
    end = start + 59999
    client = get_client()
    now = client.get_server_time().get("serverTime")
    if type(now) is not int or now <= 0:
        raise ex.Uncertain(
            "Invalid public server time for fee reference", reason_code="BNB_CANDLE_INVALID"
        )
    if now <= end:
        raise ex.Uncertain(
            "BNB fee reference history missing; containing minute not closed",
            reason_code="BNB_CANDLE_NOT_CLOSED",
        )
    rows = client.get_klines(symbol="BNBUSDT", interval="1m", startTime=start, endTime=end, limit=1)
    if rows == []:
        raise ex.Uncertain(
            "BNB fee reference history missing in both sources", reason_code="BNB_REFERENCE_MISSING"
        )
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], (list, tuple))
        or len(rows[0]) < 9
    ):
        raise ex.Uncertain(
            "Invalid BNB historical candle response", reason_code="BNB_CANDLE_INVALID"
        )
    row = rows[0]
    reference = {
        "method": "closed_1m_high_at_fill",
        "symbol": "BNBUSDT",
        "interval": "1m",
        "fill_time": fill_time,
        "open_time": row[0],
        "close_time": row[6],
        "observed_server_time": now,
        "open": row[1],
        "high": row[2],
        "low": row[3],
        "close": row[4],
        "volume": row[5],
        "quote_volume": row[7],
        "trades": row[8],
        "price": row[2],
    }
    if not valid_bnb_candle_reference(reference, fill_time):
        raise ex.Uncertain(
            "Invalid BNB historical candle evidence", reason_code="BNB_CANDLE_INVALID"
        )
    return reference


def bnb_fee_reference(fill_time):
    """Documented valuation reference, not Binance's private conversion price."""
    rows = get_client().get_aggregate_trades(
        symbol="BNBUSDT", startTime=fill_time - 1000, endTime=fill_time, limit=1000
    )
    if not isinstance(rows, list):
        raise ex.Uncertain(
            "Invalid BNB fee reference response", reason_code="BNB_REFERENCE_INVALID"
        )
    if not rows:
        return bnb_candle_fee_reference(fill_time)
    if len(rows) >= 1000:
        raise ex.Uncertain(
            "BNB fee reference history truncated", reason_code="BNB_REFERENCE_TRUNCATED"
        )
    seen = set()
    for row in rows:
        if (
            type(row.get("a")) is not int
            or row["a"] in seen
            or type(row.get("T")) is not int
            or not fill_time - 1000 <= row["T"] <= fill_time
            or D(row["p"]) <= 0
        ):
            raise ex.Uncertain(
                "Invalid BNB fee reference history", reason_code="BNB_REFERENCE_INVALID"
            )
        seen.add(row["a"])
    last = max(rows, key=lambda row: (row["T"], row["a"]))
    return {
        "method": "last_aggregate_trade_at_or_before_fill",
        "symbol": "BNBUSDT",
        "aggregate_id": last["a"],
        "time": last["T"],
        "fill_time": fill_time,
        "price": fmt(last["p"]),
    }


def attach_fee_valuations(rec):
    """After exits, persist reference evidence; missing USD valuation never delays protection."""
    references = {}
    for order in rec["orders"]:
        for row in order["trades"]:
            if (
                not D(row["commission"])
                or row["commissionAsset"] in ("USDT", rec["symbol"][:-4])
                or row.get("fee_valuation")
            ):
                continue
            if row["commissionAsset"] != "BNB" or not (
                ALLOW_BNB_FEES or rec["plan"].get("bnb_fee_support") == "True"
            ):
                raise ex.Uncertain(
                    "Third-asset commission needs enabled, supported USDT valuation",
                    reason_code="FEE_ASSET_UNSUPPORTED",
                )
            timestamp = int(row["time"])
            if timestamp not in references:
                references[timestamp] = bnb_fee_reference(timestamp)
            row["fee_valuation"] = dict(references[timestamp])
            save_trade(rec)


def fee_in_quote(row, base):
    fee = D(row["commission"])
    if not fee or row["commissionAsset"] == base:
        return D(0)  # Base commissions are accounted in inventory and cost allocation.
    if row["commissionAsset"] == "USDT":
        return fee
    reference = row.get("fee_valuation", {})
    valid = (
        row["commissionAsset"] == "BNB"
        and isinstance(reference, dict)
        and reference.get("symbol") == "BNBUSDT"
        and reference.get("fill_time") == int(row["time"])
        and D(reference.get("price", 0)) > 0
    )
    if valid:
        if reference.get("method") == "last_aggregate_trade_at_or_before_fill":
            valid = (
                type(reference.get("aggregate_id")) is int
                and type(reference.get("time")) is int
                and int(row["time"]) - 1000 <= reference["time"] <= int(row["time"])
            )
        elif reference.get("method") == "closed_1m_high_at_fill":
            valid = valid_bnb_candle_reference(reference, int(row["time"]))
        else:
            valid = False
    if not valid:
        raise ex.Uncertain(
            "Third-asset commission reference missing or invalid",
            reason_code="FEE_VALUATION_PENDING",
        )
    return fee * D(reference["price"])


def realized_events(rec):
    buys = list(executions(rec, "BUY"))
    base = rec["symbol"][:-4]
    bought = sum(
        (D(t["qty"]) - (D(t["commission"]) if t["commissionAsset"] == base else 0) for t in buys),
        D(0),
    )
    cost = D(0)
    for t in buys:
        cost += D(t["quoteQty"]) + fee_in_quote(t, base)
    events = []
    for t in executions(rec, "SELL"):
        if bought <= 0:
            raise ex.OwnershipError("No verified buy cost basis")
        units = D(t["qty"]) + (D(t["commission"]) if t["commissionAsset"] == base else 0)
        pnl = D(t["quoteQty"]) - fee_in_quote(t, base) - cost * units / bought
        events.append({"id": t["id"], "time": t["time"], "pnl": fmt(pnl)})
    return events


def todays_loss(*, watch_id=None):
    today = datetime.now(timezone.utc).date()
    daily = D(0)
    for rec in load_trades():
        if watch_id is not None:
            from trade_assistant import empty_watch

            if empty_watch(rec, watch_id):
                continue
        if rec["state"] != "closed":
            from trade_assistant import empty_watch

            if empty_watch(rec, rec["id"]):
                continue
            if rec["state"] != "protected" or not 0 < inventory(rec) <= coverage(rec):
                raise ValueError("Open trade requires protection/accounting reconciliation")
            if not any(executions(rec, "SELL")):
                continue
        # Recompute from exchange evidence, never trust missing/estimated P&L.
        for event in realized_events(rec):
            if datetime.fromtimestamp(int(event["time"]) / 1000, timezone.utc).date() == today:
                daily += D(event["pnl"])
    return max(D(0), -daily)


def finish_if_flat(rec, f):
    left = inventory(rec)
    if any(o["status"] not in TERMINAL for o in rec["orders"]):
        return False
    # Dust is retained, explicitly recorded and blocks new entries until reconciled.
    if left != 0:
        rec["residual_qty"] = fmt(left)
        return False
    attach_fee_valuations(rec)
    rec["realized_events"] = realized_events(rec)
    references = [
        row["fee_valuation"]
        for order in rec["orders"]
        for row in order["trades"]
        if row.get("fee_valuation")
    ]
    if any(ref["method"] == "closed_1m_high_at_fill" for ref in references):
        rec["pnl_valuation"] = (
            "Actual BNB commission quantities; USDT reference valuation includes closed fill-minute highs (conservative, not Binance's private conversion rate)"
        )
    elif references:
        rec["pnl_valuation"] = (
            "Actual BNB commissions valued at preceding BNBUSDT trade reference (within 1 second)"
        )
    rec.update(state="closed", accounted=True, residual_qty="0")
    if rec["realized_events"]:
        rec["result_notice_due"] = True
    save_trade(rec)
    return True


def emergency_sell_free(symbol, max_qty=None, f=None, *, rec=None):
    """Sell only verified unprotected bot inventory above pre-buy nonbot balance."""
    require_trade_permission()
    if rec is None:
        raise ex.OwnershipError("Durable trade ownership record required")
    refresh(rec)
    if any(
        o["status"] not in TERMINAL for o in rec["orders"] if o["purpose"] in ("buy", "emergency")
    ):
        raise ex.Uncertain("Buy or emergency exit remains open/uncertain")
    f = f or symbol_filters(symbol)
    left, protected = inventory(rec), coverage(rec)
    free = D(get_client().get_asset_balance(asset=f["base"])["free"])
    qty = round_to(
        min(max(D(0), left - protected), max(D(0), free - D(rec["baseline_base"]))),
        f["step"],
    )
    if max_qty is not None:
        qty = min(qty, D(max_qty))
    if qty <= 0:
        if 0 < left - protected < f["step"]:
            raise ValueError(
                "Residual below exchange lot-size minimums; manual reconciliation required"
            )
        raise ex.Uncertain("No verified free bot inventory; unrelated balance preserved")
    price = D(get_client().get_symbol_ticker(symbol=symbol)["price"])
    validate_qty(qty, f, price, market=True)
    item = intent(rec, "SELL", qty, "emergency")
    item["exit_reason"] = "manual_close" if rec.get("close_requested") else "unprotected_inventory"
    save_trade(rec)
    submit_order(rec, item)
    if item["status"] not in TERMINAL or item["status"] == "REJECTED":
        raise ex.Uncertain("Emergency exit not confirmed terminal/filled")
    log.info("Bot-owned market exit reconciled; checking remaining inventory")
    return item


def cancel_protection(rec, item):
    item["cancel_requested"] = True
    item.setdefault("cancel_cid", "bs3c" + uuid.uuid4().hex[:25])
    save_trade(rec)
    require_trade_permission()
    ex.cancel_list(
        get_client(),
        rec["symbol"],
        item["cid"],
        item["orderListId"],
        item["cancel_cid"],
    )
    refresh_lists(rec)


def reconcile_trade(rec):
    finished = _reconcile_trade(rec)
    try:
        if not getattr(LOCK_STATE, "active", False):
            notify_trade_result(rec)
    except Exception as error:
        # Notification failures must never undo closure or block safety recovery.
        log.error("Result notification pending: %s", safety.safe_error(error))
    return finished


def _reconcile_trade(rec):
    """One idempotent recovery step; never creates a buy or duplicates an exit."""
    with operation_lock():
        latest = next(r for r in load_trades() if r["id"] == rec["id"])
        rec.clear()
        rec.update(latest)
        if rec["state"] == "closed":
            return True
        if rec.get("entry_watch", {}).get("active") is True:
            from trade_assistant import advance_watch

            return advance_watch(rec)
        refresh(rec)
        buys = [o for o in rec["orders"] if o["purpose"] == "buy"]
        f = symbol_filters(rec["symbol"])
        if not buys:
            # No transmission was possible without the durable buy intent.
            rec.update(state="closed", accounted=True, realized_events=[])
            save_trade(rec)
            return True
        buy = buys[0]
        if buy["status"] not in TERMINAL:
            rec["cancel_buy_requested"] = True
            buy.setdefault("cancel_cid", "bs3c" + uuid.uuid4().hex[:25])
            save_trade(rec)
            require_trade_permission()
            get_client().cancel_order(
                symbol=rec["symbol"],
                orderId=buy["orderId"],
                newClientOrderId=buy["cancel_cid"],
            )
            refresh_order(rec, buy)
            if buy["status"] not in TERMINAL:
                raise ex.Uncertain("Buy cancellation not terminal")
        if finish_if_flat(rec, f):
            return True
        # A partially filled maker target can leave a locked remainder after its
        # stop sibling is canceled. Release only that verified uncovered list.
        if rec["close_requested"] or inventory(rec) > coverage(rec):
            for item in rec["oco"]:
                if not item.get("local_rejection"):
                    legs = [o for o in rec["orders"] if o["cid"] in item["order_cids"]]
                    stop_leg = next(o for o in legs if o["purpose"] == "stop")
                    uncovered = stop_leg["status"] in TERMINAL
                    if any(o["status"] not in TERMINAL for o in legs) and (
                        rec["close_requested"] or uncovered
                    ):
                        cancel_protection(rec, item)
                        # Unwind released ownership before removing another stop.
                        if inventory(rec) > coverage(rec):
                            emergency_sell_free(rec["symbol"], f=f, rec=rec)
        unprotected = inventory(rec) - coverage(rec)
        if unprotected > 0:
            # Recovery unwinds uncovered confirmed inventory, never re-buys/re-proposes.
            emergency_sell_free(rec["symbol"], f=f, rec=rec)
        if finish_if_flat(rec, f):
            return True
        rec["state"] = "protected" if coverage(rec) >= inventory(rec) else "needs_attention"
        save_trade(rec)
        return False


def trade_result_text(rec):
    sells = list(executions(rec, "SELL"))
    if not sells:
        return None
    left = inventory(rec)
    final = rec["state"] == "closed" and rec.get("accounted") and left == 0
    title = "TRADE CLOSED" if final else "PARTIAL EXIT — position still open"
    lines = [title, f"Ticker: {rec['symbol']} | Trade: {rec['id'][:8]}"]
    events = []
    for order in rec["orders"]:
        if order["side"] != "SELL" or D(order["executedQty"]) <= 0 or not order.get("trades"):
            continue
        complete = order["status"] == "FILLED"
        phrase = "executed successfully" if complete else "partially filled"
        linked = next(
            (
                index
                for index, item in enumerate(rec["oco"], 1)
                if order["cid"] in item["order_cids"]
            ),
            None,
        )
        if order["purpose"] == "target":
            name = f"Take Profit-{linked}" if linked is not None else "Take profit"
            event = f"{name} {phrase}."
        elif order["purpose"] == "stop":
            event = f"Stop loss {phrase}."
        elif order["purpose"] == "emergency":
            reason = order.get("exit_reason")
            if reason == "manual_close" or (reason is None and rec.get("close_requested")):
                event = f"Position close {phrase}."
            elif reason == "unprotected_inventory":
                event = f"Emergency exit {phrase} — unprotected bot quantity."
            else:
                event = f"Recovery sale {phrase}."
        else:
            event = f"Sell {phrase}."
        if event not in events:
            events.append(event)
    lines += events
    try:
        pnl = sum((D(e["pnl"]) for e in realized_events(rec)), D(0))
        cost = sum(
            (
                D(t["quoteQty"]) + fee_in_quote(t, rec["symbol"][:-4])
                for t in executions(rec, "BUY")
            ),
            D(0),
        )
        if cost <= 0:
            raise ValueError("Missing verified entry cost")
        lines.append(
            f"Net {'result' if final else 'realized so far'}: {pnl:+.4f} USDT ({pnl / cost * 100:+.2f}% of entry cost)"
        )
        candle_valued = any(
            t.get("fee_valuation", {}).get("method") == "closed_1m_high_at_fill"
            for t in sells + list(executions(rec, "BUY"))
        )
        if candle_valued:
            lines.append(
                "BNB fees valued at closed fill-minute highs; P&L is reference-valued, not an exact Binance conversion."
            )
        else:
            lines.append(
                "After recorded commissions."
                + (
                    " BNB fees use fill-time USDT reference values."
                    if rec.get("pnl_valuation")
                    or any(
                        t["commissionAsset"] == "BNB" for t in sells + list(executions(rec, "BUY"))
                    )
                    else ""
                )
            )
    except (ValueError, ex.Uncertain):
        if final:
            raise
        lines.append("Net result pending verified fee valuation; no estimated profit reported.")
    lines.append(f"Remaining bot quantity: {fmt(left)}")
    if not final:
        lines.append("Not a final result. Use /status for stop coverage.")
    return "\n".join(lines)


def notify_trade_result(rec):
    """Durable, at-least-once result delivery, isolated from order recovery."""
    if getattr(LOCK_STATE, "active", False):
        return  # Never invert the account-lock / notification-lock acquisition order.
    with NOTICE_LOCK:
        with operation_lock():
            latest = next(r for r in load_trades() if r["id"] == rec["id"])
            text = trade_result_text(latest)
            if text is None:
                return
            key = (
                "final"
                if latest["state"] == "closed"
                else "fills:" + ",".join(str(t["id"]) for t in executions(latest, "SELL"))
            )
            notices = latest.setdefault("result_notices", {})
            if key not in notices:
                # Only the newest execution snapshot should remain pending.
                for notice in notices.values():
                    notice["superseded"] = True
                notices[key] = {"text": text, "sent": False}
                save_trade(latest)
            notice = notices[key]
            if notice.get("sent"):
                return
            if notice["text"] != text:
                notice["text"] = (
                    text  # Refresh pending pre-update presentation, not execution evidence.
                )
                save_trade(latest)
            message = notice["text"]
        if alert(message) is True:
            with operation_lock():
                latest = next(r for r in load_trades() if r["id"] == rec["id"])
                latest["result_notices"][key]["sent"] = True
                if key == "final":
                    latest["result_notice_due"] = False
                save_trade(latest)


def retry_result_notices():
    # Read journal evidence only: this must never submit or cancel exchange orders.
    for rec in load_trades():
        if rec.get("result_notice_due") or any(
            not n.get("sent") and not n.get("superseded")
            for n in rec.get("result_notices", {}).values()
        ):
            try:
                notify_trade_result(rec)
            except Exception as error:
                log.error("Result notification pending: %s", safety.safe_error(error))


def result_notice_loop():
    while not STOP.is_set():
        try:
            retry_result_notices()
        except Exception as error:
            log.error("Result notification retry pending: %s", safety.safe_error(error))
        STOP.wait(30)


def mark_attention(rec, error=None):
    with operation_lock():
        latest = next(r for r in load_trades() if r["id"] == rec["id"])
        rec.clear()
        rec.update(latest)
        if rec.get("entry_watch", {}).get("active") and not rec["orders"] and not rec["oco"]:
            from trade_assistant import close_watch

            close_watch(rec, "monitor interrupted; fresh approval required")
            return
        if rec["state"] != "closed":
            rec["state"] = "needs_attention"
            if error is not None:
                code = error.reason_code if isinstance(error, safety.RecoveryError) else None
                rec["recovery_reason_code"] = code or "RECONCILIATION_PENDING"
            save_trade(rec)


def monitor_trade(rec, max_checks=None):
    failures = checks = 0
    while not STOP.is_set():
        try:
            if reconcile_trade(rec):
                return
            failures = 0
        except Exception as error:
            failures += 1
            try:
                mark_attention(rec, error)
            except Exception:
                log.error("Journal update failed; existing evidence preserved and entries blocked")
            if failures == 1 or failures % 12 == 0:
                alert(
                    safety.safe_error(error).rstrip(".")
                    + ". New entries blocked; monitor will retry reconciliation."
                )
        checks += 1
        if max_checks is not None and checks >= max_checks:
            return
        STOP.wait(
            min(
                60,
                (2 if rec.get("entry_watch", {}).get("active") else POLL_SECONDS)
                * 2 ** min(failures, 4),
            )
        )


def recover_trades():
    records = active_trades()
    for rec in records:
        try:
            with operation_lock():
                latest = next(r for r in load_trades() if r["id"] == rec["id"])
                rec.clear()
                rec.update(latest)
                if (
                    rec.get("entry_watch", {}).get("active")
                    and not rec["orders"]
                    and not rec["oco"]
                ):
                    from trade_assistant import close_watch

                    close_watch(rec, "process restarted; fresh approval required")
                    continue
            reconcile_trade(rec)
        except Exception as error:
            try:
                mark_attention(rec, error)
            except Exception:
                log.error(
                    "Recovery journal update failed; existing evidence preserved and entries blocked"
                )
            alert(
                safety.safe_error(error).rstrip(".")
                + ". Recovery will continue; new entries blocked."
            )
    return records


def request_bot_trade_close(
    symbol,
    confirmation,
    *,
    trade_id=None,
    action=None,
    expires_at=None,
    context=None,
    actor="cli",
    source="cli",
):
    """Persist an exact-trade close request before any exchange mutation/worker.

    Telegram supplies immutable trade/context identity and an expiring action;
    the CLI retains its exact symbol confirmation and resolves it under the lock.
    """
    require_trade_permission()
    if confirmation != "CLOSE_" + symbol:
        raise ValueError("Exact CLOSE_symbol confirmation required")
    with operation_lock():
        if context is not None and context != CONTEXT:
            raise ex.OwnershipError("Exit confirmation account/environment changed")
        if expires_at is not None and (STOP.is_set() or D(time.time()) >= D(expires_at)):
            raise ValueError(
                "Exit confirmation expired or shutdown requested; review /position again"
            )
        records = load_trades()
        matches = (
            [r for r in records if r["id"] == trade_id]
            if trade_id
            else [r for r in records if r["symbol"] == symbol and r["state"] != "closed"]
        )
        if len(matches) != 1:
            raise ex.OwnershipError("Cannot isolate one bot trade")
        rec = matches[0]
        if rec["symbol"] != symbol:
            raise ex.OwnershipError("Exit confirmation trade/pair mismatch")
        if rec["state"] == "closed":
            return rec  # Never substitute a new active trade with the same ticker.
        if sum(r["symbol"] == symbol and r["state"] != "closed" for r in records) != 1:
            raise ex.OwnershipError("Overlapping active pair records; cannot isolate the exit")
        from trade_assistant import close_watch, empty_watch

        watch = empty_watch(rec, rec["id"])
        if action not in (None, "exit", "cancel_watch"):
            raise ValueError("Unsupported position exit action")
        if action == "cancel_watch" and not watch:
            raise ValueError("Entry state changed. Review /position before requesting an exit")
        if action == "exit" and watch:
            raise ValueError("This entry is still a watch. Review /position and cancel the watch")
        if rec.get("close_requested"):
            return rec
        rec["close_request"] = {
            "source": str(source),
            "actor": str(actor),
            "requested_utc": datetime.now(timezone.utc).isoformat(),
            "action": "cancel_watch" if watch else "exit",
        }
        if watch:
            close_watch(rec, "cancelled by position exit request", notify=source != "telegram")
            return rec
        rec.update(close_requested=True, state="closing")
        save_trade(rec)
        return rec


def close_bot_trade(symbol, confirmation, *, trade_id=None):
    rec = request_bot_trade_close(symbol, confirmation, trade_id=trade_id)
    if rec["state"] == "closed":
        return True
    return reconcile_trade(rec)


def instance_lock():
    ensure_context()
    path = TRADES_DIR.parent / "instance.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return ProcessLock(path)


@contextmanager
def operation_lock():
    ensure_context()
    with MUTEX:
        if getattr(LOCK_STATE, "active", False):
            yield
            return
        path = TRADES_DIR.parent / "operation.lock"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with ProcessLock(path, blocking=True):
            LOCK_STATE.active = True
            try:
                yield
            finally:
                LOCK_STATE.active = False


def entry_result(rec):
    """Describe journal evidence only; do not infer fills from approval/balance."""
    buy = next((order for order in rec["orders"] if order["purpose"] == "buy"), None)
    if buy is None:
        return f"{rec['symbol']}: no buy request journaled; record {rec['state']}"
    result = (
        f"{rec['symbol']}: buy {buy['status']}; executed {buy['executedQty']}; "
        f"record {rec['state']}"
    )
    if buy.get("orderId") is not None:
        result += f"; Binance order {buy['orderId']}"
    if buy.get("rejection"):
        result += "; " + buy["rejection"]
    return result


def status_report():
    get_client()
    ensure_context()
    balance = get_client().get_asset_balance(asset="USDT")["free"]
    lines = [f"USDT free: {balance}"]
    with operation_lock():
        records = load_trades()
        active = [rec for rec in records if rec["state"] != "closed"]
        for rec in active:
            try:
                # /status never submits or cancels an order.
                if rec.get("entry_watch", {}).get("active"):
                    lines.append(
                        f"{rec['symbol']}: ENTRY WATCH; maximum buy {rec['plan']['price']}; "
                        f"stop {rec['plan']['stop']}; expires in "
                        f"{max(0, int(D(rec['entry_watch']['expires_at']) - D(time.time())))}s; "
                        "no exchange buy submitted. /cancel to stop."
                    )
                    continue
                refresh(rec)
                lines.append(
                    f"{rec['symbol']}: {rec['state']}; bot remaining {fmt(inventory(rec))}; stop coverage {fmt(coverage(rec))}"
                )
                if rec["state"] == "needs_attention":
                    if inventory(rec) == 0:
                        lines.append(
                            "Bot quantity is zero; execution/fee reconciliation is still pending."
                        )
                    reason = safety.reconciliation_reason(rec.get("recovery_reason_code"))
                    if reason:
                        lines.append(
                            f"Reconciliation reason [{rec['recovery_reason_code']}]: {reason}"
                        )
            except Exception as error:
                lines.append(
                    f"{rec['symbol']}: {rec['state']}; coverage UNKNOWN; {safety.safe_error(error)}"
                )
        if not active:
            lines.append("No active bot positions")
        if records:
            last = max(records, key=lambda rec: rec["created_utc"])
            lines.append("Latest saved entry: " + entry_result(last))
    return "\n".join(lines)


def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["status", "kill"])
    parser.add_argument("--symbol")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    initialize()
    # The operation lock serializes CLI actions with the running monitor. Holding
    # the Telegram instance lock here would make emergency kill unavailable.
    if args.cmd == "status":
        print(status_report())
    else:
        close_bot_trade(args.symbol, args.confirm)


if __name__ == "__main__":
    try:
        cli()
    except Exception as error:
        print(safety.safe_error(error))
        raise SystemExit(1) from None
