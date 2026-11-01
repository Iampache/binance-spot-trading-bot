"""NEXVANT presentation and owner-scoped menus; no exchange/order operations."""

import hashlib
import json

from branding import AVATAR_IMAGE, BOT_NAME, DESCRIPTION, SHORT_DESCRIPTION, TAGLINE

COMMANDS = [
    {"command": "start", "description": "Open the NEXVANT dashboard"},
    {"command": "discover", "description": "Discover USDT Spot tokens"},
    {"command": "check", "description": "Check a coin and review a setup"},
    {"command": "scanlog", "description": "View the last scan and skipped pairs"},
    {"command": "position", "description": "View bot positions or request an exit"},
    {"command": "portfolio", "description": "View bot holdings, estimated P&L and risk"},
    {"command": "pnl", "description": "View completed-trade P&L for today"},
    {"command": "setup", "description": "Change your trading limits"},
    {"command": "status", "description": "View balances and protection"},
    {"command": "health", "description": "Check monitoring and entry blockers"},
    {"command": "showpr", "description": "View the latest saved proposal"},
    {"command": "cancel", "description": "Cancel pending selections and entry watches"},
    {"command": "help", "description": "View all commands and trading modes"},
]
HOME_ACTIONS = {
    "discover": "/discover",
    "check": "/check",
    "positions": "/position",
    "portfolio": "/portfolio",
    "pnl": "/pnl",
    "setup": "/setup",
    "status": "/status",
    "health": "/health",
    "help": "/help",
}


def home_keyboard():
    rows = [
        [("🔎 Discover tokens", "discover"), ("📊 Check a coin", "check")],
        [("💼 Positions", "positions"), ("📈 Today's P&L", "pnl")],
        [("⚙️ Trading setup", "setup"), ("🛡️ Account status", "status")],
        [("📊 Portfolio risk", "portfolio"), ("🩺 Bot health", "health")],
        [("☰ All commands", "help")],
    ]
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": "H:" + action} for label, action in row]
            for row in rows
        ]
    }


def welcome_text():
    return (
        f"Welcome to {BOT_NAME}\n{TAGLINE}\n\n"
        "Your Binance Spot command center.\n"
        "Discover USDT setups, review bot positions and track completed-trade P&L.\n\n"
        "Manual • Assisted • Auto Setup\n"
        "Every new buy requires your approval.\n\n"
        "Choose an option below, or use Telegram's Menu for commands."
    )


def configure(api, *, chat_id, token, cache_path, atomic_write, stopped):
    """Best-effort Bot API cosmetics; cache only presentation, never trading state.

    Fingerprints bind the public content/assets to the configured bot/chat without
    persisting the token. Only successful methods are cached, so failures retry at
    a later startup and the profile photo isn't repeatedly added on every restart.
    """
    if not chat_id or not token:
        return ["configuration"]
    operations = [
        (
            "setMyCommands",
            {
                "scope": json.dumps({"type": "chat", "chat_id": chat_id}),
                "commands": json.dumps(COMMANDS),
            },
        ),
        (
            "setChatMenuButton",
            {"chat_id": chat_id, "menu_button": json.dumps({"type": "commands"})},
        ),
        ("setMyName", {"name": BOT_NAME}),
        ("setMyDescription", {"description": DESCRIPTION}),
        ("setMyShortDescription", {"short_description": SHORT_DESCRIPTION}),
        (
            "setMyProfilePhoto",
            {"photo": json.dumps({"type": "static", "photo": "attach://avatar"})},
        ),
    ]
    try:
        avatar_hash = hashlib.sha256(AVATAR_IMAGE.read_bytes()).hexdigest()
    except OSError:
        avatar_hash = "missing"
    fingerprint = hashlib.sha256(
        json.dumps([operations, avatar_hash, str(chat_id)], sort_keys=True).encode()
        + b"\0"
        + token.encode()
    ).hexdigest()
    applied = set()
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("fingerprint") == fingerprint and isinstance(cached.get("applied"), list):
            applied = {name for name in cached["applied"] if name in {op[0] for op in operations}}
    except (OSError, ValueError, TypeError, AttributeError):
        applied = set()  # A damaged cosmetic cache re-applies presentation only.
    failures = []
    for method, params in operations:
        if stopped():
            return failures + ["interrupted"]
        if method in applied:
            continue
        try:
            if method == "setMyProfilePhoto":
                with AVATAR_IMAGE.open("rb") as photo:
                    result = api(
                        method,
                        **params,
                        files={"avatar": (AVATAR_IMAGE.name, photo, "image/png")},
                        _request_timeout=10,
                    )
            else:
                result = api(method, **params, _request_timeout=10)
            if result and result.get("ok") is True:
                applied.add(method)
            else:
                failures.append(method)
        except Exception:
            failures.append(method)  # Do not stringify private HTTP errors.
    try:
        atomic_write(
            cache_path, {"version": 1, "fingerprint": fingerprint, "applied": sorted(applied)}
        )
    except Exception:
        failures.append("presentation cache")
    return failures
