"""Persistent operator preferences never authorize trades or modify secrets."""

import pytest

import configure_bot as wizard
import exit_manager as em
import operator_preferences as prefs
import telegram_assistant as tg
import trading_settings as settings


def test_save_backup_restore_and_stale_confirmation(tmp_path):
    first = {**prefs.DEFAULTS, "max_open_trades": 3, "auto_scan": False}
    prefs.save(tmp_path, first, None)
    assert prefs.read(tmp_path) == first and prefs.read_backup(tmp_path) is None
    second = {**first, "max_position_usdt": "15"}
    prefs.save(tmp_path, second, first)
    with pytest.raises(ValueError, match="another terminal"):
        prefs.save(tmp_path, first, first)
    prefs.restore(tmp_path, second, first)
    assert prefs.read(tmp_path) == first and prefs.read_backup(tmp_path) == second


def test_restore_initial_absence_and_protect_env(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("untouched")
    prefs.save(tmp_path, prefs.DEFAULTS, None)
    prefs.restore(tmp_path, prefs.DEFAULTS, None)
    assert prefs.read(tmp_path) is None and secret.read_text() == "untouched"


@pytest.mark.parametrize(
    "field,value",
    [("max_open_trades", 11), ("max_position_usdt", "NaN"), ("momentum_alerts", "false")],
)
def test_invalid_values_never_saved(tmp_path, field, value):
    with pytest.raises(ValueError):
        prefs.save(tmp_path, {**prefs.DEFAULTS, field: value}, None)
    assert prefs.read(tmp_path) is None


def test_failed_publication_keeps_previous_configuration(tmp_path, monkeypatch):
    prefs.save(tmp_path, prefs.DEFAULTS, None)
    original = prefs.os.replace

    def fail(source, destination):
        if destination.name == "preferences.json":
            raise OSError("disk error")
        original(source, destination)

    monkeypatch.setattr(prefs.os, "replace", fail)
    with pytest.raises(OSError):
        prefs.save(tmp_path, {**prefs.DEFAULTS, "auto_scan": False}, prefs.DEFAULTS)
    assert prefs.read(tmp_path) == prefs.DEFAULTS


@pytest.mark.parametrize("confirmation", ["SAVE", "cancel"])
def test_wizard_requires_confirmation_and_supports_independent_switches(
    tmp_path, monkeypatch, confirmation
):
    replies = iter(["1", "3", "15", "off", "on", confirmation])
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))
    assert wizard.main(tmp_path) == 0
    saved = prefs.read(tmp_path)
    if confirmation == "SAVE":
        assert saved == dict(
            max_open_trades=3, max_position_usdt="15", auto_scan=False, momentum_alerts=True
        )
    else:
        assert saved is None


def test_startup_applies_saved_defaults_without_touching_account_journal(sim, monkeypatch):
    saved = dict(max_open_trades=3, max_position_usdt="15", auto_scan=False, momentum_alerts=True)
    prefs.save(em.BASE_DIR, saved, None)
    for name in ("startup_preferences", "AUTO_SCAN", "momentum_enabled"):
        monkeypatch.setattr(tg, name, getattr(tg, name))
    monkeypatch.setattr(tg, "TOKEN", "dummy")
    monkeypatch.setattr(tg, "CHAT_ID", "123")
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    def run():
        assert settings.read()["max_open_trades"] == 3
        assert settings.read()["max_position_usdt"] == "15"
        assert not tg.AUTO_SCAN and tg.momentum_enabled

    monkeypatch.setattr(tg, "_run_main", run)
    tg.main()
    assert not sim.writes and not (em.TRADES_DIR.parent / "settings.json").exists()
