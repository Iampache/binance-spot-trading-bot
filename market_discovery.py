"""Public Spot rankings only; no credentials, trade records or order submission."""

from decimal import Decimal

import entry_scanner as sc
import safety
from telegram_formatting import spot_url

REQUIRED_TYPES = {"LIMIT", "MARKET", "LIMIT_MAKER", "STOP_LOSS"}


def rank_pairs(category, limit=5):
    if category not in ("volume", "gainers") or type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Discovery supports volume or gainers, up to five results")
    info = sc.get("/api/v3/exchangeInfo")
    if not isinstance(info, dict) or not isinstance(info.get("symbols"), list):
        raise ValueError("Discovery: invalid Spot market metadata; no ranking available")
    symbols = set()
    seen_info = set()
    for row in info["symbols"]:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
            raise ValueError("Discovery: incomplete Spot symbol metadata")
        symbol = row["symbol"]
        if symbol in seen_info:
            raise ValueError("Discovery: duplicate Spot symbol metadata")
        seen_info.add(symbol)
        base = row.get("baseAsset")
        if (
            row.get("status") != "TRADING"
            or row.get("quoteAsset") != "USDT"
            or row.get("isSpotTradingAllowed") is not True
            or row.get("ocoAllowed") is not True
            or not isinstance(base, str)
            or not base
            or symbol != base + "USDT"
            or not isinstance(row.get("orderTypes"), list)
            or not REQUIRED_TYPES.issubset(row["orderTypes"])
            or base in sc.STABLES
            or base.endswith(sc.BAD_SUFFIX)
        ):
            continue
        try:
            spot_url(symbol)  # Link eligibility must not imply some other quote or domain.
        except ValueError:
            continue
        symbols.add(symbol)
    tickers = sc.get("/api/v3/ticker/24hr")
    if not isinstance(tickers, list):
        raise ValueError("Discovery: invalid 24-hour market response")
    results, seen, invalid = [], set(), 0
    for row in tickers:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("symbol"), str)
            or row["symbol"] not in symbols
        ):
            continue
        symbol = row["symbol"]
        if symbol in seen:
            raise ValueError("Discovery: duplicate market ticker; refresh the ranking")
        seen.add(symbol)
        try:
            price, change, volume = (
                Decimal(str(row[key])) for key in ("lastPrice", "priceChangePercent", "quoteVolume")
            )
            if (
                not all(value.is_finite() for value in (price, change, volume))
                or price <= 0
                or volume < 0
            ):
                raise ValueError("Invalid ticker")
        except (KeyError, ValueError, ArithmeticError):
            invalid += 1
            continue
        if volume < safety.MIN_QUOTE_VOLUME or (category == "gainers" and change <= 0):
            continue
        flags = []
        if change > sc.MAX_24H_PUMP:
            flags.append("Above +25% automatic-entry limit")
        if change < sc.MAX_24H_DUMP:
            flags.append("Below -15% automatic-entry limit")
        results.append(
            {
                "symbol": symbol,
                "price": price,
                "change": change,
                "quote_volume": volume,
                "flags": flags,
            }
        )
    if category == "volume":
        results.sort(key=lambda row: (-row["quote_volume"], -row["change"], row["symbol"]))
    else:
        results.sort(key=lambda row: (-row["change"], -row["quote_volume"], row["symbol"]))
    return {
        "category": category,
        "rows": results[:limit],
        "eligible_count": len(results),
        "invalid_count": invalid,
    }
