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
SYMBOLS = ["MNQ", "MES", "MYM", "MGC"]

CONTRACT_MAP = {
    'MNQ': ('CON.F.US.MNQ.M26', 'CON.F.US.MNQ.H26', 0.25, 0.50),
    'MES': ('CON.F.US.MES.M26', 'CON.F.US.MES.H26', 0.25, 1.25),
    'MYM': ('CON.F.US.MYM.M26', 'CON.F.US.MYM.H26', 1.0, 0.50),
    'MGC': ('CON.F.US.MGC.M26', 'CON.F.US.MGC.J26', 0.10, 1.00),
}

MAX_CONTRACTS_PER_INSTRUMENT = 4  # One per level (CDM, PDM, CMM, PMM)

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
        self.pending_entries = {}  # {(symbol, level_name): {order_id, entry_price, ...}}
        self.active_positions = {} # {(symbol, level_name): {entry, stop, target, ...}}
        self.session_losses = defaultdict(int)  # {symbol: consecutive loss count}
        self.session_day = None   # Track which session day we're in
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


def get_all_eligible_levels(symbol, mode, price, tick_size):
    """Get ALL mean levels eligible for entry in the given mode."""
    levels = {
        'CDM': state.cdm.get(symbol),
        'PDM': state.pdm.get(symbol),
        'CMM': state.cmm.get(symbol),
        'PMM': state.pmm.get(symbol),
    }
    result = []
    for name, level in levels.items():
        if level is None:
            continue
        entry = round(round(level / tick_size) * tick_size, 6)
        if mode == 'BUY' and entry < price:
            result.append((name, entry))
        elif mode == 'SELL' and entry > price:
            result.append((name, entry))
    return result


# ─────────────────────────────────────────────────────────────
# ORDER MANAGEMENT
# ─────────────────────────────────────────────────────────────
async def place_or_update_entry(client, account, symbol, level_name, contract_id, side, entry_price, tick_size):
    """Place or update entry limit for a specific symbol+level."""
    import aiohttp
    token = client.get_session_token()
    base_url = client.base_url
    hdrs = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    key = (symbol, level_name)

    existing = state.pending_entries.get(key)
    if existing:
        if abs(entry_price - existing['entry_price']) < tick_size * 2:
            return  # Level hasn't moved enough
        # Cancel old
        if not state.dry_run:
            async with aiohttp.ClientSession() as http:
                await http.post(f'{base_url}/Order/cancel',
                    json={'orderId': existing['order_id'], 'accountId': account.id},
                    headers=hdrs)
        print(f"  [{symbol}] {level_name} moved: {existing['entry_price']} -> {entry_price}")

    side_str = 'BUY' if side == 0 else 'SELL'
    atr = state.atr.get(symbol)
    if not atr:
        return
    stop_dist = atr * ATR_MULTIPLIER

    if side == 0:  # BUY
        stop = round(round((entry_price - stop_dist) / tick_size) * tick_size, 6)
        target = round(round((entry_price + stop_dist * RR_RATIO) / tick_size) * tick_size, 6)
    else:  # SELL
        stop = round(round((entry_price + stop_dist) / tick_size) * tick_size, 6)
        target = round(round((entry_price - stop_dist * RR_RATIO) / tick_size) * tick_size, 6)

    if state.dry_run:
        print(f"  [{symbol}] DRY: {side_str} {level_name} @ {entry_price} | stop {stop} | target {target}")
        state.pending_entries[key] = {'order_id': 'DRY', 'entry_price': entry_price,
            'side': side, 'contract_id': contract_id, 'stop': stop, 'target': target,
            'level': level_name}
        return

    stop_side = 1 if side == 0 else 0
    async with aiohttp.ClientSession() as http:
        # Place entry
        r = await (await http.post(f'{base_url}/Order/place', json={
            'accountId': account.id, 'contractId': contract_id,
            'type': 1, 'side': side, 'size': 1, 'limitPrice': entry_price,
        }, headers=hdrs)).json()
        if not r.get('success'):
            print(f"  [{symbol}] {level_name} entry failed: {r}")
            return
        entry_id = r['orderId']

        # Place stop
        r_stop = await (await http.post(f'{base_url}/Order/place', json={
            'accountId': account.id, 'contractId': contract_id,
            'type': 4, 'side': stop_side, 'size': 1, 'stopPrice': stop,
        }, headers=hdrs)).json()
        stop_id = r_stop.get('orderId')

        # Place target
        r_tp = await (await http.post(f'{base_url}/Order/place', json={
            'accountId': account.id, 'contractId': contract_id,
            'type': 1, 'side': stop_side, 'size': 1, 'limitPrice': target,
        }, headers=hdrs)).json()
        target_id = r_tp.get('orderId')

    state.pending_entries[key] = {
        'order_id': entry_id, 'stop_id': stop_id, 'target_id': target_id,
        'entry_price': entry_price, 'stop': stop, 'target': target,
        'side': side, 'contract_id': contract_id, 'level': level_name,
    }
    print(f"  [{symbol}] {side_str} {level_name} @ {entry_price} | stop {stop} | target {target}")


# Brackets are now placed WITH the entry — no separate fill check needed


# ─────────────────────────────────────────────────────────────
# BAR HANDLER
# ─────────────────────────────────────────────────────────────
async def on_new_bar(symbol, bar_data, client, account):
    """Process a new 5-min bar. Place/update orders at ALL eligible levels."""
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

        # Weekend filter only (market closed)
        et_now = datetime.now(ET)
        if (et_now.weekday() == 4 and et_now.hour >= 18) or et_now.weekday() == 5:
            return
        if et_now.weekday() == 6 and et_now.hour < 18:
            return

        mode = state.modes.get(symbol)
        if not mode:
            return  # This symbol isn't active

        # Reset loss counter on new session day
        today = get_futures_day(datetime.now(CT))
        if state.session_day != today:
            state.session_losses.clear()
            state.session_day = today

        # 3 consecutive losses = stop this symbol for the session
        if state.session_losses[symbol] >= 3:
            return

        # Get contract ID
        try:
            instrument = await client.get_instrument(symbol)
            contract_id = instrument.id
        except:
            return

        side = 0 if mode == 'BUY' else 1

        # ── POSITION + ORDER GUARD (API-level, prevents duplicates across restarts) ──
        import aiohttp
        token = client.get_session_token()
        base_url = client.base_url
        hdrs = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

        # Count open positions for this instrument
        open_pos_count = 0
        try:
            async with aiohttp.ClientSession() as http:
                resp = await http.get(f'{base_url}/Position/searchOpen', params={'accountId': account.id}, headers=hdrs)
                pos_data = await resp.json()
                if isinstance(pos_data, list):
                    for p in pos_data:
                        if p.get('contractId', '') == contract_id:
                            open_pos_count += abs(p.get('size', 0))
        except:
            pass

        # Count open limit orders for this instrument (entry orders only, type=1=Limit)
        open_order_count = 0
        existing_order_prices = set()
        try:
            async with aiohttp.ClientSession() as http:
                resp = await http.get(f'{base_url}/Order/searchOpen', params={'accountId': account.id}, headers=hdrs)
                ord_data = await resp.json()
                if isinstance(ord_data, list):
                    for o in ord_data:
                        if o.get('contractId', '') == contract_id and o.get('type') == 1 and o.get('side') == side:
                            open_order_count += 1
                            if o.get('limitPrice'):
                                existing_order_prices.add(round(o['limitPrice'], 2))
        except:
            pass

        total_exposure = open_pos_count + open_order_count
        if total_exposure >= MAX_CONTRACTS_PER_INSTRUMENT:
            return  # Already at max — no new orders

        # Place/update at ALL eligible levels (1 order per level)
        eligible = get_all_eligible_levels(symbol, mode, close, tick_size)
        for level_name, entry_price in eligible:
            # Check total exposure again (could have added in this loop)
            if total_exposure >= MAX_CONTRACTS_PER_INSTRUMENT:
                break
            key = (symbol, level_name)
            if key in state.active_positions:
                continue  # Already filled at this level
            # Skip if we already have a limit order at this price
            if round(entry_price, 2) in existing_order_prices:
                continue
            # Place new or update existing
            await place_or_update_entry(client, account, symbol, level_name,
                                        contract_id, side, entry_price, tick_size)
            total_exposure += 1

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

    active = list(state.modes.keys()) if hasattr(state, 'modes') and state.modes else SYMBOLS
    for sym in active:
        if sym not in CONTRACT_MAP:
            print(f"  {sym}: unknown contract, skipping")
            continue
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
async def main(modes: dict, dry_run: bool = False):
    from project_x_py import ProjectX, TradingSuite, EventType

    state.modes = modes  # {symbol: 'BUY'/'SELL'}
    state.dry_run = dry_run
    active_syms = list(modes.keys())
    mode_lines = '  '.join(f"{s}:{m}" for s, m in modes.items())
    live_str = 'DRY RUN' if dry_run else 'LIVE'

    print(f"""
╔═══════════════════════════════════════════════════════╗
║  Tzu Strategic Momentum  ({live_str})                  
║  {mode_lines:<52}║
║  Stops: {ATR_MULTIPLIER}x ATR | R:R 1:{RR_RATIO} | 1 order per level         ║
╚═══════════════════════════════════════════════════════╝
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

            # Create one TradingSuite per active instrument
            suites = []
            for sym in active_syms:
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

            print(f"  STREAMING — {mode_lines}")
            print(f"  Ctrl+C to stop\n")

            # Keep alive + monitor positions + hourly status
            while True:
                await asyncio.sleep(60)

                # Check if any active positions were closed (stop/target hit)
                try:
                    import aiohttp
                    token = client.get_session_token()
                    base_url = client.base_url
                    api_h = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
                    async with aiohttp.ClientSession() as http:
                        async with http.post(f'{base_url}/Position/searchOpen',
                                             json={'accountId': account.id}, headers=api_h) as resp:
                            open_positions = await resp.json()
                            # Count open contracts per symbol
                            open_count = defaultdict(int)
                            for p in open_positions.get('positions', []):
                                cid = p.get('contractId', '')
                                parts = cid.split('.')
                                if len(parts) >= 4:
                                    open_count[parts[3]] += p.get('size', 1)

                    # Check each active position key
                    for key in list(state.active_positions.keys()):
                        sym, level = key
                        # If symbol has fewer open positions than tracked, something closed
                        tracked = sum(1 for k in state.active_positions if k[0] == sym)
                        if open_count.get(sym, 0) < tracked:
                            pos = state.active_positions[key]
                            side = pos.get('side', 1)
                            entry = pos.get('entry', 0)
                            current = state.current_price.get(sym, 0)
                            pnl = (current - entry) if side == 0 else (entry - current)

                            if pnl <= 0:
                                state.session_losses[sym] += 1
                                result = "LOSS"
                            else:
                                state.session_losses[sym] = 0
                                result = "WIN"

                            losses = state.session_losses[sym]
                            stopped = " — STOPPED for session" if losses >= 3 else ""
                            print(f"  [{sym}] {level} CLOSED ({result}) | "
                                  f"Consecutive losses: {losses}{stopped}")
                            del state.active_positions[key]
                            break  # Re-check next cycle
                except:
                    pass
                try:
                    et_now = datetime.now(ET)
                    if et_now.minute == 0:
                        print(f"\n  [{et_now.strftime('%H:%M')}] Status")
                        for sym in active_syms:
                            price = state.current_price.get(sym)
                            price_s = f"{price:.2f}" if price else "?"
                            cdm_s = f"{state.cdm.get(sym):.2f}" if state.cdm.get(sym) else "-"
                            pdm_s = f"{state.pdm.get(sym):.2f}" if state.pdm.get(sym) else "-"
                            cmm_s = f"{state.cmm.get(sym):.2f}" if state.cmm.get(sym) else "-"
                            pmm_s = f"{state.pmm.get(sym):.2f}" if state.pmm.get(sym) else "-"
                            pending = [k[1] for k in state.pending_entries if k[0] == sym]
                            active = [k[1] for k in state.active_positions if k[0] == sym]
                            losses = state.session_losses.get(sym, 0)
                            loss_s = f" ({losses}L)" if losses else ""
                            sym_mode = state.modes.get(sym, '?')
                            print(f"    {sym} [{sym_mode}]: {price_s} | CDM:{cdm_s} PDM:{pdm_s} CMM:{cmm_s} PMM:{pmm_s}")
                            if pending: print(f"      Pending: {', '.join(pending)}")
                            if active: print(f"      Active: {', '.join(active)}")
                            if losses >= 3: print(f"      STOPPED for session")
                except:
                    pass

    except KeyboardInterrupt:
        print(f"\n  Stopped.")
    except Exception as e:
        print(f"\n  Fatal: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Tzu Strategic Momentum",
        epilog="""Examples:
  All instruments SELL:   python realtime_engine.py --mode sell
  All instruments BUY:    python realtime_engine.py --mode buy
  Per-instrument:         python realtime_engine.py --mnq sell --mes buy --mym sell --mgc buy
  Mix (some off):         python realtime_engine.py --mnq sell --mes sell
  Dry run:                python realtime_engine.py --mode sell --dry-run""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mode", choices=["buy", "sell"], help="Set ALL instruments to BUY or SELL")
    parser.add_argument("--mnq", choices=["buy", "sell"], help="MNQ mode")
    parser.add_argument("--mes", choices=["buy", "sell"], help="MES mode")
    parser.add_argument("--mym", choices=["buy", "sell"], help="MYM mode")
    parser.add_argument("--mgc", choices=["buy", "sell"], help="MGC mode")
    parser.add_argument("--dry-run", action="store_true", help="Show without executing")
    args = parser.parse_args()

    # Build per-instrument mode map
    modes = {}
    if args.mode:
        for sym in SYMBOLS:
            modes[sym] = args.mode.upper()
    # Per-instrument overrides
    if args.mnq: modes['MNQ'] = args.mnq.upper()
    if args.mes: modes['MES'] = args.mes.upper()
    if args.mym: modes['MYM'] = args.mym.upper()
    if args.mgc: modes['MGC'] = args.mgc.upper()

    if not modes:
        parser.error("Specify --mode for all, or per-instrument flags (--mnq, --mes, --mym, --mgc)")

    asyncio.run(main(modes=modes, dry_run=args.dry_run))
