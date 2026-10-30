import json
import math
import sys
import types
from decimal import Decimal
from pathlib import Path

import pytest

# python-binance SDK unavailable in this offline test environment. Only its
# exception import is stubbed; all exchange operations are faked explicitly.
if 'binance.exceptions' not in sys.modules:
    binance = types.ModuleType('binance')
    exceptions = types.ModuleType('binance.exceptions')
    exceptions.BinanceAPIException = type('BinanceAPIException', (Exception,), {})
    sys.modules['binance'] = binance
    sys.modules['binance.exceptions'] = exceptions

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import entry_scanner as sc
import exit_manager as em
import trade_assistant as ta
import telegram_assistant as tg


class FakeExchange:
    def __init__(self):
        self.price = 100.0
        self.usdt = 500.0
        self.base = 0.0
        self.sell_calls = 0
        self.buy_calls = 0
        self.ocos = {}
        self.next_oco = 1
        self.fail_oco_at = 0
        self.fail_buy = False
        self.partial_buy = False
        self.fulfill = False
        self.cancelled = []

    def get_symbol_info(self, symbol):
        return {'symbol':symbol, 'status':'TRADING', 'quoteAsset':'USDT', 'baseAsset':symbol[:-4],
                'isSpotTradingAllowed':True,
                'filters':[{'filterType':'LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'1000'},
                           {'filterType':'MARKET_LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'1000'},
                           {'filterType':'PRICE_FILTER','tickSize':'0.01'},
                           {'filterType':'MIN_NOTIONAL','minNotional':'10'}]}

    def get_symbol_ticker(self, symbol): return {'price':str(self.price)}
    def get_asset_balance(self, asset): return {'free':str(self.usdt if asset=='USDT' else self.base)}
    def get_open_orders(self, symbol=None): return []
    def get_orderbook_ticker(self, symbol): return {'bidPrice':'100','askPrice':'100.05'}
    def get_order_book(self, symbol, limit):
        return {'bids':[['100','20']], 'asks':[['100.05','20']]}

    def order_market_buy(self, symbol, quantity, newClientOrderId):
        self.buy_calls += 1
        if self.fail_buy: raise TimeoutError('lost connection, buy uncertain')
        qty = Decimal(quantity)
        self.usdt -= float(qty)*self.price
        self.base += float(qty)
        return {'status':'PARTIALLY_FILLED' if self.partial_buy else 'FILLED',
                'orderId':1,'executedQty':str(qty),'cummulativeQuoteQty':str(qty * Decimal('100')),
                'fills':[{'price':'100','qty':str(qty),'commissionAsset':'USDT','commission':'0'}]}

    def create_oco_order(self, **params):
        if self.fail_oco_at == self.next_oco:
            self.next_oco += 1
            raise ValueError('exchange rejected OCO')
        oid = self.next_oco
        self.next_oco += 1
        self.ocos[oid] = params
        self.base -= float(params['quantity'])
        return {'orderListId':oid}

    def get_order_list(self, **kw):
        if 'origClientOrderId' in kw:
            raise LookupError('OCO not found by client id')
        oid = kw['orderListId']
        assert oid in self.ocos
        return {'listOrderStatus':'ALL_DONE' if self.fulfill else 'EXECUTING',
                'orders':[{'orderId':oid*10+1},{'orderId':oid*10+2}]}

    def get_order(self, symbol=None, orderId=None, origClientOrderId=None):
        if origClientOrderId: raise LookupError('buy unknown')
        oid, leg = divmod(orderId,10)
        if self.fulfill and leg == 1:
            qty = Decimal(self.ocos[oid]['quantity'])
            # target 1 or target 2
            price = Decimal(self.ocos[oid]['abovePrice'])
            return {'status':'FILLED','type':'LIMIT_MAKER','orderId':orderId,
                    'executedQty':str(qty), 'cummulativeQuoteQty':str(qty*price)}
        return {'status':'CANCELED' if self.fulfill else 'NEW', 'type':'STOP_LOSS',
                'orderId':orderId,'executedQty':'0','cummulativeQuoteQty':'0'}

    def get_my_trades(self, symbol, orderId):
        return [{'commission':'0.001','commissionAsset':'USDT','price':'100'}]

    def order_market_sell(self, symbol, quantity, newClientOrderId):
        self.sell_calls += 1
        qty = float(quantity)
        self.base -= qty
        return {'orderId':989, 'status':'FILLED'}

    def cancel_order_list(self, symbol, orderListId):
        self.cancelled.append(orderListId)
        self.base += float(self.ocos[orderListId]['quantity'])


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    # No external network, no Telegram call, no production state edits.
    monkeypatch.setattr(em, 'STATE_DIR', tmp_path/'state')
    monkeypatch.setattr(em, 'TRADES_DIR', tmp_path/'state'/'trades')
    monkeypatch.setattr(em, 'client', FakeExchange())
    monkeypatch.setattr(em, 'alert', lambda msg: None)
    monkeypatch.setattr(em, 'USE_TESTNET', True)
    monkeypatch.setattr(em, 'ALLOW_LIVE', False)
    monkeypatch.setattr(em, 'POLL_SECONDS', 0)
    monkeypatch.setattr(ta, 'LIVE', False)
    monkeypatch.setattr(ta, 'MAX_OPEN_TRADES', 1)
    monkeypatch.setattr(ta, 'MAX_POSITION_USDT', 50)
    monkeypatch.setattr(sc, 'RISK_PCT', 1)
    tg.pending.clear(); tg.checks.clear(); tg.state.clear()


def sig():
    return {'symbol':'FOOUSDT','entry':100.0,'stop':97.0,'t1':104.5,'t2':107.5,
            'setup':'MANUAL entry (your levels)','rsi':49,'chg24':1}


def test_15r_alignment():
    p=ta.build_plan(sig())
    assert p['rr'] == pytest.approx(1.5)
    assert p['cost'] <= 50
    assert p['loss'] <= 5
    assert p['half'] + p['rest'] == p['qty']


def test_10m_volume_stays_default():
    assert sc.MIN_QUOTE_VOLUME == 10_000_000


def test_manual_nan_inf_negative_rejected():
    for bad in [float('nan'),float('inf'),-1.0,0.0]:
        s=sig();s['entry']=bad
        with pytest.raises(ValueError): ta.require_finite_levels(s)


def test_order_filters_and_split():
    f=em.symbol_filters('FOOUSDT')
    with pytest.raises(ValueError): em.validate_qty(Decimal('0.1005'),f,100)
    with pytest.raises(ValueError): em.validate_qty(Decimal('0.01'),f,100)
    with pytest.raises(ValueError): em.place_oco('FOOUSDT',Decimal('.25'),104.5,101, f['tick'],f)


def test_buy_then_two_protected_ocos_and_journal():
    fake=em.client
    fake.fulfill=True
    rec=ta.execute(ta.build_plan(sig()))
    assert fake.buy_calls == 1
    assert len(fake.ocos) == 2
    assert rec['state'] == 'closed'
    assert len(rec['legs']) == 2
    assert float(rec['gross_pnl']) > 0
    assert em.active_trades() == []
    assert len(list(em.TRADES_DIR.glob('*.json'))) == 1
    assert all(v['aboveType']=='LIMIT_MAKER' and v['belowType']=='STOP_LOSS' for v in fake.ocos.values())


def test_unknown_buy_blocks_retry_and_does_not_liquidate():
    fake=em.client
    fake.fail_buy=True
    with pytest.raises(RuntimeError, match='Unknown buy result'):
        ta.execute(ta.build_plan(sig()))
    assert fake.buy_calls==1
    assert fake.sell_calls==0
    assert em.active_trades()[0]['state']=='needs_attention'
    with pytest.raises(ValueError,match='Open or unresolved'):
        ta.build_plan(sig())


def test_second_oco_fail_leaves_first_protection_in_place():
    fake=em.client
    fake.fail_oco_at=2
    with pytest.raises(RuntimeError, match='uncertain'):
        ta.execute(ta.build_plan(sig()))
    assert len(fake.ocos)==1
    assert fake.cancelled==[]
    assert fake.sell_calls==1
    assert em.active_trades()[0]['state']=='needs_attention'


def test_restart_recovery_protected_ocos_and_refuses_duplicate():
    fake=em.client
    p=ta.build_plan(sig())
    r=em.new_trade(p)
    r.update(state='bought',bought_qty='0.5',bought_quote='50',bought_avg='100',sell_qty='0.5',filters_step='0.001')
    em.save_trade(r)
    em.submit_protection(r, Decimal('0.5'), Decimal('104.5'),Decimal('107.5'), Decimal('97'), em.symbol_filters('FOOUSDT'))
    assert em.recover_trades()[0]['id'] == r['id']
    assert em.active_trades()[0]['state']=='protected'
    with pytest.raises(ValueError,match='Open or unresolved'):
        ta.build_plan(sig())


def test_closed_pnl_daily_loss_guard():
    r=em.new_trade(ta.build_plan(sig()))
    r.update(state='closed',estimated_pnl='-12.01', closed_utc=em.datetime.now(em.timezone.utc).isoformat())
    em.save_trade(r)
    assert em.todays_loss()==pytest.approx(12.01)
    with pytest.raises(ValueError,match='Daily bot-trade loss limit'):
        ta.build_plan(sig())


def test_live_disabled_without_explicit_optin():
    em.USE_TESTNET=False
    with pytest.raises(RuntimeError, match='LIVE TRADING BLOCKED'):
        em.require_trade_permission()


def test_kill_requires_confirm_and_closes_bot_owned_only():
    fake=em.client
    p=ta.build_plan(sig())
    r=em.new_trade(p)
    r.update(state='bought',sell_qty='0.5',bought_qty='0.5',bought_quote='50',filters_step='0.001')
    em.save_trade(r)
    fake.base=.5
    with pytest.raises(ValueError): em.close_bot_trade('FOOUSDT','BAD')
    em.close_bot_trade('FOOUSDT','CLOSE_FOOUSDT')
    assert fake.sell_calls==1
    assert em.active_trades()[0]['state']=='needs_attention'


def test_replay_telegram_proposal_rejected(monkeypatch):
    calls=[]
    monkeypatch.setattr(tg,'tg',lambda method,**kw: calls.append((method,kw)) or {'ok':True})
    # callback id unknown, no approval gets through
    cb={'from':{'id':tg.CHAT_ID},'message':{'chat':{'id':tg.CHAT_ID}},'data':'A:deadbeef','id':'cb1'}
    tg.handle_callback(cb)
    assert calls and calls[0][0]=='answerCallbackQuery'
    assert em.client.buy_calls==0


def test_lock_prevents_two_instances():
    lock=em.instance_lock()
    try:
        with pytest.raises(FileExistsError): em.instance_lock()
    finally:
        lock.unlink()
