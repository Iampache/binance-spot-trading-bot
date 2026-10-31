"""Shared, static product identity; no account or environment data."""

from pathlib import Path

BOT_NAME = "NEXVANT"
VERSION = "1.5.0"
TAGLINE = "Your approval. Every trade."
GIT_URL = "https://github.com/Iampache/binance-spot-trading-bot"
BRANDING = f"{BOT_NAME} | v{VERSION}\nBinance Spot Trading Assistant\nConcept & Developed by Fazil\nGit: {GIT_URL}"
ASSET_DIR = Path(__file__).resolve().parent / "assets"
WELCOME_IMAGE = ASSET_DIR / "nexvant-welcome.png"
AVATAR_IMAGE = ASSET_DIR / "nexvant-avatar.png"
SHORT_DESCRIPTION = "NEXVANT | Binance Spot: discover setups, manage positions and track P&L. Every new buy needs your approval."
DESCRIPTION = (
    "NEXVANT — your Binance Spot trading assistant.\n\n"
    "Discover liquid USDT pairs, review trading setups, manage bot positions and "
    "track completed-trade profit and loss. Choose Manual, Assisted or Auto Setup.\n\n"
    "Every new buy requires your Telegram approval. Use /start to open your dashboard."
)
