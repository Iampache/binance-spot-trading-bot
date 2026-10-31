"""Scan candidates/rejections remain visible; failed delivery never starts cooldown."""

import time

import pytest
from simulator import api_error
from test_bot import sig

import entry_scanner as sc
import telegram_assistant as tg
import trade_assistant as ta


@pytest.fixture
def scan(monkeypatch):
    messages = []
    monkeypatch.setattr(sc, "last_alert", {})
    monkeypatch.setattr(tg, "SCAN_CANDIDATE_NOTIFICATIONS", True)
    monkeypatch.setattr(
        tg,
        "say",
        lambda text, **kw: messages.append(text) or {"ok": True, "result": {"message_id": 1}},
    )
    return messages


def test_one_candidate_zero_proposals_shows_symbol_and_reason(scan, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({"SLPUSDT": sig()}, "BTC downtrend"))

    def reject(signal):
        raise ValueError("BNB-discount fees require ALLOW_BNB_FEES=true")

    monkeypatch.setattr(ta, "build_plan", reject)
    result = tg.scan_once()
    assert "SLPUSDT" in caplog.text and "0 proposal(s)" in caplog.text
    assert any("SLPUSDT: blocked" in text and "ALLOW_BNB_FEES=true" in text for text in scan)
    assert "ALLOW_BNB_FEES=true" in result[0]
    assert not sc.last_alert


def test_candidate_failure_does_not_hide_other_candidates(scan, monkeypatch):
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({"FOOUSDT": sig(), "BARUSDT": sig()}, ""))

    def plan(signal):
        if not hasattr(plan, "called"):
            plan.called = True
            raise api_error(-2015, "api_key=synthetic-secret")
        return {}

    monkeypatch.setattr(ta, "build_plan", plan)
    monkeypatch.setattr(tg, "send_proposal", lambda *args: True)
    results = tg.scan_once()
    assert len(results) == 2 and "API code -2015" in results[0]
    assert "proposal sent" in results[1]
    assert "synthetic-secret" not in "\n".join(scan)
    assert "BARUSDT" in sc.last_alert


def test_delivery_failure_is_reported_without_counting_as_sent_or_cooling_down(scan, monkeypatch):
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({"FOOUSDT": sig()}, ""))
    monkeypatch.setattr(ta, "build_plan", lambda s: {})
    monkeypatch.setattr(tg, "send_proposal", lambda *args: False)
    assert "not delivered" in tg.scan_once()[0]
    assert not sc.last_alert


def test_cooldown_candidate_is_named_and_explained(scan, monkeypatch):
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({"FOOUSDT": sig()}, ""))
    sc.last_alert["FOOUSDT"] = time.time()
    assert "FOOUSDT: cooldown" in tg.scan_once()[0]
    assert any("FOOUSDT: cooldown" in msg for msg in scan)


def test_console_only_candidate_setting_disables_telegram_summary(scan, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(tg, "SCAN_CANDIDATE_NOTIFICATIONS", False)
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({"FOOUSDT": sig()}, ""))

    def fail(s):
        raise ValueError("minimum notional")

    monkeypatch.setattr(ta, "build_plan", fail)
    tg.scan_once()
    assert not scan
    assert "FOOUSDT: blocked" in caplog.text


def test_large_scan_summary_is_split_without_losing_candidates(scan, monkeypatch):
    candidates = {f"PAIR{i}USDT": sig() for i in range(100)}
    monkeypatch.setattr(ta, "scan_candidates", lambda: (candidates, ""))

    def fail(s):
        raise ValueError("order minimums " + ("x" * 150))

    monkeypatch.setattr(ta, "build_plan", fail)
    tg.scan_once()
    assert all(len(msg) <= 3500 for msg in scan)
    combined = "\n".join(scan)
    assert all(symbol + ": blocked" in combined for symbol in candidates)


def test_send_proposal_delivery_status_is_explicit(scan, sim, monkeypatch):
    plan = ta.build_plan(sig())
    assert tg.send_proposal(sig(), plan, "") is True
    monkeypatch.setattr(tg, "say", lambda *a, **k: {"ok": False, "description": "rejected"})
    assert tg.send_proposal(sig(), plan, "") is False
