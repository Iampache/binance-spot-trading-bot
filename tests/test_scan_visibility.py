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
    assert any(
        "Ticker: SLPUSDT" in text
        and "Current status: Blocked" in text
        and "ALLOW_BNB_FEES=true" in text
        for text in scan
    )
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
    assert any("Ticker: FOOUSDT" in msg and "Current status: Cooldown" in msg for msg in scan)


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
    assert all("Ticker: " + symbol in combined for symbol in candidates)
    assert combined.count("Current status: Blocked") == len(candidates)


def test_send_proposal_delivery_status_is_explicit(scan, sim, monkeypatch):
    plan = ta.build_plan(sig())
    assert tg.send_proposal(sig(), plan, "") is True
    monkeypatch.setattr(tg, "say", lambda *a, **k: {"ok": False, "description": "rejected"})
    assert tg.send_proposal(sig(), plan, "") is False


def test_active_trade_block_still_displays_all_levels_without_new_approval(scan, sim, monkeypatch):
    import exit_manager as em

    plan = ta.build_plan(sig())
    em.new_trade(plan)  # unresolved journal must block another buy
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({"FOOUSDT": sig()}, "BTC downtrend"))
    result = tg.scan_once()[0]
    assert "blocked — Open or unresolved bot trade exists" in result
    assert "PREVIEW ONLY" in result
    for field, value in (("Entry", "100"), ("SL", "97"), ("TP1", "104.5"), ("TP2", "107.5")):
        assert f"{field}: {value}" in result
    assert "fresh executable proposal" in result
    telegram = "\n".join(scan)
    assert "Potential Candidate Detected" in telegram
    assert "Reason for rejection: Open or unresolved bot trade exists" in telegram
    assert "\nEntry: 100\nSL: 97\nTP1: 104.5\nTP2: 107.5\n" in telegram
    assert "SCAN RESULTS" not in telegram and "Git:" not in telegram
    assert "BTC downtrend" in telegram
    assert not tg.pending and not sim.writes and not sc.last_alert
    assert len(em.active_trades()) == 1


def test_cooldown_preview_has_levels_without_actionable_proposal(scan, monkeypatch):
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({"FOOUSDT": sig()}, ""))
    sc.last_alert["FOOUSDT"] = time.time()
    tg.scan_once()
    assert "TP1: 104.5" in "\n".join(scan)
    assert not tg.pending


def test_invalid_preview_levels_are_explicit_and_do_not_hide_reason(scan, monkeypatch):
    monkeypatch.setattr(
        ta, "scan_candidates", lambda: ({"FOOUSDT": {**sig(), "entry": float("nan")}}, "")
    )
    result = tg.scan_once()[0]
    assert "blocked" in result and "levels unavailable" in result
    assert not tg.pending


def message(text):
    return {"chat": {"id": tg.CHAT_ID}, "from": {"id": tg.CHAT_ID}, "text": text}


@pytest.mark.parametrize("command", ["SHOW PR", "show pr", " SHOW   PR ", "/showpr"])
def test_show_pr_retrieves_blocked_candidate_without_order_or_approval(
    scan, sim, monkeypatch, command
):
    import exit_manager as em

    em.new_trade(ta.build_plan(sig()))
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({"FOOUSDT": sig()}, "BTC downtrend"))
    tg.scan_once()
    scan.clear()
    tg.handle_message(message(command))
    text = "\n".join(scan)
    assert "VIEW ONLY" in text and "UTC" in text
    assert "Ticker: FOOUSDT" in text and "Current status: Blocked" in text
    assert "Reason for rejection: Open or unresolved" in text
    for field in ("Entry:", "SL:", "TP1:", "TP2:"):
        assert field in text
    assert not tg.pending and not sim.writes
    assert len(em.active_trades()) == 1


def test_show_pr_cannot_reactivate_cancelled_proposal(scan, sim):
    tg.send_proposal(sig(), ta.build_plan(sig()), "")
    tg.handle_message(message("/cancel"))
    assert not tg.pending
    scan.clear()
    tg.handle_message(message("SHOW PR"))
    assert "VIEW ONLY" in scan[-1] and "TRADE PROPOSAL" in scan[-1]
    assert "Expires in" not in scan[-1]
    assert not tg.pending and not sim.writes


def test_show_pr_empty_and_unauthorized_requests(scan):
    tg.handle_message(message("SHOW PR"))
    assert "since this bot started" in scan[-1]
    scan.clear()
    tg.last_preview.update(text="private preview", saved_at=time.time())
    tg.handle_message({"chat": {"id": "stranger"}, "from": {"id": "stranger"}, "text": "SHOW PR"})
    assert not scan


def test_empty_scan_keeps_previous_preview(scan, monkeypatch):
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({"FOOUSDT": sig()}, ""))
    monkeypatch.setattr(ta, "build_plan", lambda s: (_ for _ in ()).throw(ValueError("blocked")))
    tg.scan_once()
    snapshot = dict(tg.last_preview)
    monkeypatch.setattr(ta, "scan_candidates", lambda: ({}, ""))
    tg.scan_once()
    assert tg.last_preview == snapshot
