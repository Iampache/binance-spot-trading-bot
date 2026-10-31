"""Owner setup needs a current, explicit confirmation and never authorizes a buy."""

import json
import time

import pytest
from test_bot import approved, sig

import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta
import trading_settings as settings


def msg(text, sender=None):
    sender = tg.CHAT_ID if sender is None else sender
    return {"chat": {"id": sender}, "from": {"id": sender}, "text": text}


def click(action, sid, item):
    return {
        "id": "cb",
        "from": {"id": tg.CHAT_ID},
        "message": {"chat": {"id": tg.CHAT_ID}, "message_id": item["mid"]},
        "data": f"{action}:{sid}",
    }


@pytest.fixture
def ui(monkeypatch):
    calls = []

    def transport(method, **params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": len(calls)}}

    monkeypatch.setattr(tg, "tg", transport)
    return calls


def confirmation(action="ST", value="2"):
    tg.handle_message(msg("/setup"))
    sid, item = next(iter(tg.setup_views.items()))
    tg.handle_callback(click(action, sid, item))
    tg.handle_message(msg(value))
    return next(iter(tg.setup_views.items()))


@pytest.mark.parametrize(
    "action,value,field,expected",
    [("ST", "3", "max_open_trades", 3), ("SI", "25.50", "max_position_usdt", "25.50")],
)
def test_setup_review_then_explicit_confirmation_persists_limits(
    sim, ui, action, value, field, expected
):
    sid, item = confirmation(action, value)
    assert settings.read()["revision"] == 0 and not sim.writes
    assert "Confirm trading limits" in item["text"]
    tg.handle_callback(click("SA", sid, item))
    assert settings.read()[field] == expected and settings.read()["revision"] == 1
    assert not tg.setup_views and not tg.state and not tg.pending and not sim.writes
    tg.handle_callback(click("SA", sid, item))
    assert settings.read()["revision"] == 1


@pytest.mark.parametrize("how", ["cancel", "global", "expired", "stranger", "wrong_message"])
def test_invalid_confirmation_cannot_change_limits(sim, ui, how):
    sid, item = confirmation()
    cb = click("SA", sid, item)
    if how == "cancel":
        tg.handle_callback(click("SX", sid, item))
    elif how == "global":
        tg.handle_message(msg("/cancel"))
    elif how == "expired":
        item["created"] = time.time() - tg.CHECK_TTL - 1
    elif how == "stranger":
        cb["from"]["id"] = "stranger"
    else:
        cb["message"]["message_id"] += 1
    tg.handle_callback(cb)
    assert settings.read()["revision"] == 0 and not sim.writes


def test_unauthorized_setup_message_is_ignored(sim, ui):
    tg.handle_message(msg("/setup", "stranger"))
    assert not ui and not tg.setup_views and settings.read()["revision"] == 0


def test_invalid_number_keeps_prompt_without_changing_settings(sim, ui):
    tg.handle_message(msg("/setup"))
    sid, item = next(iter(tg.setup_views.items()))
    tg.handle_callback(click("ST", sid, item))
    tg.handle_message(msg("NaN"))
    assert "whole number" in ui[-1][1]["text"]
    assert tg.state["step"] == "setup_value" and settings.read()["revision"] == 0


def test_saving_limits_invalidates_pending_proposals_and_untriggered_watch(sim, ui):
    sim.price = em.D(101)
    plan = ta.build_plan({**sig(), "entry_mode": "watch", "manual_override": True})
    rec = ta.execute(plan, approval=approved(plan), monitor=False)
    tg.send_proposal(sig(), plan, "")
    assert tg.pending and rec["entry_watch"]["active"]
    sid, item = confirmation()
    tg.handle_callback(click("SA", sid, item))
    assert not tg.pending and not em.active_trades() and not sim.writes
    assert em.load_trades()[0]["entry_watch"]["result"] == "cancelled by user"
    assert any("SETTINGS CHANGED" in p.get("text", "") for _, p in ui)


def test_saving_limits_preserves_protected_position_and_uses_new_text(sim, ui):
    plan = ta.build_plan(sig())
    rec = ta.execute(plan, approval=approved(plan), monitor=False)
    writes = len(sim.writes)
    sid, item = confirmation("SI", "25")
    tg.handle_callback(click("SA", sid, item))
    saved = em.active_trades()[0]
    assert saved["id"] == rec["id"] and em.coverage(saved) >= em.inventory(saved) > 0
    assert len(sim.writes) == writes
    tg.handle_message(msg("/setup"))
    assert "25 USDT" in ui[-1][1]["text"]
    buttons = json.loads(ui[-1][1]["reply_markup"])["inline_keyboard"]
    assert buttons[0][0]["text"] == "Change max trades"


def test_two_open_menus_cannot_overwrite_a_newer_revision(sim, ui):
    sid, item = confirmation()
    settings.update("max_position_usdt", "25", expected_revision=0, actor="other-confirmation")
    tg.handle_callback(click("SA", sid, item))
    assert settings.read()["max_open_trades"] == 1
    assert "could not be confirmed" in ui[-1][1]["text"]
    assert not sim.writes
