"""Canceled/consumed Telegram controls disappear and remain inert after failure."""

import json
import time

import pytest
from test_bot import approved, sig

import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta


def msg(text):
    return {"chat": {"id": tg.CHAT_ID}, "from": {"id": tg.CHAT_ID}, "text": text}


def callback(action, cid, mid):
    return {
        "id": "cb",
        "from": {"id": tg.CHAT_ID},
        "message": {"chat": {"id": tg.CHAT_ID}, "message_id": mid},
        "data": f"{action}:{cid}",
    }


@pytest.fixture
def ui(monkeypatch):
    calls = []
    serial = 100

    def transport(method, **kwargs):
        nonlocal serial
        calls.append((method, kwargs))
        serial += 1
        return {"ok": True, "result": {"message_id": serial}}

    monkeypatch.setattr(tg, "tg", transport)
    result = {
        "symbol": "FOOUSDT",
        "price": 100,
        "chg": 1,
        "qv": 20000000,
        "sig": sig(),
        "reasons": [],
        "status": "SAFE",
        "note": "",
    }
    tg.send_check_card(result)
    return calls, result


def empty_keyboard(call):
    return json.loads(call[1]["reply_markup"]) == {"inline_keyboard": []}


def test_cancel_targets_current_options_message_and_invalidates_old_buttons(sim, ui):
    calls, result = ui
    cid, original = next(iter(tg.checks.items()))
    original_mid = original["mid"]
    tg.handle_callback(callback("C3", cid, original_mid))
    menu = tg.checks[cid]
    assert menu["mid"] != original_mid and menu["created"] == original["created"]
    assert any(
        method == "editMessageText"
        and data["message_id"] == original_mid
        and empty_keyboard((method, data))
        for method, data in calls
    )
    tg.handle_callback(callback("CX", cid, menu["mid"]))
    assert not tg.checks and not tg.state
    assert any(
        method == "editMessageText"
        and data["message_id"] == menu["mid"]
        and "CANCELLED" in data["text"]
        and empty_keyboard((method, data))
        for method, data in calls
    )
    tg.handle_callback(callback("CQ", cid, menu["mid"]))
    assert calls[-1][0] == "answerCallbackQuery"
    assert any(
        method == "editMessageReplyMarkup" and data["message_id"] == menu["mid"]
        for method, data in calls
    )
    assert not tg.pending and not sim.writes


def test_global_cancel_removes_current_menu_and_approval_keyboards(sim, ui):
    calls, result = ui
    cid, original = next(iter(tg.checks.items()))
    tg.handle_callback(callback("C3", cid, original["mid"]))
    menu_mid = tg.checks[cid]["mid"]
    tg.send_proposal(sig(), ta.build_plan(sig()), "")
    proposal_mid = next(iter(tg.pending.values()))["mid"]
    tg.handle_message(msg("/cancel"))
    removed = {
        data["message_id"]
        for method, data in calls
        if method == "editMessageText" and empty_keyboard((method, data))
    }
    assert menu_mid in removed and proposal_mid in removed
    assert not tg.checks and not tg.pending and not tg.state and not sim.writes


def test_successful_manual_proposal_consumes_mode_menu_without_buying(sim, ui):
    calls, result = ui
    cid, original = next(iter(tg.checks.items()))
    tg.handle_callback(callback("C3", cid, original["mid"]))
    menu = tg.checks[cid]
    tg.handle_callback(callback("CN", cid, menu["mid"]))
    tg.handle_message(msg("100 97 105 108"))
    assert tg.pending and not tg.checks and not sim.writes
    assert any(
        method == "editMessageText"
        and data["message_id"] == menu["mid"]
        and "Proposal sent below" in data["text"]
        and empty_keyboard((method, data))
        for method, data in calls
    )


def test_expired_selector_keyboard_is_removed_without_executing(sim, ui):
    calls, result = ui
    cid, item = next(iter(tg.checks.items()))
    item["created"] = time.time() - tg.CHECK_TTL - 1
    tg.expire_pending()
    assert cid not in tg.checks
    assert any(
        method == "editMessageText"
        and data["message_id"] == item["mid"]
        and "EXPIRED" in data["text"]
        and empty_keyboard((method, data))
        for method, data in calls
    )
    assert not sim.writes


def test_backend_stays_cancelled_if_telegram_edit_fails(sim, ui, monkeypatch):
    calls, result = ui
    cid, item = next(iter(tg.checks.items()))
    monkeypatch.setattr(tg, "tg", lambda *args, **kw: {"ok": False})
    tg.handle_callback(callback("CX", cid, item["mid"]))
    tg.handle_callback(callback("C1", cid, item["mid"]))
    assert not tg.checks and not tg.pending and not sim.writes


def test_late_check_after_generation_cancel_has_no_active_buttons(sim, ui):
    calls, result = ui
    tg.checks.clear()
    tg.send_check_card(result, generation=tg.proposal_generation - 1)
    assert not tg.checks
    assert calls[-1][0] == "editMessageText" and empty_keyboard(calls[-1])
    assert "CANCELLED" in calls[-1][1]["text"] and not sim.writes


def test_cancel_and_restart_leave_saved_position_and_exchange_protection_intact(
    sim, ui, monkeypatch
):
    calls, result = ui
    monkeypatch.setattr(em, "monitor_trade", lambda record: None)
    plan = ta.build_plan(sig())
    rec = ta.execute(plan, approval=approved(plan))
    quantity = em.inventory(rec)
    writes = len(sim.writes)
    tg.handle_message(msg("/cancel"))
    assert len(em.active_trades()) == 1 and len(sim.writes) == writes
    em.recover_trades()
    saved = em.load_trades()[0]
    assert saved["state"] == "protected" and em.inventory(saved) == quantity
    assert em.coverage(saved) >= quantity and len(sim.writes) == writes


def test_replaced_parent_card_cannot_trigger_a_new_plan(sim, ui):
    calls, result = ui
    cid, original = next(iter(tg.checks.items()))
    tg.handle_callback(callback("C3", cid, original["mid"]))
    menu_mid = tg.checks[cid]["mid"]
    tg.handle_callback(callback("C1", cid, original["mid"]))
    assert tg.checks[cid]["mid"] == menu_mid
    assert not tg.pending and not sim.writes
    assert calls[-1][1]["text"] == "This menu was replaced. Use the latest menu."


def test_expired_menu_also_clears_associated_input_form(sim, ui):
    calls, result = ui
    cid, original = next(iter(tg.checks.items()))
    tg.handle_callback(callback("C3", cid, original["mid"]))
    tg.handle_callback(callback("CN", cid, tg.checks[cid]["mid"]))
    assert tg.state["menu_id"] == cid
    tg.checks[cid]["created"] = time.time() - tg.CHECK_TTL - 1
    tg.expire_pending()
    tg.handle_message(msg("100 97 105 108"))
    assert not tg.state and not tg.pending and not sim.writes
