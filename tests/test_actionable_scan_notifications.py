"""Busy users get proposal controls; routine scanning stays in diagnostics."""

import copy
import json
import time

import pytest
from test_bot import approved, sig

import entry_scanner as sc
import exit_manager as em
import telegram_assistant as tg
import telegram_presentation as ui
import trade_assistant as ta


def message(text, *, sender=None, chat=None):
    return {
        "chat": {"id": tg.CHAT_ID if chat is None else chat},
        "from": {"id": tg.CHAT_ID if sender is None else sender},
        "text": text,
    }


@pytest.fixture
def messages(monkeypatch):
    calls = []
    monkeypatch.setattr(sc, "last_alert", {})
    monkeypatch.setattr(
        tg,
        "say",
        lambda text, **params: (
            calls.append((text, params)) or {"ok": True, "result": {"message_id": len(calls)}}
        ),
    )
    return calls


def test_mixed_scan_only_interrupts_for_the_real_approvable_proposal(sim, messages, monkeypatch):
    original = ta.build_plan
    attempted = []
    candidates = {
        symbol: {**sig(), "symbol": symbol} for symbol in ("NEARUSDT", "FOOUSDT", "ONDOUSDT")
    }

    def plan(signal):
        attempted.append(signal["symbol"])
        if signal["symbol"] == "NEARUSDT":
            raise ValueError("TP1 reward/risk is below 1.5R after price/tick checks")
        return original(signal)

    monkeypatch.setattr(ta, "scan_candidates", lambda: (candidates, "BTC warning"))
    monkeypatch.setattr(ta, "build_plan", plan)
    sc.last_alert["ONDOUSDT"] = time.time() - sc.COOLDOWN_SECONDS + 5
    tg.scan_once()
    assert attempted == ["NEARUSDT", "FOOUSDT"]
    assert len(messages) == 1 and "TRADE PROPOSAL" in messages[0][0]
    buttons = json.loads(messages[0][1]["reply_markup"])["inline_keyboard"]
    assert any(button["callback_data"].startswith("A:") for row in buttons for button in row)
    assert any(button["callback_data"].startswith("R:") for row in buttons for button in row)
    assert "Potential Token Detected" not in messages[0][0]
    assert len(tg.pending) == 1 and tg.last_scan["sent"] == 1
    assert tg.last_preview["kind"] == "proposal" and "FOOUSDT" in tg.last_preview["text"]
    assert "NEARUSDT" not in sc.last_alert and "FOOUSDT" in sc.last_alert
    assert not sim.writes
    messages.clear()
    tg.handle_message(message("/scanlog"))
    report = "\n".join(text for text, _ in messages)
    assert "NEARUSDT: Blocked" in report and "ONDOUSDT: Already alerted recently" in report
    assert "Proposals delivered: 1" in report and "Dubai" in report
    assert not any("reply_markup" in params for _, params in messages)
    assert len(tg.pending) == 1 and not sim.writes


def test_all_valid_candidates_each_get_exactly_one_card_without_a_duplicate_summary(
    sim, messages, monkeypatch
):
    original = ta.build_plan
    candidates = {symbol: {**sig(), "symbol": symbol} for symbol in ("FOOUSDT", "BARUSDT")}
    sim.symbols.append("BARUSDT")
    sim.free["BAR"], sim.locked["BAR"] = em.D(0), em.D(0)
    monkeypatch.setattr(ta, "scan_candidates", lambda: (candidates, ""))
    monkeypatch.setattr(ta, "build_plan", original)
    tg.scan_once()
    assert len(messages) == len(tg.pending) == tg.last_scan["sent"] == 2
    assert all("reply_markup" in params for _, params in messages)
    assert all("TRADE PROPOSAL" in text and "Potential Token" not in text for text, _ in messages)
    assert not sim.writes


def test_later_rejections_and_cooldowns_cannot_bury_the_last_delivered_proposal(
    sim, messages, monkeypatch
):
    assert tg.send_proposal(sig(), ta.build_plan(sig()), "")
    saved = copy.deepcopy(tg.last_preview)
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({"NEARUSDT": sig(), "ONDOUSDT": sig()}, ""))
    monkeypatch.setattr(
        ta, "build_plan", lambda signal: (_ for _ in ()).throw(ValueError("minimum order value"))
    )
    sc.last_alert["ONDOUSDT"] = time.time()
    messages.clear()
    tg.scan_once()
    assert not messages and tg.last_preview == saved
    tg.handle_message(message("SHOW PR"))
    assert "TRADE PROPOSAL" in messages[-1][0] and "VIEW ONLY" in messages[-1][0]
    assert "Potential Token" not in messages[-1][0] and "reply_markup" not in messages[-1][1]


def test_delivery_failure_does_not_send_a_second_fallback_or_start_cooldown(sim, monkeypatch):
    attempts = []
    monkeypatch.setattr(sc, "last_alert", {})
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({"FOOUSDT": sig()}, ""))
    monkeypatch.setattr(
        tg, "say", lambda text, **params: attempts.append((text, params)) or {"ok": False}
    )
    tg.scan_once()
    assert len(attempts) == 1 and "TRADE PROPOSAL" in attempts[0][0]
    assert not tg.pending and not sc.last_alert and not sim.writes
    assert (
        tg.last_scan["sent"] == 0
        and tg.last_scan["outcomes"][0]["status"] == "Not delivered / canceled"
    )


def test_owner_cancellation_during_delivery_does_not_register_a_proposal_or_emit_a_skip_summary(
    sim, monkeypatch
):
    sent, edits = [], []
    monkeypatch.setattr(sc, "last_alert", {})
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({"FOOUSDT": sig()}, ""))

    def canceled(text, **params):
        sent.append(text)
        monkeypatch.setattr(tg, "proposal_generation", tg.proposal_generation + 1)
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(tg, "say", canceled)
    monkeypatch.setattr(
        tg, "tg", lambda method, **params: edits.append((method, params)) or {"ok": True}
    )
    tg.scan_once()
    assert len(sent) == 1 and len(edits) == 1
    assert "CANCELLED" in edits[0][1]["text"]
    assert not tg.pending and not sc.last_alert and tg.last_scan["sent"] == 0
    assert not sim.writes


def test_quiet_scanning_does_not_suppress_entry_or_final_trade_notifications(
    sim, messages, monkeypatch
):
    plan = ta.build_plan(sig())
    notices = []
    monkeypatch.setattr(em, "alert", lambda text: notices.append(text) or True)
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({"NEARUSDT": sig()}, ""))
    with monkeypatch.context() as temporary:
        temporary.setattr(
            ta,
            "build_plan",
            lambda signal: (_ for _ in ()).throw(ValueError("reward/risk below 1.5R")),
        )
        tg.scan_once()
    assert not messages and not notices
    rec = ta.execute(plan, approval=approved(plan), monitor=False)
    assert any("BUY FILLED / PROTECTION CONFIRMED" in text for text in notices)
    for parent in sim.lists.values():
        sim.fill(sim.orders[parent["orders"][1]["orderId"]], price="97")
    em.reconcile_trade(rec)
    assert any(
        "TRADE CLOSED" in text and "Stop loss executed successfully" in text for text in notices
    )


@pytest.mark.parametrize("field", ["sender", "chat"])
def test_scanlog_is_owner_only(messages, field):
    tg.handle_message(message("/scanlog", **{field: "stranger"}))
    assert not messages


def test_scanlog_reports_empty_startup_then_zero_candidates_without_background_messages(
    messages, monkeypatch
):
    tg.handle_message(message("/scanlog"))
    assert "No automatic scan recorded since startup" in messages[-1][0]
    messages.clear()
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({}, ""))
    tg.scan_once()
    assert not messages
    tg.handle_message(message("/scanlog@NexvantBot"))
    assert "Candidates: 0" in messages[-1][0] and "No chart candidates" in messages[-1][0]


def test_scanlog_preserves_forms_approvals_and_has_a_native_menu_entry(messages, monkeypatch):
    tg.state.update(step="manual_levels", symbol="FOOUSDT")
    tg.pending["existing"] = {"approved": False}
    saved = copy.deepcopy((tg.state, tg.pending))
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({}, ""))
    tg.scan_once()
    tg.handle_message(message("/scanlog"))
    assert (tg.state, tg.pending) == saved
    assert any(command["command"] == "scanlog" for command in ui.COMMANDS)
    assert "/scanlog" in tg.HELP
