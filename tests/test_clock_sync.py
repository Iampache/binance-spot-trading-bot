"""Public-time correction, real SDK signing, and strict no-write-retry boundaries."""

from urllib.parse import parse_qs

import pytest
from binance.exceptions import BinanceAPIException
from simulator import api_error
from test_bot import approved, sig

import exchange as ex
import exit_manager as em
import trade_assistant as ta


class Clock:
    def __init__(self):
        self.wall = 1_800_000_000.0
        self.mono = 100.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def use_clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ex.time, "time", lambda: clock.wall)
    monkeypatch.setattr(ex.time, "monotonic", lambda: clock.mono)
    return clock


@pytest.mark.parametrize("host_offset", [-60_000, 60_000])
def test_actual_sdk_signed_timestamp_uses_corrected_server_time(sim, monkeypatch, host_offset):
    clock = use_clock(monkeypatch)
    calls = []

    def server_time():
        calls.append(True)
        clock.advance(0.1)
        return {"serverTime": int(clock.wall * 1000 + host_offset - 50)}

    monkeypatch.setattr(sim.client, "get_server_time", server_time)
    original_window = sim.client.REQUEST_RECVWINDOW
    ex.synchronize_clock(sim.client, force=True)
    assert len(calls) == 3
    assert sim.client.timestamp_offset == host_offset
    # Exercise the pinned SDK's real signer (synthetic credentials, no HTTP).
    params = sim.client._get_request_kwargs("get", signed=True, data={"symbol": "FOOUSDT"})
    signed = parse_qs(params["params"])
    assert int(signed["timestamp"][0]) == int(clock.wall * 1000 + host_offset)
    assert sim.client.REQUEST_RECVWINDOW == original_window
    assert int(signed["recvWindow"][0]) == original_window
    assert not sim.writes


def test_lowest_latency_sample_is_selected(sim, monkeypatch):
    clock = use_clock(monkeypatch)
    rows = iter([(0.8, 999), (0.1, 2000), (0.6, 888)])

    def server_time():
        delay, offset = next(rows)
        start = clock.wall
        clock.advance(delay)
        return {"serverTime": round((start + clock.wall) * 500 + offset)}

    monkeypatch.setattr(sim.client, "get_server_time", server_time)
    ex.synchronize_clock(sim.client, force=True)
    assert abs(sim.client.timestamp_offset - 2000) <= 1


def test_clock_cache_expires_and_detects_domain_clock_jump(sim, monkeypatch):
    clock = use_clock(monkeypatch)
    calls = []

    def server_time():
        calls.append(True)
        return {"serverTime": int(clock.wall * 1000)}

    monkeypatch.setattr(sim.client, "get_server_time", server_time)
    ex.synchronize_clock(sim.client, force=True)
    ex.synchronize_clock(sim.client)
    assert len(calls) == 3
    clock.advance(31)
    ex.synchronize_clock(sim.client)
    assert len(calls) == 6
    clock.wall += 20  # Windows/domain NTP jumps without monotonic time moving.
    ex.synchronize_clock(sim.client)
    assert len(calls) == 9


@pytest.mark.parametrize(
    "response", [{}, {"serverTime": True}, {"serverTime": "private"}, {"serverTime": 0}]
)
def test_invalid_time_response_fails_closed_and_invalidates_cache(sim, monkeypatch, response):
    previous = sim.client.timestamp_offset
    monkeypatch.setattr(sim.client, "get_server_time", lambda: response)
    with pytest.raises(ex.ClockSyncError):
        ex.synchronize_clock(sim.client, force=True)
    assert sim.client.timestamp_offset == previous
    assert sim.client._spot_clock_sample is None
    assert not sim.writes


def test_slow_time_samples_fail_closed(sim, monkeypatch):
    clock = use_clock(monkeypatch)

    def server_time():
        clock.advance(2)
        return {"serverTime": int(clock.wall * 1000)}

    monkeypatch.setattr(sim.client, "get_server_time", server_time)
    with pytest.raises(ex.ClockSyncError):
        ex.synchronize_clock(sim.client, force=True)
    assert not sim.writes


def test_unavailable_public_clock_prevents_any_signed_account_read(sim, monkeypatch):
    clock_reads, account_reads = [], []

    def unavailable():
        clock_reads.append(True)
        raise TimeoutError("synthetic transport failure")

    monkeypatch.setattr(sim.client, "_spot_clock_sample", None)
    monkeypatch.setattr(sim.client, "get_server_time", unavailable)
    monkeypatch.setattr(sim.client, "get_account", lambda: account_reads.append(True))
    with pytest.raises(ex.ClockSyncError):
        em.initialize()
    assert len(clock_reads) == 3 and not account_reads and not sim.writes


def test_account_timestamp_error_forces_one_resync_and_read_retry(sim, monkeypatch):
    reads, syncs = [], []
    original_sync = ex.synchronize_clock

    def sync(client, force=False):
        syncs.append(force)
        original_sync(client, force=force)

    def account():
        reads.append(True)
        if len(reads) == 1:
            raise api_error(-1021)
        return {"uid": 12345, "accountType": "SPOT"}

    monkeypatch.setattr(ex, "synchronize_clock", sync)
    monkeypatch.setattr(sim.client, "get_account", account)
    assert em.initialize()["account_uid"] == "12345"
    assert len(reads) == 2 and syncs.count(True) == 1
    assert not sim.writes


def test_persistent_timestamp_error_has_only_one_read_retry(sim, monkeypatch):
    reads = []

    def fail():
        reads.append(True)
        raise api_error(-1021)

    monkeypatch.setattr(sim.client, "get_account", fail)
    with pytest.raises(BinanceAPIException) as result:
        ex.signed_read(sim.client, "get_account")
    assert result.value.code == -1021 and len(reads) == 2


def test_other_account_error_is_not_retried(sim, monkeypatch):
    reads = []

    def fail():
        reads.append(True)
        raise api_error(-2015)

    monkeypatch.setattr(sim.client, "get_account", fail)
    with pytest.raises(BinanceAPIException) as result:
        ex.signed_read(sim.client, "get_account")
    assert result.value.code == -2015 and len(reads) == 1


@pytest.mark.parametrize("method", ["create_order", "order_market_sell", "v3_delete_order_list"])
def test_clock_retry_helper_cannot_be_used_for_writes(sim, method):
    with pytest.raises(ValueError, match="restricted to read"):
        ex.signed_read(sim.client, method)
    assert not sim.writes


def test_timestamp_rejected_buy_is_journaled_but_never_retried(sim, monkeypatch):
    attempts = []

    def fail(method, path, data):
        if method == "post" and path == "order":
            attempts.append(data["newClientOrderId"])
            raise api_error(-1021)

    sim.response_override = fail
    monkeypatch.setattr(em, "monitor_trade", lambda rec: em.reconcile_trade(rec))
    plan = ta.build_plan(sig())
    rec = ta.execute(plan, approval=approved(plan))
    em.recover_trades()
    assert len(attempts) == 1
    assert rec["state"] == "closed"
    assert rec["orders"][0]["local_rejection"]
    assert rec["orders"][0]["status"] == "REJECTED"
    assert not sim.orders and not sim.lists
