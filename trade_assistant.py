"""Plan and execute scanner/manual Binance Spot buys with exchange-backed exit protection.
Run telegram_assistant.py for interactive operation; this file contains no standalone loop.
"""
import math
import os
import time
import uuid
from decimal import Decimal

import entry_scanner as sc
import exit_manager as em

MAX_POSITION_USDT = float(os.getenv('MAX_POSITION_USDT', '50'))
MAX_OPEN_TRADES = int(os.getenv('MAX_OPEN_TRADES', '1'))
MIN_RR = float(os.getenv('MIN_RR', '1.5'))  # aligned with entry_scanner TP1 = 1.5R
MAX_SPREAD_BPS = float(os.getenv('MAX_SPREAD_BPS', '30'))
MIN_DEPTH_MULTIPLE = float(os.getenv('MIN_DEPTH_MULTIPLE', '5'))
MAX_PRICE_DRIFT_PCT = float(os.getenv('MAX_PRICE_DRIFT_PCT', '0.5'))
LIVE = not em.USE_TESTNET


def scan_candidates():
    note = sc.btc_note()
    coins = sc.universe()
    sc.log.info('Scanning %s pairs', len(coins))
    cands = {}
    for symbol, _, chg in coins:
        try:
            res = sc.analyze(symbol, chg)
            if res:
                cands[symbol] = res
            time.sleep(0.15)
        except Exception as e:
            sc.log.warning('%s scan error: %s', symbol, e)
    return cands, note


def require_finite_levels(sig):
    vals = [sig['entry'], sig['stop'], sig['t1'], sig['t2']]
    if not all(math.isfinite(float(x)) and float(x) > 0 for x in vals):
        raise ValueError('All levels must be finite and positive')
    if not (float(sig['stop']) < float(sig['entry']) < float(sig['t1']) < float(sig['t2'])):
        raise ValueError('STOP < ENTRY < TP1 < TP2 required')


def market_liquidity(symbol, estimated_cost, *, enforce=True):
    c = em.get_client()
    book = c.get_orderbook_ticker(symbol=symbol)
    bid, ask = em.D(book['bidPrice']), em.D(book['askPrice'])
    if not (bid > 0 and ask >= bid):
        raise ValueError('Invalid best bid / ask')
    spread_bps = float((ask - bid) / ask * 10000)
    if enforce and spread_bps > MAX_SPREAD_BPS:
        raise ValueError(f'Spread too wide: {spread_bps:.1f} bps > {MAX_SPREAD_BPS} bps')
    depths = c.get_order_book(symbol=symbol, limit=20)
    bid_depth = sum(em.D(price) * em.D(qty) for price, qty in depths['bids'])
    ask_depth = sum(em.D(price) * em.D(qty) for price, qty in depths['asks'])
    if enforce and min(bid_depth, ask_depth) < em.D(estimated_cost) * em.D(MIN_DEPTH_MULTIPLE):
        raise ValueError('Order-book depth too low for requested quantity')
    return spread_bps, float(min(bid_depth, ask_depth))


def build_plan(sig):
    """Live exchange checks; no order is placed. Raises ValueError when any check fails."""
    require_finite_levels(sig)
    symbol = sig['symbol']
    c = em.get_client()
    price = float(c.get_symbol_ticker(symbol=symbol)['price'])
    if not math.isfinite(price) or price <= 0:
        raise ValueError('Invalid live price')
    stop, t1, t2 = map(float, (sig['stop'], sig['t1'], sig['t2']))
    drift = price / float(sig['entry']) - 1
    if em.USE_TESTNET and sig.get('setup', '').startswith('MANUAL') is False:
        ratio = price / float(sig['entry'])
        stop, t1, t2 = stop * ratio, t1 * ratio, t2 * ratio
    elif abs(drift) > 0.01:
        raise ValueError(f'Live entry moved {drift * 100:+.2f}% from signal; max 1%')
    if not (0 < stop < price < t1 < t2):
        raise ValueError('Invalid STOP < live entry < TP1 < TP2')
    risk = price - stop
    rr = (t1 - price) / risk
    if rr + 1e-9 < MIN_RR:
        raise ValueError(f'TP1 reward/risk is {rr:.2f}R; require {MIN_RR}R')
    if not (0.01 <= risk / price <= 0.06):
        raise ValueError('Stop must be 1%-6% below the live entry')
    if not math.isfinite(sc.RISK_PCT) or not (0 < sc.RISK_PCT <= 2):
        raise ValueError('RISK_PCT must be within (0,2]')
    if not (math.isfinite(MAX_POSITION_USDT) and MAX_POSITION_USDT > 0):
        raise ValueError('Invalid position cap')
    if em.todays_loss() >= em.MAX_DAILY_LOSS_USD:
        raise ValueError('Daily bot-trade loss limit reached')
    open_symbols = {o['symbol'] for o in c.get_open_orders()}
    if len(open_symbols) >= MAX_OPEN_TRADES:
        raise ValueError(f'Open orders on {sorted(open_symbols)}; open trade limit {MAX_OPEN_TRADES}')
    balance = float(c.get_asset_balance(asset='USDT')['free'])
    if not math.isfinite(balance) or balance <= 0:
        raise ValueError('No available USDT')
    f = em.symbol_filters(symbol)
    max_cost = min(MAX_POSITION_USDT, balance * 0.95)
    units = (balance * sc.RISK_PCT / 100) / risk
    units = min(units, max_cost / price)
    # Satisfy both LOT_SIZE (protective OCO) and MARKET_LOT_SIZE (buy) simultaneously.
    qty = em.round_to(units, f['step'])
    for _ in range(20):
        adjusted = em.round_to(qty, f['market_step'])
        if adjusted == qty:
            break
        qty = em.round_to(adjusted, f['step'])
    else:
        raise ValueError('Incompatible exchange market/limit step sizes')
    em.validate_qty(qty, f, price, market=True)
    em.validate_qty(qty, f, price)
    half = em.round_to(qty / 2, f['step'])
    rest = qty - half
    if half <= 0 or rest <= 0:
        raise ValueError('Position too small to split')
    # Binance minimum notional must hold for every stop leg, not just targets.
    for part in (half, rest):
        em.validate_qty(part, f, stop)
    spread, depth = market_liquidity(symbol, float(qty) * price, enforce=LIVE)
    return {'symbol': symbol, 'price': price, 'stop': stop, 't1': t1, 't2': t2,
            'qty': qty, 'half': half, 'rest': rest, 'step': f['step'],
            'cost': float(qty) * price, 'balance': balance,
            'loss': float(qty) * risk, 'gain1': float(half) * (t1 - price),
            'gain2': float(rest) * (t2 - price), 'risk_pct': risk / price * 100,
            'rr': rr, 'spread_bps': spread, 'depth_usdt': depth}


def show_plan(p, sig, note):
    print(f'[{"LIVE" if LIVE else "TESTNET"}] {p["symbol"]}, entry {p["price"]}, '
          f'SL {p["stop"]}, TP1 {p["t1"]}, TP2 {p["t2"]}, '
          f'qty {em.fmt(p["qty"])}, cost {p["cost"]:.2f} USDT, max-loss ~{p["loss"]:.2f} plus fees')
    if note:
        print(note)


def execute(plan):
    """Execute exactly ONE protected trade; refuse duplicates/uncertain journal states."""
    em.require_trade_permission()
    if em.active_trades():
        raise RuntimeError('Another tracked trade is active or unresolved. Reconcile first.')
    symbol = plan['symbol']
    c = em.get_client()
    f = em.symbol_filters(symbol)
    qty = em.D(plan['qty'])
    em.validate_qty(qty, f, plan['price'], market=True)
    em.validate_qty(qty, f, plan['price'])
    current = float(c.get_symbol_ticker(symbol=symbol)['price'])
    deviation = abs(current / plan['price'] - 1) * 100
    if deviation > MAX_PRICE_DRIFT_PCT:
        raise ValueError('Price moved too far before order submission')
    if not (plan['stop'] < current < plan['t1']):
        raise ValueError('Price has crossed exit levels')
    market_liquidity(symbol, float(qty) * current, enforce=LIVE)
    # Check we are not about to duplicate any open positions since plan generation.
    if c.get_open_orders():
        raise RuntimeError('Open exchange orders detected before new buy: refuse ambiguous ownership')
    if em.todays_loss() >= em.MAX_DAILY_LOSS_USD:
        raise RuntimeError('Daily loss limit reached')
    free_usdt = float(c.get_asset_balance(asset='USDT')['free'])
    if free_usdt < float(qty) * current * 1.01:
        raise ValueError('Insufficient USDT including 1% cost buffer')
    rec = em.new_trade(plan)
    cid = 'bsv2b' + uuid.uuid4().hex[:22]
    rec.update(state='buy_submitting', buy_client_id=cid)
    em.save_trade(rec)
    try:
        try:
            buy = c.order_market_buy(symbol=symbol, quantity=em.fmt(qty), newClientOrderId=cid)
        except Exception:
            # Query by the original client ID. Never retry a timed-out market buy.
            try:
                buy = c.get_order(symbol=symbol, origClientOrderId=cid)
            except Exception as e:
                rec['state'] = 'needs_attention'
                em.save_trade(rec)
                em.alert(f'BUY UNKNOWN for {symbol}, client order {cid}: {e}. CHECK BINANCE NOW, DO NOT RE-RUN.')
                raise RuntimeError('Unknown buy result; manual reconciliation required')
        got = em.D(buy.get('executedQty') or '0')
        quote = em.D(buy.get('cummulativeQuoteQty') or '0')
        if got <= 0 or quote <= 0:
            rec['state'] = 'needs_attention'
            em.save_trade(rec)
            raise RuntimeError('Market buy not confirmed filled: inspect order history')
        avg = quote / got
        if buy.get('status') != 'FILLED':
            rec['state'] = 'needs_attention'
            em.save_trade(rec)
            raise RuntimeError('Partial/incomplete market buy; manual reconciliation required')
        rec.update(state='bought', buy_order_id=buy['orderId'], bought_qty=str(got),
                   bought_quote=str(quote), bought_avg=str(avg), filters_step=str(f['step']))
        em.save_trade(rec)
        # An OCO requires stop < live ticker < target. Rapid slippage might invalidate it.
        free_base = em.D(c.get_asset_balance(asset=f['base'])['free'])
        fees_base = em.D('0')
        fills = buy.get('fills') or []
        for fill in fills:
            if fill.get('commissionAsset') == f['base']:
                fees_base += em.D(fill['commission'])
        safe_sell = em.round_to(min(free_base, got - fees_base), f['step'])
        rec['sell_qty'] = str(safe_sell)
        em.save_trade(rec)
        if safe_sell <= 0:
            raise RuntimeError('No sellable base asset after buy fees')
        em.submit_protection(rec, safe_sell, plan['t1'], plan['t2'], plan['stop'], f)
        em.alert(f'BOUGHT {symbol} {got} @ {avg}; 2 OCOs confirmed on Binance. '
                 'Stops remain at ORIGINAL levels after TP1; no automatic unsafe cancel/replace.')
        em.monitor_trade(rec)
        return rec
    except Exception as e:
        if rec.get('state') in ('intent', 'buy_submitting', 'bought', 'protection_submitting'):
            rec['state'] = 'needs_attention'
            em.save_trade(rec)
            # Sell ONLY unlocked balance; leave any surviving protective OCO untouched.
            # Never sell if buy outcome itself is uncertain.
            if rec.get('bought_qty'):
                try:
                    em.emergency_sell_free(symbol, rec.get('sell_qty', rec['bought_qty']), f)
                except Exception as e2:
                    em.alert(f'EMERGENCY sell could not be confirmed: {e2}.')
        em.alert(f'TRADE NEEDS MANUAL RECONCILIATION {symbol}: {e}. New buys blocked.')
        raise
