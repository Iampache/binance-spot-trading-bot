"""Closed-trade accounting and cooperative shutdown remain read-only/order-safe."""

import signal
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from test_bot import approved, sig

import exit_manager as em
import shutdown_control as shutdown
import telegram_assistant as tg
import trade_assistant as ta
import trade_reporting as reports

NOW = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)


def closed_trade(
    sim, *, stop=False, first="2030-01-01T20:01:00+00:00", last="2030-01-01T20:02:00+00:00"
):
    plan = ta.build_plan(sig())
    rec = ta.execute(plan, approval=approved(plan), monitor=False)
    for item, timestamp in zip(list(sim.lists.values())[-2:], (first, last)):
        sim.clock = int(datetime.fromisoformat(timestamp).timestamp() * 1000)
        sim.fill(
            sim.orders[item["orders"][1 if stop else 0]["orderId"]], price="97" if stop else "104.5"
        )
    assert em.reconcile_trade(rec)
    return rec


@pytest.mark.parametrize("stop", [False, True])
def test_completed_today_uses_dubai_fill_day_and_verified_net_fees(sim, stop):
    balance = sim.free["USDT"]
    closed_trade(sim, stop=stop)
    count = len(sim.writes)
    report = reports.todays_pnl(NOW)
    net = sim.free["USDT"] - balance
    assert "2030-01-02 · Dubai (UTC+4)" in report
    assert "Completed: 1" in report and f"Net P&L: {net:+.4f} USDT" in report
    assert len(sim.writes) == count
    assert f"Losses: {int(stop)}" in report


def test_pnl_uses_final_sell_day_and_full_trade_result_across_midnight(sim):
    before = sim.free["USDT"]
    closed_trade(sim, first="2030-01-01T19:30:00+00:00", last="2030-01-01T20:30:00+00:00")
    text = reports.todays_pnl(NOW)
    assert "Completed: 1" in text and f"Net P&L: {sim.free['USDT'] - before:+.4f} USDT" in text


def test_pnl_sums_winners_and_losers_without_counting_other_account_holdings(sim):
    before = sim.free["USDT"]
    closed_trade(sim)
    closed_trade(sim, stop=True)
    text = reports.todays_pnl(NOW)
    assert "Completed: 2 | Wins: 1 | Losses: 1" in text
    assert f"Net P&L: {sim.free['USDT'] - before:+.4f} USDT" in text
    assert "Profit: +" in text and "Loss: -" in text


def test_completed_previous_day_and_unfilled_watch_are_not_counted(sim):
    closed_trade(sim, first="2030-01-01T19:00:00+00:00", last="2030-01-01T19:59:00+00:00")
    sim.price = em.D(101)
    plan = ta.build_plan({**sig(), "entry_mode": "watch", "manual_override": True})
    rec = ta.execute(plan, approval=approved(plan), monitor=False)
    ta.cancel_entry_watches()
    assert em.load_trades()[0]["state"] == "closed" and rec["state"] == "watching_entry"
    assert "Completed: 0" in reports.todays_pnl(NOW)


def test_partial_exits_are_excluded_until_trade_is_fully_accounted(sim):
    plan = ta.build_plan(sig())
    rec = ta.execute(plan, approval=approved(plan), monitor=False)
    first = next(iter(sim.lists.values()))
    sim.fill(sim.orders[first["orders"][0]["orderId"]], price="104.5")
    em.reconcile_trade(rec)
    report = reports.todays_pnl()
    assert "Completed: 0" in report and "Net P&L: +0.0000 USDT" in report
    assert "excluded: 1" in report and em.inventory(rec) > 0


def test_pnl_command_is_owner_only_and_does_not_hide_invalid_accounting(sim, monkeypatch):
    messages = []
    monkeypatch.setattr(tg, "say", lambda text, **kwargs: messages.append(text))
    tg.handle_message({"chat": {"id": "stranger"}, "from": {"id": "stranger"}, "text": "/pnl"})
    assert not messages
    (em.TRADES_DIR / "invalid.json").parent.mkdir(parents=True, exist_ok=True)
    (em.TRADES_DIR / "invalid.json").write_text("{}")
    tg.handle_message({"chat": {"id": tg.CHAT_ID}, "from": {"id": tg.CHAT_ID}, "text": "/pnl"})
    assert "P&L unavailable" in messages[-1] and "Net P&L" not in messages[-1]
    assert not sim.writes


def test_stop_notice_is_flushed_once_before_pending_work_finishes(sim, capsys):
    tg.request_shutdown()
    assert em.STOP.is_set()
    assert (
        "Please wait while I finish saving the current session details" in capsys.readouterr().out
    )
    tg.request_shutdown()
    assert not capsys.readouterr().out and not sim.writes


def test_signal_handlers_request_cooperative_stop_and_restore_previous_handlers(monkeypatch):
    previous = {signal.SIGINT: object(), signal.SIGTERM: object()}
    installed = dict(previous)
    calls = []
    monkeypatch.setattr(shutdown, "os", SimpleNamespace(name="posix"))

    def replace(number, handler):
        old = installed[number]
        installed[number] = handler
        return old

    monkeypatch.setattr(shutdown.signal, "signal", replace)
    restore = shutdown.install(lambda: calls.append("stop"))
    installed[signal.SIGINT](signal.SIGINT, None)
    assert calls == ["stop"]
    restore()
    assert installed == previous


def test_windows_console_callback_can_announce_stop_while_main_thread_waits(monkeypatch):
    import ctypes

    class Register:
        def __init__(self):
            self.calls = []

        def __call__(self, callback, enabled):
            self.calls.append((callback, enabled))
            return True

    register = Register()
    monkeypatch.setattr(shutdown, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        ctypes, "WINFUNCTYPE", lambda *args: lambda callback: callback, raising=False
    )
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *a, **kw: SimpleNamespace(SetConsoleCtrlHandler=register),
        raising=False,
    )
    monkeypatch.setattr(shutdown.signal, "signal", lambda *args: None)
    calls = []
    restore = shutdown.install(lambda: calls.append("stop"))
    callback = register.calls[0][0]
    assert callback(0) is True and callback(1) is True and callback(2) is False
    assert calls == ["stop", "stop"]
    restore()
    assert register.calls[-1] == (callback, False)


def test_shutdown_notice_precedes_join_and_instance_lock_release(sim, monkeypatch, capsys):
    order = []

    class Worker:
        def join(self):
            assert "Please wait" in capsys.readouterr().out
            order.append("joined")

    class Lock:
        def unlink(self):
            order.append("released")

    def run():
        tg.workers.add(Worker())
        tg.request_shutdown()

    monkeypatch.setattr(tg, "TOKEN", "synthetic")
    monkeypatch.setattr(tg, "CHAT_ID", "synthetic")
    monkeypatch.setattr(tg, "_run_main", run)
    monkeypatch.setattr(tg, "prompt_parallel_count", lambda: 1)
    monkeypatch.setattr(em, "instance_lock", lambda: Lock())
    tg.main()
    assert order == ["joined", "released"] and not tg.workers
    assert "Session records preserved" in capsys.readouterr().out and not sim.writes
