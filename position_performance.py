"""Read-only reference valuation of bot inventory; never a realizable exit quote."""

from datetime import datetime, timezone

import exit_manager as em
import safety
from trade_reporting import DUBAI


def checked_time(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Explicit report timezone required")
    return now


def calculate(rec, price, fee_rate, *, now=None):
    """Allocate actual entry cost once; sold and remaining units never overlap."""
    now = checked_time(now)
    buys = list(em.executions(rec, "BUY"))
    if not buys:
        return {"status": "pending", "checked": now, "errors": []}
    bought, cost = em.acquisition_basis(rec)
    if bought <= 0 or cost <= 0:
        raise ValueError("Verified net acquisition quantity and cost required")
    left = em.inventory(rec)
    realized = sum((em.D(event["pnl"]) for event in em.realized_events(rec)), em.D(0))
    opened = datetime.fromtimestamp(min(int(row["time"]) for row in buys) / 1000, timezone.utc)
    closed = rec["state"] == "closed"
    end = now
    if closed or left == 0:
        fills = list(em.executions(rec, "SELL")) or buys
        end = datetime.fromtimestamp(max(int(row["time"]) for row in fills) / 1000, timezone.utc)
    age = (end - opened).total_seconds()
    result = {
        "status": "closed" if closed else "flat" if left == 0 else "ready",
        "checked": now,
        "age": age if age >= 0 else None,
        "quantity": em.D(0) if closed else left,
        "remaining_cost": em.D(0) if closed else cost * left / bought,
        "realized": realized,
        "bnb_reference": any(row.get("fee_valuation") for row in em.executions(rec)),
        "market_value": None,
        "unrealized": None,
        "unrealized_pct": None,
        "total": None,
        "exit_fee": None,
        "fee_rate": fee_rate,
        "at_stop": None,
        "distances": {},
        "errors": [],
    }
    if closed or left == 0:
        result.update(
            market_value=em.D(0),
            unrealized=em.D(0),
            total=realized,
            exit_fee=em.D(0),
            at_stop=em.D(0),
        )
        return result
    if price is not None:
        price = em.D(price)
        if price <= 0:
            raise ValueError("Positive reference price required")
        result["market_value"] = left * price
        result["distances"] = {
            name: (em.D(rec["plan"][key]) / price - 1) * 100
            for name, key in (("SL", "stop"), ("TP1", "t1"), ("TP2", "t2"))
        }
    if fee_rate is not None:
        fee_rate = em.D(fee_rate)
        if not 0 <= fee_rate < em.D(".05"):
            raise ValueError("Invalid estimated sell fee rate")
        result["fee_rate"] = fee_rate
        result["at_stop"] = (
            left * em.D(rec["plan"]["stop"]) * (1 - fee_rate) - result["remaining_cost"]
        )
        if result["market_value"] is not None:
            result["exit_fee"] = result["market_value"] * fee_rate
            result["unrealized"] = (
                result["market_value"] - result["exit_fee"] - result["remaining_cost"]
            )
            result["unrealized_pct"] = result["unrealized"] / result["remaining_cost"] * 100
            result["total"] = realized + result["unrealized"]
    return result


def observe(rec, price, *, verification_error="", now=None):
    """Enrich a detached snapshot only; missing fees are never estimated as zero."""
    now = checked_time(now)
    if verification_error:
        return {"status": "unavailable", "checked": now, "errors": [verification_error]}
    errors = []
    fee_rate = None
    try:
        # Historical BNB references use actual fill time, never today's BNB price.
        em.attach_fee_valuations(rec, persist=False)
        if rec["state"] != "closed" and em.inventory(rec) > 0:
            try:
                fee_rate = em.commission_schedule(rec["symbol"])["reserve_rate"]
            except Exception as error:
                errors.append("Estimated sell fees unavailable: " + safety.safe_error(error))
        result = calculate(rec, price, fee_rate, now=now)
        result["errors"].extend(errors)
        return result
    except Exception as error:
        return {"status": "unavailable", "checked": now, "errors": [safety.safe_error(error)]}


def lines(result):
    if not result:
        return []
    text = [
        "",
        "Position performance",
        "Checked: " + result["checked"].astimezone(DUBAI).strftime("%d %b %H:%M:%S Dubai"),
    ]
    if result["status"] == "pending":
        return text + ["No buy filled; performance starts after verified entry."]
    if result["status"] == "unavailable":
        return text + [
            "Performance unavailable — execution/fee verification pending.",
            *result["errors"],
        ]
    age = result["age"]
    text.append(
        f"Time {'in trade' if result['status'] == 'ready' else 'held'}: {int(age) // 86400}d {int(age) % 86400 // 3600}h {int(age) % 3600 // 60}m"
        if age is not None
        else "Time in trade: unavailable — entry timestamp is ahead of report clock"
    )
    text.append(f"Realized P&L to date: {result['realized']:+.4f} USDT (recorded fills/fees)")
    if result["bnb_reference"]:
        text.append(
            "BNB fees use historical USDT reference values, not Binance's private conversion rate."
        )
    if result["status"] == "closed":
        return text + ["Tradable position closed; retained dust is reported separately."]
    if result["status"] == "flat":
        return text + [
            "No remaining bot quantity; closure/accounting awaits monitor reconciliation."
        ]
    text.append(f"Cost of remaining holdings: {result['remaining_cost']:.4f} USDT")
    if result["market_value"] is not None:
        text.append(f"Remaining reference value: {result['market_value']:.4f} USDT")
    if result["unrealized"] is not None:
        percent = (
            f" ({result['unrealized_pct']:+.2f}%)" if result["unrealized_pct"] is not None else ""
        )
        text.extend(
            [
                f"Estimated unrealized P&L: {result['unrealized']:+.4f} USDT{percent}",
                f"Estimated remaining sell fee: {result['exit_fee']:.4f} USDT (reserve rate {result['fee_rate'] * 100:.3f}%)",
                f"Realized + estimated unrealized: {result['total']:+.4f} USDT",
            ]
        )
    else:
        text.append(
            "Estimated unrealized P&L: unavailable — price or sell fees could not be verified"
        )
    if result["distances"]:
        text.append(
            "Move from reference to level: "
            + " | ".join(f"{key} {value:+.2f}%" for key, value in result["distances"].items())
        )
    text.extend(result["errors"])
    text.append(
        "Reference valuation includes any fractional remainder. Estimates can change with fills, fees and slippage; they are not an executable sell quote."
    )
    return text
