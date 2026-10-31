"""A flat but unaccounted trade must be diagnosable, never forcibly cleared."""

import pytest
from test_bnb_fees import buy, enabled, fill_exits
from test_bot import sig

import exchange as ex
import exit_manager as em
import safety
import trade_assistant as ta


def test_zero_inventory_missing_fee_reference_is_explained_then_closes_on_evidence(
    sim, monkeypatch
):
    monitor = em.monitor_trade
    enabled(sim, monkeypatch)
    sim.free["FOO"] = em.D(8)
    rec = buy(sim)
    fill_exits(sim)
    sim.server_time_ms = sim.clock + 61000  # Closed minute, but both reference sources unavailable.
    writes = len(sim.writes)
    messages = []
    monkeypatch.setattr(em, "alert", lambda text: messages.append(text) or True)
    sim.response_override = lambda method, path, data: [] if path == "aggTrades" else None
    monitor(rec, max_checks=1)
    saved = em.load_trades()[0]
    assert saved["state"] == "needs_attention" and em.inventory(saved) == 0
    assert saved["recovery_reason_code"] == "BNB_REFERENCE_MISSING"
    assert all(order["status"] in em.TERMINAL for order in saved["orders"])
    report = em.status_report()
    assert "Bot quantity is zero" in report and "BNB_REFERENCE_MISSING" in report
    assert "Historical BNB fee reference unavailable" in report
    assert "0E-" not in report
    assert any("BNB_REFERENCE_MISSING" in text for text in messages)
    assert not any("TRADE CLOSED" in text for text in messages)
    assert len(sim.writes) == writes and sim.free["FOO"] == 8
    with pytest.raises(ta.ActiveTradeError):
        ta.build_plan(sig())
    sim.response_override = None
    monitor(rec, max_checks=1)
    assert em.load_trades()[0]["state"] == "closed"
    assert not em.active_trades() and len(sim.writes) == writes
    assert any("TRADE CLOSED" in text for text in messages)


def test_truncated_bnb_history_is_distinct_and_still_blocked(sim):
    sim.response_override = lambda method, path, data: [{}] * 1000 if path == "aggTrades" else None
    with pytest.raises(ex.Uncertain) as caught:
        em.bnb_fee_reference(1000)
    assert caught.value.reason_code == "BNB_REFERENCE_TRUNCATED"
    assert "truncated" in safety.safe_error(caught.value)


def test_known_recovery_code_does_not_render_exception_text():
    class PrivateRecovery(ex.Uncertain):
        def __str__(self):
            raise AssertionError("Private exception text must not be accessed")

    error = PrivateRecovery("token=synthetic-secret", reason_code="BNB_REFERENCE_MISSING")
    report = safety.safe_error(error)
    assert "BNB_REFERENCE_MISSING" in report
    assert "synthetic-secret" not in report and "token=" not in report


@pytest.mark.parametrize("code", ["token=synthetic-secret", {"private": "secret"}, True])
def test_unknown_recovery_codes_are_never_exposed(code):
    error = ex.Uncertain("api_key=synthetic-secret", reason_code=code)
    assert error.reason_code is None
    assert safety.safe_error(error) == "Uncertain: operation failed; reconcile exchange state"


def test_sdk_cannot_inject_a_reason_or_private_error_text():
    error = RuntimeError("api_key=synthetic-secret")
    error.reason_code = "BNB_REFERENCE_MISSING"
    assert safety.safe_error(error) == "RuntimeError: operation failed; reconcile exchange state"
