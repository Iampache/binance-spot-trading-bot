"""Decision-card estimates and read-only disclosure must preserve authorization."""

import json
import time
from decimal import Decimal as D

import pytest
from test_bot import sig

import telegram_assistant as tg
import trade_assistant as ta


def callback(action, pid, sender=None):
    sender = tg.CHAT_ID if sender is None else sender
    return {
        "id": "cb",
        "from": {"id": sender},
        "message": {"chat": {"id": sender}},
        "data": f"{action}:{pid}",
    }


def test_profit_estimate_charges_all_entry_cost_and_full_reserved_exit_fees(sim):
    p = ta.build_plan(sig())
    text = tg.proposal_text(sig(), p, "")
    expected = (p["half"] * p["t1"] + p["rest"] * p["t2"]) * (1 - p["fee_rate"]) - p["cost"]
    assert f"USD {expected:+.2f}" in text
    assert f"initial stop: USD {p['loss']:.2f}" in text
    assert "reserved fees" in text and "slippage" in text
    assert "no automatic breakeven" in text
    assert "maximum buy price" in text and "Type: Automated" in text
    assert len(text) < len(tg.proposal_details_text(sig(), p, ""))
    assert not sim.writes


def test_negative_target_scenario_not_presented_as_positive_profit(sim):
    p = ta.build_plan(sig())
    # Synthetic costly scenario tests display only, never execution validation.
    p["fee_rate"] = D(".10")
    text = tg.proposal_text(sig(), p, "")
    assert "both targets fill: USD -" in text
    assert not sim.writes


def test_details_does_not_consume_or_renew_approval(sim, monkeypatch):
    messages = []
    replies = []
    monkeypatch.setattr(
        tg,
        "say",
        lambda text, **kw: messages.append((text, kw)) or {"ok": True, "result": {"message_id": 1}},
    )
    monkeypatch.setattr(tg, "tg", lambda *a, **kw: replies.append(kw) or {"ok": True})
    tg.send_proposal(sig(), ta.build_plan(sig()), "BTC downtrend")
    pid, item = next(iter(tg.pending.items()))
    created = item["created"]
    keyboard = json.loads(messages[0][1]["reply_markup"])
    assert any(b["callback_data"] == f"D:{pid}" for row in keyboard["inline_keyboard"] for b in row)
    tg.handle_callback(callback("D", pid))
    assert tg.pending[pid] is item and item["created"] == created
    assert "DETAILS — VIEW ONLY" in messages[-1][0]
    assert "BTC downtrend" in messages[-1][0]
    assert "reply_markup" not in messages[-1][1]
    assert not tg.trade_lock.locked() and not sim.writes


@pytest.mark.parametrize("state", ["expired", "cancelled", "stranger"])
def test_details_invalid_authorization_has_no_side_effects(sim, monkeypatch, state):
    messages = []
    monkeypatch.setattr(
        tg,
        "say",
        lambda text, **kw: messages.append(text) or {"ok": True, "result": {"message_id": 1}},
    )
    monkeypatch.setattr(tg, "tg", lambda *a, **kw: {})
    tg.send_proposal(sig(), ta.build_plan(sig()), "")
    pid = next(iter(tg.pending))
    if state == "expired":
        tg.pending[pid]["created"] = time.time() - tg.APPROVAL_TTL - 1
    elif state == "cancelled":
        tg.pending.clear()
    messages.clear()
    tg.handle_callback(callback("D", pid, "stranger" if state == "stranger" else None))
    assert not messages and not sim.writes and not tg.trade_lock.locked()


@pytest.mark.parametrize("command", ["/help", "/start"])
def test_help_lists_supported_commands_and_cancel_boundary(sim, monkeypatch, command):
    messages = []
    monkeypatch.setattr(tg, "say", lambda text, **kw: messages.append(text))
    tg.handle_message({"chat": {"id": tg.CHAT_ID}, "from": {"id": tg.CHAT_ID}, "text": command})
    text = messages[-1]
    for name in (
        "/help",
        "/start",
        "/check",
        "/discover",
        "/scan",
        "/check_ticker",
        "/check-ticker",
        "/status",
        "/showpr",
        "SHOW PR",
        "/cancel",
    ):
        assert name in text
    assert "Does NOT close a position" in text and "Details" in text
    assert "STOP TP1 TP2" in text and "ENTRY STOP TP1 TP2" in text
    assert "one active/unresolved trade maximum" in text
    assert not sim.writes


def test_identity_is_only_in_startup_not_routine_telegram_output(sim, monkeypatch):
    from branding import BRANDING

    p = ta.build_plan(sig())
    startup = tg.startup_text()
    assert BRANDING in startup and "Connected to Binance Spot" in startup
    assert "Trade execution: Requires Telegram approval" in startup
    assert "Market scanning:" in startup
    assert "REAL MONEY" not in startup and "fake money" not in startup
    monkeypatch.setattr(tg, "AUTO_SCAN", True)
    monkeypatch.setattr(ta, "LIVE", True)
    assert tg.startup_status() == (
        "Connected to Binance Spot\nMarket scanning: Active\n"
        "Trade execution: Requires Telegram approval"
    )
    monkeypatch.setattr(tg, "AUTO_SCAN", False)
    monkeypatch.setattr(ta, "LIVE", False)
    assert "Market scanning: Disabled" in tg.startup_status()
    assert "Connected to Binance Spot Testnet" in tg.startup_status()
    messages = []
    monkeypatch.setattr(tg, "say", lambda text, **kw: messages.append(text))
    tg.handle_message({"chat": {"id": tg.CHAT_ID}, "from": {"id": tg.CHAT_ID}, "text": "/status"})
    texts = [
        tg.proposal_text(sig(), p, ""),
        tg.proposal_details_text(sig(), p, ""),
        tg.candidate_card("FOOUSDT", sig(), "Blocked", "Active trade", ""),
        *messages,
    ]
    for text in texts:
        assert "LIVE - REAL MONEY" not in text and "TESTNET" not in text
        assert "Fazil" not in text and "Git:" not in text and "TRBT v" not in text
