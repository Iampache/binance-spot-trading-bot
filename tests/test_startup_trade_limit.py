"""Beginner startup choices are bounded, session-only and precede connections."""

import json
from datetime import datetime, timezone

import pytest
from test_bot import approved, sig
from test_setup_commands import click, confirmation
from test_setup_commands import ui as ui

import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta
import trading_settings as settings
from telegram_assistant import prompt_parallel_count


@pytest.mark.parametrize("value,expected", [("", 1), ("1", 1), ("10", 10)])
def test_startup_accepts_only_a_bounded_explicit_count_or_safe_default(
    sim, monkeypatch, value, expected
):
    monkeypatch.setattr("builtins.input", lambda prompt: value)
    assert prompt_parallel_count() == expected and not sim.writes


def test_startup_reprompts_for_invalid_numbers_including_fifty(sim, monkeypatch, capsys):
    replies = iter(["50", "11", "0", "2.5", "NaN", "3"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))
    assert prompt_parallel_count() == 3
    assert "from 1 to 10" in capsys.readouterr().out
    assert not sim.writes


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_canceling_the_prompt_makes_no_connection_or_order_request(sim, monkeypatch, error):
    def stopped(prompt):
        raise error

    monkeypatch.setattr("builtins.input", stopped)
    assert prompt_parallel_count() is None and not sim.writes


def test_main_prompts_before_exchange_initialization_and_applies_only_a_session_limit(
    sim, monkeypatch
):
    events = []
    initialize = em.initialize

    def answer(prompt):
        assert not events
        events.append("prompt")
        return "4"

    def connect():
        events.append("exchange")
        return initialize()

    def run():
        events.append("run")
        assert settings.read()["max_open_trades"] == 4
        assert settings.read()["scopes"]["max_open_trades"] == "session"

    monkeypatch.setattr(tg, "TOKEN", "synthetic")
    monkeypatch.setattr(tg, "CHAT_ID", "synthetic")
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(em, "initialize", connect)
    monkeypatch.setattr(tg, "_run_main", run)
    tg.main()
    assert events == ["prompt", "exchange", "run"]
    assert not (em.TRADES_DIR.parent / "settings.json").exists() and not sim.writes
    settings.reset_session()
    assert settings.read()["max_open_trades"] == 1


def test_aborted_startup_never_connects_or_starts_workers(sim, monkeypatch):
    monkeypatch.setattr(tg, "TOKEN", "synthetic")
    monkeypatch.setattr(tg, "CHAT_ID", "synthetic")
    monkeypatch.setattr(tg, "prompt_parallel_count", lambda: None)
    monkeypatch.setattr(em, "initialize", lambda: pytest.fail("Canceled startup cannot connect"))
    monkeypatch.setattr(
        tg, "_run_main", lambda: pytest.fail("Canceled startup cannot start workers")
    )
    tg.main()
    assert not sim.writes


@pytest.mark.parametrize("action", ["SU", "SP"])
def test_chat_cannot_persist_limits_even_with_a_forged_old_save_action(sim, ui, action):
    sid, item = confirmation("ST", "4")
    tg.handle_callback(click(action, sid, item))
    assert settings.read()["max_open_trades"] == 1 and settings.read()["revision"] == 0
    assert not (em.TRADES_DIR.parent / "settings.json").exists()
    assert any("administrator-managed" in data.get("text", "") for _, data in ui)
    tg.handle_callback(click("SA", sid, item))
    assert settings.read()["max_open_trades"] == 4 and not sim.writes


@pytest.mark.parametrize("value", [11, 50, 100])
def test_all_new_limit_changes_enforce_the_application_ceiling(sim, value):
    with pytest.raises(ValueError, match="1 to 10"):
        settings.update(
            "max_open_trades", value, expected_revision=0, actor="test", scope="session"
        )
    assert settings.read()["max_open_trades"] == 1 and not sim.writes


def test_historical_saved_fifty_is_readable_but_cannot_authorize_more_than_ten(sim):
    data = dict(
        version=1,
        context=dict(em.CONTEXT),
        revision=1,
        max_open_trades=50,
        max_position_usdt="12",
        changes=[
            dict(
                revision=1,
                field="max_open_trades",
                old=1,
                new=50,
                actor="legacy-owner",
                changed_utc=datetime.now(timezone.utc).isoformat(),
            )
        ],
    )
    path = em.TRADES_DIR.parent / "settings.json"
    em._atomic_write(path, data)
    before = path.read_bytes()
    current = settings.read()
    assert current["max_open_trades"] == 10 and current["saved_limits"]["max_open_trades"] == 50
    settings.update(
        "max_open_trades",
        3,
        expected_revision=current["revision"],
        actor="console",
        scope="session",
    )
    assert settings.read()["max_open_trades"] == 3 and path.read_bytes() == before
    assert json.loads(path.read_text())["max_open_trades"] == 50


def test_restart_with_a_lower_limit_preserves_both_existing_protected_trades(sim):
    sim.symbols.append("BARUSDT")
    sim.free["BAR"] = sim.locked["BAR"] = em.D(0)
    settings.update("max_open_trades", 2, expected_revision=0, actor="console", scope="session")
    for symbol in ("FOOUSDT", "BARUSDT"):
        plan = ta.build_plan({**sig(), "symbol": symbol})
        ta.execute(plan, approval=approved(plan), monitor=False)
    count = len(sim.writes)
    settings.reset_session()
    assert settings.read()["max_open_trades"] == 1
    em.recover_trades()
    assert len(em.active_trades()) == 2 and all(
        rec["state"] == "protected" for rec in em.active_trades()
    )
    assert len(sim.writes) == count
    with pytest.raises(ta.ActiveTradeError):
        ta.build_plan({**sig(), "symbol": "BAZUSDT"})


def test_invalid_saved_settings_still_allow_startup_recovery_of_existing_stops(
    sim, monkeypatch, capsys
):
    plan = ta.build_plan(sig())
    ta.execute(plan, approval=approved(plan), monitor=False)
    before = len(sim.writes)
    path = em.TRADES_DIR.parent / "settings.json"
    path.write_text('{"private":"private-invalid-settings-content"}')
    recovered = []

    def run():
        recovered.extend(em.recover_trades())
        with pytest.raises(RuntimeError, match="settings invalid"):
            ta.build_plan(sig())

    monkeypatch.setattr(tg, "TOKEN", "synthetic")
    monkeypatch.setattr(tg, "CHAT_ID", "synthetic")
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    monkeypatch.setattr(tg, "_run_main", run)
    tg.main()
    assert len(recovered) == 1 and recovered[0]["state"] == "protected"
    assert len(sim.writes) == before
    assert "private-invalid-settings-content" not in capsys.readouterr().out
