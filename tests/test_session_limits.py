"""Effective session revisions never leak into durable settings or old approval."""

import copy
import json

import pytest
from test_bot import approved, sig
from test_setup_commands import click, confirmation
from test_setup_commands import ui as ui

import exit_manager as em
import safety
import telegram_assistant as tg
import trade_assistant as ta
import trading_settings as settings


def update(field, value, scope="session"):
    return settings.update(
        field, value, expected_revision=settings.read()["revision"], actor="test-owner", scope=scope
    )


def settings_path():
    return em.TRADES_DIR.parent / "settings.json"


def test_current_session_changes_neither_source_defaults_nor_saved_file(sim):
    current = update("max_open_trades", 8)
    assert current["max_open_trades"] == 8 and current["scopes"]["max_open_trades"] == "session"
    assert safety.MAX_OPEN_TRADES == 1 and not settings_path().exists()
    settings.reset_session()
    assert settings.read()["max_open_trades"] == 1 and not sim.writes


def test_restart_restores_saved_values_instead_of_hardcoded_defaults(sim):
    update("max_open_trades", 3, "persistent")
    data = settings_path().read_bytes()
    update("max_open_trades", 5)
    assert settings.read()["max_open_trades"] == 5 and settings_path().read_bytes() == data
    settings.reset_session()
    assert settings.read()["max_open_trades"] == 3


def test_saving_current_session_value_actually_makes_it_persistent(sim):
    temporary = update("max_open_trades", 4)
    saved = update("max_open_trades", 4, "persistent")
    assert (
        saved["revision"] > temporary["revision"] and saved["scopes"]["max_open_trades"] == "saved"
    )
    assert json.loads(settings_path().read_text())["max_open_trades"] == 4
    settings.reset_session()
    assert settings.read()["max_open_trades"] == 4


def test_saving_one_field_never_persists_another_fields_temporary_override(sim):
    update("max_open_trades", 8)
    current = update("max_position_usdt", "25", "persistent")
    disk = json.loads(settings_path().read_text())
    assert disk["max_open_trades"] == 1 and disk["max_position_usdt"] == "25"
    assert current["max_open_trades"] == 8 and current["scopes"]["max_open_trades"] == "session"
    settings.reset_session()
    assert settings.read()["max_open_trades"] == 1 and settings.read()["max_position_usdt"] == "25"


def test_persistent_file_uses_its_own_contiguous_audit_even_after_many_session_updates(sim):
    for value in (2, 3, 4, 5):
        update("max_open_trades", value)
    before = settings.read()["revision"]
    current = update("max_position_usdt", "25", "persistent")
    disk = json.loads(settings_path().read_text())
    assert current["revision"] > before and disk["revision"] == 1
    assert [event["revision"] for event in disk["changes"]] == [1]
    assert disk["changes"][0]["old"] == "12" and "saved_limits" not in disk
    assert disk["max_open_trades"] == 1


def test_failed_durable_save_keeps_the_confirmed_session_value_and_scope(sim, monkeypatch):
    before = update("max_position_usdt", "25")

    def failed(*args, **kwargs):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(em, "_atomic_write", failed)
    with pytest.raises(OSError):
        update("max_position_usdt", "25", "persistent")
    assert settings.read() == before and not settings_path().exists()


def test_session_limits_still_invalidate_old_trade_approval_and_bound_the_new_plan(sim):
    old = ta.build_plan(sig())
    update("max_position_usdt", "25")
    fresh = ta.build_plan(sig())
    assert fresh["settings_revision"] > old["settings_revision"] and fresh["cost"] <= 25
    with pytest.raises(ta.PlanChangedError):
        ta.execute(old, approval=approved(old), monitor=False)
    assert not sim.writes and not em.active_trades()


def test_invalid_saved_state_cannot_be_hidden_by_a_session_override(sim):
    update("max_open_trades", 4)
    settings_path().write_text('{"invalid":true}')
    with pytest.raises(RuntimeError, match="settings invalid"):
        settings.read()


def test_saved_configuration_change_supersedes_old_overrides_without_reusing_revision(sim):
    saved = update("max_open_trades", 2, "persistent")
    update("max_open_trades", 4)
    current = settings.read()
    disk = json.loads(settings_path().read_text())
    disk["changes"].append(
        {
            "revision": 2,
            "field": "max_position_usdt",
            "old": "12",
            "new": "25",
            "actor": "another-confirmation",
            "changed_utc": disk["changes"][0]["changed_utc"],
        }
    )
    disk.update(revision=2, max_position_usdt="25")
    em._atomic_write(settings_path(), disk)
    updated = settings.read()
    assert updated["revision"] > current["revision"]
    assert updated["max_open_trades"] == saved["max_open_trades"] == 2
    assert updated["max_position_usdt"] == "25" and updated["scopes"]["max_open_trades"] == "saved"


def test_session_overrides_do_not_cross_account_identity(sim):
    update("max_open_trades", 8)
    sim.uid = "6789"
    em.CONTEXT = em.TRADES_DIR = None
    assert settings.read()["max_open_trades"] == 1


def test_setup_requires_session_confirmation_and_explains_administrator_policy(sim, ui):
    sid, item = confirmation("ST", "8")
    labels = [
        button["text"]
        for row in json.loads(ui[-1][1]["reply_markup"])["inline_keyboard"]
        for button in row
    ]
    assert "Use for this session (default)" in labels and "About permanent limits" in labels
    assert "Save for future runs" not in labels
    assert "96" in item["text"] and "These are maximums, not orders" in item["text"]
    assert settings.read()["max_open_trades"] == 1
    tg.handle_callback(click("SA", sid, item))
    current = settings.read()
    assert current["max_open_trades"] == 8 and current["scopes"]["max_open_trades"] == "session"
    assert not sim.writes and not tg.pending
    settings.reset_session()
    assert settings.read()["max_open_trades"] == 1
    assert safety.MAX_OPEN_TRADES == 1


def test_copy_of_effective_settings_cannot_modify_the_running_session(sim):
    current = update("max_open_trades", 3)
    clone = copy.deepcopy(current)
    clone["saved_limits"]["max_open_trades"] = 99
    clone["session_changes"][0]["new"] = 99
    assert settings.read() == current


def test_buy_permission_is_checked_again_after_a_prepared_proposal(sim):
    plan = ta.build_plan(sig())
    sim.response_override = lambda m, path, data: (
        {"uid": 12345, "accountType": "SPOT", "canTrade": False} if path == "account" else None
    )
    rec = ta.execute(plan, approval=approved(plan), monitor=False)
    assert not sim.writes and not rec["orders"][0]["submitted"]
    assert "ACCOUNT_TRADE_DISABLED" in rec["orders"][0]["rejection"]


def test_entry_permission_failure_does_not_disable_monitoring_existing_stops(sim):
    plan = ta.build_plan(sig())
    rec = ta.execute(plan, approval=approved(plan), monitor=False)
    before = len(sim.writes), em.inventory(rec), em.coverage(rec)
    sim.response_override = lambda m, path, data: (
        {"uid": 12345, "accountType": "SPOT", "canTrade": False} if path == "account" else None
    )
    em.reconcile_trade(rec)
    assert (
        rec["state"] == "protected"
        and (len(sim.writes), em.inventory(rec), em.coverage(rec)) == before
    )


@pytest.mark.parametrize(
    "field,first,second", [("max_open_trades", 2, 3), ("max_position_usdt", "25", "30")]
)
def test_reused_session_revision_cannot_reauthorize_different_approved_limits(
    sim, field, first, second
):
    update(field, first)
    old = ta.build_plan(sig())
    authorization = approved(old)
    settings.reset_session()
    update(field, second)
    fresh = ta.build_plan(sig())
    assert old["settings_revision"] == fresh["settings_revision"]
    assert not ta.approval_bounds(authorization, fresh)
    with pytest.raises(ta.PlanChangedError, match="settings changed"):
        ta.build_plan(sig(), approved_bounds=authorization)
    with pytest.raises(ta.PlanChangedError, match="settings changed"):
        ta.execute(old, approval=authorization, monitor=False)
    assert not sim.writes and not em.active_trades()
