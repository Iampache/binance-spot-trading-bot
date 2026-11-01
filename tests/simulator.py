"""Offline matching-engine model behind the actual pinned SDK's HTTP boundary."""

import copy
import time
from decimal import Decimal as D

from binance.client import Client
from binance.exceptions import BinanceAPIException


class Crash(BaseException):
    """Simulates process loss, bypassing application exception handlers."""


def api_error(code, message="simulated exchange rejection"):
    return BinanceAPIException(None, 400, '{"code":' + str(code) + ',"msg":"' + message + '"}')


class SpotSimulator:
    def __init__(self):
        self.client = Client("offline-test-key", "offline-test-secret", testnet=True, ping=False)
        self.client._request_api = self.request
        self.uid = "12345"
        self.symbols = ["FOOUSDT"]
        self.price = D("100")
        self.free = {"USDT": D("500"), "FOO": D("0"), "BNB": D(1)}
        self.locked = {"USDT": D("0"), "FOO": D("0"), "BNB": D(0)}
        self.orders = {}
        self.lists = {}
        self.trades = []
        self.writes = []
        self.next_id = 1
        self.next_list = 1
        self.buy_behavior = "filled"
        self.sell_behavior = "filled"
        self.oco_behavior = {}
        self.read_failures = 0
        self.fulfill = False
        self.min_notional = "1"
        self.fee_rate = D(".001")
        self.fee_asset = "USDT"
        self.base_fee_step = None  # Optional venue commission-precision quantum.
        self.bnb_price = D(600)
        self.bnb_discount = False
        self.discount_multiplier = D(".75")
        self.bnb_history = []
        self.bnb_candles = []
        self.server_time_ms = None
        self.clock = int(time.time() * 1000)
        self.prewrite = lambda kind, data: None
        self.response_override = None

    def info(self):
        return {
            "symbol": "FOOUSDT",
            "status": "TRADING",
            "quoteAsset": "USDT",
            "baseAsset": "FOO",
            "isSpotTradingAllowed": True,
            "ocoAllowed": True,
            "orderTypes": ["LIMIT", "MARKET", "LIMIT_MAKER", "STOP_LOSS"],
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "stepSize": ".001",
                    "minQty": ".001",
                    "maxQty": "1000",
                },
                {
                    "filterType": "MARKET_LOT_SIZE",
                    "stepSize": "0",
                    "minQty": "0",
                    "maxQty": "1000",
                },
                {
                    "filterType": "PRICE_FILTER",
                    "tickSize": ".01",
                    "minPrice": ".01",
                    "maxPrice": "1000000",
                },
                {"filterType": "MIN_NOTIONAL", "minNotional": self.min_notional},
            ],
        }

    def make_order(self, data, side, kind="MARKET", cid=None):
        oid = self.next_id
        self.next_id += 1
        qty = D(data["quantity"])
        order = {
            "symbol": data.get("symbol", "FOOUSDT"),
            "orderId": oid,
            "clientOrderId": cid or data["newClientOrderId"],
            "side": side,
            "type": data.get("type", kind),
            "price": data.get("price", "0"),
            "timeInForce": data.get("timeInForce", "GTC"),
            "status": "NEW",
            "origQty": str(qty),
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
        }
        self.orders[oid] = order
        return order

    def fill(self, order, qty=None, price=None, terminal=True):
        base = order["symbol"][:-4]
        qty = D(qty if qty is not None else D(order["origQty"]) - D(order["executedQty"]))
        price = D(price if price is not None else self.price)
        if qty <= 0:
            return
        quote = qty * price
        asset = self.fee_asset
        fee = (qty if asset == base else quote) * self.fee_rate
        if asset == base and self.base_fee_step is not None:
            fee = (fee // self.base_fee_step) * self.base_fee_step
        if asset == "BNB":
            multiplier = self.discount_multiplier if self.bnb_discount else D(1)
            fee = quote * self.fee_rate * multiplier / self.bnb_price
            if self.free["BNB"] < fee:
                asset, fee = "USDT", quote * self.fee_rate
            else:
                self.free["BNB"] -= fee
                self.bnb_history.append(
                    {"a": len(self.bnb_history) + 1, "T": self.clock, "p": str(self.bnb_price)}
                )
        if order.get("orderListId"):
            item = self.lists[order["orderListId"]]
            self.locked[base] -= qty
            for other in item["orders"]:
                sibling = self.orders[other["orderId"]]
                if sibling["orderId"] != order["orderId"]:
                    sibling["status"] = "CANCELED"
        if order["side"] == "BUY":
            self.free["USDT"] -= quote + (fee if asset == "USDT" else 0)
            self.free[base] += qty - (fee if asset == base else 0)
        else:
            if not order.get("orderListId"):
                self.free[base] -= qty + (fee if asset == base else 0)
            elif asset == base:
                self.free[base] -= fee
            self.free["USDT"] += quote - (fee if asset == "USDT" else 0)
        order["executedQty"] = str(D(order["executedQty"]) + qty)
        order["cummulativeQuoteQty"] = str(D(order["cummulativeQuoteQty"]) + quote)
        order["status"] = "FILLED" if terminal else "PARTIALLY_FILLED"
        self.trades.append(
            {
                "id": len(self.trades) + 1,
                "symbol": order["symbol"],
                "orderId": order["orderId"],
                "isBuyer": order["side"] == "BUY",
                "time": self.clock,
                "qty": str(qty),
                "quoteQty": str(quote),
                "price": str(price),
                "commission": str(fee),
                "commissionAsset": asset,
            }
        )
        if order.get("orderListId") and terminal:
            self.lists[order["orderListId"]]["listOrderStatus"] = "ALL_DONE"

    def request(self, method, path, signed=False, version=None, **kwargs):
        data = kwargs.get("data", {})
        if self.response_override:
            value = self.response_override(method, path, data)
            if value is not None:
                return value
        if method == "get":
            if path == "time":
                return {
                    "serverTime": self.server_time_ms
                    if self.server_time_ms is not None
                    else int(time.time() * 1000)
                }
            if self.read_failures and path in ("order", "orderList", "myTrades"):
                self.read_failures -= 1
                raise TimeoutError("synthetic transport failure")
            if path == "account":
                return {"uid": int(self.uid), "accountType": "SPOT", "canTrade": True}
            if path == "exchangeInfo":
                info = self.info()
                return {
                    "symbols": [info]
                    + [{**info, "symbol": s, "baseAsset": s[:-4]} for s in self.symbols[1:]]
                }
            if path == "ticker/24hr":
                return {
                    "symbol": data.get("symbol", "FOOUSDT"),
                    "quoteVolume": "20000000",
                    "lastPrice": str(self.price),
                    "priceChangePercent": "1",
                }
            if path == "ticker/price":
                if data.get("symbol") == "BNBUSDT":
                    return {"symbol": "BNBUSDT", "price": str(self.bnb_price)}
                return {"symbol": data.get("symbol", "FOOUSDT"), "price": str(self.price)}
            if path == "ticker/bookTicker":
                return {
                    "symbol": data.get("symbol", "FOOUSDT"),
                    "bidPrice": str(self.price - D(".01")),
                    "askPrice": str(self.price),
                }
            if path == "depth":
                return {
                    "bids": [[str(self.price), "20"]],
                    "asks": [[str(self.price), "20"]],
                }
            if path == "avgPrice":
                return {"mins": 5, "price": str(self.price)}
            if path == "account/commission":
                return {
                    "symbol": data.get("symbol", "FOOUSDT"),
                    "discount": {
                        "enabledForAccount": self.bnb_discount,
                        "enabledForSymbol": self.bnb_discount,
                        "discountAsset": "BNB",
                        "discount": str(self.discount_multiplier),
                    },
                    **{
                        name: {
                            "maker": str(self.fee_rate if name == "standardCommission" else 0),
                            "taker": str(self.fee_rate if name == "standardCommission" else 0),
                            "buyer": "0",
                            "seller": "0",
                        }
                        for name in (
                            "standardCommission",
                            "taxCommission",
                            "specialCommission",
                        )
                    },
                }
            if path == "klines" and data["symbol"] == "BNBUSDT":
                return copy.deepcopy(self.bnb_candles)
            if path == "aggTrades":
                return [
                    copy.deepcopy(row)
                    for row in self.bnb_history
                    if data["startTime"] <= row["T"] <= data["endTime"]
                ][: data["limit"]]
            if path == "openOrders":
                return [
                    copy.deepcopy(o)
                    for o in self.orders.values()
                    if o["status"] not in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
                ]
            if path == "myTrades":
                return [copy.deepcopy(t) for t in self.trades if t["orderId"] == data["orderId"]]
            if path == "order":
                order = next(
                    (
                        o
                        for o in self.orders.values()
                        if o["clientOrderId"] == data.get("origClientOrderId")
                        or o["orderId"] == data.get("orderId")
                    ),
                    None,
                )
                if order is None:
                    raise api_error(-2013, "Order does not exist")
                return copy.deepcopy(order)
            if path == "orderList":
                item = next(
                    (
                        v
                        for v in self.lists.values()
                        if v["listClientOrderId"] == data.get("origClientOrderId")
                        or v["orderListId"] == data.get("orderListId")
                    ),
                    None,
                )
                if item is None:
                    raise api_error(-2013, "Order list does not exist")
                return copy.deepcopy(item)
        if method == "post" and path == "order":
            self.prewrite("order", data)
            self.writes.append((method, path, copy.deepcopy(data)))
            behavior = self.buy_behavior if data["side"] == "BUY" else self.sell_behavior
            if behavior == "timeout_before":
                raise TimeoutError("synthetic timeout")
            if behavior == "rejected":
                raise api_error(-2010)
            order = self.make_order(data, data["side"])
            if behavior not in ("new", "crash_before_fill", "expired_empty"):
                quantity = D(order["origQty"])
                if behavior in ("partial", "expired"):
                    quantity = (quantity / 2 // D(".001")) * D(".001")
                self.fill(order, qty=quantity, terminal=behavior not in ("partial", "expired"))
                if behavior == "expired":
                    order["status"] = "EXPIRED"
            if behavior == "expired_empty":
                order["status"] = "EXPIRED"
            if behavior.startswith("crash"):
                raise Crash()
            if behavior == "timeout_accept":
                raise TimeoutError("accepted but timed out")
            return copy.deepcopy(order)
        if method == "post" and path == "orderList/oco":
            self.prewrite("oco", data)
            self.writes.append((method, path, copy.deepcopy(data)))
            behavior = self.oco_behavior.get(self.next_list, "filled" if self.fulfill else "open")
            if behavior == "rejected":
                raise api_error(-2010)
            lid = self.next_list
            self.next_list += 1
            qty = D(data["quantity"])
            base = data["symbol"][:-4]
            if self.free[base] < qty:
                raise api_error(-2010)
            top = self.make_order(data, "SELL", "LIMIT_MAKER", data["aboveClientOrderId"])
            bottom = self.make_order(data, "SELL", "STOP_LOSS", data["belowClientOrderId"])
            top["orderListId"] = bottom["orderListId"] = lid
            top["price"] = data["abovePrice"]
            bottom["stopPrice"] = data["belowStopPrice"]
            self.free[base] -= qty
            self.locked[base] += qty
            item = {
                "symbol": data["symbol"],
                "listClientOrderId": data["listClientOrderId"],
                "orderListId": lid,
                "listOrderStatus": "EXECUTING",
                "orders": [
                    {
                        "symbol": data["symbol"],
                        "orderId": o["orderId"],
                        "clientOrderId": o["clientOrderId"],
                    }
                    for o in (top, bottom)
                ],
            }
            self.lists[lid] = item
            if behavior == "filled":
                self.fill(top, price=data["abovePrice"])
            if behavior == "timeout_accept":
                raise TimeoutError("accepted OCO; timeout")
            if behavior == "crash":
                raise Crash()
            return copy.deepcopy(item)
        if method == "delete" and path == "order":
            self.prewrite("cancel", data)
            self.writes.append((method, path, copy.deepcopy(data)))
            o = next(o for o in self.orders.values() if o["orderId"] == data["orderId"])
            o["status"] = "CANCELED"
            o["clientOrderId"] = data["newClientOrderId"]
            return copy.deepcopy(o)
        if method == "delete" and path == "orderList":
            self.prewrite("cancel_list", data)
            self.writes.append((method, path, copy.deepcopy(data)))
            item = self.lists[data["orderListId"]]
            item["listClientOrderId"] = data["newClientOrderId"]
            legs = [self.orders[o["orderId"]] for o in item["orders"]]
            left = D(legs[0]["origQty"]) - sum((D(o["executedQty"]) for o in legs), D(0))
            self.locked[item["symbol"][:-4]] -= left
            self.free[item["symbol"][:-4]] += left
            for o in legs:
                if o["status"] not in ("FILLED", "CANCELED"):
                    o["status"] = "CANCELED"
            item["listOrderStatus"] = "ALL_DONE"
            return copy.deepcopy(item)
        raise AssertionError(f"Unexpected simulated API call: {method} {path}")
