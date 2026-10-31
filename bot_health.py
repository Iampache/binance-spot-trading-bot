"""Read-only health report; observations never change trading permissions."""

from datetime import datetime, timedelta, timezone

import exchange as ex
import exit_manager as em
import runtime_health as runtime
import safety
import trading_settings as settings
from branding import BOT_NAME, VERSION

DUBAI = timezone(timedelta(hours=4))


def timestamp(value):
    if value is None:
        return "not observed in this session"
    return datetime.fromisoformat(value).astimezone(DUBAI).strftime("%d %b %H:%M:%S Dubai")


def duration(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}h {seconds % 3600 // 60}m {seconds % 60}s"


def fingerprint(records):
    """Ignore notification/timestamp-only writes, but detect lifecycle changes."""
    ignored = {"updated_utc", "result_notice_due", "result_notices"}
    return [{k: v for k, v in rec.items() if k not in ignored} for rec in records]


def exchange_check():
    client = em.get_client()
    em.ensure_context()
    account = ex.signed_read(client, "get_account")
    if (
        account.get("accountType") != "SPOT"
        or str(account.get("uid")) != em.CONTEXT["account_uid"]
        or type(account.get("canTrade")) is not bool
    ):
        raise ex.OwnershipError("Authenticated Spot account identity/permissions unavailable")
    balance = next(row for row in account["balances"] if row["asset"] == "USDT")
    free = em.D(balance["free"])
    if free < 0 or em.D(balance["locked"]) < 0:
        raise ValueError("Invalid account balance")
    opened = client.get_open_orders()
    if not isinstance(opened, list):
        raise ValueError("Invalid open-order response")
    keys = set()
    for row in opened:
        key = (row["symbol"], row["orderId"])
        if (
            not isinstance(key[0], str)
            or type(key[1]) is not int
            or key[1] <= 0
            or key in keys
            or em.D(row["origQty"]) <= 0
            or not 0 <= em.D(row["executedQty"]) <= em.D(row["origQty"])
        ):
            raise ValueError("Invalid open-order evidence")
        keys.add(key)
    return account["canTrade"], free, opened


def order_issues(records, opened):
    known = {}
    issues = []
    for rec in records:
        for order in rec["orders"]:
            if order["status"] not in em.TERMINAL:
                key = (rec["symbol"], order["orderId"])
                if key in known:
                    issues.append("[ORDER_OWNERSHIP] Overlapping bot order records.")
                known[key] = (rec, order)
    seen = set()
    external = 0
    for row in opened:
        key = (row["symbol"], row["orderId"])
        if key not in known:
            external += 1
            continue
        seen.add(key)
        rec, order = known[key]
        matches = (
            row["clientOrderId"] in {order["cid"], order.get("cancel_cid")}
            and row["side"] == order["side"]
            and row["status"] == order["status"]
            and em.D(row["origQty"]) == em.D(order["qty"])
            and em.D(row["executedQty"]) == em.D(order["executedQty"])
        )
        if order["purpose"] in {"target", "stop"}:
            parent = next(item for item in rec["oco"] if order["cid"] in item["order_cids"])
            target = order["purpose"] == "target"
            matches = (
                matches
                and row["type"] == ("LIMIT_MAKER" if target else "STOP_LOSS")
                and row["orderListId"] == parent["orderListId"]
                and em.D(row["price" if target else "stopPrice"])
                == em.D(parent["tp" if target else "stop"])
            )
        else:
            matches = False  # An entry/exit still in flight requires reconciliation.
        if not matches:
            issues.append(
                f"[ORDER_CHANGED] {rec['symbol']}: exchange order differs from saved state."
            )
    for key in known.keys() - seen:
        issues.append(f"[ORDER_MISSING] {key[0]}: saved open order absent; reconciliation pending.")
    if external:
        issues.append(
            f"[EXTERNAL_ORDERS] {external} unrecognized exchange open order(s); ownership not isolated."
        )
    return list(dict.fromkeys(issues))


def normal_budget(records, *, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Explicit report timezone required")
    today = now.astimezone(timezone.utc).date()
    realized = em.D(0)
    reserve = em.D(0)
    for rec in records:
        if rec["state"] != "closed":
            loss = em.D(rec["plan"]["loss"])
            if loss <= 0:
                raise ValueError("Invalid active risk reservation")
            reserve += loss
            if not any(em.executions(rec, "SELL")):
                continue
        elif rec.get("retained_dust"):
            reserve += em.D(rec["retained_dust"]["cost_basis_usdt"])
        for event in em.realized_events(rec):
            if datetime.fromtimestamp(int(event["time"]) / 1000, timezone.utc).date() == today:
                realized += em.D(event["pnl"])
    return em.D(safety.MAX_DAILY_LOSS_USD) - max(em.D(0), -realized) - reserve


def monitor_lines(records, observed):
    lines, problems = [], []
    for rec in records:
        tasks = [
            item
            for item in observed["tasks"]
            if item["kind"] == "monitor" and item["identity"] == rec["id"]
        ]
        alive = [item for item in tasks if item["running"]]
        latest = (alive or tasks or [None])[-1]
        label = f"{rec['symbol']} | {rec['id'][:8]}"
        if not alive:
            lines.append(f"{label}: NOT RUNNING; restart the assistant to recover monitoring.")
            problems.append("[MONITOR_STOPPED] An active trade has no running monitor.")
        else:
            task = alive[-1]
            state = "RETRYING" if task["error"] else "RUNNING"
            if task["age"] > 180:
                state = "DELAYED — thread alive"
                problems.append(
                    "[MONITOR_DELAYED] No monitor progress observed for over 180 seconds."
                )
            elif task["error"]:
                problems.append(
                    "[MONITOR_RETRY] A reconciliation attempt failed; monitoring will retry."
                )
            lines.append(f"{label}: {state} ({task['phase']}; {duration(task['age'])} ago).")
        successes = [item["success"] for item in tasks if item["success"]]
        lines.append(
            f"Last successful reconciliation: {timestamp(max(successes) if successes else None)}"
        )
        if latest and latest["error"]:
            lines.append("Latest monitor error: " + latest["error"])
    if not records:
        lines.append("Position monitoring: not needed — no active bot trades.")
    return lines, problems


def report(*, auto_scan):
    issues, blockers, normal_blocks = [], [], []
    details = []
    records = limits = probe = None
    try:
        records = em.load_trades()
        details.append("Trade journal: OK")
    except Exception:
        blockers.append("[JOURNAL_INVALID] Trade records unavailable or invalid; entries blocked.")
    try:
        limits = settings.read()
        details.append(
            f"Limits: {limits['max_open_trades']} active trades / {limits['max_position_usdt']} USDT per trade"
        )
    except Exception:
        blockers.append(
            "[SETTINGS_INVALID] Trading limits unavailable or invalid; entries blocked."
        )
    try:
        probe = exchange_check()
        runtime.checked("exchange")
        details.append("Exchange check: OK — authenticated account and open-order reads")
    except Exception as error:
        runtime.checked("exchange", error)
        issues.append(
            "[EXCHANGE_UNAVAILABLE] Current account/open-order check could not be verified."
        )
        details.append("Exchange check: UNAVAILABLE; " + safety.safe_error(error))
    observed = runtime.snapshot()
    details.append(
        "Last successful exchange check: "
        + timestamp(observed["checks"].get("exchange", {}).get("success"))
    )
    active = None if records is None else [rec for rec in records if rec["state"] != "closed"]
    if active is not None:
        details.append(
            f"Active/unresolved bot trades: {len(active)}"
            + (f" / {limits['max_open_trades']}" if limits else "")
        )
        if limits and len(active) >= limits["max_open_trades"]:
            blockers.append("[TRADE_LIMIT] All configured trade slots are occupied.")
        if len({rec["symbol"] for rec in active}) != len(active):
            blockers.append(
                "[DUPLICATE_PAIR] Duplicate active pair records require reconciliation."
            )
        for rec in active:
            watch = (
                rec.get("entry_watch", {}).get("active") and not rec["orders"] and not rec["oco"]
            )
            try:
                if watch:
                    details.append(f"{rec['symbol']}: entry watch; no buy submitted.")
                else:
                    summary = em.protection_summary(rec)
                    details.append(
                        f"{rec['symbol']}: {rec['state']}; saved remaining {em.fmt(summary['remaining'])}; "
                        f"saved stop coverage {em.fmt(summary['covered'])}"
                    )
                    if summary["dust"]:
                        details.append(
                            f"Below-step fraction retained: {em.fmt(summary['dust'])} {rec['symbol'][:-4]}."
                        )
                    if (
                        rec["state"] != "protected"
                        or not summary["sufficient"]
                        or rec["close_requested"]
                    ):
                        code = rec.get("recovery_reason_code")
                        reason = safety.reconciliation_reason(code)
                        blockers.append(
                            f"[{code if reason else 'RECONCILIATION_PENDING'}] {rec['symbol']}: "
                            + (reason or "Trade state/protection requires reconciliation.")
                        )
            except Exception as error:
                blockers.append(
                    f"[PROTECTION_UNKNOWN] {rec['symbol']}: " + safety.safe_error(error)
                )
        monitors, monitor_problems = monitor_lines(active, observed)
        details.extend(monitors)
        issues.extend(monitor_problems)
        dust = em.retained_dust_totals(records)
        if dust:
            details.append("Retained dust — no active slots used:")
            details.extend(
                f"{asset}: {em.fmt(value['quantity'])}" for asset, value in sorted(dust.items())
            )
        if probe is not None:
            try:
                if fingerprint(records) != fingerprint(em.load_trades()) or (
                    limits and limits != settings.read()
                ):
                    issues.append(
                        "[STATE_CHANGING] Trade/settings state changed during this check; refresh /health."
                    )
                else:
                    blockers.extend(order_issues(active, probe[2]))
            except Exception:
                issues.append(
                    "[SNAPSHOT_UNAVAILABLE] Local/exchange state comparison could not complete."
                )
        try:
            remaining = normal_budget(records)
            details.append(
                f"Normal daily risk budget remaining: {remaining:.4f} USDT (UTC accounting)"
            )
            if remaining <= 0:
                normal_blocks.append(
                    "[DAILY_RISK] Normal daily loss/risk budget is exhausted or reserved."
                )
        except Exception as error:
            blockers.append(
                "[RISK_ACCOUNTING_PENDING] Risk/fee accounting unavailable; "
                + safety.safe_error(error)
            )
    if probe is not None:
        can_trade, free, _ = probe
        details.append(f"USDT free: {em.fmt(free)}")
        if not can_trade:
            blockers.append("[ACCOUNT_PERMISSION] Exchange account trading permission is disabled.")
        if active is not None:
            try:
                reserved = sum(
                    (
                        em.D(rec["approved_bounds"]["cost"])
                        for rec in active
                        if rec.get("entry_watch", {}).get("active")
                    ),
                    em.D(0),
                )
                if free - reserved <= 0:
                    blockers.append(
                        "[FUNDS_UNAVAILABLE] No free USDT remains after entry-watch reservations."
                    )
            except Exception:
                blockers.append(
                    "[WATCH_ACCOUNTING_PENDING] Entry-watch reservation could not be verified."
                )
    if not em.USE_TESTNET and not em.ALLOW_LIVE:
        blockers.append("[LIVE_DISABLED] Live trading is disabled by runtime configuration.")
    if em.STOP.is_set():
        blockers.append("[SHUTTING_DOWN] Shutdown requested; no new buys.")
    scanners = [task for task in observed["tasks"] if task["kind"] == "scanner"]
    running_scanners = [task for task in scanners if task["running"]]
    if not auto_scan:
        details.append("Automatic scanner: OFF — on-demand checks remain available.")
    elif not running_scanners:
        details.append("Automatic scanner: NOT RUNNING")
        issues.append(
            "[SCANNER_STOPPED] Automatic scanning is enabled but its worker is not running."
        )
    else:
        scanner = running_scanners[-1]
        scan_state = "RETRYING" if scanner["error"] else "RUNNING"
        if scanner["age"] > 1200:
            scan_state = "DELAYED — thread alive"
            issues.append("[SCANNER_DELAYED] No scanner progress observed for over 20 minutes.")
        details.append(
            f"Automatic scanner: {scan_state}; last successful cycle {timestamp(scanner['success'])}"
        )
        if scanner["error"]:
            issues.append("[SCANNER_ERROR] Latest scan failed; " + scanner["error"])
    # Slot/funds/risk limits are normal operating gates, not a broken monitor.
    expected_gates = ("[TRADE_LIMIT]", "[FUNDS_UNAVAILABLE]")
    faults = issues + [line for line in blockers if not line.startswith(expected_gates)]
    lines = [
        f"{BOT_NAME} | HEALTH | v{VERSION}",
        f"Checked: {timestamp(runtime.stamp())} | uptime {duration(observed['uptime'])}",
        f"Environment: {'Spot Testnet' if em.USE_TESTNET else 'Live Spot'}",
        "Health: " + ("NEEDS ATTENTION" if faults else "OK"),
        "",
        *details,
        "",
        "New entries (all modes): "
        + (
            "BLOCKED"
            if blockers
            else "NOT VERIFIED"
            if issues
            else "no account-wide blocker detected"
        ),
        *blockers,
        *issues,
        "Normal entries: "
        + (
            "BLOCKED"
            if blockers or normal_blocks
            else "NOT VERIFIED"
            if issues
            else "fresh plan checks required"
        ),
        *normal_blocks,
        "Normal risk limits apply to algorithm modes; Manual retains its explicit risk override.",
        "Coin-specific strategy, minimum order, price and fee checks still run on a fresh plan.",
        "Every new buy requires approval. /health is read-only and does not repair or change orders.",
        "Observations are from this process; restart resets check history. Saved quantities await monitor reconciliation.",
    ]
    return "\n".join(lines)
