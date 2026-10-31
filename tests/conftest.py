import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import binance.base_client
import binance.ws.reconnecting_websocket
import pytest
import requests
from simulator import SpotSimulator

import exit_manager as em
import runtime_health
import telegram_assistant as tg
import trading_settings


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("All network access is forbidden in tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(em, "STATE_DIR", tmp_path / "runtime_state")
    monkeypatch.setattr(em, "TRADES_DIR", None)
    monkeypatch.setattr(em, "CONTEXT", None)
    monkeypatch.setattr(em, "BASE_DIR", tmp_path)
    monkeypatch.setattr(em, "USE_TESTNET", True)
    monkeypatch.setattr(em, "ALLOW_LIVE", False)
    monkeypatch.setattr(em, "ALLOW_BNB_FEES", False)
    monkeypatch.setattr(em, "alert", lambda message: None)
    monkeypatch.setattr(em, "POLL_SECONDS", 0)
    em.STOP.clear()
    runtime_health.reset()
    trading_settings.reset_session()
    tg.last_preview.clear()
    tg.last_scan.clear()
    tg.pending.clear()
    tg.checks.clear()
    tg.setup_watches.clear()
    tg.scan_selections.clear()
    tg.discovery_views.clear()
    tg.setup_views.clear()
    tg.position_views.clear()
    tg.state.clear()
    tg.recheck_timers.clear()
    if tg.trade_lock.locked():
        tg.trade_lock.release()


@pytest.fixture
def sim(monkeypatch):
    # The synchronous SDK eagerly creates unused websocket event loops. On
    # Windows asyncio builds its wakeup socketpair using loopback connections,
    # which our strict socket guard correctly forbids. REST tests need no event
    # loop: an inert object avoids that setup without permitting any networking.
    unused_loop = object()
    monkeypatch.setattr(binance.base_client, "get_loop", lambda: unused_loop)
    monkeypatch.setattr(binance.ws.reconnecting_websocket, "get_loop", lambda: unused_loop)
    sim = SpotSimulator()
    monkeypatch.setattr(em, "client", sim.client)
    em.initialize()
    # The SDK's get_asset_balance calls get_account; retain that real interface.
    original = sim.request

    def request(method, path, signed=False, version=None, **kwargs):
        result = original(method, path, signed, version, **kwargs)
        if method == "get" and path == "account":
            result["balances"] = [
                {"asset": a, "free": str(sim.free[a]), "locked": str(sim.locked[a])}
                for a in sim.free
            ]
        return result

    sim.client._request_api = request
    return sim
