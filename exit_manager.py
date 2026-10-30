"""Durable Spot inventory ledger. Unknown outcomes never authorize another write."""

import argparse
import fcntl
import json
import logging
import os
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

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "runtime_state"
TRADES_DIR = None
CONTEXT = None
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
ALLOW_LIVE = os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"
MAX_DAILY_LOSS_USD = safety.MAX_DAILY_LOSS_USD
POLL_SECONDS = 5
client = None
STOP = threading.Event()
MUTEX = threading.RLock()
LOCK_STATE = threading.local()
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
    return client


def initialize():
    """Account UID is verified by Binance; aliases/keys are not account identity."""
    global CONTEXT, TRADES_DIR
    account = get_client().get_account()
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
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat, "text": message},
                timeout=12,
            ).raise_for_status()
        except Exception as error:
            log.error("%s", safety.safe_error(error))


def _atomic_write(path, item):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fp:
            json.dump(item, fp, indent=2, allow_nan=False)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
            rec = json.loads(path.read_text())
            if (
                rec["version"] != 3
                or rec["context"] != CONTEXT
                or rec["id"] != path.stem
                or rec["state"]
                not in {
                    "intent",
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


def new_trade(plan, approval=None):
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
            "12 USDT cap cannot satisfy required order minimums/fee buffer; skip trade"
        )


def commission_rate(symbol):
    info = get_client().v3_get_account_commission(symbol=symbol)
    if info.get("symbol") != symbol:
        raise ValueError("Commission symbol mismatch")
    # Ignore discounts; reserve undiscounted taker+buyer/seller, including tax/special.
    discount = info.get("discount", {})
    if discount.get("enabledForAccount") and discount.get("enabledForSymbol"):
        raise ValueError(
            "BNB-discount commission valuation unsupported; skip until disabled by account owner"
        )
    rates = []
    for side in ("buyer", "seller"):
        rate = sum(
            D(info[name]["taker"]) + D(info[name][side])
            for name in ("standardCommission", "taxCommission", "specialCommission")
        )
        rates.append(rate)
    rate = max(rates)
    if not 0 <= rate < D(".05"):
        raise ValueError("Invalid/unsupported commission schedule")
    return rate


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


def preflight(plan, f):
    f["symbol"] = plan["symbol"]
    for key in ("stop", "t1", "t2"):
        validate_price(plan[key], f, "SELL")
    price = D(plan["price"])
    validate_price(round_to(price, f["tick"]), f, "BUY")
    if not D(plan["stop"]) < price < D(plan["t1"]) < D(plan["t2"]):
        raise ValueError("Rounded protective levels invalid")
    qty = D(plan["qty"])
    validate_qty(qty, f, price, market=True, entry=True)
    # Reserve worst-side base commission before sizing the two protective lists.
    net = round_to(qty * (1 - D(plan["fee_rate"])), f["step"])
    half = round_to(net / 2, f["step"])
    for part, target in ((half, plan["t1"]), (net - half, plan["t2"])):
        validate_qty(part, f, plan["stop"], entry=True)
        validate_qty(part, f, target, entry=True)
    if qty * price * (1 + D(plan["fee_rate"])) > safety.MAX_POSITION_USDT:
        raise ValueError("Position including commission exceeds 12 USDT")
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
    for name, field, needed in [
        ("MAX_NUM_ORDERS", "maxNumOrders", 4),
        ("MAX_NUM_ALGO_ORDERS", "maxNumAlgoOrders", 2),
        ("MAX_NUM_ORDER_LISTS", "maxNumOrderLists", 2),
    ]:
        rule = f["raw"].get(name)
        if rule and int(rule[field]) and int(rule[field]) < needed:
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
        if int(rule[field]) and int(rule[field]) < needed:
            raise ValueError("Insufficient exchange-wide protective order capacity")
    limit = f["raw"].get("MAX_POSITION")
    if limit and D(plan["baseline_base"]) + qty > D(limit["maxPosition"]):
        raise ValueError("Exchange MAX_POSITION exceeded")


def trades_for(order, symbol):
    rows = get_client().get_my_trades(symbol=symbol, orderId=order["orderId"], limit=1000)
    if len(rows) >= 1000:
        raise ex.Uncertain("Execution pagination requires manual reconciliation")
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
        raise ex.Uncertain("Execution quote totals do not reconcile")
    if total != D(order["executedQty"]):
        raise ex.Uncertain("Execution history incomplete; no liquidation authorized")
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
        raise ex.Uncertain("Unsupported order state")
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
        if not rec.get("approval_id") or time.time() + 15 >= float(bounds.get("expires_at", 0)):
            raise ValueError("Approval expired during validation; no entry submitted")
        if D(item["qty"]) * D(item["price_cap"]) * (1 + D(rec["plan"]["fee_rate"])) > D(
            bounds["cost"]
        ):
            raise ValueError("Buy would exceed approved cost ceiling")
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
            item.update(status="REJECTED", local_rejection=True)
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
            raise ex.Uncertain("Unsupported OCO list state")
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
    f = f or symbol_filters(symbol)
    tp, stop = round_to(tp, tick), round_to(stop, tick)
    validate_qty(qty, f, stop)
    live = D(get_client().get_symbol_ticker(symbol=symbol)["price"])
    if not 0 < stop < live < tp:
        raise ValueError("Market moved outside protective levels")
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
            if ex.definitely_rejected(error):
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


def realized_events(rec):
    buys = list(executions(rec, "BUY"))
    base = rec["symbol"][:-4]
    bought = sum(
        (D(t["qty"]) - (D(t["commission"]) if t["commissionAsset"] == base else 0) for t in buys),
        D(0),
    )
    cost = D(0)
    for t in buys:
        if D(t["commission"]) and t["commissionAsset"] not in (base, "USDT"):
            raise ex.Uncertain("Third-asset buy commission needs verified USDT valuation")
        cost += D(t["quoteQty"]) + (D(t["commission"]) if t["commissionAsset"] == "USDT" else 0)
    events = []
    for t in executions(rec, "SELL"):
        if D(t["commission"]) and t["commissionAsset"] not in (base, "USDT"):
            raise ex.Uncertain("Third-asset sell commission needs verified USDT valuation")
        if bought <= 0:
            raise ex.OwnershipError("No verified buy cost basis")
        units = D(t["qty"]) + (D(t["commission"]) if t["commissionAsset"] == base else 0)
        pnl = (
            D(t["quoteQty"])
            - (D(t["commission"]) if t["commissionAsset"] == "USDT" else 0)
            - cost * units / bought
        )
        events.append({"id": t["id"], "time": t["time"], "pnl": fmt(pnl)})
    return events


def todays_loss():
    today = datetime.now(timezone.utc).date()
    daily = D(0)
    for rec in load_trades():
        if rec["state"] != "closed":
            raise ValueError("Open or unresolved bot trade exists")
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
    rec["realized_events"] = realized_events(rec)
    rec.update(state="closed", accounted=True, residual_qty="0")
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
    submit_order(rec, item)
    if item["status"] not in TERMINAL or item["status"] == "REJECTED":
        raise ex.Uncertain("Emergency exit not confirmed terminal/filled")
    alert("Emergency exit reconciled; inspect /status for residual inventory")
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
    """One idempotent recovery step; never creates a buy or duplicates an exit."""
    with operation_lock():
        latest = next(r for r in load_trades() if r["id"] == rec["id"])
        rec.clear()
        rec.update(latest)
        if rec["state"] == "closed":
            return True
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


def mark_attention(rec):
    with operation_lock():
        latest = next(r for r in load_trades() if r["id"] == rec["id"])
        rec.clear()
        rec.update(latest)
        if rec["state"] != "closed":
            rec["state"] = "needs_attention"
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
                mark_attention(rec)
            except Exception:
                log.error("Journal update failed; existing evidence preserved and entries blocked")
            if failures == 1 or failures % 12 == 0:
                alert(
                    safety.safe_error(error)
                    + ". New entries blocked; monitor will retry reconciliation."
                )
        checks += 1
        if max_checks is not None and checks >= max_checks:
            return
        STOP.wait(min(60, POLL_SECONDS * 2 ** min(failures, 4)))


def recover_trades():
    records = active_trades()
    for rec in records:
        try:
            reconcile_trade(rec)
        except Exception as error:
            try:
                mark_attention(rec)
            except Exception:
                log.error(
                    "Recovery journal update failed; existing evidence preserved and entries blocked"
                )
            alert(safety.safe_error(error) + ". Recovery will continue; new entries blocked.")
    return records


def close_bot_trade(symbol, confirmation):
    require_trade_permission()
    if confirmation != "CLOSE_" + symbol:
        raise ValueError("Exact CLOSE_symbol confirmation required")
    with operation_lock():
        matches = [r for r in active_trades() if r["symbol"] == symbol]
        if len(matches) != 1:
            raise ex.OwnershipError("Cannot isolate one bot trade")
        rec = matches[0]
        rec.update(close_requested=True, state="closing")
        save_trade(rec)
    return reconcile_trade(rec)


class ProcessLock:
    def __init__(self, path):
        self.fp = path.open("a+")
        try:
            fcntl.flock(self.fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fp.close()
            raise FileExistsError("Another instance holds the account lock") from None

    def unlink(self, missing_ok=True):
        # Never unlink the inode: another process could otherwise lock a new inode.
        if not self.fp.closed:
            fcntl.flock(self.fp, fcntl.LOCK_UN)
            self.fp.close()


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
        with path.open("a+") as fp:
            fcntl.flock(fp, fcntl.LOCK_EX)
            LOCK_STATE.active = True
            try:
                yield
            finally:
                LOCK_STATE.active = False
                fcntl.flock(fp, fcntl.LOCK_UN)


def status_report():
    get_client()
    ensure_context()
    balance = get_client().get_asset_balance(asset="USDT")["free"]
    lines = [f"USDT free: {balance}"]
    with operation_lock():
        for rec in active_trades():
            try:
                # /status never submits or cancels an order.
                refresh(rec)
                lines.append(
                    f"{rec['symbol']}: {rec['state']}; bot remaining {inventory(rec)}; stop coverage {coverage(rec)}"
                )
            except Exception as error:
                lines.append(
                    f"{rec['symbol']}: {rec['state']}; coverage UNKNOWN; {safety.safe_error(error)}"
                )
    if len(lines) == 1:
        lines.append("No active bot positions")
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
