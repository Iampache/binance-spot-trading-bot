"""Safer Binance Spot exit engine. Testnet by default; no live access without opt-in.

Manages ONLY bot-owned orders recorded in state/trades. Orders remain at Binance
when the process stops. A stopped exchange or failed API is never a guarantee of
protection. Breakeven OCO replacement is intentionally NOT automated, because
cancel-then-create exposes a window without a stop.
"""
import csv
import json
import logging
import math
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / 'state'
TRADES_DIR = STATE_DIR / 'trades'
USE_TESTNET = os.getenv('USE_TESTNET', 'true').lower() == 'true'
ALLOW_LIVE = os.getenv('ALLOW_LIVE_TRADING', 'false').lower() == 'true'
MAX_DAILY_LOSS_USD = float(os.getenv('MAX_DAILY_LOSS_USD', '10'))
POLL_SECONDS = int(os.getenv('POLL_SECONDS', '5'))
FEE_ALLOWANCE_PCT = float(os.getenv('FEE_ALLOWANCE_PCT', '0.5'))  # conservative two-side fee allowance
LOG_FILE = str(BASE_DIR / 'exit_manager.log')
TRADE_LOG = str(BASE_DIR / 'trade_log.csv')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger('exit_manager')
client = None  # lazy: importing files never connects to exchange


def get_client():
    global client
    if client is None:
        from binance.client import Client
        key = os.getenv('API_KEY', '')
        secret = os.getenv('API_SECRET', '')
        if not key or not secret:
            raise RuntimeError('Missing API_KEY/API_SECRET in .env')
        client = Client(key, secret, testnet=USE_TESTNET)
    return client


def require_trade_permission():
    if not USE_TESTNET and not ALLOW_LIVE:
        raise RuntimeError('LIVE TRADING BLOCKED. Requires USE_TESTNET=false AND ALLOW_LIVE_TRADING=true')
    if not math.isfinite(MAX_DAILY_LOSS_USD) or MAX_DAILY_LOSS_USD <= 0:
        raise ValueError('Invalid daily loss limit')


def alert(msg):
    log.warning(msg)
    token, chat = os.getenv('TELEGRAM_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    if token and chat:
        try:
            r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                              data={'chat_id': chat, 'text': ('[TESTNET] ' if USE_TESTNET else '[LIVE] ') + msg}, timeout=12)
            r.raise_for_status()
        except Exception as e:
            log.error('Telegram notification failed: %s', str(e).replace(token, '***'))


def D(value):
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError('Price/quantity must be finite')
    return d


def round_to(value, step):
    value, step = D(value), D(step)
    if step <= 0 or value < 0:
        raise ValueError('Invalid value or exchange step')
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def fmt(x):
    return format(D(x).normalize(), 'f')


def symbol_filters(symbol):
    data = get_client().get_symbol_info(symbol)
    if not data or data.get('status') != 'TRADING' or data.get('quoteAsset') != 'USDT':
        raise ValueError(f'{symbol} not trading against USDT on this account')
    if not data.get('isSpotTradingAllowed', True):
        raise ValueError(f'{symbol} Spot trading disabled')
    fs = {f['filterType']: f for f in data['filters']}
    lot, price = fs['LOT_SIZE'], fs['PRICE_FILTER']
    market = fs.get('MARKET_LOT_SIZE', {})
    no = fs.get('NOTIONAL') or fs.get('MIN_NOTIONAL') or {}
    minnotional = D(no.get('minNotional', '0'))
    if minnotional <= 0:
        raise ValueError(f'{symbol} missing minimum notional exchange filter')
    return {
        'step': D(lot['stepSize']), 'tick': D(price['tickSize']),
        'min_qty': D(lot['minQty']), 'max_qty': D(lot['maxQty']),
        'market_step': D(market.get('stepSize') or lot['stepSize']) if D(market.get('stepSize') or '0') > 0 else D(lot['stepSize']),
        'market_min_qty': D(market.get('minQty') or lot['minQty']),
        'market_max_qty': D(market.get('maxQty') or lot['maxQty']),
        'min_notional': minnotional, 'max_notional': D(no.get('maxNotional') or '99999999999'),
        'base': data['baseAsset'],
    }


def validate_qty(qty, f, reference_price, *, market=False):
    qty, price = D(qty), D(reference_price)
    mn, mx = ('market_min_qty', 'market_max_qty') if market else ('min_qty', 'max_qty')
    step = f['market_step'] if market else f['step']
    if qty <= 0 or qty < f[mn] or qty > f[mx] or round_to(qty, step) != qty:
        raise ValueError('Quantity violates exchange LOT_SIZE/MARKET_LOT_SIZE')
    if qty * price < f['min_notional'] * D('1.05') or qty * price > f['max_notional']:
        raise ValueError('Order notional outside exchange limits (including safety margin)')


def position_size(account_usdt, risk_pct, entry, stop):
    account, rp, entry, stop = map(D, (account_usdt, risk_pct, entry, stop))
    if not (account > 0 and D('0') < rp <= D('2') and entry > stop > 0):
        raise ValueError('Invalid position size inputs')
    return (account * rp / 100) / (entry - stop)


def _atomic_write(path, item):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.pending-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fp:
            json.dump(item, fp, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_trade(rec):
    rec['updated_utc'] = datetime.now(timezone.utc).isoformat()
    _atomic_write(TRADES_DIR / (rec['id'] + '.json'), rec)


def new_trade(plan):
    rec = {'id': uuid.uuid4().hex, 'created_utc': datetime.now(timezone.utc).isoformat(),
           'symbol': plan['symbol'], 'plan': {k: str(v) for k, v in plan.items()
                                            if k in {'symbol','price','stop','t1','t2','qty','cost'}},
           'state': 'intent', 'oco': [], 'legs': [], 'breakeven_note_sent': False}
    save_trade(rec)
    return rec


def load_trades():
    result = []
    for path in sorted(TRADES_DIR.glob('*.json')) if TRADES_DIR.exists() else []:
        try:
            with path.open(encoding='utf-8') as f:
                result.append(json.load(f))
        except Exception as e:
            raise RuntimeError(f'Trade journal unreadable: {path}: {e}')
    return result


def active_trades():
    return [x for x in load_trades() if x['state'] != 'closed']


def todays_loss():
    """Bot-only pessimistic losses from exchange-confirmed exit fills saved in the journal.
    Block any new trade while a journal has uncertain/unfinished execution.
    """
    trades = load_trades()
    if any(t['state'] != 'closed' for t in trades):
        raise ValueError('Open or unresolved bot trade exists: inspect /status and Binance')
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(max(0.0, -float(t.get('estimated_pnl', 0))) for t in trades
               if t.get('closed_utc', '').startswith(today))


def place_oco(symbol, qty, tp, stop, tick, f=None, client_id=None):
    """Exchange-linked OCO, using current above/below API and stop-MARKET trigger."""
    f = f or symbol_filters(symbol)
    qty, tp, stop = D(qty), round_to(tp, tick), round_to(stop, tick)
    if not (stop > 0 and tp > stop):
        raise ValueError('Invalid protective price order')
    validate_qty(qty, f, min(tp, stop))
    live = D(get_client().get_symbol_ticker(symbol=symbol)['price'])
    if not (stop < live < tp):
        raise ValueError('Market moved outside Stop < Market < TP; abort OCO')
    return get_client().create_oco_order(
        symbol=symbol, side='SELL', quantity=fmt(qty),
        aboveType='LIMIT_MAKER', abovePrice=fmt(tp),
        belowType='STOP_LOSS', belowStopPrice=fmt(stop),
        listClientOrderId=client_id or ('bsv2' + uuid.uuid4().hex[:24]))


def resolve_oco(cid):
    """Reconcile a timed-out submission by exchange client ID; never blindly re-submit."""
    try:
        return get_client().get_order_list(origClientOrderId=cid)
    except Exception:
        return None


def submit_protection(rec, qty, t1, t2, stop, f):
    """Persist intent IDs before submission, so reconnection can find uncertain OCOs."""
    half = round_to(D(qty) / 2, f['step'])
    rest = D(qty) - half
    for legqty, tp in ((half, t1), (rest, t2)):
        validate_qty(legqty, f, min(D(stop), D(tp)))
    for n, (legqty, tp) in enumerate(((half, t1), (rest, t2)), 1):
        cid = 'bsv2' + uuid.uuid4().hex[:24]
        item = {'n': n, 'qty': fmt(legqty), 'tp': str(tp), 'stop': str(stop),
                'cid': cid, 'status': 'submitting', 'orderListId': None}
        rec['oco'].append(item)
        rec['state'] = 'protection_submitting'
        save_trade(rec)
        try:
            response = place_oco(rec['symbol'], legqty, tp, stop, f['tick'], f, cid)
        except Exception:
            response = resolve_oco(cid)
            if response is None:
                raise RuntimeError(f'OCO {n} response uncertain, id={cid}; inspect Binance before any retry')
        item['orderListId'] = response['orderListId']
        item['status'] = 'open'
        save_trade(rec)
    rec['state'] = 'protected'
    save_trade(rec)
    return rec


def oco_state(symbol, item):
    oid = item['orderListId']
    if not oid:
        raise RuntimeError('OCO has no confirmed exchange orderListId')
    orderlist = get_client().get_order_list(orderListId=oid)
    fills = []
    for leg in orderlist['orders']:
        order = get_client().get_order(symbol=symbol, orderId=leg['orderId'])
        if order['status'] == 'PARTIALLY_FILLED':
            raise RuntimeError(f'Partial OCO exit on {symbol}, order {leg["orderId"]}; manual reconciliation required')
        if order['status'] == 'FILLED':
            executed = D(order['executedQty'])
            quote = D(order['cummulativeQuoteQty'])
            if executed <= 0:
                raise RuntimeError('Filled order reports 0 executed amount')
            fills.append({'type': order['type'], 'order_id': order['orderId'],
                          'qty': fmt(executed), 'quote': fmt(quote), 'price': fmt(quote / executed)})
    if len(fills) > 1:
        raise RuntimeError('Unexpected both OCO legs filled')
    if fills:
        return 'filled', fills[0]
    if orderlist['listOrderStatus'] == 'EXECUTING':
        return 'open', None
    raise RuntimeError(f'OCO {oid} has no fill and status {orderlist["listOrderStatus"]}')


def _fee_in_quote(symbol, order_id, avg_price):
    """Return USDT fee for quote/base-asset commissions, or None for third-party asset fee."""
    trades = get_client().get_my_trades(symbol=symbol, orderId=order_id)
    base = symbol[:-4]
    total = D('0')
    for fill in trades:
        asset, fee = fill.get('commissionAsset'), D(fill.get('commission', '0'))
        if asset == 'USDT':
            total += fee
        elif asset == base:
            total += fee * D(fill['price'])
        elif fee != 0:
            return None
    return total


def account_and_journal_status(rec):
    """Every report queries Binance order states, not just old in-memory flags."""
    statuses = []
    for item in rec['oco']:
        if item.get('orderListId'):
            status, fill = oco_state(rec['symbol'], item)
            statuses.append((item, status, fill))
        else:
            resp = resolve_oco(item['cid'])
            if resp:
                item['orderListId'] = resp['orderListId']
                item['status'] = 'open'
                save_trade(rec)
                status, fill = oco_state(rec['symbol'], item)
                statuses.append((item, status, fill))
            else:
                raise RuntimeError(f'Unconfirmed OCO {item["cid"]}: do not assume unprotected or cancel blindly')
    return statuses


def check_trade(rec):
    """Reconcile exchange fills and report. Never cancel a remaining protective OCO for breakeven."""
    results = account_and_journal_status(rec)
    for item, status, fill in results:
        if status == 'filled' and item['status'] != 'filled':
            fee = _fee_in_quote(rec['symbol'], fill['order_id'], fill['price'])
            item['status'] = 'filled'
            item['fill'] = {**fill, 'fee_usdt': str(fee) if fee is not None else None}
            rec['legs'].append({'n': item['n'], **item['fill']})
            save_trade(rec)
            alert(f'{rec["symbol"]}: Exit leg {item["n"]} filled at ~{fill["price"]}.')
    if rec['oco'][0]['status'] == 'filled' and rec['oco'][1]['status'] == 'open' and not rec['breakeven_note_sent']:
        rec['breakeven_note_sent'] = True
        save_trade(rec)
        alert(f'{rec["symbol"]}: TP1/first leg finished. Original OCO2 protection retained; '
              'breakeven was NOT moved automatically (unsafe cancel/replace).')
    if len(rec['oco']) == 2 and all(x['status'] == 'filled' for x in rec['oco']):
        # Exchange-confirmed net gross P&L, pessimistic fee allowance when conversions are uncertain.
        bought_quote = D(rec['bought_quote'])
        bought_qty = D(rec['bought_qty'])
        exited_qty = sum(D(x['qty']) for x in rec['legs'])
        exit_quote = sum(D(x['quote']) for x in rec['legs'])
        if abs(exited_qty - D(rec['sell_qty'])) > D(rec['filters_step']):
            raise RuntimeError('Exit fills do not reconcile to protected quantity')
        allocated_buy = bought_quote * (exited_qty / bought_qty)
        gross = exit_quote - allocated_buy
        # Assume conservative 0.5% of total turnover; do not claim exact net P&L.
        allowance = (allocated_buy + exit_quote) * D(FEE_ALLOWANCE_PCT) / 100
        conservative = gross - allowance
        rec.update(state='closed', closed_utc=datetime.now(timezone.utc).isoformat(),
                   gross_pnl=str(gross), estimated_pnl=str(conservative),
                   fee_allowance_usdt=str(allowance))
        save_trade(rec)
        alert(f'{rec["symbol"]} complete; gross P&L {gross:.4f} USDT; '
              f'conservative P&L after fee allowance {conservative:.4f} USDT. Confirm account statement.')
        return True
    return False


def monitor_trade(rec, max_checks=None):
    failures = 0
    checks = 0
    while True:
        try:
            done = check_trade(rec)
            if done:
                return
            failures = 0
        except Exception as e:
            failures += 1
            alert(f'ORDER MONITOR ERROR: {e}; Binance open orders must be inspected. Attempt {failures}/3.')
            if failures >= 3:
                rec['state'] = 'needs_attention'
                save_trade(rec)
                alert('MONITOR HALTED, trade remains unresolved. New trades blocked; MANUAL INTERVENTION REQUIRED.')
                return
        checks += 1
        if max_checks is not None and checks >= max_checks:
            return
        time.sleep(POLL_SECONDS)


def recover_trades():
    """Call on startup. Never blindly duplicate an uncertain buy or OCO."""
    unresolved = active_trades()
    for rec in unresolved:
        alert(f'RECOVERY {rec["symbol"]}: journal state={rec["state"]}. Checking exchange order IDs.')
        if rec['state'] in ('protected', 'protection_submitting') and len(rec.get('oco', [])) == 2:
            try:
                account_and_journal_status(rec)
                # Leave protection intact and let monitor continue
            except Exception as e:
                rec['state'] = 'needs_attention'
                save_trade(rec)
                alert(f'RECOVERY REQUIRES MANUAL REVIEW: {e}')
        else:
            rec['state'] = 'needs_attention'
            save_trade(rec)
            alert('UNCERTAIN BUY/PROTECTION. MANUAL REVIEW REQUIRED. New buys blocked.')
    return unresolved


def emergency_sell_free(symbol, max_qty, f):
    """Sell ONLY base-asset balance currently free (not locked in surviving OCOs).
    Never cancel any existing protective order. Any residual needs manual attention.
    """
    base = f['base']
    free = D(get_client().get_asset_balance(asset=base)['free'])
    qty = round_to(min(D(max_qty), free), f['market_step'])
    if qty <= 0:
        raise RuntimeError('No free balance to emergency-sell; possible locked OCO balance')
    price = D(get_client().get_symbol_ticker(symbol=symbol)['price'])
    validate_qty(qty, f, price, market=True)
    response = get_client().order_market_sell(symbol=symbol, quantity=fmt(qty),
                                               newClientOrderId='bsv2e' + uuid.uuid4().hex[:22])
    alert(f'EMERGENCY sold available unprotected {symbol} qty={qty}. '
          'Check remaining holdings and existing OCOs manually.')
    return response


def close_bot_trade(symbol, confirmation):
    """Kill: close only bot-tracked holdings. No unqualified cancel-all-orders operation."""
    if confirmation != f'CLOSE_{symbol}':
        raise ValueError(f'Type exact confirmation CLOSE_{symbol}')
    matches = [x for x in active_trades() if x['symbol'] == symbol]
    if len(matches) != 1:
        raise RuntimeError('Cannot isolate exactly one bot-owned active trade')
    rec = matches[0]
    rec['state'] = 'closing'
    save_trade(rec)
    c = get_client()
    for o in rec.get('oco', []):
        if o.get('orderListId'):
            c.cancel_order_list(symbol=symbol, orderListId=o['orderListId'])
    # Cancel then close is NOT atomic. Verify actual account balance after cancellation.
    f = symbol_filters(symbol)
    response = emergency_sell_free(symbol, rec.get('sell_qty', rec['plan']['qty']), f)
    rec['state'] = 'needs_attention'  # must reconcile resulting fills/fees manually
    rec['kill_response'] = str(response.get('orderId', 'unknown'))
    save_trade(rec)
    alert(f'{symbol} close attempt completed; inspect Binance holdings/order history; journal needs reconciliation.')


def instance_lock():
    """Fail closed across processes on same PC, even when a previous process crashed."""
    STATE_DIR.mkdir(exist_ok=True, parents=True)
    file = STATE_DIR / 'instance.lock'
    fd = os.open(str(file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(f'pid={os.getpid()} at {datetime.now(timezone.utc).isoformat()}\n')
    return file


def cli():
    import argparse
    p = argparse.ArgumentParser()
    subs = p.add_subparsers(dest='cmd', required=True)
    subs.add_parser('status')
    k = subs.add_parser('kill')
    k.add_argument('--symbol', required=True)
    k.add_argument('--confirm', required=True)
    a = p.parse_args()
    lock = instance_lock()
    try:
        if a.cmd == 'status':
            print(json.dumps(load_trades(), indent=2))
            for record in active_trades():
                try:
                    print(record['symbol'], account_and_journal_status(record))
                except Exception as e:
                    print('MANUAL REVIEW:', str(e))
        elif a.cmd == 'kill':
            require_trade_permission()
            close_bot_trade(a.symbol, a.confirm)
    finally:
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    cli()
