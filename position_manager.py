"""Read-only bot position snapshots and presentation; no cancel/sell calls."""

from datetime import datetime

import exit_manager as em
import safety
import trade_assistant as ta
from trade_reporting import DUBAI

STATES = {
    "intent": "Entry pending",
    "watching_entry": "Entry watch — not bought yet",
    "buy_submitting": "Entry confirmation pending",
    "bought": "Bought — protection pending",
    "protection_submitting": "Protection confirmation pending",
    "protected": "Active",
    "closing": "Exit requested — reconciliation in progress",
    "needs_attention": "Reconciliation required",
    "closed": "Closed",
}


def list_positions():
    with em.operation_lock():
        records = sorted(em.active_trades(), key=lambda rec: (rec["created_utc"], rec["id"]))
        return {
            "context": dict(em.CONTEXT),
            "rows": [
                {"id": r["id"], "symbol": r["symbol"], "state": STATES[r["state"]]} for r in records
            ],
        }


def snapshot(trade_id, context):
    with em.operation_lock():
        if context != em.CONTEXT:
            raise ValueError("Position account/environment changed; open /position again")
        matches = [r for r in em.load_trades() if r["id"] == trade_id]
        if len(matches) != 1:
            raise ValueError("Saved trade unavailable; open /position again")
        rec = matches[0]
        error = ""
        if rec["state"] != "closed" and not ta.empty_watch(rec, rec["id"]):
            try:
                em.refresh(rec)  # Read exchange evidence; never reconcile/cancel/sell here.
            except Exception as failure:
                error = safety.safe_error(failure)
        price = None
        try:
            quote = em.get_client().get_symbol_ticker(symbol=rec["symbol"])
            if quote.get("symbol") != rec["symbol"] or em.D(quote["price"]) <= 0:
                raise ValueError("Invalid position quote")
            price = em.D(quote["price"])
        except Exception:
            price = None  # Explicitly show the unavailable quote; no execution follows.
        action = None
        if rec["state"] != "closed" and not rec.get("close_requested"):
            if ta.empty_watch(rec, rec["id"]):
                action = "cancel_watch"
            elif any(o["side"] == "BUY" for o in rec["orders"]) and (
                error
                or any(o.get("evidence_pending") for o in rec["orders"])
                or any(o["side"] == "BUY" and o["status"] not in em.TERMINAL for o in rec["orders"])
                or em.inventory(rec) > 0
            ):
                action = "exit"
        return {
            "record": rec,
            "context": dict(em.CONTEXT),
            "price": price,
            "error": error,
            "action": action,
        }


def target_status(rec, index):
    if len(rec["oco"]) < index:
        return "Not placed"
    parent = rec["oco"][index - 1]
    orders = [
        o for o in rec["orders"] if o["cid"] in parent["order_cids"] and o["purpose"] == "target"
    ]
    if len(orders) != 1:
        return "Verification pending"
    order = orders[0]
    if order["status"] == "FILLED":
        return "Executed"
    if em.D(order["executedQty"]) > 0:
        return "Partially executed"
    return (
        "Pending"
        if order["status"] in ("NEW", "PENDING_NEW")
        else order["status"].replace("_", " ").title()
    )


def details_text(view):
    rec, price = view["record"], view["price"]
    state = STATES[rec["state"]]
    if rec.get("retained_dust"):
        state += " — dust retained"
    if view["error"]:
        state += " (saved state; exchange check pending)"
    if rec.get("close_requested") and rec["state"] != "closed":
        state = "Exit requested — reconciliation in progress"
    lines = [
        f"Bot position | {rec['symbol'][:-4]}/USDT | Trade {rec['id'][:8]}",
        f"Status: {state}",
        "",
    ]
    buys = list(em.executions(rec, "BUY"))
    quantity = sum((em.D(t["qty"]) for t in buys), em.D(0))
    if quantity > 0:
        average = sum((em.D(t["quoteQty"]) for t in buys), em.D(0)) / quantity
        lines.append(
            f"Entry price ({'saved ' if view['error'] else ''}average fill): {average:.8f}"
        )
        opened = min(int(t["time"]) for t in buys)
        lines.append(
            "Opened: "
            + datetime.fromtimestamp(opened / 1000, DUBAI).strftime("%d %b %Y %H:%M Dubai")
        )
    else:
        lines.append("Entry price: awaiting verified fills")
    lines += [
        f"Entry cap: {rec['plan']['price']}",
        f"Current reference price: {price:f}"
        if price is not None
        else "Current reference price: unavailable",
        f"SL: {rec['plan']['stop']}",
        f"TP1: {rec['plan']['t1']} — {target_status(rec, 1)}",
        f"TP2: {rec['plan']['t2']} — {target_status(rec, 2)}",
    ]
    if view["error"] or any(o.get("evidence_pending") for o in rec["orders"]):
        lines.append("Remaining quantity / stop coverage: verification pending")
    else:
        left, covered = em.inventory(rec), em.coverage(rec)
        lines += [
            f"Remaining bot quantity: {em.fmt(left)}",
            f"Observed stop coverage: {em.fmt(covered)}",
        ]
        if rec["state"] != "closed":
            summary = em.protection_summary(rec)
            em.record_protection_dust(rec, summary)
            if summary["dust"]:
                lines.append(em.protection_dust_text(rec))
        if left == 0 and rec["state"] != "closed" and view["action"] != "cancel_watch":
            lines.append("No verified held quantity; entry/exit accounting may still be pending.")
    if view["error"]:
        lines += [
            "",
            "Exchange verification pending: " + view["error"],
            "Details may be stale. An exit request waits for verified ownership before selling.",
        ]
    if view["action"] == "cancel_watch":
        lines += [
            "",
            "This is an unfilled entry watch. Cancel Entry Watch prevents a buy; it does not sell coins.",
        ]
    elif view["action"] == "exit":
        lines += [
            "",
            "Exit Trade closes this bot trade at market after confirmation. Prices and fees can change the result.",
        ]
    elif rec.get("close_requested") and rec["state"] != "closed":
        lines += [
            "",
            "The accepted exit request is saved. Recovery continues; a final result confirms closure.",
        ]
    if rec.get("retained_dust"):
        lines += [
            "",
            em.retained_dust_text(rec),
            "The exchange-tradable position is closed; this retained asset does not use a trade slot.",
        ]
    return "\n".join(lines)


def confirmation_text(view):
    rec = view["record"]
    text = f"Confirm exit | {rec['symbol'][:-4]}/USDT | Trade {rec['id'][:8]}\n\n"
    if view["action"] == "cancel_watch":
        return (
            text
            + "Cancel this unfilled entry watch? No sell order will be placed. If entry has already started, this confirmation will be rejected."
        )
    reference = f"{view['price']:f}" if view["price"] is not None else "unavailable"
    left = (
        "verification pending"
        if view["error"] or any(o.get("evidence_pending") for o in rec["orders"])
        else em.fmt(em.inventory(rec))
    )
    text += (
        f"Current reference price: {reference}\nRemaining bot quantity at last check: {left}\n\n"
    )
    return (
        text + "Close this bot trade now?\n"
        "Any pending buy is canceled. Its protective OCOs are released one at a time, "
        "and only the remaining bot-owned quantity is sold at market.\n"
        "Other positions and unrelated holdings are preserved. The final price and P&L "
        "can differ from the displayed reference; losses and fees are possible.\n\n"
        "Once confirmed, the close request is saved and recovery may continue it after restart. "
        "/cancel cannot undo an accepted exit. Keep Position leaves this trade running."
    )
