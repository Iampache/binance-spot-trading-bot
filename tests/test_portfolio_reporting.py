"""Portfolio totals preserve cash/risk accounting, ownership and partial results."""

import copy
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from test_bot import approved, sig
from test_fractional_dust import fill_pepe_exits, replay_pepe
from test_fractional_dust import market as market

import exit_manager as em
import portfolio_reporting as portfolio
import telegram_assistant as tg
import trade_assistant as ta
import trading_settings as settings


def buy(symbol="FOOUSDT", **fields):
    plan = ta.build_plan({**sig(), "symbol": symbol, **fields})
    return ta.execute(plan, approval=approved(plan), monitor=False)


def files():
    return {str(path): path.read_bytes() for path in em.STATE_DIR.rglob("*.json")}


def message(text="/portfolio", *, sender=None, chat=None):
    return {
        "from": {"id": tg.CHAT_ID if sender is None else sender},
        "chat": {"id": tg.CHAT_ID if chat is None else chat},
        "text": text,
    }


@pytest.fixture
def multi(sim):
    sim.symbols += ["BARUSDT", "BAZUSDT", "QUXUSDT"]
    for base in ("BAR", "BAZ", "QUX"):
        sim.free[base] = sim.locked[base] = D(0)
    settings.update("max_open_trades", 4, expected_revision=0, actor="offline-owner")
    return sim


def test_empty_bot_portfolio_does_not_adopt_external_holdings_or_count_locked_cash_twice(sim):
    sim.free["FOO"], sim.locked["USDT"] = D(50), D(20)
    before = files(), len(sim.writes)
    report = portfolio.report()
    assert "Trade slots: 0 / 1 occupied | 1 free" in report
    assert (
        "Account USDT free: 500.0000" in report
        and "Account USDT locked on exchange: 20.0000" in report
    )
    assert "Cost of remaining bot holdings: 0.0000" in report
    assert "Normal daily risk budget remaining: 3.0000" in report
    assert "external positions are excluded" in report and "No active bot positions" in report
    assert (files(), len(sim.writes)) == before


def test_filled_positions_and_unfilled_watch_reserve_funds_and_match_next_plans_risk(multi):
    cash = multi.free["USDT"]
    first, second = buy(), buy("BARUSDT")
    actual_cost = cash - multi.free["USDT"]
    multi.price = D(101)
    watch = buy("BAZUSDT", entry_mode="watch", manual_override=True)
    reserved_cash = D(watch["approved_bounds"]["cost"])
    expected_risk = sum((D(rec["plan"]["loss"]) for rec in (first, second, watch)), D(0))
    before = files(), len(multi.writes)
    report = portfolio.report()
    assert "Trade slots: 3 / 4 occupied | 1 free" in report and "Entry watches: 1" in report
    assert f"Cost of remaining bot holdings: {actual_cost:.4f}" in report
    assert f"Reserved for bot entry watches: {reserved_cash:.4f}" in report
    assert f"Free USDT after watch reservations: {multi.free['USDT'] - reserved_cash:.4f}" in report
    assert f"Full planned risk reserved for active trades/watches: {expected_risk:.4f}" in report
    assert "Entry watch/pending entry — no verified buy fills" in report
    assert (files(), len(multi.writes)) == before
    next_plan = ta.build_plan({**sig(), "symbol": "QUXUSDT", "t1": 107, "t2": 111})
    assert next_plan["committed_risk"] == expected_risk
    assert (
        f"Normal new-trade risk allowance before plan checks: {next_plan['risk_budget']:.4f}"
        in report
    )


def test_partial_exit_keeps_full_planned_reservation_and_marks_only_remaining_inventory(sim):
    initial_cash = sim.free["USDT"]
    rec = buy()
    first = next(iter(sim.lists.values()))
    sim.fill(sim.orders[first["orders"][0]["orderId"]], price="104.5")
    sim.price = D(110)
    owned = sim.free["FOO"] + sim.locked["FOO"]
    expected_total = sim.free["USDT"] - initial_cash + owned * D(110) * D(".999")
    before = files(), len(sim.writes)
    report = portfolio.report()
    assert f"Realized + estimated unrealized (active trades): {expected_total:+.4f}" in report
    assert (
        f"Full planned risk reserved for active trades/watches: {D(rec['plan']['loss']):.4f}"
        in report
    )
    assert "Full planned risk stays reserved after partial exits" in report
    assert (files(), len(sim.writes)) == before


def test_completed_loss_enters_utc_budget_but_not_active_trade_pnl(sim):
    initial_cash = sim.free["USDT"]
    old = buy()
    for parent in sim.lists.values():
        sim.fill(sim.orders[parent["orders"][1]["orderId"]], price="97")
    assert em.reconcile_trade(old)
    loss = initial_cash - sim.free["USDT"]
    current = buy()
    report = portfolio.report()
    assert "Trade slots: 1 / 1 occupied" in report
    assert "Realized P&L on active trades: +0.0000" in report
    assert f"Net realized loss used today (UTC): {loss:.4f}" in report
    assert (
        f"Normal daily risk budget remaining: {D(3) - loss - D(current['plan']['loss']):.4f}"
        in report
    )


def test_a_single_missing_quote_does_not_become_a_false_partial_portfolio_total(multi):
    buy()
    buy("BARUSDT")
    multi.response_override = lambda m, path, data: (
        {"symbol": "BARUSDT", "price": "0"}
        if path == "ticker/price" and data.get("symbol") == "BARUSDT"
        else None
    )
    report = portfolio.report()
    assert "Remaining reference market value: unavailable" in report
    assert "Estimated unrealized P&L (after sell-fee reserve): unavailable" in report
    assert "Estimated unrealized P&L unavailable — price/sell fee missing" in report
    assert "Cost of remaining bot holdings: unavailable" not in report


def test_unavailable_fee_schedule_keeps_known_cost_but_withholds_net_estimate(sim):
    buy()

    def missing(m, path, data):
        if path == "account/commission":
            raise TimeoutError("API_SECRET=private")

    sim.response_override = missing
    report = portfolio.report()
    assert "Estimated unrealized P&L (after sell-fee reserve): unavailable" in report
    assert "Estimated loss on remaining holdings at original SLs: unavailable" in report
    assert "Cost of remaining bot holdings: unavailable" not in report
    assert "private" not in report


def test_unavailable_account_cash_does_not_hide_other_verified_metrics_or_invent_funds(
    sim, monkeypatch
):
    buy()

    def unavailable():
        raise TimeoutError("API_SECRET=private")

    monkeypatch.setattr(portfolio, "account_cash", unavailable)
    report = portfolio.report()
    assert (
        "Account USDT free: unavailable" in report
        and "Account USDT locked on exchange: unavailable" in report
    )
    assert "Free USDT after watch reservations: unavailable" in report
    assert "1% sizing reference" not in report and "private" not in report
    assert "Cost of remaining bot holdings: unavailable" not in report


def test_unresolved_exchange_evidence_is_not_valued_from_saved_fills(sim):
    buy()
    sim.read_failures = 1
    before = files(), len(sim.writes)
    report = portfolio.report()
    assert "Cost of remaining bot holdings: unavailable" in report
    assert "Estimated unrealized P&L (after sell-fee reserve): unavailable" in report
    assert "exchange verification pending" in report
    assert (files(), len(sim.writes)) == before


def test_a_partially_filled_target_without_its_stop_is_not_presented_as_protected_risk(sim):
    buy()
    first = next(iter(sim.lists.values()))
    sim.fill(sim.orders[first["orders"][0]["orderId"]], qty=".01", price="104.5", terminal=False)
    before = files(), len(sim.writes)
    report = portfolio.report()
    assert "stop coverage is incomplete" in report and "not confirmed protection" in report
    assert (files(), len(sim.writes)) == before


def test_concurrent_settings_change_withholds_totals_instead_of_mixing_revisions(sim, monkeypatch):
    buy()
    original = portfolio.positions.snapshot

    def changing(*args, **kwargs):
        view = original(*args, **kwargs)
        settings.update("max_open_trades", 2, expected_revision=0, actor="concurrent-owner")
        return view

    monkeypatch.setattr(portfolio.positions, "snapshot", changing)
    report = portfolio.report()
    assert "state changed during this check; totals unavailable" in report
    assert "Cost of remaining bot holdings: unavailable" in report
    assert "Normal daily risk budget remaining: unavailable" in report


def test_closed_dust_retains_cost_reservation_and_quantity_without_occupying_a_slot(sim, market):
    rec = replay_pepe(sim)
    fill_pepe_exits(sim, rec)
    em.reconcile_trade(rec)
    saved = em.load_trades()[0]
    cost = D(saved["retained_dust"]["cost_basis_usdt"])
    before = files(), len(sim.writes)
    report = portfolio.report()
    assert "Trade slots: 0 / 2 occupied | 2 free" in report
    assert f"0.05 PEPE | acquisition cost {cost:f}" in report
    assert "Cost of remaining bot holdings: 0.0000" in report
    assert "Estimated unrealized P&L (after sell-fee reserve): +0.0000" in report
    assert "closed retained dust is separate" in report
    assert (files(), len(sim.writes)) == before


def test_manual_risk_override_can_be_overcommitted_without_hiding_negative_budget(sim):
    settings.update("max_position_usdt", "200", expected_revision=0, actor="offline-owner")
    rec = buy(manual_override=True)
    assert D(rec["plan"]["loss"]) > 3
    report = portfolio.report()
    assert "Normal daily risk budget remaining: -" in report
    assert "Normal daily budget is overcommitted" in report
    assert "Normal new-trade risk allowance before plan checks: 0.0000" in report


def test_utc_risk_day_remains_distinct_from_dubai_report_date(sim):
    rec = buy()
    sim.clock = int(datetime(2030, 1, 1, 20, 30, tzinfo=timezone.utc).timestamp() * 1000)
    before = sim.free["USDT"]
    for parent in sim.lists.values():
        sim.fill(sim.orders[parent["orders"][1]["orderId"]], price="97")
    em.reconcile_trade(rec)
    first = portfolio.report(now=datetime(2030, 1, 1, 21, tzinfo=timezone.utc))
    next_day = portfolio.report(now=datetime(2030, 1, 2, 0, 1, tzinfo=timezone.utc))
    assert "Checked: 02 Jan 2030" in first
    assert "Net realized loss used today (UTC): 0.0000" not in first
    assert "Net realized loss used today (UTC): 0.0000" in next_day
    assert sim.free["USDT"] > before and "Normal daily risk budget remaining: 3.0000" in next_day


def test_report_cannot_write_journals_plan_or_submit_cancel_sell(sim, monkeypatch):
    buy()

    def forbidden(*args, **kwargs):
        pytest.fail("Portfolio report may not write or trade")

    for name in (
        "_atomic_write",
        "submit_order",
        "cancel_protection",
        "reconcile_trade",
        "emergency_sell_free",
    ):
        monkeypatch.setattr(em, name, forbidden)
    monkeypatch.setattr(ta, "build_plan", forbidden)
    report = portfolio.report()
    assert (
        "NEXVANT | PORTFOLIO" in report
        and "Cost of remaining bot holdings: unavailable" not in report
    )


@pytest.mark.parametrize("field", ["sender", "chat"])
def test_portfolio_command_is_owner_only(sim, monkeypatch, field):
    monkeypatch.setattr(portfolio, "report", lambda **kwargs: pytest.fail("Unauthorized report"))
    monkeypatch.setattr(tg, "say", lambda *args, **kwargs: pytest.fail("Unauthorized reply"))
    tg.handle_message(message(**{field: "stranger"}))
    assert not sim.writes


@pytest.mark.parametrize("broken", ["journal", "settings"])
def test_invalid_local_state_never_falls_back_to_empty_positions_or_default_limits(
    sim, monkeypatch, broken
):
    path = (
        em.TRADES_DIR / "invalid.json"
        if broken == "journal"
        else em.TRADES_DIR.parent / "settings.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"API_SECRET":"private"}')
    captured = []
    monkeypatch.setattr(tg, "say", lambda text, **kwargs: captured.append(text))
    tg.handle_message(message())
    assert "Portfolio unavailable" in captured[-1]
    assert "Trade slots: 0" not in captured[-1] and "private" not in captured[-1]


def test_command_chunks_long_report_without_consuming_forms_or_approvals(sim, monkeypatch):
    captured = []
    tg.pending["existing"] = {"approved": False}
    tg.state.update(step="manual_levels", symbol="FOOUSDT")
    before = copy.deepcopy((tg.pending, tg.state))
    monkeypatch.setattr(portfolio, "report", lambda: "a" * 7200)
    monkeypatch.setattr(tg, "say", lambda text, **kwargs: captured.append(text))
    tg.handle_message(message("/portfolio@NexvantBot"))
    assert [len(text) for text in captured] == [3500, 3500, 200]
    assert (tg.pending, tg.state) == before and not sim.writes
