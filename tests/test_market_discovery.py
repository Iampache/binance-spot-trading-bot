"""Public rankings never grant approval or alter an existing Spot position."""

import copy
import json
import time
from decimal import Decimal as D

import pytest
from test_bot import approved, sig

import exit_manager as em
import market_discovery as md
import telegram_assistant as tg
import trade_assistant as ta


def symbol(base, **changes):
    return {
        "symbol": base + "USDT",
        "baseAsset": base,
        "quoteAsset": "USDT",
        "status": "TRADING",
        "isSpotTradingAllowed": True,
        "ocoAllowed": True,
        "orderTypes": sorted(md.REQUIRED_TYPES),
        **changes,
    }


def ticker(base, volume="20000000", change="3", **changes):
    return {
        "symbol": base + "USDT",
        "lastPrice": "10",
        "priceChangePercent": change,
        "quoteVolume": volume,
        **changes,
    }


@pytest.fixture
def feed(monkeypatch):
    data = {"/api/v3/exchangeInfo": {"symbols": []}, "/api/v3/ticker/24hr": []}
    calls = []

    def public(path, **params):
        calls.append((path, params))
        return copy.deepcopy(data[path])

    def private_forbidden(*args, **kwargs):
        pytest.fail("Discovery must use only public market data")

    monkeypatch.setattr(md.sc, "get", public)
    monkeypatch.setattr(em, "get_client", private_forbidden)
    monkeypatch.setattr(em, "active_trades", private_forbidden)
    return data, calls


def add(data, base, volume="20000000", change="3", **metadata):
    data["/api/v3/exchangeInfo"]["symbols"].append(symbol(base, **metadata))
    data["/api/v3/ticker/24hr"].append(ticker(base, volume, change))


@pytest.mark.parametrize("category", ["volume", "gainers"])
def test_rankings_are_top_five_with_deterministic_ties_using_only_public_data(feed, category):
    data, calls = feed
    for base in reversed("ABCDEFG"):
        add(data, base)
    ranking = md.rank_pairs(category)
    assert [row["symbol"] for row in ranking["rows"]] == [base + "USDT" for base in "ABCDE"]
    assert ranking["eligible_count"] == 7 and ranking["invalid_count"] == 0
    assert calls == [("/api/v3/exchangeInfo", {}), ("/api/v3/ticker/24hr", {})]
    assert not tg.pending


def test_volume_ranks_usdt_turnover_and_gainers_ranks_positive_percent_change(feed):
    data, _ = feed
    add(data, "BIG", "60000000", "-2")
    add(data, "FAST", "11000000", "24")
    add(data, "MID", "20000000", "12")
    add(data, "FLAT", "30000000", "0")
    assert [r["symbol"] for r in md.rank_pairs("volume")["rows"]] == [
        "BIGUSDT",
        "FLATUSDT",
        "MIDUSDT",
        "FASTUSDT",
    ]
    assert [r["symbol"] for r in md.rank_pairs("gainers")["rows"]] == ["FASTUSDT", "MIDUSDT"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"status": "BREAK"},
        {"quoteAsset": "BTC"},
        {"isSpotTradingAllowed": False},
        {"isSpotTradingAllowed": "true"},
        {"ocoAllowed": False},
        {"orderTypes": ["LIMIT", "MARKET"]},
        {"baseAsset": "WRONG"},
    ],
)
def test_non_executable_spot_metadata_is_excluded(feed, metadata):
    data, _ = feed
    add(data, "BAD", **metadata)
    assert not md.rank_pairs("volume")["rows"]


def test_minimum_volume_boundary_and_shared_stable_leveraged_exclusions(feed):
    data, _ = feed
    add(data, "LOW", "9999999.99")
    add(data, "ENOUGH", "10000000")
    add(data, "USDC")
    add(data, "ETHUP")
    assert [r["symbol"] for r in md.rank_pairs("volume")["rows"]] == ["ENOUGHUSDT"]


def test_extreme_moves_are_visible_but_marked_outside_automatic_entry_limits(feed):
    data, _ = feed
    add(data, "PUMP", change="26")
    add(data, "DUMP", change="-16")
    rows = {r["symbol"]: r for r in md.rank_pairs("volume")["rows"]}
    assert rows["PUMPUSDT"]["flags"] == ["Above +25% automatic-entry limit"]
    assert rows["DUMPUSDT"]["flags"] == ["Below -15% automatic-entry limit"]
    assert md.rank_pairs("gainers")["rows"][0]["symbol"] == "PUMPUSDT"


@pytest.mark.parametrize(
    "field,value",
    [
        ("lastPrice", "NaN"),
        ("lastPrice", "0"),
        ("priceChangePercent", "inf"),
        ("quoteVolume", "-1"),
        ("quoteVolume", None),
    ],
)
def test_invalid_ticker_values_are_skipped_without_a_false_ranking(feed, field, value):
    data, _ = feed
    add(data, "BAD")
    data["/api/v3/ticker/24hr"][0][field] = value
    ranking = md.rank_pairs("gainers")
    assert not ranking["rows"] and ranking["invalid_count"] == 1


@pytest.mark.parametrize("path", ["/api/v3/exchangeInfo", "/api/v3/ticker/24hr"])
def test_duplicate_identities_fail_instead_of_choosing_conflicting_evidence(feed, path):
    data, _ = feed
    add(data, "COIN")
    rows = data[path]["symbols"] if path.endswith("exchangeInfo") else data[path]
    rows.append(copy.deepcopy(rows[0]))
    with pytest.raises(ValueError, match="duplicate"):
        md.rank_pairs("volume")


@pytest.mark.parametrize("category,limit", [("new", 5), ("volume", 6), ("volume", True)])
def test_invalid_queries_do_not_fetch_data(feed, category, limit):
    _, calls = feed
    with pytest.raises(ValueError):
        md.rank_pairs(category, limit)
    assert not calls


def message(text, sender=None):
    sender = tg.CHAT_ID if sender is None else sender
    return {"chat": {"id": sender}, "from": {"id": sender}, "text": text}


def click(action, vid, item, selection=None):
    return {
        "id": "cb",
        "from": {"id": tg.CHAT_ID},
        "message": {"chat": {"id": tg.CHAT_ID}, "message_id": item["mid"]},
        "data": f"{action}:{vid}" + (f":{selection}" if selection is not None else ""),
    }


@pytest.fixture
def ui(monkeypatch):
    calls = []

    def transport(method, **params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": len(calls)}}

    monkeypatch.setattr(tg, "tg", transport)
    rows = [
        {
            "symbol": "FOOUSDT",
            "price": D(100),
            "change": D(3),
            "quote_volume": D(20000000),
            "flags": [],
        }
    ]
    monkeypatch.setattr(
        md,
        "rank_pairs",
        lambda category: {
            "category": category,
            "rows": rows,
            "eligible_count": 1,
            "invalid_count": 0,
        },
    )
    return calls


def result_view():
    tg.handle_message(message("/discover volume"))
    return next(iter(tg.discovery_views.items()))


def test_discover_menu_and_category_buttons_do_not_create_a_proposal(sim, ui):
    tg.handle_message(message("/discover"))
    vid, item = next(iter(tg.discovery_views.items()))
    keyboard = json.loads(ui[-1][1]["reply_markup"])["inline_keyboard"]
    assert {b["text"] for row in keyboard for b in row} == {
        "High Volume",
        "Top Gainers",
        "New Listings",
        "Cancel",
    }
    tg.handle_callback(click("VG", vid, item))
    assert vid not in tg.discovery_views
    assert next(iter(tg.discovery_views.values()))["category"] == "gainers"
    assert not tg.pending and not tg.checks and not sim.writes


def test_ranked_rows_have_binance_links_and_exact_token_actions(sim, ui):
    vid, item = result_view()
    params = [p for method, p in ui if method == "sendMessage"][-1]
    buttons = json.loads(params["reply_markup"])["inline_keyboard"][0]
    assert buttons[0]["url"] == "https://www.binance.com/en-AE/trade/FOO_USDT?type=spot"
    assert buttons[1]["callback_data"] == f"VC:{vid}:0"
    assert buttons[2]["callback_data"] == f"VM:{vid}:0"
    assert "reference price" in item["text"] and "no setup/risk approval" in item["text"]
    assert not tg.pending and not sim.writes


def test_check_setup_runs_fresh_existing_analysis_for_selected_symbol(sim, ui, monkeypatch):
    seen = []
    monkeypatch.setattr(tg, "run_check", lambda *args, **kwargs: seen.append((args, kwargs)))
    vid, item = result_view()
    tg.handle_callback(click("VC", vid, item, 0))
    assert seen == [(("FOOUSDT",), {"generation": tg.proposal_generation})]
    assert not tg.discovery_views and not tg.pending and not sim.writes


def test_discovery_opens_all_three_modes_without_assuming_a_chart_signal(sim, ui):
    vid, item = result_view()
    tg.handle_callback(click("VM", vid, item, 0))
    cid, menu = next(iter(tg.checks.items()))
    tg.handle_callback(click("CT2", cid, menu))
    menu = tg.checks[cid]
    params = [p for method, p in ui if method == "sendMessage"][-1]
    actions = [
        b["callback_data"]
        for row in json.loads(params["reply_markup"])["inline_keyboard"]
        for b in row
    ]
    assert actions == [f"CN:{cid}", f"CW:{cid}", f"CQ:{cid}", f"CX:{cid}"]
    assert menu["res"]["sig"] is None and menu["res"]["status"] == "DISCOVERY"
    assert not tg.discovery_views and not tg.pending and not sim.writes


def test_read_only_discovery_during_protected_trade_then_modes_enforce_slot(sim, ui, monkeypatch):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    plan = ta.build_plan(sig())
    rec = ta.execute(plan, approval=approved(plan))
    count = len(sim.writes)
    vid, item = result_view()
    tg.handle_callback(click("VM", vid, item, 0))
    cid, menu = next(iter(tg.checks.items()))
    tg.handle_callback(click("CT2", cid, menu))
    tg.handle_callback(click("CQ", cid, menu))
    assert any("trade already in progress" in p.get("text", "") for _, p in ui)
    assert not tg.pending and len(sim.writes) == count
    assert em.coverage(rec) >= em.inventory(rec) > 0


@pytest.mark.parametrize("text", ["/discover new", "/discover listings"])
def test_new_listings_never_infers_dates_or_fetches_an_unverified_feed(sim, ui, monkeypatch, text):
    monkeypatch.setattr(md, "rank_pairs", lambda *a: pytest.fail("No verified listing feed"))
    tg.handle_message(message(text))
    assert "not available yet" in ui[-1][1]["text"]
    assert not tg.discovery_views and not tg.pending and not sim.writes


def test_new_listings_button_keeps_usable_menu_and_explains_limitation(sim, ui):
    tg.handle_message(message("/discover"))
    vid, item = next(iter(tg.discovery_views.items()))
    tg.handle_callback(click("VN", vid, item))
    assert vid in tg.discovery_views and "official listing date" in ui[-1][1]["text"]
    assert not sim.writes


@pytest.mark.parametrize("how", ["button", "global", "expired", "generation"])
def test_canceled_or_expired_discovery_buttons_are_inert(sim, ui, how):
    vid, item = result_view()
    if how == "button":
        tg.handle_callback(click("VX", vid, item))
    elif how == "global":
        tg.handle_message(message("/cancel"))
    elif how == "expired":
        item["created"] = time.time() - tg.CHECK_TTL - 1
        tg.expire_pending()
    else:
        item["generation"] -= 1
    tg.handle_callback(click("VM", vid, item, 0))
    assert not tg.discovery_views and not tg.checks and not tg.pending and not sim.writes
    edits = [p for m, p in ui if m in ("editMessageText", "editMessageReplyMarkup")]
    assert edits and all(json.loads(p["reply_markup"]) == {"inline_keyboard": []} for p in edits)


def test_unauthorized_requests_and_forged_selection_cannot_start_any_flow(sim, ui):
    tg.handle_message(message("/discover", "stranger"))
    assert not ui
    vid, item = result_view()
    forged = click("VM", vid, item, 0)
    forged["from"]["id"] = "stranger"
    count = len(ui)
    tg.handle_callback(forged)
    assert len(ui) == count
    tg.handle_callback(click("VM", vid, item, 99))
    assert vid in tg.discovery_views and not tg.checks and not sim.writes


def test_cancellation_while_public_snapshot_is_loading_cannot_restore_controls(
    sim, ui, monkeypatch
):
    ranking = md.rank_pairs("volume")

    def delayed(category):
        tg.handle_message(message("/cancel"))
        return ranking

    monkeypatch.setattr(md, "rank_pairs", delayed)
    tg.handle_message(message("/discover volume"))
    assert not tg.discovery_views and not tg.checks and not tg.pending and not sim.writes


def test_unavailable_feed_reports_failure_without_leaking_exception_or_creating_menu(
    sim, ui, monkeypatch
):
    def unavailable(category):
        raise TimeoutError("private upstream diagnostic")

    monkeypatch.setattr(md, "rank_pairs", unavailable)
    tg.handle_message(message("/discover gainers"))
    assert "unavailable" in ui[-1][1]["text"]
    assert "private upstream diagnostic" not in ui[-1][1]["text"]
    assert not tg.discovery_views and not tg.pending and not sim.writes
