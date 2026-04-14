"""
Tzu Strategic Momentum — Real-Time Mean Level Execution Engine
===============================================================
Streams live 5-min bars, computes running CDM dynamically, and places
limit orders at the closest mean level in the direction you choose.

Usage:
    python realtime_engine.py --mode sell          # SELL mode, live
    python realtime_engine.py --mode buy           # BUY mode, live
    python realtime_engine.py --mode sell --dry-run # SELL mode, dry run

The mode (BUY or SELL) is set by you. The engine handles execution:
  - Streams 5-min bars via WebSocket
  - Recomputes CDM after every bar close
  - Places/adjusts limit entry at the closest mean level
  - Places bracket (stop + target) immediately on fill
  - 1 lot per symbol, max 3 positions (MNQ, MES, MYM)
"""

import asyncio
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# Suppress verbose SDK logging
logging.getLogger('project_x_py').setLevel(logging.WARNING)

import pytz
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
SYMBOLS = ["MNQ", "MES", "MYM"]

CONTRACT_MAP = {
    'MNQ': ('CON.F.US.MNQ.M26', 'CON.F.US.MNQ.H26', 0.25, 0.50),
    'MES': ('CON.F.US.MES.M26', 'CON.F.US.MES.H26', 0.25, 1.25),
    'MYM': ('CON.F.US.MYM.M26', 'CON.F.US.MYM.H26', 1.0, 0.50),
}

ATR_MULTIPLIER = 1.0
RR_RATIO = 2.0

ET = pytz.timezone("America/New_York")
CT = pytz.timezone("America/Chicago")


# ─────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.mode = None          # 'BUY' or 'SELL' — set by user
        self.dry_run = False
        self.current_price = {}   # {symbol: price}
        self.cdm = {}             # {symbol: CDM}
        self.pdm = {}             # {symbol: PDM}
        self.cmm = {}             # {symbol: CMM}
        self.pmm = {}             # {symbol: PMM}
        self.atr = {}             # {symbol: ATR}
        self.day_closes = defaultdict(list)    # {(symbol, date): [closes]}
        self.month_closes = defaultdict(list)  # {(symbol, year, month): [closes]}
        self.pending_entry = {}   # {symbol: {order_id, entry_price, ...}}
        self.active_position = {} # {symbol: {entry, stop, target, ...}}
        self.current_day = None
        self.current_month = None


state = State()


# ─────────────────────────────────────────────────────────────
# FUTURES DAY BOUNDARY (5 PM CT = 6 PM ET)
# ─────────────────────────────────────────────────────────────
def get_futures_day(ct_time):
    if ct_time.hour >= 17:
        return (ct_time + timedelta(days=1)).date()
    return ct_time.date()


def get_futures_month(ct_time):
    d = get_futures_day(ct_time)
    return (d.year, d.month)


# ─────────────────────────────────────────────────────────────
# RUNNING MEAN LEVELS
# ─────────────────────────────────────────────────────────────
def update_running_means(symbol, close, timestamp):
    """Update CDM/CMM with a new 5-min bar close."""
    ct_time = timestamp.astimezone(CT)
    today = get_futures_day(ct_time)
    this_month = get_futures_month(ct_time)

    # Day roll — save yesterday's CDM as PDM
    if state.current_day and state.current_day != today:
        for sym in SYMBOLS:
            prev_key = (sym, state.current_day)
            if prev_key in state.day_closes and state.day_closes[prev_key]:
                closes = state.day_closes[prev_key]
                state.pdm[sym] = sum(closes) / len(closes)

    # Month roll — save last month's CMM as PMM
    if state.current_month and state.current_month != this_month:
        for sym in SYMBOLS:
            prev_key = (sym, *state.current_month)
            if prev_key in state.month_closes and state.month_closes[prev_key]:
                closes = state.month_closes[prev_key]
                state.pmm[sym] = sum(closes) / len(closes)

    state.current_day = today
    state.current_month = this_month

    # Accumulate closes
    day_key = (symbol, today)
    state.day_closes[day_key].append(close)
    month_key = (symbol, *this_month)
    state.month_closes[month_key].append(close)

    # Compute running means
    state.cdm[symbol] = sum(state.day_closes[day_key]) / len(state.day_closes[day_key])
    state.cmm[symbol] = sum(state.month_closes[month_key]) / len(state.month_closes[month_key])


def get_closest_entry(symbol, mode, price, tick_size):
    """Get closest mean level for entry in the given mode."""
    levels = {
        'CDM': state.cdm.get(symbol),
        'PDM': state.pdm.get(symbol),
        'CMM': state.cmm.get(symbol),
        'PMM': state.pmm.get(symbol),
    }
    candidates = []
    for name, level in levels.items():
        if level is None:
            continue
        if mode == 'BUY' and level < price:
            candidates.append((abs(price - level), name, level))
        elif mode == 'SELL' and level > price:
            candidates.append((abs(level - price), name, level))

    if not candidates:
        return None, None
    candidates.sort()
    _, name, level = candidates[0]
    entry = round(round(level / tick_size) * tick_size, 6)
    return entry, name


# ─────────────────────────────────────────────────────────────
# ORDER MANAGEMENT
# ─────────────────────────────────────────────────────────────
async def place_or_update_entry(client, account, symbol, contract_id, side, entry_price, tick_size):
    """Place new entry or move existing to track CDM."""
    import aiohttp
    token = client.get_session_token()
    base_url = client.base_url
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    existing = state.pending_entry.get(symbol)
    if existing:
        if abs(entry_price - existing['entry_price']) < tick_size * 2:
            return  # CDM hasn't moved enough
        # Cancel old
        if not state.dry_run:
            async with aiohttp.ClientSession() as http:
                await http.post(f'{base_url}/Order/cancel',
                    json={'orderId': existing['order_id'], 'accountId': account.id},
                    headers=headers)
        print(f"  [{symbol}] Entry moved: {existing['entry_price']} -> {entry_price}")

    side_str = 'BUY' if side == 0 else 'SELL'
    if state.dry_run:
        print(f"  [{symbol}] DRY RUN: {side_str} LIMIT @ {entry_price}")
        state.pending_entry[symbol] = {'order_id': 'DRY', 'entry_price': entry_price,
                                        'side': side, 'contract_id': contract_id}
        return

    async with aiohttp.ClientSession() as http:
        async with http.post(f'{base_url}/Order/place', json={
            'accountId': account.id, 'contractId': contract_id,
            'type': 1, 'side': side, 'size': 1, 'limitPrice': entry_price,
        }, headers=headers) as resp:
            res = await resp.json()
            if res.get('success'):
                state.pending_entry[symbol] = {
                    'order_id': res['orderId'], 'entry_price': entry_price,
                    'side': side, 'contract_id': contract_id,
                }
                print(f"  [{symbol}] {side_str} LIMIT @ {entry_price}")
            else:
                print(f"  [{symbol}] Order failed: {res}")


async def check_fill_and_bracket(client, account, symbol):
    """Check if pending entry filled, place bracket if so."""
    import aiohttp
    pending = state.pending_entry.get(symbol)
    if not pending or pending.get('order_id') == 'DRY':
        return

    token = client.get_session_token()
    base_url = client.base_url
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    async with aiohttp.ClientSession() as http:
        async with http.post(f'{base_url}/Position/searchOpen',
                             json={'accountId': account.id}, headers=headers) as resp:
            data = await resp.json()

    for p in data.get('positions', []):
        cid = p.get('contractId', '')
        parts = cid.split('.')
        sym = parts[3] if len(parts) >= 4 else ''
        if sym == symbol and symbol not in state.active_position:
            entry_price = pending['entry_price']
            side = pending['side']
            stop_side = 1 if side == 0 else 0
            atr = state.atr.get(symbol)
            if not atr:
                print(f"  [{symbol}] WARNING: No ATR — cannot bracket")
                return

            tick_size = CONTRACT_MAP[symbol][2]
            stop_dist = atr * ATR_MULTIPLIER

            if side == 0:  # BUY
                stop = round(round((entry_price - stop_dist) / tick_size) * tick_size, 6)
                target = round(round((entry_price + stop_dist * RR_RATIO) / tick_size) * tick_size, 6)
            else:  # SELL
                stop = round(round((entry_price + stop_dist) / tick_size) * tick_size, 6)
                target = round(round((entry_price - stop_dist * RR_RATIO) / tick_size) * tick_size, 6)

            print(f"\n  *** [{symbol}] FILLED @ {entry_price} ***")
            print(f"  Stop: {stop} | Target: {target}")

            if not state.dry_run:
                async with aiohttp.ClientSession() as http:
                    r1 = await (await http.post(f'{base_url}/Order/place', json={
                        'accountId': account.id, 'contractId': pending['contract_id'],
                        'type': 4, 'side': stop_side, 'size': 1, 'stopPrice': stop,
                    }, headers=headers)).json()
                    r2 = await (await http.post(f'{base_url}/Order/place', json={
                        'accountId': account.id, 'contractId': pending['contract_id'],
                        'type': 1, 'side': stop_side, 'size': 1, 'limitPrice': target,
                    }, headers=headers)).json()
                    print(f"  STOP: {r1.get('orderId')} | TARGET: {r2.get('orderId')}")

            state.active_position[symbol] = {
                'entry': entry_price, 'stop': stop, 'target': target,
                'side': side, 'contract_id': pending['contract_id'],
            }
            del state.pending_entry[symbol]
            return


# ─────────────────────────────────────────────────────────────
# BAR HANDLER
# ─────────────────────────────────────────────────────────────
async def on_new_bar(symbol, bar_data, client, account):
    """Process a new 5-min bar. Core logic."""
    try:
        close = bar_data['close']
        tick_size = CONTRACT_MAP[symbol][2]

        state.current_price[symbol] = close
        update_running_means(symbol, close, bar_data['timestamp'])

        cdm = state.cdm.get(symbol)
        cdm_str = f"{cdm:.2f}" if cdm else "?"
        print(f"  [{symbol}] {close:.2f} | CDM: {cdm_str}")

        if not cdm:
            return

        # Time filter: no orders 6-10 PM ET (CDM building)
        et_now = datetime.now(ET)
        if 18 <= et_now.hour < 22:
            return
        # Friday close / Saturday
        if (et_now.weekday() == 4 and et_now.hour >= 18) or et_now.weekday() == 5:
            return
        # Sunday before 10 PM
        if et_now.weekday() == 6 and et_now.hour < 22:
            return

        mode = state.mode

        # DUPLICATE PREVENTION
        if symbol in state.active_position:
            return
        if symbol in state.pending_entry:
            await check_fill_and_bracket(client, account, symbol)
            return

        # API position check (catches state desync)
        import aiohttp
        try:
            token = client.get_session_token()
            base_url = client.base_url
            api_h = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
            async with aiohttp.ClientSession() as http:
                async with http.post(f'{base_url}/Position/searchOpen',
                                     json={'accountId': account.id}, headers=api_h) as resp:
                    for p in (await resp.json()).get('positions', []):
                        cid = p.get('contractId', '')
                        parts = cid.split('.')
                        if len(parts) >= 4 and parts[3] == symbol:
                            state.active_position[symbol] = {'entry': p['averagePrice']}
                            return
                async with http.post(f'{base_url}/Order/searchOpen',
                                     json={'accountId': account.id}, headers=api_h) as resp:
                    for o in (await resp.json()).get('orders', []):
                        if symbol in o.get('symbolId', '') and o.get('type') == 1:
                            return
        except:
            pass

        # Get entry level
        entry_price, level_name = get_closest_entry(symbol, mode, close, tick_size)
        if not entry_price:
            return

        # Get contract ID
        try:
            instrument = await client.get_instrument(symbol)
            contract_id = instrument.id
        except:
            return

        side = 0 if mode == 'BUY' else 1
        await place_or_update_entry(client, account, symbol, contract_id, side, entry_price, tick_size)

    except Exception as e:
        print(f"  [{symbol}] Error (non-fatal): {e}")


# ─────────────────────────────────────────────────────────────
# STARTUP — SEED HISTORICAL DATA
# ─────────────────────────────────────────────────────────────
def seed_historical(client):
    """Fetch historical bars to seed CDM/PDM/CMM/PMM/ATR."""
    import requests as sync_requests
    token = client.get_session_token()
    base_url = client.base_url
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    now_utc = datetime.now(timezone.utc)
    now_ct = datetime.now(CT)
    today = get_futures_day(now_ct)
    this_month = get_futures_month(now_ct)

    # Session start = 5 PM CT yesterday (or today if after 5 PM)
    if now_ct.hour >= 17:
        session_start = now_ct.replace(hour=17, minute=0, second=0, microsecond=0)
    else:
        session_start = (now_ct - timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
    session_start_utc = session_start.astimezone(timezone.utc)

    # Previous session
    prev_start = session_start - timedelta(days=1)
    prev_start_utc = prev_start.astimezone(timezone.utc)

    for sym in SYMBOLS:
        curr, prior, tick, tick_val = CONTRACT_MAP[sym]

        # Today's 5-min bars (from session start)
        bars_today = sync_requests.post(f'{base_url}/History/retrieveBars', json={
            "contractId": curr, "live": False,
            "startTime": session_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unit": 1, "unitNumber": 5, "limit": 5000, "includePartialBar": True,
        }, headers=headers).json().get('bars', [])

        # Yesterday's 5-min bars
        bars_yesterday = sync_requests.post(f'{base_url}/History/retrieveBars', json={
            "contractId": curr, "live": False,
            "startTime": prev_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": session_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unit": 1, "unitNumber": 5, "limit": 5000, "includePartialBar": True,
        }, headers=headers).json().get('bars', [])

        # CDM
        if bars_today:
            today_closes = [b['c'] for b in bars_today]
            state.day_closes[(sym, today)] = today_closes
            state.cdm[sym] = sum(today_closes) / len(today_closes)
            print(f"  {sym} CDM: {state.cdm[sym]:.2f} ({len(today_closes)} bars)")

        # PDM
        if bars_yesterday:
            yd_closes = [b['c'] for b in bars_yesterday]
            state.pdm[sym] = sum(yd_closes) / len(yd_closes)
            print(f"  {sym} PDM: {state.pdm[sym]:.2f} ({len(yd_closes)} bars)")

        # CMM/PMM from hourly (current contract covers recent months)
        hourly = sync_requests.post(f'{base_url}/History/retrieveBars', json={
            "contractId": curr, "live": False,
            "startTime": (now_utc - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unit": 3, "unitNumber": 1, "limit": 5000, "includePartialBar": True,
        }, headers=headers).json().get('bars', [])

        month_data = defaultdict(list)
        for b in hourly:
            ts = datetime.fromisoformat(b['t']).astimezone(CT)
            fm = get_futures_month(ts)
            month_data[fm].append(b['c'])

        if this_month in month_data:
            state.cmm[sym] = sum(month_data[this_month]) / len(month_data[this_month])
            state.month_closes[(sym, *this_month)] = month_data[this_month]
            print(f"  {sym} CMM: {state.cmm[sym]:.2f}")

        prev_m = (this_month[0], this_month[1]-1) if this_month[1]>1 else (this_month[0]-1, 12)
        if prev_m in month_data:
            state.pmm[sym] = sum(month_data[prev_m]) / len(month_data[prev_m])
            print(f"  {sym} PMM: {state.pmm[sym]:.2f}")

        # ATR from daily bars
        daily = sync_requests.post(f'{base_url}/History/retrieveBars', json={
            "contractId": curr, "live": False,
            "startTime": (now_utc - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unit": 4, "unitNumber": 1, "limit": 500, "includePartialBar": True,
        }, headers=headers).json().get('bars', [])
        daily.sort(key=lambda x: x['t'])

        if len(daily) >= 15:
            trs = [daily[i]['h'] - daily[i]['l'] for i in range(-14, 0)]
            state.atr[sym] = sum(trs) / len(trs)
            print(f"  {sym} ATR: {state.atr[sym]:.2f}")

    state.current_day = today
    state.current_month = this_month


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
async def main(mode: str, dry_run: bool = False):
    from project_x_py import ProjectX, TradingSuite, EventType

    state.mode = mode.upper()
    state.dry_run = dry_run
    mode_display = f"{state.mode} {'(DRY RUN)' if dry_run else '(LIVE)'}"

    print(f"""
╔═══════════════════════════════════════════════════╗
║  Tzu Strategic Momentum                           ║
║  Mode: {mode_display:<42}║
║  Instruments: MNQ, MES, MYM                      ║
║  Stops: {ATR_MULTIPLIER}x ATR | R:R 1:{RR_RATIO}                        ║
╚═══════════════════════════════════════════════════╝
""")

    try:
        async with ProjectX.from_env() as client:
            await client.authenticate()
            account = client.get_account_info()
            print(f"  Account: {account.name}")
            print(f"  Balance: ${account.balance:,.2f}")

            # Seed historical data
            print(f"\n  Loading historical data...")
            seed_historical(client)
            print(f"  Ready.\n")

            # Create one TradingSuite per instrument
            suites = []
            for sym in SYMBOLS:
                suite = await TradingSuite.create(instruments=[sym], timeframes=["5min"])

                def make_callback(symbol):
                    async def cb(event):
                        data = event.data
                        if data.get('timeframe') != '5min':
                            return
                        bar = data.get('data', {})
                        close = bar.get('close') if isinstance(bar, dict) else getattr(bar, 'close', None)
                        if close:
                            await on_new_bar(symbol, {
                                'close': close,
                                'high': bar.get('high', close) if isinstance(bar, dict) else getattr(bar, 'high', close),
                                'low': bar.get('low', close) if isinstance(bar, dict) else getattr(bar, 'low', close),
                                'open': bar.get('open', close) if isinstance(bar, dict) else getattr(bar, 'open', close),
                                'timestamp': datetime.now(CT),
                            }, client, account)
                    return cb

                await suite.on(EventType.NEW_BAR, make_callback(sym))
                suites.append(suite)

            print(f"  STREAMING — {state.mode} mode on MNQ, MES, MYM")
            print(f"  Ctrl+C to stop\n")

            # Keep alive + hourly status
            while True:
                await asyncio.sleep(60)
                try:
                    et_now = datetime.now(ET)
                    if et_now.minute == 0:
                        print(f"\n  [{et_now.strftime('%H:%M')}] {state.mode} mode")
                        for sym in SYMBOLS:
                            cdm = state.cdm.get(sym)
                            price = state.current_price.get(sym)
                            cdm_s = f"{cdm:.2f}" if cdm else "?"
                            price_s = f"{price:.2f}" if price else "?"
                            status = "POSITION" if sym in state.active_position else \
                                     "PENDING" if sym in state.pending_entry else ""
                            print(f"    {sym}: price={price_s} cdm={cdm_s} {status}")
                except:
                    pass

    except KeyboardInterrupt:
        print(f"\n  Stopped.")
    except Exception as e:
        print(f"\n  Fatal: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tzu Strategic Momentum")
    parser.add_argument("--mode", required=True, choices=["buy", "sell"], help="BUY or SELL mode")
    parser.add_argument("--dry-run", action="store_true", help="Show without executing")
    args = parser.parse_args()
    asyncio.run(main(mode=args.mode, dry_run=args.dry_run))
