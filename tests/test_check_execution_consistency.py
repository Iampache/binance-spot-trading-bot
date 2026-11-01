"""On-demand checks use the same risk planner, with a fresh quote and no override."""

from decimal import Decimal as D

import pytest
from test_bot import sig

import entry_scanner as sc
import telegram_assistant as tg


@pytest.mark.parametrize("command", ["/check FOO", "/scan FOO"])
def test_changed_quote_can_make_new_plan_valid_without_bypassing_gates(sim, monkeypatch, command):
    original_info = sim.info

    def info():
        data = original_info()
        next(rule for rule in data["filters"] if rule["filterType"] == "PRICE_FILTER")[
            "tickSize"
        ] = ".001"
        return data

    monkeypatch.setattr(sim, "info", info)
    signal = {
        **sig(),
        "entry": D("6.995"),
        "stop": D("6.91609"),
        "t1": D("7.113365"),
        "t2": D("7.192275"),
        "target_policy": "scanner_r_multiple_v1",
        "resistance_ceiling": D("7.4"),
    }
    monkeypatch.setattr(tg.ta, "scan_candidates", lambda: ({"FOOUSDT": signal}, "BTC warning"))
    monkeypatch.setattr(sc, "last_alert", {})
    messages = []
    monkeypatch.setattr(
        tg,
        "say",
        lambda text, **kw: messages.append((text, kw)) or {"ok": True, "result": {"message_id": 1}},
    )
    monkeypatch.setattr(tg, "tg", lambda *args, **kw: {"ok": True})
    sim.price = D("6.998")
    result = tg.scan_once()[0]
    assert "below 1.5R" in result and "minimum TP1 7.121" in result
    assert not tg.pending and not sim.writes
    assert not messages  # A background rejection must not interrupt the owner.
    result = {
        "symbol": "FOOUSDT",
        "price": 6.995,
        "chg": 4.3,
        "qv": 60000000,
        "sig": signal,
        "reasons": [],
        "status": "SAFE",
        "note": "BTC warning",
    }
    monkeypatch.setattr(tg, "check_ticker", lambda symbol: result)
    sim.price = D("6.995")
    tg.handle_message({"chat": {"id": tg.CHAT_ID}, "from": {"id": tg.CHAT_ID}, "text": command})
    assert "Setup found — execution checks pending" in messages[-1][0]
    assert "Reference price" in messages[-1][0] and "SAFE TO ENTER" not in messages[-1][0]
    cid = next(iter(tg.checks))
    tg.handle_check_callback({"id": "cb"}, "C1", cid)
    tg.handle_check_callback({"id": "cb"}, "CT2", cid)
    assert len(tg.pending) == 1 and not sim.writes
    p = next(iter(tg.pending.values()))["plan"]
    assert p["price"] == D("6.995") and p["rr"] >= D("1.5")
    assert p["manual_override"] is False
    assert p["cost"] <= 12 and p["loss"] <= p["risk_budget"]
