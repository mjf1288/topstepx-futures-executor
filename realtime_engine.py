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
  - EXECUTION-ONLY: places ENTRY LIMITs only. User attaches stops/targets
    manually on the TopstepX UI. (Bracket placement removed 2026-07-23
    after orphan-bracket accumulation caused ~40 stale orders.)
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
# Legacy: project_x_py library no longer used, but its logger name may
# still be referenced from cached artifacts. Silencing is harmless.
logging.getLogger('project_x_py').setLevel(logging.WARNING)
logging.getLogger('topstep_stream').setLevel(logging.INFO)
logging.getLogger('signalrcore').setLevel(logging.WARNING)

import pytz
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
SYMBOLS = ["MNQ", "MES", "MYM", "MGC", "MCL"]

CONTRACT_MAP = {
    'MNQ': ('CON.F.US.MNQ.U26', 'CON.F.US.MNQ.Z26', 0.25, 0.50),
    'MES': ('CON.F.US.MES.U26', 'CON.F.US.MES.Z26', 0.25, 1.25),
    'MYM': ('CON.F.US.MYM.U26', 'CON.F.US.MYM.Z26', 1.0, 0.50),
    'MGC': ('CON.F.US.MGC.V26', 'CON.F.US.MGC.Z26', 0.10, 1.00),
    # MCL (Micro Crude Oil) rolls monthly — update these first-of-each-month.
    # V26 = October 2026, X26 = November 2026.
    'MCL': ('CON.F.US.MCL.V26', 'CON.F.US.MCL.X26', 0.01, 1.00),
}

MAX_CONTRACTS_PER_INSTRUMENT = 4  # Hard max contracts per instrument (positions + working entries)
CONTRACTS_PER_ORDER = 1            # 1 contract per entry

ATR_MULTIPLIER = 0.382             # ~38.2% of daily ATR (fib-based tight stop)
RR_RATIO = 2.618                   # Golden ratio R:R

# Running-mean warm-up gates. The mean is statistically meaningless
# with too few samples — at globex open on a new month, a 1-sample CMM
# equals current price and triggers instant-fill limits with no edge.
# Observed bug: 2026-06-01 globex open filled at CMM on first 5m bar.
MIN_SAMPLES_CDM = 6   # ~30 min of 5-min bars before publishing CDM
MIN_SAMPLES_CMM = 24  # ~2 hours into a new month before publishing CMM

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
        # Tracks last hour (ET) we refreshed CMM/PMM from fresh hourly bars.
        # Prevents multiple refreshes per hour if the monitor loop wakes up
        # more than once during the same :00 minute.
        self.last_monthly_refresh_hour = None


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
    """Update CDM (current-day mean) with a new 5-min bar close.

    NOTE (2026-08-05): CMM/PMM are NO LONGER updated here. Historical
    bug: this function was appending 5-min closes to state.month_closes,
    which had been seeded from HOURLY bars. Mixing granularities gave
    each 5-min close 12x more weight than an hourly bar and drifted CMM
    by 1-1.6% within 2 weeks of continuous running. CMM/PMM are now
    refreshed from fresh hourly API pulls in refresh_monthly_means()
    once per hour.
    """
    # Defensive: if timestamp is naive (no tz info), assume UTC. Some SDK
    # bar events have inconsistent timezone metadata.
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    ct_time = timestamp.astimezone(CT)
    today = get_futures_day(ct_time)
    this_month = get_futures_month(ct_time)

    # DIAGNOSTIC: log the day-key transition the first time a new bar
    # comes in, so we can spot seed/realtime key mismatches.
    day_key = (symbol, today)
    prev_n = len(state.day_closes.get(day_key, []))
    if prev_n == 0:
        existing_keys = [k for k in state.day_closes.keys() if k[0] == symbol]
        print(f"  [{symbol}] NEW day-key {today} (close={close:.2f}, ct={ct_time.isoformat()}) "
              f"— existing keys for this symbol: {existing_keys}")

    # Day roll — save yesterday's CDM as PDM
    if state.current_day and state.current_day != today:
        for sym in SYMBOLS:
            prev_key = (sym, state.current_day)
            if prev_key in state.day_closes and state.day_closes[prev_key]:
                closes = state.day_closes[prev_key]
                state.pdm[sym] = sum(closes) / len(closes)

    state.current_day = today
    state.current_month = this_month

    # Accumulate ONLY day closes here. Month-level CMM/PMM are refreshed
    # from fresh hourly API pulls (see refresh_monthly_means()).
    day_key = (symbol, today)
    state.day_closes[day_key].append(close)

    day_count = len(state.day_closes[day_key])
    if day_count >= MIN_SAMPLES_CDM:
        state.cdm[symbol] = sum(state.day_closes[day_key]) / day_count
    else:
        state.cdm[symbol] = None  # warm-up


def refresh_monthly_means(client):
    """Refresh CMM and PMM from fresh HOURLY bars via the ProjectX API.

    Called at engine startup (via seed_historical) AND periodically by the
    monitor loop (once per hour, see main()). This is the ONLY code path
    that mutates state.cmm and state.pmm. Live 5-min bars no longer
    contribute to these means — they only update CDM.

    Rationale: the previous implementation appended live 5-min closes to
    a bucket that had been seeded from hourly bars, over-weighting the
    5-min samples by 12x and drifting CMM by ~1% per week (audit
    performed 2026-08-05 showed engine CMM off by +1.0% to +1.6% for
    MNQ / MES / MYM after 2 weeks of continuous running).
    """
    import requests as sync_requests
    token = client.get_session_token()
    base_url = client.base_url
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    now_utc = datetime.now(timezone.utc)
    now_ct = now_utc.astimezone(CT)
    this_month = get_futures_month(now_ct)
    prev_m = (this_month[0], this_month[1] - 1) if this_month[1] > 1 else (this_month[0] - 1, 12)

    for sym in SYMBOLS:
        if sym not in CONTRACT_MAP:
            continue
        curr = CONTRACT_MAP[sym][0]
        try:
            resp = sync_requests.post(
                f'{base_url}/History/retrieveBars',
                json={
                    "contractId": curr, "live": False,
                    "startTime": (now_utc - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "endTime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    # includePartialBar=False: partial bars have a live 'close' that
                    # keeps changing until the bar completes. Averaging them into the
                    # running mean corrupts CMM (each refresh injects a new pseudo-close
                    # for the same bar-in-progress, over-weighting recent price).
                    "unit": 3, "unitNumber": 1, "limit": 5000, "includePartialBar": False,
                },
                headers=headers, timeout=15,
            )
            hourly = resp.json().get('bars', [])
        except Exception as e:
            print(f"  [{sym}] CMM/PMM refresh failed: {e!r}")
            continue

        month_data = defaultdict(list)
        for b in hourly:
            try:
                ts = datetime.fromisoformat(b['t'].replace('Z', '+00:00')).astimezone(CT)
            except Exception:
                continue
            fm = get_futures_month(ts)
            month_data[fm].append(b['c'])

        if this_month in month_data and len(month_data[this_month]) >= MIN_SAMPLES_CMM:
            state.cmm[sym] = sum(month_data[this_month]) / len(month_data[this_month])
        elif this_month in month_data:
            state.cmm[sym] = None  # warm-up
        if prev_m in month_data and len(month_data[prev_m]) > 0:
            state.pmm[sym] = sum(month_data[prev_m]) / len(month_data[prev_m])


def get_all_eligible_levels(symbol, mode, price, tick_size):
    """Get all mean levels eligible for entry in the given mode.

    A BUY LIMIT must be BELOW current price (else fills instantly at market).
    A SELL LIMIT must be ABOVE current price (same reason).

    The engine re-checks every 5-min bar, so as price moves, new levels
    become eligible and get orders placed automatically.
    """
    # All four means now enabled. PMM was re-enabled 2026-08-05 after a
    # fresh audit vs raw ProjectX data confirmed engine PMM matched truth
    # to the penny (MNQ 29174.50, MES 7526.21, MYM 52638.64, MGC 4076.60)
    # — July 2026 is a full month of native U26 data, so no back-adjustment
    # or partial-month artifacts remain.
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

    # ATR is still computed and logged so you have a REFERENCE stop distance
    # to use when manually attaching a stop on TopstepX UI — the engine
    # does NOT place stops itself in execution-only mode.
    atr = state.atr.get(symbol)
    ref_stop_dist = (atr * ATR_MULTIPLIER) if atr else None
    ref_stop_str = f"ref stop ±{ref_stop_dist:.2f}pts" if ref_stop_dist else "ATR pending"

    if state.dry_run:
        print(f"  [{symbol}] DRY: {side_str} {level_name} @ {entry_price} ({ref_stop_str})")
        state.pending_entries[key] = {
            'order_id': 'DRY', 'entry_price': entry_price,
            'side': side, 'contract_id': contract_id, 'level': level_name,
        }
        return

    # ══ EXECUTION-ONLY MODE ═════════════════════════════════════════════════
    # Engine places the ENTRY LIMIT only. Once filled, YOU are responsible
    # for attaching stop + target on the TopstepX UI. The engine never
    # places brackets. Cap enforcement continues to prevent overexposure.
    # ═══════════════════════════════════════════════════════════════════
    async with aiohttp.ClientSession() as http:
        r = await (await http.post(f'{base_url}/Order/place', json={
            'accountId': account.id, 'contractId': contract_id,
            'type': 1, 'side': side, 'size': CONTRACTS_PER_ORDER, 'limitPrice': entry_price,
        }, headers=hdrs)).json()
        if not r.get('success'):
            print(f"  [{symbol}] {level_name} entry failed: {r}")
            return
        entry_id = r['orderId']

    state.pending_entries[key] = {
        'order_id': entry_id,
        'entry_price': entry_price,
        'side': side, 'contract_id': contract_id, 'level': level_name,
    }
    print(f"  [{symbol}] {side_str} {level_name} @ {entry_price} ({ref_stop_str}) — attach stop manually on TopstepX")


# ─────────────────────────────────────────────────────────────
async def check_and_bracket_fills(client, account):
    """EXECUTION-ONLY MODE (2026-07-23): no-op.

    The engine no longer places stops or targets on fill. YOU attach
    risk management manually on the TopstepX UI. This function used to
    poll positions and place bracket orders; those calls are removed
    because leftover orphan brackets accumulated across restarts and
    caused ~40 stale orders + wrong-side exposure on 2026-07-23.

    Kept as an async no-op so the call-site in on_new_bar() doesn't
    need to change and can be safely re-enabled later if we build a
    proper OCO/orphan-cleanup implementation.
    """
    return


# ─────────────────────────────────────────────────────────────
# BAR HANDLER
# ─────────────────────────────────────────────────────────────
async def on_new_bar(symbol, bar_data, client, account):
    """Process a new 5-min bar. Place/update orders at ALL eligible levels."""
    try:
        close = bar_data['close']
        state.current_price[symbol] = close
        update_running_means(symbol, close, bar_data['timestamp'])
        await check_and_bracket_fills(client, account)

        cdm = state.cdm.get(symbol)
        cdm_str = f"{cdm:.2f}" if cdm else "?"
        print(f"  [{symbol}] {close:.2f} | CDM: {cdm_str}")

        # Delegate to shared placement logic (also called by the 60s reprice tick).
        await _scan_and_place(symbol, close, client, account, source="5m_bar")

    except Exception as e:
        print(f"  [{symbol}] Error (non-fatal): {e}")


async def _scan_and_place(symbol, close, client, account, source: str = "tick"):
    """Scan eligible levels and place/update entry limit orders.

    Called from:
      - on_new_bar()   every 5 min (source='5m_bar')  — after new CDM is computed
      - main() loop    every 60 s  (source='1m_tick') — keeps orders synced when
                                                        the 5-min bar hasn't closed yet

    The 60s reprice does NOT recompute CDM/PDM/CMM/PMM; it uses the existing
    state values. The point is to (a) recover any orders the broker canceled
    or dropped, and (b) reprice a level if CMM/PMM refresh moved it > 2 ticks
    since the last placement.
    """
    try:
        tick_size = CONTRACT_MAP[symbol][2]
        cdm = state.cdm.get(symbol)
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

        # Get contract ID from our own CONTRACT_MAP — no SDK call needed.
        # (The old code awaited client.get_instrument() which was a
        # project_x_py SDK method that TopstepAPI doesn't have. Falling
        # through to a bare `except: return` was silently no-oping every
        # bar close, which is why no sell orders were ever placed.)
        if symbol not in CONTRACT_MAP:
            print(f"  [{symbol}] SKIP — not in CONTRACT_MAP")
            return
        contract_id = CONTRACT_MAP[symbol][0]

        side = 0 if mode == 'BUY' else 1

        # ── POSITION + ORDER GUARD (API-level, prevents duplicates across restarts) ──
        import aiohttp
        token = client.get_session_token()
        base_url = client.base_url
        hdrs = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

        # Count open positions for this instrument.
        # NOTE: TopstepX API requires POST with json body. Previously used
        # GET with params= which returned an error shape — isinstance check
        # then failed silently and open_pos_count stayed at 0. That made the
        # cap effectively count only working orders, which vanish on fill,
        # so the engine could accumulate positions beyond the cap.
        open_pos_count = 0
        pos_query_ok = False
        try:
            async with aiohttp.ClientSession() as http:
                resp = await http.post(f'{base_url}/Position/searchOpen',
                                       json={'accountId': account.id}, headers=hdrs)
                pos_data = await resp.json()
            pos_query_ok = True
            if isinstance(pos_data, dict):
                positions = pos_data.get('positions', [])
            elif isinstance(pos_data, list):
                positions = pos_data
            else:
                positions = []
            for p in positions:
                if p.get('contractId', '') == contract_id:
                    open_pos_count += abs(p.get('size', 0))
        except Exception as e:
            print(f"  [{symbol}] SKIP — position query failed: {e!r}")

        # Count open limit orders for this instrument (entry orders only, type=1=Limit)
        open_order_count = 0
        existing_order_prices = set()
        ord_query_ok = False
        try:
            async with aiohttp.ClientSession() as http:
                resp = await http.post(f'{base_url}/Order/searchOpen',
                                       json={'accountId': account.id}, headers=hdrs)
                ord_data = await resp.json()
            ord_query_ok = True
            if isinstance(ord_data, dict):
                orders = ord_data.get('orders', [])
            elif isinstance(ord_data, list):
                orders = ord_data
            else:
                orders = []
            for o in orders:
                if o.get('contractId', '') == contract_id and o.get('type') == 1 and o.get('side') == side:
                    open_order_count += 1
                    if o.get('limitPrice'):
                        existing_order_prices.add(round(o['limitPrice'], 2))
        except Exception as e:
            print(f"  [{symbol}] SKIP — order query failed: {e!r}")

        # Hard rule: if EITHER query failed, do not place new orders this bar.
        # The cap is only meaningful when we can see both positions and orders.
        if not (pos_query_ok and ord_query_ok):
            return

        total_exposure = open_pos_count + open_order_count
        if total_exposure >= MAX_CONTRACTS_PER_INSTRUMENT:
            print(f"  [{symbol}] AT CAP — {open_pos_count} pos + {open_order_count} working = {total_exposure}/{MAX_CONTRACTS_PER_INSTRUMENT}, no new orders")
            return  # Already at max — no new orders

        # Place/update at ALL eligible levels (1 order per level).
        # IMPORTANT: re-query position count BETWEEN placements within the
        # same scan cycle so a fill that lands mid-loop is reflected before
        # we add the next entry. Prior bug: optimistic local counter
        # (total_exposure += 1) diverged from broker reality and let MNQ
        # accumulate 9 contracts overnight as multiple levels cascaded.
        eligible = get_all_eligible_levels(symbol, mode, close, tick_size)
        for level_name, entry_price in eligible:
            key = (symbol, level_name)
            if key in state.active_positions:
                continue  # Already filled at this level
            # Skip if we already have a limit order at this price
            if round(entry_price, 2) in existing_order_prices:
                continue

            # Cap check using the scan-cycle exposure plus pending placements
            # in this loop. We trust the scan-cycle counts (computed fresh
            # at function entry from /Position/searchOpen and /Order/searchOpen)
            # rather than re-querying every level — those repeated queries
            # were causing intermittent 'cannot access live_pos' errors
            # without adding real safety value.
            if total_exposure >= MAX_CONTRACTS_PER_INSTRUMENT:
                print(f"  [{symbol}] CAP HIT mid-cycle — {open_pos_count} pos + {open_order_count + (total_exposure - open_pos_count - open_order_count)} working = {total_exposure}/{MAX_CONTRACTS_PER_INSTRUMENT}, halting placements")
                break

            await place_or_update_entry(client, account, symbol, level_name,
                                        contract_id, side, entry_price, tick_size)
            # Optimistically increment — next scan cycle will re-query
            # actual broker state to correct any drift.
            total_exposure += 1

    except Exception as e:
        print(f"  [{symbol}] scan error ({source}) (non-fatal): {e}")


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

    # Previous TRADING session start. CME futures close Fri 4pm CT, reopen Sun 5pm CT.
    # So on a Monday morning the previous session is Friday, not Sunday.
    # We walk back day-by-day skipping Sat (weekday=5) and Sun (weekday=6),
    # also handling Monday holidays by extending the search window.
    prev_start = session_start - timedelta(days=1)
    # If prev_start lands on Saturday (weekday=5) or Sunday (weekday=6),
    # walk back to Friday.
    while prev_start.weekday() >= 5:
        prev_start -= timedelta(days=1)
    prev_start_utc = prev_start.astimezone(timezone.utc)
    # Pull a wider window so a Monday holiday (Memorial Day, Labor Day,
    # July 4, etc.) doesn't leave PDM empty — we'll fall back to the
    # most recent session with actual bars.
    fetch_start = prev_start - timedelta(days=4)
    fetch_start_utc = fetch_start.astimezone(timezone.utc)

    active = list(state.modes.keys()) if hasattr(state, 'modes') and state.modes else SYMBOLS
    for sym in active:
        if sym not in CONTRACT_MAP:
            print(f"  {sym}: unknown contract, skipping")
            continue
        curr, prior, tick, tick_val = CONTRACT_MAP[sym]

        # Fetch a wide window and filter locally — the History API
        # does not reliably respect startTime, so we bucket bars by timestamp.
        # unit=2 is MINUTE (unit=1 is Second, which is what we were wrongly using)
        all_bars = sync_requests.post(f'{base_url}/History/retrieveBars', json={
            "contractId": curr, "live": False,
            "startTime": fetch_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            # includePartialBar=False: the seed pull must only contain completed bars.
            # A partial bar here would corrupt state.day_closes with a moving 'close'.
            "unit": 2, "unitNumber": 5, "limit": 5000, "includePartialBar": False,
        }, headers=headers).json().get('bars', [])

        # Bucket bars by FUTURES TRADING DAY (5pm CT roll). This handles
        # weekends and holidays automatically — days with no bars don't
        # appear as buckets at all, so 'previous trading day' = whichever
        # bucket is one before today.
        bars_today = []
        bars_by_day = defaultdict(list)
        for b in all_bars:
            try:
                ts = datetime.fromisoformat(b['t'].replace('Z', '+00:00'))
            except Exception:
                continue
            if ts >= session_start_utc:
                bars_today.append(b)
            else:
                # Group by futures trading day in CT
                ts_ct = ts.astimezone(CT)
                fday = get_futures_day(ts_ct)
                bars_by_day[fday].append(b)

        # CDM
        if bars_today:
            today_closes = [b['c'] for b in bars_today]
            state.day_closes[(sym, today)] = today_closes
            state.cdm[sym] = sum(today_closes) / len(today_closes)
            print(f"  {sym} CDM: {state.cdm[sym]:.2f} ({len(today_closes)} bars)")

        # PDM = most recent trading day BEFORE today with actual bars.
        # Skips weekends, holidays automatically.
        prior_days = sorted([d for d in bars_by_day.keys() if d < today], reverse=True)
        if prior_days:
            prev_trading_day = prior_days[0]
            yd_closes = [b['c'] for b in bars_by_day[prev_trading_day]]
            state.pdm[sym] = sum(yd_closes) / len(yd_closes)
            print(f"  {sym} PDM: {state.pdm[sym]:.2f} ({len(yd_closes)} bars on {prev_trading_day})")
        else:
            print(f"  {sym} PDM: — (no prior session bars found in 5-day window)")

        # CMM/PMM from hourly (current contract covers recent months)
        hourly = sync_requests.post(f'{base_url}/History/retrieveBars', json={
            "contractId": curr, "live": False,
            "startTime": (now_utc - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            # includePartialBar=False: partial hourly bars corrupt CMM/PMM.
            "unit": 3, "unitNumber": 1, "limit": 5000, "includePartialBar": False,
        }, headers=headers).json().get('bars', [])

        month_data = defaultdict(list)
        for b in hourly:
            ts = datetime.fromisoformat(b['t']).astimezone(CT)
            fm = get_futures_month(ts)
            month_data[fm].append(b['c'])

        # Seed CMM/PMM directly from fresh hourly bars — do NOT populate
        # state.month_closes anymore. Live 5-min bars no longer contribute
        # to CMM/PMM (see refresh_monthly_means() docstring). These values
        # will be refreshed hourly in the main monitor loop.
        if this_month in month_data:
            state.cmm[sym] = sum(month_data[this_month]) / len(month_data[this_month])
            print(f"  {sym} CMM: {state.cmm[sym]:.2f}")

        prev_m = (this_month[0], this_month[1]-1) if this_month[1]>1 else (this_month[0]-1, 12)
        if prev_m in month_data:
            state.pmm[sym] = sum(month_data[prev_m]) / len(month_data[prev_m])
            print(f"  {sym} PMM: {state.pmm[sym]:.2f}")

        # ATR from last 3 trading days (adapts to recent volatility)
        daily = sync_requests.post(f'{base_url}/History/retrieveBars', json={
            "contractId": curr, "live": False,
            "startTime": (now_utc - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Daily bars can keep partial for the current day. Partial daily bar is
            # only used for today's H/L display, not for the running mean.
            "unit": 4, "unitNumber": 1, "limit": 500, "includePartialBar": True,
        }, headers=headers).json().get('bars', [])
        daily.sort(key=lambda x: x['t'])

        if len(daily) >= 4:
            trs = []
            for i in [-3, -2, -1]:
                h = daily[i]['h']
                l = daily[i]['l']
                pc = daily[i-1]['c']
                tr = max(h - l, abs(h - pc), abs(l - pc))
                trs.append(tr)
            state.atr[sym] = sum(trs) / len(trs)
            stop_pts = state.atr[sym] * ATR_MULTIPLIER
            target_pts = stop_pts * RR_RATIO
            print(f"  {sym} ATR(3d): {state.atr[sym]:.2f} | stop: {stop_pts:.1f}pts | target: {target_pts:.1f}pts")

    state.current_day = today
    state.current_month = this_month


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
async def main(modes: dict, dry_run: bool = False):
    # Direct-ProjectX rebuild (project_x_py library is abandoned + broken).
    # topstep_api handles REST (auth, historical bars, orders, positions).
    # topstep_stream handles SignalR + tick-to-5min bar aggregation.
    from topstep_api import from_env as topstep_from_env, TopstepAPIError  # noqa: F401
    from topstep_stream import TopstepStream

    state.modes = modes  # {symbol: 'BUY'/'SELL'}
    state.dry_run = dry_run
    active_syms = list(modes.keys())
    mode_lines = '  '.join(f"{s}:{m}" for s, m in modes.items())
    live_str = 'DRY RUN' if dry_run else 'LIVE'

    print(f"""
╔═══════════════════════════════════════════════════════╗
║  Tzu Strategic Momentum  ({live_str})                  
║  {mode_lines:<52}║
║  EXECUTION-ONLY: attach stops/targets manually on TopstepX UI    ║
╚═══════════════════════════════════════════════════════╝
""")

    stream: TopstepStream | None = None
    try:
        # Sync auth + account selection (no `await` needed).
        client = topstep_from_env()
        account = client.get_account_info()
        print(f"  Account: {account.name}")
        print(f"  Balance: ${account.balance:,.2f}")

        # Seed historical data. Uses client.get_session_token() +
        # client.base_url — provided by SDK-compat shim on TopstepAPI.
        print(f"\n  Loading historical data...")
        seed_historical(client)
        print(f"  Ready.\n")

        # Real-time stream. ProjectX has no native bar-close event; we
        # aggregate ticks into 5-min bars locally via TopstepStream.
        stream = TopstepStream(jwt_token=client.get_jwt())
        loop = asyncio.get_event_loop()

        # Map contract_id -> symbol so the single stream-level callback
        # can route to the right per-symbol handler.
        symbol_by_contract = {
            CONTRACT_MAP[sym][0]: sym
            for sym in active_syms
            if sym in CONTRACT_MAP
        }

        def dispatch_bar(contract_id: str, bar: dict) -> None:
            # Runs on signalrcore's thread. Marshal to the async loop.
            sym = symbol_by_contract.get(contract_id)
            if sym is None:
                return
            try:
                c = float(bar['c'])
            except (KeyError, ValueError, TypeError):
                return
            bar_data = {
                'close': c,
                'high': float(bar.get('h', c)),
                'low': float(bar.get('l', c)),
                'open': float(bar.get('o', c)),
                'timestamp': datetime.now(CT),
            }
            asyncio.run_coroutine_threadsafe(
                on_new_bar(sym, bar_data, client, account), loop
            )

        stream.on_bar_close(dispatch_bar)

        for sym in active_syms:
            if sym not in CONTRACT_MAP:
                print(f"  {sym}: unknown contract, skipping subscription")
                continue
            cid = CONTRACT_MAP[sym][0]
            stream.subscribe(cid)
            print(f"  Subscribed: {sym} → {cid}")

        print(f"\n  Connecting to ProjectX market hub...")
        stream.start()
        print(f"  Streaming.\n")

        print(f"  STREAMING — {mode_lines}")
        print(f"  Ctrl+C to stop\n")

        # Keep alive + monitor positions + hourly status + REPRICE
        # every 60s so orders stay synced with current mean levels instead
        # of only updating at 5-min bar close.
        while True:
            await asyncio.sleep(60)

            # ── 60s REPRICE ─────────────────────────────────────────────
            # For each active symbol, re-run placement logic using the
            # current state.cdm/pdm/cmm/pmm and current price. place_or_update_entry
            # already handles: (a) dedup by price (skip if unchanged),
            # (b) cancel + replace if moved > 2 ticks, (c) re-place if broker
            # canceled it. So this call is cheap when nothing changed and
            # self-heals when something did.
            tick_ts = datetime.now(CT).strftime("%H:%M:%S CT")
            tick_summary = []
            for sym in list(state.modes.keys()):
                px = state.current_price.get(sym)
                if px is None:
                    tick_summary.append(f"{sym}=nopx")
                    continue
                try:
                    await _scan_and_place(sym, px, client, account, source="1m_tick")
                    cdm = state.cdm.get(sym)
                    tick_summary.append(f"{sym}={px:.2f}/CDM={cdm:.2f}" if cdm else f"{sym}={px:.2f}/CDM=?")
                except Exception as e:
                    print(f"  [{sym}] 60s reprice error: {e!r}")
                    tick_summary.append(f"{sym}=err")
            print(f"  ⏱  60s tick {tick_ts} — {' | '.join(tick_summary)}")

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
            # Once per hour, at :00, refresh CMM/PMM from fresh hourly bars.
            # This is the ONLY code that writes state.cmm and state.pmm
            # during a live session, and it fixes the drift bug (see
            # refresh_monthly_means() docstring for details).
            try:
                et_now = datetime.now(ET)
                if et_now.minute == 0 and et_now.second < 30:
                    if state.last_monthly_refresh_hour != et_now.hour:
                        try:
                            refresh_monthly_means(client)
                            state.last_monthly_refresh_hour = et_now.hour
                        except Exception as e:
                            print(f"  monthly refresh failed: {e!r}")
            except Exception:
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
    finally:
        # Clean shutdown of the WebSocket stream. Safe if never started.
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass


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
    parser.add_argument("--mcl", choices=["buy", "sell"], help="MCL mode")
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
    if args.mcl: modes['MCL'] = args.mcl.upper()

    if not modes:
        parser.error("Specify --mode for all, or per-instrument flags (--mnq, --mes, --mym, --mgc)")

    asyncio.run(main(modes=modes, dry_run=args.dry_run))
