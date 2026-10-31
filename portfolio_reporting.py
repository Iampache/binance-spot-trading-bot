"""Bot-only exposure and normal risk reservations; no trading or journal writes."""

import bot_health
import exchange as ex
import exit_manager as em
import position_manager as positions
import position_performance as performance
import safety
import trade_assistant as ta
import trading_settings as settings
from branding import BOT_NAME, VERSION
from trade_reporting import DUBAI


def account_cash():
    account = ex.signed_read(em.get_client(), "get_account")
    if account.get("accountType") != "SPOT" or str(account.get("uid")) != em.CONTEXT["account_uid"]:
        raise ex.OwnershipError("Portfolio account identity mismatch")
    rows = [row for row in account["balances"] if row["asset"] == "USDT"]
    if len(rows) != 1:
        raise ValueError("Unique USDT balance required")
    free, locked = em.D(rows[0]["free"]), em.D(rows[0]["locked"])
    if free < 0 or locked < 0:
        raise ValueError("Invalid account cash")
    return free, locked


def subtotal(results, field):
    total = em.D(0)
    for item in results:
        if item["status"] == "pending":
            continue  # Verified no acquisition; a watch is not a filled position.
        if item["status"] == "unavailable" or item.get(field) is None:
            return None
        total += item[field]
    return total


def report(*, now=None):
    now = performance.checked_time(now)
    # Hold the account lock only for local snapshot selection. Each position uses
    # its own existing verified read path; no recovery or valuation is persisted.
    with em.operation_lock():
        records = em.load_trades()
        limits = settings.read()
        context = dict(em.CONTEXT)
    active = [rec for rec in records if rec["state"] != "closed"]
    watches = [rec for rec in active if ta.empty_watch(rec, rec["id"])]
    reservations = [em.D(rec["approved_bounds"]["cost"]) for rec in watches]
    risks = [em.D(rec["plan"]["loss"]) for rec in active]
    if any(value <= 0 for value in reservations + risks):
        raise ValueError("Invalid portfolio reservation evidence")
    watch_cost = sum(reservations, em.D(0))
    planned_risk = sum(risks, em.D(0))
    dust = em.retained_dust_totals(records)
    dust_cost = sum((item["cost_basis_usdt"] for item in dust.values()), em.D(0))
    issues = []
    free = locked = None
    try:
        free, locked = account_cash()
    except Exception as error:
        issues.append("Account USDT balances unavailable: " + safety.safe_error(error))
    results = []
    refreshed = {rec["id"]: rec for rec in records}
    position_lines = []
    for index, rec in enumerate(active, 1):
        try:
            view = positions.snapshot(rec["id"], context, now=now)
            result = view["performance"]
            refreshed[rec["id"]] = view["record"]
            if view["error"]:
                issues.append(f"{rec['symbol']}: exchange verification pending.")
            elif result["status"] == "ready":
                try:
                    protection = em.protection_summary(view["record"])
                    if not protection["sufficient"]:
                        issues.append(
                            f"{rec['symbol']}: stop coverage is incomplete; the original-SL figure is a hypothetical scenario, not confirmed protection."
                        )
                except Exception:
                    issues.append(
                        f"{rec['symbol']}: stop coverage could not be verified. Use /health."
                    )
            if rec["state"] not in {"protected", "watching_entry"} or rec.get("close_requested"):
                issues.append(
                    f"{rec['symbol']}: trade reconciliation/exit is pending; exposure may change."
                )
            position_lines.append(f"{index}. {rec['symbol']} | Trade {rec['id'][:8]}")
            if result["status"] == "pending":
                position_lines.append("Entry watch/pending entry — no verified buy fills.")
            elif result["status"] == "closed":
                issues.append(f"{rec['symbol']}: closed while checking; refresh /portfolio.")
                position_lines.append("Closed during this check; refresh the report.")
            elif result["status"] == "unavailable":
                position_lines.append(
                    "Performance unavailable; execution/fee verification pending."
                )
            else:
                unrealized = result["unrealized"]
                position_lines.append(
                    f"Estimated unrealized {unrealized:+.4f} USDT | realized {result['realized']:+.4f} USDT"
                    if unrealized is not None
                    else "Estimated unrealized P&L unavailable — price/sell fee missing."
                )
                position_lines.append(f"Remaining cost {result['remaining_cost']:.4f} USDT")
            position_lines.extend(result["errors"])
            results.append(result)
        except Exception as error:
            results.append({"status": "unavailable"})
            position_lines.append(
                f"{index}. {rec['symbol']}: performance unavailable; " + safety.safe_error(error)
            )
    changing = False
    try:
        if (
            bot_health.fingerprint(records) != bot_health.fingerprint(em.load_trades())
            or limits != settings.read()
        ):
            changing = True
            issues.append(
                "Trade/settings state changed during this check; totals unavailable. Refresh /portfolio."
            )
    except Exception:
        changing = True
        issues.append("Could not reverify local snapshot; totals unavailable. Refresh /portfolio.")
    totals = {
        name: None if changing else subtotal(results, name)
        for name in ("remaining_cost", "market_value", "unrealized", "realized", "total")
    }
    stop_loss = None
    if not changing and all(
        item["status"] == "pending"
        or (item["status"] in {"ready", "flat"} and item["at_stop"] is not None)
        for item in results
    ):
        stop_loss = sum(
            (max(em.D(0), -item["at_stop"]) for item in results if item["status"] != "pending"),
            em.D(0),
        )
    budget = None
    try:
        if not changing:
            budget = bot_health.normal_budget(list(refreshed.values()), now=now)
    except Exception as error:
        issues.append("Normal daily budget unavailable: " + safety.safe_error(error))

    def amount(value, *, signed=False):
        return (
            "unavailable"
            if value is None
            else f"{value:+.4f} USDT"
            if signed
            else f"{value:.4f} USDT"
        )

    lines = [
        f"{BOT_NAME} | PORTFOLIO | v{VERSION}",
        "Checked: " + now.astimezone(DUBAI).strftime("%d %b %Y %H:%M:%S Dubai"),
        f"Environment: {'Spot Testnet' if em.USE_TESTNET else 'Live Spot'}",
        "",
        f"Trade slots: {len(active)} / {limits['max_open_trades']} occupied | {max(0, limits['max_open_trades'] - len(active))} free",
        f"Entry watches: {len(watches)} | other active/unresolved: {len(active) - len(watches)}",
        f"Per-trade investment cap: {limits['max_position_usdt']} USDT (including fee reserve)",
        "",
        "Account USDT free: " + amount(free),
        "Account USDT locked on exchange: " + amount(locked),
        "Reserved for bot entry watches: " + amount(None if changing else watch_cost),
        "Free USDT after watch reservations: "
        + amount(None if changing or free is None else free - watch_cost),
        "",
        "Cost of remaining bot holdings: " + amount(totals["remaining_cost"]),
        "Remaining reference market value: " + amount(totals["market_value"]),
        "Estimated unrealized P&L (after sell-fee reserve): "
        + amount(totals["unrealized"], signed=True),
        "Realized P&L on active trades: " + amount(totals["realized"], signed=True),
        "Realized + estimated unrealized (active trades): " + amount(totals["total"], signed=True),
        "",
        "Normal daily risk limit: 3.0000 USDT (UTC fill-day accounting)",
        "Full planned risk reserved for active trades/watches: "
        + amount(None if changing else planned_risk),
        "Retained-dust cost reserved against risk: " + amount(None if changing else dust_cost),
        "Net realized loss used today (UTC): "
        + amount(
            None
            if budget is None
            else max(em.D(0), em.D(safety.MAX_DAILY_LOSS_USD) - planned_risk - dust_cost - budget)
        ),
        "Normal daily risk budget remaining: " + amount(budget),
        "Estimated loss on remaining holdings at original SLs: " + amount(stop_loss),
    ]
    if free is not None and not changing:
        available = max(em.D(0), free - watch_cost)
        lines.append("1% sizing reference (USDT after watches): " + amount(available / 100))
        if budget is not None:
            lines.append(
                "Normal new-trade risk allowance before plan checks: "
                + amount(max(em.D(0), min(available / 100, budget)))
            )
    if budget is not None and budget < 0:
        lines.append(
            "Normal daily budget is overcommitted; Manual retains its explicit risk override."
        )
    if dust:
        lines.extend(["", "Retained dust — unsold; no active slots used:"])
        for asset, item in sorted(dust.items()):
            lines.append(
                f"{em.fmt(item['quantity'])} {asset} | acquisition cost {em.fmt(item['cost_basis_usdt'])} USDT"
            )
    if position_lines:
        lines.extend(["", *position_lines])
    else:
        lines.extend(["", "No active bot positions or entry watches."])
    if any(item.get("bnb_reference") for item in results):
        lines.append(
            "BNB fees use historical USDT reference values, not Binance's private conversion rate."
        )
    lines.extend(
        [
            "",
            *issues,
            "Bot positions only. USDT balances cover the whole account; external positions are excluded from bot P&L/risk.",
            "Full planned risk stays reserved after partial exits. Stop scenarios and marked values are estimates; gaps, slippage and fees can change losses.",
            "Reference valuation includes active fractional units; closed retained dust is separate. Neither is assumed sold.",
            "Free slots/budget do not authorize entry. Fresh plan checks and approval still apply. Use /health for blockers and /position for live details.",
        ]
    )
    return "\n".join(lines)
