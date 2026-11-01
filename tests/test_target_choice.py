"""Target selection changes proposal geometry, never grants execution authority."""

import json
import time
from types import SimpleNamespace

import pytest
from test_algorithmic_planner import market as market
from test_bot import sig

import algorithmic_planner as ap
import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta
import trading_settings as settings


def result():
    return dict(
        symbol="FOOUSDT",
        price=100,
        chg=1,
        qv=50000000,
        sig=sig(),
        status="SAFE",
        reasons=[],
        note="",
    )


def message(text, *, owner=None):
    owner = tg.CHAT_ID if owner is None else owner
    return {"from": {"id": owner}, "chat": {"id": owner}, "text": text}


def callback(action, key, mid, *, owner=None):
    owner = tg.CHAT_ID if owner is None else owner
    return {
        "id": "cb",
        "data": f"{action}:{key}",
        "from": {"id": owner},
        "message": {"chat": {"id": owner}, "message_id": mid},
    }


@pytest.fixture
def ui(sim, monkeypatch):
    calls, jobs = [], []

    def transport(method, **data):
        calls.append((method, data))
        return {"ok": True, "result": {"message_id": len(calls)}}

    monkeypatch.setattr(tg, "tg", transport)
    monkeypatch.setattr(tg, "start_managed_worker", lambda job: jobs.append(job))
    return SimpleNamespace(calls=calls, jobs=jobs, transport=transport)


def choose(ui, *, style="single", route="C3", numeric=False):
    cid = tg.send_check_card(result())
    tg.handle_callback(callback(route, cid, tg.checks[cid]["mid"]))
    if numeric:
        tg.handle_message(message("1" if style == "single" else "2"))
    else:
        tg.handle_callback(
            callback("CT1" if style == "single" else "CT2", cid, tg.checks[cid]["mid"])
        )
    return cid


@pytest.mark.parametrize(
    "style,numeric", [("single", False), ("single", True), ("two", False), ("two", True)]
)
def test_named_buttons_and_number_replies_select_before_modes_without_buying(
    sim, ui, style, numeric
):
    cid = choose(ui, style=style, numeric=numeric)
    item = tg.checks[cid]
    assert item["stage"] == "modes" and item["exit_style"] == style
    assert tg.state["exit_style"] == style and not tg.pending and not sim.writes
    assert any(
        "1 — Single target" in p.get("text", "") and "2 — Two targets" in p.get("text", "")
        for _, p in ui.calls
    )


@pytest.mark.parametrize(
    "style,values,required", [("single", "100 97 105", 3), ("two", "100 97 105 108", 4)]
)
def test_manual_collects_only_selected_prices_and_displays_actual_exit_quantities(
    sim, ui, style, values, required
):
    cid = choose(ui, style=style)
    tg.handle_callback(callback("CN", cid, tg.checks[cid]["mid"]))
    assert f"Send {required} prices" in ui.calls[-1][1]["text"]
    tg.handle_message(message(values))
    item = next(iter(tg.pending.values()))
    assert item["plan"]["exit_style"] == style and not sim.writes
    if style == "single":
        assert "t2" not in item["sig"] and "t2" not in item["plan"]
        assert (
            "full tradable position" in item["text"] and "TP2" not in item["text"] + item["details"]
        )
        assert "if the target fills" in item["text"]
    else:
        assert "TP1" in item["text"] and "TP2" in item["text"]
        assert em.fmt(item["plan"]["half"]) in item["text"]
        assert em.fmt(item["plan"]["rest"]) in item["text"]
    assert not tg.checks


def test_single_manual_rejects_an_extra_target_instead_of_silently_ignoring_it(sim, ui):
    cid = choose(ui)
    tg.handle_callback(callback("CN", cid, tg.checks[cid]["mid"]))
    tg.handle_message(message("100 97 105 108"))
    assert not tg.pending and "3 numbers" in ui.calls[-1][1]["text"]
    assert tg.state["exit_style"] == "single" and not sim.writes


@pytest.mark.parametrize("style", ["single", "two"])
def test_auto_setup_uses_the_existing_first_target_and_requires_approval(sim, market, ui, style):
    expected = ap.propose("FOOUSDT")
    cid = choose(ui, style=style)
    tg.handle_callback(callback("CQ", cid, tg.checks[cid]["mid"]))
    item = next(iter(tg.pending.values()))
    assert item["plan"]["exit_style"] == style and item["plan"]["algorithm_mode"] == "full"
    assert item["plan"]["t1"] == expected["t1"]
    assert ("t2" in item["plan"]) == (style == "two") and not sim.writes


@pytest.mark.parametrize("style", ["single", "two"])
def test_assisted_asks_for_one_or_two_targets_and_keeps_normal_checks(sim, market, ui, style):
    cid = choose(ui, style=style)
    tg.handle_callback(callback("CW", cid, tg.checks[cid]["mid"]))
    suggestion = tg.state["signal"]
    assert ("Send ONE" if style == "single" else "Send TWO") in ui.calls[-1][1]["text"]
    targets = " ".join(str(suggestion[k]) for k in em.target_fields(suggestion))
    tg.handle_message(message(targets))
    item = next(iter(tg.pending.values()))
    assert item["plan"]["exit_style"] == style and item["plan"]["algorithm_mode"] == "assisted"
    assert item["plan"]["manual_override"] is False and not sim.writes


def test_small_cap_is_checked_after_target_choice_and_can_accept_single(sim, ui):
    sim.min_notional = "6"
    cid = choose(ui)
    assert tg.checks[cid]["stage"] == "modes"
    tg.handle_callback(callback("CN", cid, tg.checks[cid]["mid"]))
    tg.handle_message(message("100 97 105"))
    assert next(iter(tg.pending.values()))["plan"]["exit_style"] == "single"
    assert not sim.writes


def test_fresh_scanner_review_can_choose_single_with_one_exit_minimum(sim, ui):
    sim.min_notional = "6"
    choose(ui, route="C1")
    item = next(iter(tg.pending.values()))
    assert item["plan"]["exit_style"] == "single" and not item["plan"]["manual_override"]
    assert item["plan"]["rr"] >= em.D("1.5") and not sim.writes


@pytest.mark.parametrize(
    "how", ["cancel", "expired", "settings", "account", "wrong_message", "stranger"]
)
def test_invalid_selection_cannot_open_modes_or_authorize_orders(sim, ui, how):
    cid = tg.send_check_card(result())
    tg.handle_callback(callback("C3", cid, tg.checks[cid]["mid"]))
    item = tg.checks[cid]
    tap = callback("CT1", cid, item["mid"])
    if how == "cancel":
        tg.handle_message(message("/cancel"))
    elif how == "expired":
        item["created"] = time.time() - tg.CHECK_TTL - 1
    elif how == "settings":
        settings.update(
            "max_position_usdt", "25", expected_revision=0, actor="test", scope="session"
        )
    elif how == "account":
        em.CONTEXT = {**em.CONTEXT, "account_uid": "6789"}
    elif how == "wrong_message":
        tap["message"]["message_id"] += 100
    else:
        tap = callback("CT1", cid, item["mid"], owner="stranger")
    tg.handle_callback(tap)
    assert not tg.pending and not sim.writes
    assert not any(
        p.get("text", "").startswith("Trading options |") for m, p in ui.calls if m == "sendMessage"
    )


def test_number_reply_from_a_stranger_cannot_select_the_owners_targets(sim, ui):
    cid = tg.send_check_card(result())
    tg.handle_callback(callback("C3", cid, tg.checks[cid]["mid"]))
    tg.handle_message(message("1", owner="stranger"))
    assert tg.checks[cid]["stage"] == "targets" and not tg.pending and not sim.writes


def test_change_targets_consumes_old_approval_and_requires_a_fresh_proposal(sim, ui):
    p = ta.build_plan(sig())
    tg.send_proposal(sig(), p, "")
    pid, old = next(iter(tg.pending.items()))
    tg.handle_callback(callback("E", pid, old["mid"]))
    assert pid not in tg.pending and not sim.writes
    cid = next(iter(tg.checks))
    tg.handle_message(message("1"))
    tg.handle_callback(callback("CN", cid, tg.checks[cid]["mid"]))
    tg.handle_message(message("100 97 105"))
    new_id, new = next(iter(tg.pending.items()))
    assert new_id != pid and new["plan"]["exit_style"] == "single"
    tg.handle_callback(callback("A", pid, old["mid"]))
    assert not sim.writes and new_id in tg.pending


def test_change_targets_requires_original_proposal_message(sim, ui):
    tg.send_proposal(sig(), ta.build_plan(sig()), "")
    pid, item = next(iter(tg.pending.items()))
    tg.handle_callback(callback("E", pid, item["mid"] + 100))
    assert pid in tg.pending and not tg.checks and not sim.writes


def test_forged_mode_before_target_choice_does_not_start_algorithm(sim, ui, monkeypatch):
    cid = tg.send_check_card(result())
    monkeypatch.setattr(ap, "propose", lambda *a, **k: pytest.fail("Choose targets before mode"))
    tg.handle_callback(callback("CQ", cid, tg.checks[cid]["mid"]))
    assert tg.checks[cid]["stage"] == "targets" and not tg.pending and not sim.writes


def test_target_choice_delivery_after_cancel_cannot_restore_controls(sim, ui, monkeypatch):
    original = ui.transport

    def racing(method, **data):
        response = original(method, **data)
        if method == "sendMessage" and data.get("text", "").startswith("Choose profit targets"):
            tg.handle_message(message("/cancel"))
        return response

    monkeypatch.setattr(tg, "tg", racing)
    cid = tg.send_check_card(result())
    tg.handle_callback(callback("C3", cid, tg.checks[cid]["mid"]))
    assert not tg.checks and not tg.pending and not tg.state and not sim.writes


def test_setup_watch_carries_selected_single_style_into_a_fresh_proposal(sim, ui, monkeypatch):
    sim.min_notional = "6"
    res = {**result(), "exit_style": "single"}
    cid = tg.send_check_card(res)
    tg.handle_callback(callback("CM1", cid, tg.checks[cid]["mid"]))
    monkeypatch.setattr(tg, "check_ticker", lambda symbol: result())
    ui.jobs[0]()
    item = next(iter(tg.pending.values()))
    assert item["plan"]["exit_style"] == "single" and "t2" not in item["sig"]
    assert not tg.setup_watches and not sim.writes


def test_background_proposal_keeps_two_targets_and_exposes_change_choice(sim, ui):
    tg.send_proposal(sig(), ta.build_plan(sig()), "")
    pid, item = next(iter(tg.pending.items()))
    payload = next(p for m, p in ui.calls if m == "sendMessage")
    buttons = [b for row in json.loads(payload["reply_markup"])["inline_keyboard"] for b in row]
    assert item["plan"]["exit_style"] == "two"
    assert {b["callback_data"] for b in buttons} >= {f"A:{pid}", f"R:{pid}", f"D:{pid}", f"E:{pid}"}
    assert not sim.writes


@pytest.mark.parametrize("phase", ["analysis", "delivery"])
@pytest.mark.parametrize("change", ["cancel", "settings"])
def test_selected_target_proposal_cannot_register_after_a_slow_racing_operation(
    sim, ui, monkeypatch, phase, change
):
    cid = tg.send_check_card(result())
    tg.handle_callback(callback("C1", cid, tg.checks[cid]["mid"]))
    original_build, original_transport = ta.build_plan, ui.transport

    def invalidate():
        if change == "cancel":
            tg.handle_message(message("/cancel"))
        else:
            settings.update(
                "max_position_usdt", "25", expected_revision=0, actor="test", scope="session"
            )

    def build(*args, **kwargs):
        plan = original_build(*args, **kwargs)
        if phase == "analysis":
            invalidate()
        return plan

    def transport(method, **data):
        reply = original_transport(method, **data)
        if (
            phase == "delivery"
            and method == "sendMessage"
            and data.get("text", "").startswith("TRADE PROPOSAL")
        ):
            invalidate()
        return reply

    monkeypatch.setattr(ta, "build_plan", build)
    monkeypatch.setattr(tg, "tg", transport)
    tg.handle_callback(callback("CT1", cid, tg.checks[cid]["mid"]))
    assert not tg.pending and not sim.writes
