"""Plain Telegram text with safe, UTF-16-indexed Binance Spot ticker links."""

import json
import re
from urllib.parse import quote

PAIR = re.compile(r"(?<!\w)([^\W_]+/?USDT)(?!\w)")


def spot_url(symbol):
    symbol = symbol.upper().replace("/", "")
    if not symbol.endswith("USDT") or not symbol[:-4] or not symbol[:-4].isalnum():
        raise ValueError("A USDT Spot pair is required for this link")
    return f"https://www.binance.com/en-AE/trade/{quote(symbol[:-4], safe='')}_USDT?type=spot"


def ticker_link_options(text):
    """Keep visible text unchanged; Telegram entity offsets count UTF-16 units."""
    entities = [
        {
            "type": "text_link",
            "offset": len(text[: match.start()].encode("utf-16-le")) // 2,
            "length": len(match.group().encode("utf-16-le")) // 2,
            "url": spot_url(match.group()),
        }
        for match in PAIR.finditer(text)
    ]
    options = {"disable_web_page_preview": "true"}
    if entities:
        options["entities"] = json.dumps(entities)
    return options
