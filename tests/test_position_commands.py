"""Position browsing and close confirmations are scoped to immutable bot trades."""

import json
import time
from types import SimpleNamespace

import pytest
from test_bot import approved, sig

import exit_manager as em
import position_manager as pm
import telegram_assistant as tg
import trade_assistant as ta
import trading_settings as settings


def message(text, sender=None):
    sender = tg.CHAT_ID if sender is None else sender
    return {"chat": {"id": sender}, "from": {"id": sender}, "text": text}


def callback(action, pid, item, selection=None):
    return {
        "id": "cb",
        "from": {"id": tg.CHAT_ID},
        "message": {"chat": {"id": tg.CHAT_ID}, "message_id": item["mid"]},
        "data": f"{action}:{pid}" + (f":{selection}" if selection is not None else ""),
    }


def latest(kind):
    return [(pid, item) for pid, item in tg.position_views.items() if item["kind"] == kind][-1]


def buy(symbol="FOOUSDT", **fields):
    plan = ta.build_plan({**sig(), "symbol": symbol, **fields})
    return ta.execute(plan, approval=approved(plan), monitor=False)


@pytest.fixture
def ui(monkeypatch):
    calls, jobs, notices = [], [], []

    def transport(method, **params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": len(calls)}}

    original_monitor = em.monitor_trade
    monkeypatch.setattr(tg, "tg", transport)
    monkeypatch.setattr(tg, "start_managed_worker", lambda target: jobs.append(target))
    monkeypatch.setattr(em, "monitor_trade", lambda rec: original_monitor(rec, max_checks=2))
    monkeypatch.setattr(em, "alert", lambda text: notices.append(text) or True)
    return SimpleNamespace(calls=calls, jobs=jobs, notices=notices)


@pytest.fixture
def multi(sim):
    sim.symbols += ["BARUSDT", "BAZUSDT"]
    for base in ("BAR", "BAZ"):
        sim.free[base] = em.D(0)
        sim.locked[base] = em.D(0)
    settings.update("max_open_trades", 3, expected_revision=0, actor="test")
    return sim


def open_detail(number=1, command="/position"):
    tg.handle_message(message(command))
    tg.handle_message(message(str(number)))
    return latest("detail")


def confirm_view():
    pid, item = open_detail()
    tg.handle_callback(callback("PE", pid, item))
    return latest("confirm")


@pytest.mark.parametrize("command", ["/position", "/positions", "/position@NexvantBot"])
def test_numbered_position_list_and_selected_detail_are_read_only(sim, ui, command):
    rec = buy()
    count = len(sim.writes)
    pid, item = open_detail(command=command)
    text = item["text"]
    assert item["trade_id"] == rec["id"] and "average fill): 100.00000000" in text
    assert "SL: 97" in text and "TP1: 104.5" in text and "TP2: 107.5" in text
    assert "Remaining bot quantity:" in text and "Observed stop coverage:" in text
    assert "Opened:" in text and "Dubai" in text and "Current reference price: 100" in text
    assert len(sim.writes) == count and not ui.jobs
    buttons = json.loads([p for m, p in ui.calls if m == "sendMessage"][-1]["reply_markup"])
    assert buttons["inline_keyboard"][0][0]["text"] == "Exit Trade"
    assert not em.load_trades()[0]["close_requested"]


def test_inline_selection_chooses_the_exact_pair_not_the_first_active_trade(multi, ui):
    first, second = buy(), buy("BARUSDT")
    tg.handle_message(message("/position"))
    pid, item = latest("list")
    assert [row["id"] for row in item["rows"]] == [first["id"], second["id"]]
    tg.handle_callback(callback("PS", pid, item, 1))
    assert latest("detail")[1]["trade_id"] == second["id"] and not ui.jobs


def test_typing_a_number_uses_the_frozen_list_even_after_one_position_closes(multi, ui):
    first, second = buy(), buy("BARUSDT")
    tg.handle_message(message("/position"))
    em.close_bot_trade(first["symbol"], "CLOSE_" + first["symbol"])
    count = len(multi.writes)
    tg.handle_message(message("1"))
    item = latest("detail")[1]
    assert item["trade_id"] == first["id"] and "Status: Closed" in item["text"]
    assert em.active_trades()[0]["id"] == second["id"] and len(multi.writes) == count


def test_position_selection_handles_invalid_numbers_and_empty_list(sim, ui):
    tg.handle_message(message("/position"))
    assert "No active bot positions" in ui.calls[-1][1]["text"] and not tg.position_views
    buy()
    tg.handle_message(message("/position"))
    pid, item = latest("list")
    for text in ("0", "2", "abc", "-1"):
        tg.handle_message(message(text))
        assert pid in tg.position_views and "Choose a number" in ui.calls[-1][1]["text"]
    assert not ui.jobs


def test_exit_requires_two_taps_then_reconciles_and_reports_real_net_result(sim, ui):
    rec = buy()
    count = len(sim.writes)
    pid, item = confirm_view()
    assert "Close this bot trade now?" in item["text"] and "Expires in" in item["text"]
    assert "at market" in item["text"] and len(sim.writes) == count and not ui.jobs
    tg.handle_callback(callback("PF", pid, item))
    saved = em.active_trades()[0]
    assert saved["close_requested"] and saved["state"] == "closing"
    assert saved["close_request"]["source"] == "telegram"
    assert saved["id"] == rec["id"] and len(sim.writes) == count and len(ui.jobs) == 1
    assert "not a confirmed fill" in ui.calls[-1][1]["text"]
    ui.jobs[0]()
    assert not em.active_trades() and em.load_trades()[0]["accounted"]
    assert any(
        "TRADE CLOSED" in text
        and "Position close executed successfully" in text
        and "Net result:" in text
        for text in ui.notices
    )
    count = len(sim.writes)
    tg.handle_callback(callback("PF", pid, item))
    assert len(sim.writes) == count and len(ui.jobs) == 1


@pytest.mark.parametrize(
    "how", ["keep", "global", "expired", "stranger", "wrong_chat", "wrong_message"]
)
def test_unconfirmed_or_invalid_exit_keeps_existing_orders_intact(sim, ui, how):
    buy()
    count = len(sim.writes)
    pid, item = confirm_view()
    cb = callback("PF", pid, item)
    if how == "keep":
        tg.handle_callback(callback("PX", pid, item))
    elif how == "global":
        tg.handle_message(message("/cancel"))
    elif how == "expired":
        item["expires_at"] = time.time() - 1
    elif how == "stranger":
        cb["from"]["id"] = "stranger"
    elif how == "wrong_chat":
        cb["message"]["chat"]["id"] = "stranger"
    else:
        cb["message"]["message_id"] += 100
    tg.handle_callback(cb)
    assert not em.active_trades()[0]["close_requested"] and len(sim.writes) == count and not ui.jobs


def test_expiry_removes_keyboard_and_number_selection_state(sim, ui):
    buy()
    tg.handle_message(message("/position"))
    pid, item = latest("list")
    item["expires_at"] = time.time() - 1
    tg.expire_pending()
    assert not tg.position_views and not tg.state
    assert json.loads(ui.calls[-1][1]["reply_markup"]) == {"inline_keyboard": []}
    assert "EXPIRED" in ui.calls[-1][1]["text"] and not ui.jobs


def test_wrong_stage_confirm_and_forged_index_are_inert(sim, ui):
    buy()
    tg.handle_message(message("/position"))
    pid, item = latest("list")
    tg.handle_callback(callback("PF", pid, item))
    tg.handle_callback(callback("PS", pid, item, 99))
    assert pid in tg.position_views and not em.active_trades()[0]["close_requested"] and not ui.jobs


def test_position_exit_preserves_other_pair_and_nonbot_holdings(multi, ui):
    multi.free["FOO"] = em.D(5)
    first, second = buy(), buy("BARUSDT")
    pid, item = confirm_view()
    tg.handle_callback(callback("PF", pid, item))
    ui.jobs[0]()
    assert em.active_trades()[0]["id"] == second["id"]
    assert multi.free["FOO"] == 5
    assert em.coverage(em.active_trades()[0]) >= em.inventory(second) > 0
    assert next(r for r in em.load_trades() if r["id"] == first["id"])["state"] == "closed"


def test_old_confirmation_cannot_close_a_new_trade_in_the_same_pair(sim, ui):
    old = buy()
    pid, item = confirm_view()
    em.close_bot_trade(old["symbol"], "CLOSE_" + old["symbol"])
    new = buy()
    count = len(sim.writes)
    tg.handle_callback(callback("PF", pid, item))
    assert len(sim.writes) == count and em.active_trades()[0]["id"] == new["id"] and not ui.jobs
    assert "already closed" in ui.calls[-1][1]["text"]


def test_worker_start_failure_preserves_close_intent_for_restart_recovery(sim, ui, monkeypatch):
    buy()
    pid, item = confirm_view()

    def failure(target):
        raise RuntimeError("private transport diagnostic")

    monkeypatch.setattr(tg, "start_managed_worker", failure)
    tg.handle_callback(callback("PF", pid, item))
    assert em.active_trades()[0]["close_requested"]
    assert "private transport diagnostic" not in ui.calls[-1][1]["text"]
    em.recover_trades()
    assert not em.active_trades()


def test_cancel_after_acceptance_cannot_undo_the_saved_exit(sim, ui):
    buy()
    pid, item = confirm_view()
    tg.handle_callback(callback("PF", pid, item))
    tg.handle_message(message("/cancel"))
    assert em.active_trades()[0]["close_requested"]
    ui.jobs[0]()
    assert not em.active_trades()


def test_watch_has_its_own_cancel_confirmation_without_an_exchange_order(sim, ui):
    sim.price = em.D(101)
    rec = buy(entry_mode="watch", manual_override=True)
    pid, item = open_detail()
    assert "not bought yet" in item["text"] and "Cancel Entry Watch" in item["text"]
    tg.handle_callback(callback("PE", pid, item))
    pid, item = latest("confirm")
    assert item["exit_action"] == "cancel_watch"
    tg.handle_callback(callback("PF", pid, item))
    assert not sim.writes and not ui.jobs and not em.active_trades()
    assert em.load_trades()[0]["id"] == rec["id"]


def test_watch_confirm_cannot_sell_if_entry_started_while_menu_was_open(sim, ui):
    sim.price = em.D(101)
    rec = buy(entry_mode="watch", manual_override=True)
    pid, item = confirm_view()
    sim.price = em.D(100)
    em.reconcile_trade(rec)
    assert em.active_trades()[0]["state"] == "protected"
    count = len(sim.writes)
    tg.handle_callback(callback("PF", pid, item))
    assert len(sim.writes) == count and not em.active_trades()[0]["close_requested"] and not ui.jobs
    assert "Entry state changed" in ui.calls[-1][1]["text"]


def test_tp1_execution_appears_in_details_and_exit_sells_only_remaining_quantity(sim, ui):
    rec = buy()
    first = next(iter(sim.lists.values()))
    sim.fill(sim.orders[first["orders"][0]["orderId"]], price="104.5")
    em.reconcile_trade(rec)
    pid, item = open_detail()
    assert "TP1: 104.5 — Executed" in item["text"]
    remaining = em.inventory(em.active_trades()[0])
    tg.handle_callback(callback("PE", pid, item))
    pid, item = latest("confirm")
    tg.handle_callback(callback("PF", pid, item))
    ui.jobs[0]()
    assert (
        sum(
            em.D(d["quantity"])
            for _, path, d in sim.writes
            if path == "order" and d["side"] == "SELL"
        )
        == remaining
    )
    assert not em.active_trades()


def test_exchange_read_failure_is_labeled_without_claiming_coverage_or_mutating_orders(sim, ui):
    buy()
    count = len(sim.writes)
    sim.read_failures = 1
    pid, item = open_detail()
    assert (
        "Exchange verification pending" in item["text"]
        and "quantity / stop coverage: verification pending" in item["text"]
    )
    assert len(sim.writes) == count and not ui.jobs


def test_canceled_generation_during_send_cannot_restore_position_buttons(sim, ui, monkeypatch):
    buy()
    original = tg.tg

    def transport(method, **params):
        if method == "sendMessage":
            tg.proposal_generation += 1
        return original(method, **params)

    monkeypatch.setattr(tg, "tg", transport)
    tg.handle_message(message("/position"))
    assert not tg.position_views and not tg.state and not ui.jobs
    assert "CANCELLED" in ui.calls[-1][1]["text"]


def test_multi_page_list_keeps_record_number_identity(sim, ui, monkeypatch):
    rows = [{"id": str(i), "symbol": f"COIN{i}USDT", "state": "Active"} for i in range(12)]
    monkeypatch.setattr(pm, "list_positions", lambda: {"context": dict(em.CONTEXT), "rows": rows})
    tg.handle_message(message("/position"))
    pid, item = latest("list")
    tg.handle_callback(callback("PN", pid, item, 1))
    pid, item = latest("list")
    assert "Showing 11–12 of 12" in item["text"] and item["rows"] == rows
    assert not ui.jobs and not sim.writes


def test_cancel_during_confirmation_ack_prevents_durable_exit_acceptance(sim, ui, monkeypatch):
    buy()
    count = len(sim.writes)
    pid, item = confirm_view()
    original = tg.tg

    def transport(method, **params):
        if method == "answerCallbackQuery" and params.get("text") == "Saving your exit request...":
            tg.handle_message(message("/cancel"))
        return original(method, **params)

    monkeypatch.setattr(tg, "tg", transport)
    tg.handle_callback(callback("PF", pid, item))
    assert not em.active_trades()[0]["close_requested"] and not ui.jobs and len(sim.writes) == count
    assert "Exit selection canceled" in ui.calls[-1][1]["text"]


def test_closed_watch_that_had_entered_is_not_falsely_reported_as_an_unfilled_cancellation(sim, ui):
    sim.price = em.D(101)
    rec = buy(entry_mode="watch", manual_override=True)
    pid, item = confirm_view()
    sim.price = em.D(100)
    em.reconcile_trade(rec)
    for parent in sim.lists.values():
        sim.fill(sim.orders[parent["orders"][0]["orderId"]], price="104.5")
    em.reconcile_trade(rec)
    count = len(sim.writes)
    tg.handle_callback(callback("PF", pid, item))
    assert "already closed" in ui.calls[-1][1]["text"]
    assert "Entry watch closed" not in ui.calls[-1][1]["text"]
    assert len(sim.writes) == count and not ui.jobs
