"""Branded navigation is owner-only, never an order or a trading-setting change."""

import json
import struct

import pytest
from test_bot import sig

import branding
import exit_manager as em
import telegram_assistant as tg
import telegram_presentation as ui
import trade_assistant as ta
import trading_settings as settings


def message(text, sender=None):
    sender = tg.CHAT_ID if sender is None else sender
    return {"chat": {"id": sender}, "from": {"id": sender}, "text": text}


def callback(action):
    return {
        "id": "cb",
        "from": {"id": tg.CHAT_ID},
        "message": {"chat": {"id": tg.CHAT_ID}, "message_id": 1},
        "data": "H:" + action,
    }


@pytest.fixture
def calls(monkeypatch):
    captured = []

    def transport(method, **params):
        files = params.get("files", {})
        for _, photo, mime in files.values():
            assert not photo.closed and mime == "image/png"
            assert photo.read(8) == b"\x89PNG\r\n\x1a\n"
            photo.seek(0)
        captured.append((method, params))
        return {"ok": True, "result": {"message_id": len(captured)}}

    monkeypatch.setattr(tg, "tg", transport)
    return captured


def test_start_opens_branded_photo_dashboard_without_creating_a_trade(sim, calls):
    tg.handle_message(message("/start"))
    method, data = calls[-1]
    assert method == "sendPhoto" and "Welcome to NEXVANT" in data["caption"]
    keyboard = json.loads(data["reply_markup"])["inline_keyboard"]
    assert {b["callback_data"] for row in keyboard for b in row} == {
        "H:" + action for action in ui.HOME_ACTIONS
    }
    assert "your approval" in data["caption"] and not tg.pending and not sim.writes
    assert settings.read()["revision"] == 0


def test_missing_banner_keeps_navigation_available_as_text(sim, calls, monkeypatch, tmp_path):
    monkeypatch.setattr(tg, "WELCOME_IMAGE", tmp_path / "missing.png")
    tg.send_welcome()
    assert calls[-1][0] == "sendMessage"
    assert "Welcome to NEXVANT" in calls[-1][1]["text"] and "reply_markup" in calls[-1][1]
    assert not sim.writes


def test_telegram_photo_rejection_falls_back_without_exposing_transport_errors(
    sim, calls, monkeypatch
):
    original = tg.tg

    def api(method, **params):
        if method == "sendPhoto":
            return {"ok": False, "description": "private transport diagnostic"}
        return original(method, **params)

    monkeypatch.setattr(tg, "tg", api)
    tg.send_welcome()
    assert calls[-1][0] == "sendMessage" and "NEXVANT" in calls[-1][1]["text"]
    assert "private transport diagnostic" not in calls[-1][1]["text"] and not sim.writes


@pytest.mark.parametrize(
    "action,expected",
    [
        ("discover", "Discover tokens"),
        ("check", "Which coin?"),
        ("positions", "No active bot positions"),
        ("portfolio", "NEXVANT | PORTFOLIO"),
        ("pnl", "completed trades"),
        ("setup", "Trading setup"),
        ("status", "USDT free:"),
        ("health", "NEXVANT | HEALTH"),
        ("help", "NEXVANT | Commands"),
    ],
)
def test_dashboard_navigation_reuses_existing_handlers_without_placing_orders(
    sim, calls, action, expected
):
    tg.handle_callback(callback(action))
    assert any(expected in params.get("text", "") for _, params in calls)
    assert not sim.writes and not tg.pending and settings.read()["revision"] == 0


@pytest.mark.parametrize("field", ["sender", "chat"])
def test_strangers_cannot_use_dashboard_navigation(sim, calls, field):
    cb = callback("status")
    if field == "sender":
        cb["from"]["id"] = "stranger"
    else:
        cb["message"]["chat"]["id"] = "stranger"
    tg.handle_callback(cb)
    tg.handle_message(message("/start", sender="stranger"))
    assert not calls and not sim.writes


def test_unknown_home_action_cannot_consume_or_approve_a_pending_trade(sim, calls):
    tg.send_proposal(sig(), ta.build_plan(sig()), "")
    original = dict(tg.pending)
    pid = next(iter(tg.pending))
    tg.handle_callback(callback("A:" + pid))
    assert tg.pending == original and not sim.writes
    assert calls[-1][1]["text"] == "Unknown menu option."


def configure(api, sim, **changes):
    args = {
        "chat_id": "42",
        "token": "offline-presentation-token",
        "cache_path": em.TRADES_DIR.parent / "telegram_ui.json",
        "atomic_write": em._atomic_write,
        "stopped": em.STOP.is_set,
        **changes,
    }
    return ui.configure(api, **args)


def test_profile_menu_setup_is_owner_scoped_and_cached_without_persisting_token(sim, calls):
    assert configure(tg.tg, sim) == []
    methods = {m: params for m, params in calls}
    assert methods["setMyName"]["name"] == "NEXVANT"
    assert json.loads(methods["setMyCommands"]["scope"]) == {"type": "chat", "chat_id": "42"}
    assert json.loads(methods["setChatMenuButton"]["menu_button"]) == {"type": "commands"}
    assert json.loads(methods["setMyProfilePhoto"]["photo"]) == {
        "type": "static",
        "photo": "attach://avatar",
    }
    assert len(methods["setMyDescription"]["description"]) <= 512
    assert len(methods["setMyShortDescription"]["short_description"]) <= 120
    cache = em.TRADES_DIR.parent / "telegram_ui.json"
    assert "offline-presentation-token" not in cache.read_text()
    count = len(calls)
    assert configure(tg.tg, sim) == [] and len(calls) == count
    assert not sim.writes


def test_partially_supported_telegram_server_keeps_working_commands_and_retries_only_failed_photo(
    sim, calls
):
    def partial(method, **params):
        if method == "setMyProfilePhoto":
            return {"ok": False}
        return tg.tg(method, **params)

    assert configure(partial, sim) == ["setMyProfilePhoto"]
    assert any(method == "setMyCommands" for method, _ in calls)
    calls.clear()
    assert configure(tg.tg, sim) == []
    assert [method for method, _ in calls] == ["setMyProfilePhoto"] and not sim.writes


def test_cache_from_another_bot_cannot_skip_new_bots_profile_registration(sim, calls):
    configure(tg.tg, sim)
    calls.clear()
    assert configure(tg.tg, sim, token="different-offline-token") == []
    assert any(method == "setMyName" for method, _ in calls) and not sim.writes


def test_presentation_failure_is_sanitized_and_does_not_raise_from_startup(
    sim, calls, monkeypatch, caplog
):
    def fail(*args, **kwargs):
        raise RuntimeError("API_SECRET=private-presentation-diagnostic")

    monkeypatch.setattr(ui, "configure", fail)
    tg.configure_telegram_ui()
    assert "private-presentation-diagnostic" not in caplog.text
    assert "appearance unavailable" in caplog.text and not sim.writes


def test_multipart_files_and_transport_timeout_never_become_bot_api_fields(sim, monkeypatch):
    requests = []

    def post(url, **kwargs):
        requests.append(kwargs)
        return type("Response", (), {"json": lambda self: {"ok": True}})()

    monkeypatch.setattr(tg.requests, "post", post)
    with branding.AVATAR_IMAGE.open("rb") as photo:
        tg.tg(
            "setMyProfilePhoto",
            photo=json.dumps({"type": "static", "photo": "attach://avatar"}),
            files={"avatar": ("avatar.png", photo, "image/png")},
            _request_timeout=10,
        )
    assert requests[0]["timeout"] == 10 and "files" in requests[0]
    assert set(requests[0]["data"]) == {"photo"} and not sim.writes


def test_bundled_assets_and_startup_caption_fit_telegram_photo_limits():
    for path in (branding.WELCOME_IMAGE, branding.AVATAR_IMAGE):
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) < 10 * 1024 * 1024
        width, height = struct.unpack(">II", data[16:24])
        assert 320 <= min(width, height) and width + height < 10000
        if path == branding.AVATAR_IMAGE:
            assert width == height
    assert len(tg.startup_text().encode("utf-16-le")) // 2 <= 1024
