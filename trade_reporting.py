"""Read-only completed bot-trade reports from verified local execution evidence."""

from datetime import datetime, timedelta, timezone

import exit_manager as em

DUBAI = timezone(timedelta(hours=4), "Dubai")


def todays_pnl(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("An explicit report timezone is required")
    today = now.astimezone(DUBAI).date()
    with em.operation_lock():
        records = em.load_trades()
    completed = []
    for rec in records:
        if rec["state"] != "closed":
            continue
        events = em.realized_events(rec)
        if not events:  # Canceled watches and zero-fill entries are not completed trades.
            continue
        closed = max(int(event["time"]) for event in events)
        if datetime.fromtimestamp(closed / 1000, DUBAI).date() == today:
            completed.append((rec, sum((em.D(e["pnl"]) for e in events), em.D(0))))
    profit = sum((pnl for _, pnl in completed if pnl > 0), em.D(0))
    loss = sum((pnl for _, pnl in completed if pnl < 0), em.D(0))
    lines = [
        f"Today's completed trades · {today.isoformat()} · Dubai (UTC+4)",
        f"Completed: {len(completed)} | Wins: {sum(pnl > 0 for _, pnl in completed)} | "
        f"Losses: {sum(pnl < 0 for _, pnl in completed)} | "
        f"Breakeven: {sum(pnl == 0 for _, pnl in completed)}",
        f"Profit: {profit:+.4f} USDT",
        f"Loss: {loss:+.4f} USDT",
        f"Net P&L: {profit + loss:+.4f} USDT",
        "",
        "After recorded commissions and cost allocated to sold quantity. Open/partial positions excluded.",
        "Completion day uses the final sell fill; each trade's full realized result is included.",
    ]
    if any(rec.get("pnl_valuation") for rec, _ in completed):
        lines.append("BNB fees use saved USDT reference values, not an exact Binance conversion.")
    dust_totals = em.retained_dust_totals([rec for rec, _ in completed])
    if dust_totals:
        lines.append("Retained dust from these completed tradable positions (not sold):")
        for asset, totals in sorted(dust_totals.items()):
            lines.append(
                f"{em.fmt(totals['quantity'])} {asset} | retained cost "
                f"{em.fmt(totals['cost_basis_usdt'])} USDT; not realized P&L"
            )
    pending = sum(rec["state"] != "closed" for rec in records)
    if pending:
        lines.append(f"Open or reconciliation-pending records excluded: {pending}.")
    lines.append("The automatic daily risk limit uses UTC fill-day accounting separately.")
    return "\n".join(lines)
