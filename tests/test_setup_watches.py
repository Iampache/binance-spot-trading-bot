"""Quiet observation has no entry authority, including cancellation/deadline races."""

import json
import time
from types import SimpleNamespace

import pytest
from test_bot import approved, sig

import entry_readiness as readiness
import entry_scanner as sc
import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta
import trading_settings as settings


def result(*, ready=False, symbol="FOOUSDT"):
    return dict(
        symbol=symbol,
        price=100,
        chg=1,
        qv=20000000,
        sig={**sig(), "symbol": symbol} if ready else None,
        reasons=[]
        if ready
        else [
            "Waiting for a pullback",
            "Trend confirmation pending",
            "Volume confirmation pending",
        ],
        status="SAFE" if ready else "WARNING",
        note="BTC weakness is advisory",
    )


def callback(action, key, mid, *, sender=None, chat=None):
    return dict(
        id="cb",
        data=f"{action}:{key}",
        **{
            "from": {"id": tg.CHAT_ID if sender is None else sender},
            "message": {"chat": {"id": tg.CHAT_ID if chat is None else chat}, "message_id": mid},
        },
    )


def message(text):
    return {"chat": {"id": tg.CHAT_ID}, "from": {"id": tg.CHAT_ID}, "text": text}


@pytest.fixture
def ui(sim, monkeypatch):
    calls, jobs = [], []
    wall = time.time()
    clock = [100.0]

    def transport(method, **params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": len(calls)}}

    def advance(seconds):
        clock[0] += seconds
        tg.expire_pending()

    monkeypatch.setattr(tg, "tg", transport)
    monkeypatch.setattr(tg, "start_managed_worker", lambda job: jobs.append(job))
    monkeypatch.setattr(tg, "check_ticker", lambda symbol: result(symbol=symbol))
    monkeypatch.setattr(sc, "last_alert", {})
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "time", lambda: wall + clock[0] - 100)
    monkeypatch.setattr(em.STOP, "wait", advance)
    return SimpleNamespace(calls=calls, jobs=jobs, clock=clock, transport=transport)


def start(ui, minutes=1, *, res=None):
    cid = tg.send_check_card(res or result())
    card = tg.checks[cid]
    tg.handle_callback(callback(f"CM{minutes}", cid, card["mid"]))
    wid, watch = list(tg.setup_watches.items())[-1]
    return wid, watch


def notices(ui):
    return [params["text"] for method, params in ui.calls if method == "sendMessage"]


def test_requested_card_has_numbered_reasons_and_useful_actions_without_false_safety_claim(sim, ui):
    cid = tg.send_check_card(result())
    card = tg.checks[cid]
    assert "👀 Watch setup — FOO/USDT" in card["text"]
    assert all(
        f"{index}. {reason}" in card["text"] for index, reason in enumerate(result()["reasons"], 1)
    )
    labels = [
        button["text"]
        for row in json.loads(ui.calls[-1][1]["reply_markup"])["inline_keyboard"]
        for button in row
    ]
    assert {
        "Watch 1 min",
        "Watch 3 min",
        "Watch 15 min",
        "Ignore",
        "Details",
        "Trading options",
    } <= set(labels)
    tg.handle_callback(callback("CD", cid, card["mid"]))
    assert "VIEW ONLY" in notices(ui)[-1] and not tg.pending and not sim.writes


@pytest.mark.parametrize("minutes", [1, 3, 15])
def test_watch_observes_for_the_whole_duration_then_edits_quietly_without_a_proposal(
    sim, ui, minutes
):
    wid, watch = start(ui, minutes)
    initial = ui.clock[0]
    ui.calls.clear()
    ui.jobs[0]()
    assert ui.clock[0] - initial == minutes * 60
    assert not tg.setup_watches and not tg.pending and not sim.writes and not notices(ui)
    assert any("WATCH ENDED" in params.get("text", "") for _, params in ui.calls)
    assert tg.checks and wid not in tg.checks
    assert not em.active_trades()  # Observing neither reserves a slot nor creates a journal.


def test_a_fresh_valid_setup_sends_one_real_proposal_only_after_all_checks(sim, ui, monkeypatch):
    attempts = []

    def refresh(symbol):
        attempts.append(symbol)
        return result(ready=len(attempts) >= 3)

    monkeypatch.setattr(tg, "check_ticker", refresh)
    start(ui, 3)
    ui.calls.clear()
    ui.jobs[0]()
    assert len(attempts) == 3 and len(notices(ui)) == 1 and "TRADE PROPOSAL" in notices(ui)[0]
    assert len(tg.pending) == 1 and not tg.setup_watches and not sim.writes
    assert "FOOUSDT" in sc.last_alert
    proposal = next(params for method, params in ui.calls if method == "sendMessage")
    actions = [
        button["callback_data"]
        for row in json.loads(proposal["reply_markup"])["inline_keyboard"]
        for button in row
    ]
    assert any(action.startswith("A:") for action in actions) and any(
        action.startswith("R:") for action in actions
    )


def test_fresh_scanner_signal_does_not_bypass_a_real_live_reward_risk_rejection(
    sim, ui, monkeypatch
):
    sim.price = em.D("100.01")
    monkeypatch.setattr(tg, "check_ticker", lambda symbol: result(ready=True))
    start(ui)
    ui.calls.clear()
    ui.jobs[0]()
    assert not notices(ui) and not tg.pending and not sim.writes
    card = next(iter(tg.checks.values()))
    assert "first target leaves too little reward" in card["text"]
    assert card["problem"].code == "SETUP" and "below 1.5R" in card["problem"].reason


def test_an_existing_pending_proposal_stops_observation_without_another_notification(sim, ui):
    tg.send_proposal(sig(), ta.build_plan(sig()), "")
    start(ui)
    ui.calls.clear()
    ui.jobs[0]()
    assert not notices(ui) and len(tg.pending) == 1 and not tg.setup_watches and not sim.writes


def test_ignore_during_analysis_prevents_later_planning_or_delivery(sim, ui, monkeypatch):
    wid, watch = start(ui)

    def ignored(symbol):
        tg.handle_callback(callback("W", wid, watch["mid"]))
        return result(ready=True)

    monkeypatch.setattr(tg, "check_ticker", ignored)
    monkeypatch.setattr(
        ta, "build_plan", lambda *args, **kwargs: pytest.fail("Ignored watch must not build a plan")
    )
    ui.calls.clear()
    ui.jobs[0]()
    assert not tg.setup_watches and not tg.pending and not sim.writes and not notices(ui)


def test_slow_analysis_cannot_emit_after_the_watch_deadline(sim, ui, monkeypatch):
    start(ui)

    def late(symbol):
        ui.clock[0] += 61
        return result(ready=True)

    monkeypatch.setattr(tg, "check_ticker", late)
    monkeypatch.setattr(
        readiness,
        "check",
        lambda *args, **kwargs: pytest.fail("Expired watch cannot continue preparation"),
    )
    ui.calls.clear()
    ui.jobs[0]()
    assert not notices(ui) and not tg.pending and not tg.setup_watches and not sim.writes
    assert any("WATCH ENDED" in params.get("text", "") for _, params in ui.calls)


@pytest.mark.parametrize("change", ["deadline", "settings", "cancel"])
def test_a_late_delivery_is_retired_without_registering_approval(sim, ui, monkeypatch, change):
    monkeypatch.setattr(tg, "check_ticker", lambda symbol: result(ready=True))
    start(ui)
    original = ui.transport

    def racing(method, **params):
        reply = original(method, **params)
        if method == "sendMessage" and "TRADE PROPOSAL" in params.get("text", ""):
            if change == "deadline":
                ui.clock[0] += 61
            elif change == "settings":
                settings.update("max_open_trades", 2, expected_revision=0, actor="concurrent-owner")
            else:
                tg.cancel_setup_watches()
        return reply

    monkeypatch.setattr(tg, "tg", racing)
    ui.calls.clear()
    ui.jobs[0]()
    assert len(notices(ui)) == 1 and not tg.pending and not tg.setup_watches and not sc.last_alert
    assert any(
        "CANCELLED" in params.get("text", "")
        and json.loads(params["reply_markup"]) == {"inline_keyboard": []}
        for method, params in ui.calls
        if method == "editMessageText"
    )
    assert not sim.writes


def test_replacing_a_same_pair_watch_keeps_only_the_new_one_active(sim, ui):
    first_id, first = start(ui)
    second_id, second = start(ui, 3)
    assert list(tg.setup_watches) == [second_id] and first["cancelled"]
    ui.jobs[0]()
    assert tg.setup_watches[second_id] is second
    tg.handle_callback(callback("W", second_id, second["mid"]))
    ui.jobs[1]()
    assert not tg.setup_watches and not tg.pending and not sim.writes


@pytest.mark.parametrize("how", ["cancel", "settings", "shutdown"])
def test_cancel_settings_and_shutdown_invalidate_setup_authority(sim, ui, monkeypatch, how):
    start(ui)
    if how == "cancel":
        tg.handle_message(message("/cancel"))
    elif how == "settings":
        tg.invalidate_for_settings_change()
    else:
        em.STOP.set()
    monkeypatch.setattr(
        tg, "check_ticker", lambda symbol: pytest.fail("Invalidated watch cannot read data")
    )
    ui.calls.clear()
    ui.jobs[0]()
    assert not notices(ui) and not tg.setup_watches and not tg.pending and not sim.writes


@pytest.mark.parametrize("field", ["sender", "chat"])
def test_strangers_cannot_start_or_stop_setup_watches(sim, ui, field):
    cid = tg.send_check_card(result())
    item = tg.checks[cid]
    ui.calls.clear()
    tg.handle_callback(callback("CM1", cid, item["mid"], **{field: "stranger"}))
    assert not ui.calls and not tg.setup_watches
    wid, watch = start(ui)
    ui.calls.clear()
    tg.handle_callback(callback("W", wid, watch["mid"], **{field: "stranger"}))
    assert not ui.calls and tg.setup_watches[wid] is watch


def test_wrong_message_or_expired_controls_cannot_start_a_watch(sim, ui):
    cid = tg.send_check_card(result())
    card = tg.checks[cid]
    tg.handle_callback(callback("CM1", cid, card["mid"] + 100))
    assert not tg.setup_watches
    ui.clock[0] += tg.CHECK_TTL + 1
    tg.handle_callback(callback("CM1", cid, card["mid"]))
    assert not tg.setup_watches and cid not in tg.checks


@pytest.mark.parametrize("action", ["CN", "CW", "CQ"])
def test_all_modes_check_occupied_slots_before_input_or_algorithm_analysis(
    sim, ui, monkeypatch, action
):
    plan = ta.build_plan(sig())
    ta.execute(plan, approval=approved(plan), monitor=False)
    cid = tg.send_check_card(result(symbol="BARUSDT"))
    card = tg.checks[cid]
    monkeypatch.setattr(
        tg.algo, "propose", lambda *args, **kwargs: pytest.fail("Capacity must be checked first")
    )
    count = len(sim.writes)
    tg.handle_callback(callback(action, cid, card["mid"]))
    text = notices(ui)[-1]
    assert "FOOUSDT" in text and "limit" in text and "/setup" in text
    assert "Capacity: blocked" in text and "System/ownership: not checked" in text
    assert not tg.state and not tg.pending and len(sim.writes) == count


def test_funding_failure_has_a_matching_setup_cta_instead_of_opening_a_mode(sim, ui):
    sim.min_notional = "6"
    cid = tg.send_check_card(result())
    tg.handle_callback(callback("C3", cid, tg.checks[cid]["mid"]))
    text = notices(ui)[-1]
    assert "12.1121" in text and "/setup" in text and "Funding: blocked" in text
    assert not tg.state and not tg.pending and not sim.writes
    buttons = json.loads(
        next(
            params["reply_markup"]
            for method, params in reversed(ui.calls)
            if method == "sendMessage"
        )
    )["inline_keyboard"]
    assert any(button["callback_data"] == "H:setup" for row in buttons for button in row)


def test_scan_log_pair_choices_are_frozen_and_only_open_fresh_checks(sim, ui, monkeypatch):
    tg.last_scan.update(
        saved_at=time.time(),
        candidates=1,
        outcomes=[{"symbol": "FOOUSDT", "status": "Blocked", "reason": "No setup"}],
        sent=0,
        note="",
    )
    tg.show_scan_log()
    sid, selection = next(iter(tg.scan_selections.items()))
    tg.last_scan["outcomes"] = [{"symbol": "BARUSDT", "status": "Blocked", "reason": "Newer scan"}]
    opened = []
    monkeypatch.setattr(tg, "run_check", lambda symbol, **kwargs: opened.append(symbol))
    tg.handle_callback(callback("Q", sid + ":0", selection["mid"]))
    assert opened == ["FOOUSDT"] and not tg.pending and not sim.writes
    tg.handle_message(message("/cancel"))
    tg.handle_callback(callback("Q", sid + ":0", selection["mid"]))
    assert opened == ["FOOUSDT"]


def test_stop_observing_never_cancels_or_sells_an_existing_position(sim, ui):
    plan = ta.build_plan(sig())
    rec = ta.execute(plan, approval=approved(plan), monitor=False)
    before = len(sim.writes), em.inventory(rec), em.coverage(rec)
    wid, watch = start(ui, res=result(symbol="BARUSDT"))
    tg.handle_callback(callback("W", wid, watch["mid"]))
    ui.jobs[0]()
    saved = em.active_trades()[0]
    assert (
        saved["state"] == "protected"
        and (len(sim.writes), em.inventory(saved), em.coverage(saved)) == before
    )
