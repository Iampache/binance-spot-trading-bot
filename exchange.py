"""Verified synchronous python-binance 1.0.37 Spot adapter; no retry of writes."""

from importlib.metadata import version

from binance.exceptions import BinanceAPIException


class Uncertain(RuntimeError):
    pass


class OwnershipError(RuntimeError):
    pass


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
    ):
        if not callable(getattr(Client, method, None)):
            raise RuntimeError("Unsupported Binance SDK interface")


def order(client, symbol, cid, side, order_id=None, aliases=()):
    selector = {"orderId": order_id} if order_id is not None else {"origClientOrderId": cid}
    result = client.get_order(symbol=symbol, **selector)
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
    result = client.v3_get_order_list(**selector)
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
        -1100,
        -1101,
        -1102,
        -1111,
        -1121,
        -2010,
    }
