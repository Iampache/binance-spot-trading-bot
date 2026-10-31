"""Safe clickable Spot tickers and evidence-based one-position explanations."""

import json
from types import SimpleNamespace

import pytest
from test_bnb_fees import buy, enabled, fill_exits
from test_bot import approved, sig

import exchange as ex
import exit_manager as em
import telegram_assistant as tg
import trade_assistant as ta
from telegram_formatting import spot_url, ticker_link_options

REAL_ALERT = em.alert


def entity_text(text, entity):
    units = text.encode("utf-16-le")
    return units[entity["offset"] * 2 : (entity["offset"] + entity["length"]) * 2].decode(
        "utf-16-le"
    )


def test_links_point_to_exact_spot_pair_after_emoji_and_slash():
    text = "🔵 Ticker: UNIUSDT\n🔴 Blocked by LINK/USDT"
    options = ticker_link_options(text)
    links = json.loads(options["entities"])
    assert [entity_text(text, entity) for entity in links] == ["UNIUSDT", "LINK/USDT"]
    assert links[0]["url"] == "https://www.binance.com/en-AE/trade/UNI_USDT?type=spot"
    assert links[1]["url"] == "https://www.binance.com/en-AE/trade/LINK_USDT?type=spot"
    assert options["disable_web_page_preview"] == "true"
    assert all(entity["type"] == "text_link" for entity in links)


@pytest.mark.parametrize(
    "symbol", ["UNI/BTC", "UNIUSDT?redirect=evil", "https://evil.example/USDT", "USDT"]
)
def test_link_builder_never_accepts_other_quotes_or_url_injection(symbol):
    with pytest.raises(ValueError):
        spot_url(symbol)


def test_send_and_edit_keep_text_and_buttons_with_valid_link_entities(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "tg", lambda method, **kw: calls.append((method, kw)) or {"ok": True})
    text = "🔵 Ticker: UNIUSDT\nPreview only"
    buttons = '{"inline_keyboard":[[{"text":"Details","callback_data":"D:test"}]]}'
    tg.say(text, reply_markup=buttons)
    assert calls[-1][1]["text"] == text and calls[-1][1]["reply_markup"] == buttons
    tg.edit({"text": text, "mid": 1}, "LINKUSDT trade is in progress")
    for method, data in calls:
        assert "parse_mode" not in data
        assert data["disable_web_page_preview"] == "true"
        entities = json.loads(data["entities"])
        assert entity_text(data["text"], entities[0]) == "UNIUSDT"
    assert calls[-1][0] == "editMessageText"
    assert len(json.loads(calls[-1][1]["entities"])) == 2


def test_result_alert_transport_also_links_ticker_without_real_telegram(monkeypatch):
    captured = []
    monkeypatch.setenv("TELEGRAM_TOKEN", "offline-synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(
        em.requests,
        "post",
        lambda *args, **kwargs: (
            captured.append(kwargs["data"])
            or SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"ok": True})
        ),
    )
    assert REAL_ALERT("TRADE CLOSED\nTicker: UNIUSDT") is True
    assert json.loads(captured[0]["entities"])[0]["url"] == spot_url("UNIUSDT")
    assert captured[0]["disable_web_page_preview"] == "true"


def protected(sim, monkeypatch):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    plan = ta.build_plan(sig())
    return ta.execute(plan, approval=approved(plan))


def test_same_pair_active_trade_blocks_duplicate_with_specific_reason(sim, monkeypatch):
    protected(sim, monkeypatch)
    count = len(sim.writes)
    with pytest.raises(ta.ActiveTradeError, match="FOOUSDT trade already in progress"):
        ta.build_plan(sig())
    assert len(sim.writes) == count


def test_other_pair_message_names_the_existing_trade_and_one_trade_limit(sim, monkeypatch):
    protected(sim, monkeypatch)
    count = len(sim.writes)
    with pytest.raises(
        ta.ActiveTradeError, match="FOOUSDT trade is in progress; only one active"
    ) as caught:
        ta.build_plan({**sig(), "symbol": "BARUSDT"})
    assert "BARUSDT trade is in progress" not in str(caught.value)
    assert len(sim.writes) == count


def test_pending_watch_is_not_reported_as_a_filled_trade(sim, monkeypatch):
    monkeypatch.setattr(em, "monitor_trade", lambda rec: None)
    sim.price = em.D(101)
    p = ta.build_plan({**sig(), "entry_mode": "watch", "manual_override": True})
    ta.execute(p, approval=approved(p))
    with pytest.raises(ta.ActiveTradeError, match="entry watch is already active"):
        ta.build_plan(sig())
    assert not sim.writes


def test_flat_unaccounted_trade_is_reconciliation_pending_not_in_progress(sim, monkeypatch):
    enabled(sim, monkeypatch)
    rec = buy(sim)
    fill_exits(sim)
    monkeypatch.setattr(
        em, "bnb_fee_reference", lambda *args: (_ for _ in ()).throw(ex.Uncertain("missing"))
    )
    with pytest.raises(ex.Uncertain):
        em.reconcile_trade(rec)
    assert em.inventory(rec) == 0
    with pytest.raises(ta.ActiveTradeError, match="awaiting reconciliation"):
        ta.build_plan(sig())


def test_suggestion_alone_is_not_reported_as_active_trade(sim, monkeypatch):
    p = ta.build_plan(sig())
    monkeypatch.setattr(tg, "say", lambda *args, **kw: {"ok": True, "result": {"message_id": 1}})
    assert tg.send_proposal(sig(), p, "")
    assert tg.pending and not em.active_trades()
    assert ta.build_plan(sig())["symbol"] == "FOOUSDT"
    assert not sim.writes


def test_non_ascii_asset_name_is_encoded_without_breaking_ticker_offsets():
    text = "🔵 Ticker: 币安USDT"
    link = json.loads(ticker_link_options(text)["entities"])[0]
    assert entity_text(text, link) == "币安USDT"
    assert link["url"] == "https://www.binance.com/en-AE/trade/%E5%B8%81%E5%AE%89_USDT?type=spot"
