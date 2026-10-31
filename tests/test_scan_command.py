"""Coin-check alias resolves ticker-only input without trading or duplicate suffixes."""

import pytest

import telegram_assistant as tg


def message(text, sender=None):
    sender = tg.CHAT_ID if sender is None else sender
    return {"chat": {"id": sender}, "from": {"id": sender}, "text": text}


@pytest.fixture
def checks(monkeypatch):
    seen = []
    replies = []
    monkeypatch.setattr(
        tg, "check_ticker", lambda symbol: seen.append(symbol) or {"symbol": symbol}
    )
    monkeypatch.setattr(tg, "send_check_card", lambda *args: None)
    monkeypatch.setattr(tg, "say", lambda text, **kwargs: replies.append(text))
    return seen, replies


@pytest.mark.parametrize(
    "text",
    ["/scan ZRO", "/check ZRO", "/scan zro", "/scan ZROUSDT", "/scan ZRO/USDT", "/scan@Trbt ZRO"],
)
def test_direct_ticker_resolves_usdt_once_and_does_not_trade(sim, checks, text):
    seen, replies = checks
    tg.handle_message(message(text))
    assert seen == ["ZROUSDT"]
    assert not sim.writes and not tg.pending


def test_bare_scan_prompts_for_ticker_then_resolves_pair(sim, checks):
    seen, replies = checks
    tg.handle_message(message("/scan"))
    assert "ticker" in replies[-1] and "USDT is added automatically" in replies[-1]
    assert not seen and tg.state["step"] == "ticker"
    tg.handle_message(message("PARTI"))
    assert seen == ["PARTIUSDT"] and not tg.state and not sim.writes


def test_cancelled_ticker_prompt_cannot_process_late_reply(sim, checks):
    seen, replies = checks
    tg.handle_message(message("/scan"))
    tg.handle_message(message("/cancel"))
    tg.handle_message(message("ZRO"))
    assert not seen and not tg.state and not sim.writes


def test_scan_alias_still_requires_authorized_user(sim, checks):
    tg.handle_message(message("/scan ZRO", sender="stranger"))
    assert checks == ([], []) and not sim.writes


def test_unrecognized_command_does_not_accidentally_start_scan(sim, checks):
    tg.handle_message(message("/scanner ZRO"))
    assert checks == ([], []) and not tg.state and not sim.writes
