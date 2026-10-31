"""Verified synchronous python-binance 1.0.37 Spot adapter; no retry of writes."""

import logging
import threading
import time
from importlib.metadata import version

from binance.exceptions import BinanceAPIException


class Uncertain(RuntimeError):
    pass


class OwnershipError(RuntimeError):
    pass


class ClockSyncError(RuntimeError):
    pass


_CLOCK_LOCK = threading.RLock()
log = logging.getLogger(__name__)


def synchronize_clock(client, *, force=False):
    """Use public Spot server time; leave recvWindow and host clock unchanged.

    Select the lowest-latency of three samples and account for round-trip time.
    Cache briefly, but invalidate on a wall-clock jump (for example domain NTP).
    Slow/inconsistent samples fail closed rather than guessing a signing clock.
    """
    with _CLOCK_LOCK:
        now, wall = time.monotonic(), time.time() * 1000
        cached = getattr(client, "_spot_clock_sample", None)
        if cached and not force:
            elapsed = now - cached["at"]
            if (
                0 <= elapsed < 30
                and abs(wall - cached["wall"] - elapsed * 1000) < 250
                and client.timestamp_offset == cached["offset"]
            ):
                return
        client._spot_clock_sample = None  # Failed refresh must not revive a stale cache.
        samples = []
        for _ in range(3):
            start, start_wall = time.monotonic(), time.time() * 1000
            try:
                response = client.get_server_time()
            except Exception:
                log.debug("Public server-time sample unavailable; no request details logged")
                continue  # Only retry the public read, never an order submission.
            end, end_wall = time.monotonic(), time.time() * 1000
            rtt = (end - start) * 1000
            server = response.get("serverTime") if isinstance(response, dict) else None
            if (
                type(server) is int
                and server > 0
                and 0 <= rtt <= 1000
                and abs(end_wall - start_wall - rtt) < 250
            ):
                samples.append((rtt, int(server - (start_wall + end_wall) / 2), end, end_wall))
        if not samples:
            raise ClockSyncError("No reliable public server-time sample")
        _, offset, measured_at, measured_wall = min(samples, key=lambda sample: sample[0])
        if abs(time.time() * 1000 - measured_wall - (time.monotonic() - measured_at) * 1000) >= 250:
            raise ClockSyncError("Host clock changed while synchronizing")
        client.timestamp_offset = offset
        client._spot_clock_sample = {
            "at": measured_at,
            "wall": measured_wall,
            "offset": offset,
        }


def signed_read(client, method, **params):
    """One clock-correction retry for a known read endpoint, never for writes."""
    allowed = {"get_account", "get_order", "v3_get_order_list"}
    if method not in allowed:
        raise ValueError("Clock-correction retry is restricted to read endpoints")
    synchronize_clock(client)
    try:
        return getattr(client, method)(**params)
    except BinanceAPIException as error:
        if error.code != -1021:
            raise
        synchronize_clock(client, force=True)
        return getattr(client, method)(**params)


def verify_sdk():
    from binance.client import Client

    if version("python-binance") != "1.0.37":
        raise RuntimeError("Requires python-binance==1.0.37")
    for method in (
        "v3_get_order_list",
        "v3_delete_order_list",
        "create_oco_order",
        "get_order",
        "create_order",
        "cancel_order",
        "get_my_trades",
        "v3_get_account_commission",
        "get_server_time",
    ):
        if not callable(getattr(Client, method, None)):
            raise RuntimeError("Unsupported Binance SDK interface")


def order(client, symbol, cid, side, order_id=None, aliases=()):
    selector = {"orderId": order_id} if order_id is not None else {"origClientOrderId": cid}
    result = signed_read(client, "get_order", symbol=symbol, **selector)
    if (
        result.get("symbol") != symbol
        or result.get("side") != side
        or result.get("clientOrderId") not in {cid, *aliases}
    ):
        raise OwnershipError("Exchange order identity mismatch")
    return result


def order_list(client, symbol, cid, order_list_id=None, aliases=()):
    selector = (
        {"orderListId": order_list_id} if order_list_id is not None else {"origClientOrderId": cid}
    )
    result = signed_read(client, "v3_get_order_list", **selector)
    if result.get("symbol") != symbol or result.get("listClientOrderId") not in {
        cid,
        *aliases,
    }:
        raise OwnershipError("Exchange OCO identity mismatch")
    return result


def cancel_list(client, symbol, cid, order_list_id, cancel_cid):
    # Cancellation can replace a client ID; both aliases must be durable first.
    order_list(client, symbol, cid, order_list_id, [cancel_cid])
    return client.v3_delete_order_list(
        symbol=symbol, orderListId=order_list_id, newClientOrderId=cancel_cid
    )


def definitely_rejected(error):
    # Transport errors, rate limits and matching-engine timeouts are ambiguous.
    return isinstance(error, BinanceAPIException) and error.code in {
        -1013,
        -1021,  # Timestamp validation rejected the write before execution; do not retry it.
        -1100,
        -1101,
        -1102,
        -1111,
        -1121,
        -2010,
    }
