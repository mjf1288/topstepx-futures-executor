"""
Tzu Strategic Momentum — Real-Time Mean Level Execution Engine
===============================================================
Streams prices, recomputes four means from completed broker bars every 60s, and places
limit orders at the closest mean level in the direction you choose.

Usage:
    python realtime_engine.py --mode sell          # SELL mode, live
    python realtime_engine.py --mode buy           # BUY mode, live
    python realtime_engine.py --mode sell --dry-run # SELL mode, dry run

The mode (BUY or SELL) is set by you. The engine handles execution:
  - Streams 5-min bars via WebSocket
  - Recomputes CDM/PDM from broker 5-min bars and CMM/PMM from broker hourly bars
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
import math
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

# (active_contract, prior_contract, tick_size, tick_value)
#
# This is only a FALLBACK. resolve_active_contracts() asks the broker which
# month is actually active at startup and overwrites slot 0, because these
# months go stale: MYM/MNQ/MES roll quarterly, MGC every two months, and MCL
# EVERY month. A stale entry here means trading an expired contract.
# Slot 1 is the month immediately BEFORE the active one (build2.py stitches
# older history from it) and is not used for order routing.
# Verified 2026-09-22: MYM U26 expired 09-18, MCL V26 expired 09-21.
CONTRACT_MAP = {
    'MNQ': ('CON.F.US.MNQ.Z26', 'CON.F.US.MNQ.U26', 0.25, 0.50),
    'MES': ('CON.F.US.MES.Z26', 'CON.F.US.MES.U26', 0.25, 1.25),
    'MYM': ('CON.F.US.MYM.Z26', 'CON.F.US.MYM.U26', 1.0, 0.50),
    'MGC': ('CON.F.US.MGC.V26', 'CON.F.US.MGC.Q26', 0.10, 1.00),
    'MCL': ('CON.F.US.MCL.X26', 'CON.F.US.MCL.V26', 0.01, 1.00),
}

# Futures month codes, in calendar order.
MONTH_CODES = "FGHJKMNQUVXZ"

# Combined cap: total contracts per instrument across BOTH the mean-level
# engine AND the VWAP engine. Each engine independently queries the broker
# for total exposure (positions + working orders on that contract, any side)
# and refuses to place a new order that would push the total above this cap.
# Set to 2 so mean + VWAP together never exceed 2 contracts per symbol.
MAX_CONTRACTS_PER_INSTRUMENT = 2  # combined cap with VWAP engine (was 4 solo)
CONTRACTS_PER_ORDER = 1            # 1 contract per entry

ATR_MULTIPLIER = 0.382             # ~38.2% of daily ATR (fib-based tight stop)
RR_RATIO = 2.618                   # Golden ratio R:R

# Running-mean warm-up gates. The mean is statistically meaningless
# with too few samples — at globex open on a new month, a 1-sample CMM
# equals current price and triggers instant-fill limits with no edge.
# Observed bug: 2026-06-01 globex open filled at CMM on first 5m bar.
MIN_SAMPLES_CDM = 6   # ~30 min of 5-min bars before publishing CDM
MIN_SAMPLES_CMM = 24  # 24 completed hourly bars; preserve existing warm-up gate

ET = pytz.timezone("America/New_York")
CT = pytz.timezone("America/Chicago")


# ─────────────────────────────────────────────────────────────
# CONTRACT RESOLUTION
# ─────────────────────────────────────────────────────────────
def _parse_broker_datetime(value):
    """Parse a broker ISO timestamp to aware UTC, or None if unusable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def resolve_active_contracts(client, symbols, now_utc=None):
    """Ask the broker which contract month is live for each root symbol.

    MCL rolls monthly and the index/metal roots roll quarterly or bi-monthly,
    so a hardcoded month silently becomes an expired contract. This queries
    /Contract/search and picks, among contracts whose lastTradingDate is still
    in the future, the broker's flagged active contract, then the nearest
    expiry as a tiebreak.

    Returns (resolved, failures). `resolved` maps symbol -> (contract_id,
    expiry, was_flagged_active). A symbol in `failures` must NOT be traded:
    routing to the wrong month is worse than not trading it.
    """
    now = now_utc or datetime.now(timezone.utc)
    resolved, failures = {}, {}
    for symbol in symbols:
        try:
            candidates = client.search_contract(symbol)
        except Exception as exc:  # network/auth/shape — never guess past it
            failures[symbol] = f"contract search failed: {exc!r}"
            continue
        if not isinstance(candidates, list) or not candidates:
            failures[symbol] = "contract search returned nothing"
            continue

        best = None
        expired_seen = 0
        for contract in candidates:
            if not isinstance(contract, dict):
                continue
            cid = str(contract.get('id') or '')
            parts = cid.split('.')
            # Require exactly CON.F.US.<ROOT>.<MonthCode><YY> for this root, so
            # a spread or a different product can never be selected.
            if len(parts) != 5 or parts[3] != symbol:
                continue
            month = parts[4]
            if len(month) != 3 or month[0] not in MONTH_CODES or not month[1:].isdigit():
                continue
            expiry = _parse_broker_datetime(contract.get('lastTradingDate'))
            if expiry is not None and expiry <= now:
                expired_seen += 1
                continue
            flagged = bool(contract.get('activeContract'))
            # Prefer the broker's active flag, then the nearest expiry. Unknown
            # expiry sorts last so a dated contract always wins.
            rank = (0 if flagged else 1, expiry or datetime.max.replace(tzinfo=timezone.utc))
            if best is None or rank < best[0]:
                best = (rank, cid, expiry, flagged)

        if best is None:
            failures[symbol] = (
                f"no unexpired {symbol} contract in {len(candidates)} result(s)"
                + (f"; {expired_seen} already expired" if expired_seen else "")
            )
            continue
        resolved[symbol] = (best[1], best[2], best[3])
    return resolved, failures


def apply_resolved_contracts(resolved):
    """Point CONTRACT_MAP slot 0 at the broker's active month, keeping ticks."""
    changes = {}
    for symbol, (cid, _expiry, _flagged) in resolved.items():
        if symbol not in CONTRACT_MAP:
            continue
        current, prior, tick, tick_value = CONTRACT_MAP[symbol]
        if cid == current:
            continue
        # The month we were about to trade becomes the prior month.
        CONTRACT_MAP[symbol] = (cid, current, tick, tick_value)
        changes[symbol] = (current, cid)
    return changes


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
        self.refresh_locks = defaultdict(asyncio.Lock)


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
def prior_month_start_utc(now_utc):
    """Start of the entire previous futures month, including its 5pm CT open."""
    year, month = get_futures_month(now_utc.astimezone(CT))
    first = datetime(year, month, 1)
    prior_first = (first - timedelta(days=1)).replace(day=1)
    # Do calendar arithmetic BEFORE localizing so DST cannot shift the open.
    session_open = (prior_first - timedelta(days=1)).replace(hour=17)
    return CT.localize(session_open).astimezone(timezone.utc)


def completed_bars(bars, start, end, minutes):
    """Normalize, deduplicate and exclude partial/out-of-window broker bars."""
    if not isinstance(bars, list) or not bars:
        raise ValueError("Broker returned no usable bar history")
    by_time = {}
    for bar in bars:
        ts = datetime.fromisoformat(bar['t'].replace('Z', '+00:00'))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        if start <= ts and ts + timedelta(minutes=minutes) <= end:
            if not math.isfinite(float(bar['c'])):
                raise ValueError("Broker bar has a non-finite close")
            by_time[ts] = bar
    return sorted(by_time.items())


def refresh_broker_means(client, symbol, now_utc=None):
    """Replace all four means atomically from fresh, completed broker bars.

    No local tick aggregate or prior cached mean contributes to these values.
    Both requests must succeed before publishing any state. Empty/missing
    period buckets clear that period's old value rather than leaking it across
    a day/month roll. A failed request raises, so the caller skips placement.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    today = get_futures_day(now_utc.astimezone(CT))
    this_month = get_futures_month(now_utc.astimezone(CT))
    prev_m = (this_month[0], this_month[1] - 1) if this_month[1] > 1 else (this_month[0] - 1, 12)
    day_start = now_utc - timedelta(days=7)
    month_start = prior_month_start_utc(now_utc)
    cid = CONTRACT_MAP[symbol][0]
    daily_bars = completed_bars(client.get_bars(
        contract_id=cid, unit=2, unit_number=5,
        start_time=day_start, end_time=now_utc, limit=5000,
        include_partial=False,
    ), day_start, now_utc, 5)
    hourly_bars = completed_bars(client.get_bars(
        contract_id=cid, unit=3, unit_number=1,
        start_time=month_start, end_time=now_utc, limit=5000,
        include_partial=False,
    ), month_start, now_utc, 60)
    if not daily_bars or not hourly_bars:
        raise ValueError(f"{symbol}: no completed broker bars in requested window")

    days, months = defaultdict(list), defaultdict(list)
    for ts, bar in daily_bars:
        days[get_futures_day(ts.astimezone(CT))].append(float(bar['c']))
    for ts, bar in hourly_bars:
        months[get_futures_month(ts.astimezone(CT))].append(float(bar['c']))
    prior_days = sorted(day for day in days if day < today)
    previous = days[prior_days[-1]] if prior_days else []

    def mean(values, minimum=1):
        return sum(values) / len(values) if len(values) >= minimum else None

    values = {
        'CDM': mean(days[today], MIN_SAMPLES_CDM),
        'PDM': mean(previous),
        'CMM': mean(months[this_month], MIN_SAMPLES_CMM),
        'PMM': mean(months[prev_m]),
    }
    for name, value in values.items():
        getattr(state, name.lower())[symbol] = value
    # Replace, never append, so repeated fetches cannot double-count a close.
    for key in list(state.day_closes):
        if key[0] == symbol:
            del state.day_closes[key]
    state.day_closes.update({(symbol, day): closes for day, closes in days.items()})
    state.current_day, state.current_month = today, this_month
    state.current_price.setdefault(symbol, float(daily_bars[-1][1]['c']))
    return values


# Documented mean-level strength: CMM strongest, PDM weakest. The 60s scan
# places orders in THIS order, so when MAX_CONTRACTS_PER_INSTRUMENT is reached
# the cap is spent on the strongest levels available. Before 2026-09-22 the
# scan walked plain dict order (CDM, PDM, CMM, PMM), so with a 2-contract cap
# the two WEAKEST levels claimed both slots on every scan and CMM/PMM were
# never armed while a nearer daily mean was eligible.
LEVEL_STRENGTH = {'CMM': 4, 'PMM': 3, 'CDM': 2, 'PDM': 1}


def get_all_eligible_levels(symbol, mode, price, tick_size):
    """Get all mean levels eligible for entry, strongest level first.

    A BUY LIMIT must be BELOW current price (else fills instantly at market).
    A SELL LIMIT must be ABOVE current price (same reason).

    The engine re-checks every 60 seconds, so as price moves, new levels
    become eligible and get orders placed automatically. Results are ordered by
    LEVEL_STRENGTH so a contract cap never starves a stronger level.
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
    result.sort(key=lambda item: -LEVEL_STRENGTH[item[0]])
    return result


# ─────────────────────────────────────────────────────────────
# ORDER MANAGEMENT
# ─────────────────────────────────────────────────────────────
async def place_or_update_entry(client, account, symbol, level_name, contract_id, side, entry_price, tick_size):
    """Place/update an entry; return True only after successful placement."""
    import aiohttp
    token = client.get_session_token()
    base_url = client.base_url
    hdrs = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    key = (symbol, level_name)

    existing = state.pending_entries.get(key)
    if existing:
        if round(entry_price / tick_size) == round(existing['entry_price'] / tick_size):
            return False  # Same executable price; even one tick merits repricing.
        # Cancel old
        if not await cancel_pending_entry(client, account, key):
            return False
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
        return True

    # ══ EXECUTION-ONLY MODE ═════════════════════════════════════════════════
    # Engine places the ENTRY LIMIT only. Once filled, YOU are responsible
    # for attaching stop + target on the TopstepX UI. The engine never
    # places brackets. Cap enforcement continues to prevent overexposure.
    # ═══════════════════════════════════════════════════════════════════
    async with aiohttp.ClientSession() as http:
        response = await http.post(f'{base_url}/Order/place', json={
            'accountId': account.id, 'contractId': contract_id,
            'type': 1, 'side': side, 'size': CONTRACTS_PER_ORDER, 'limitPrice': entry_price,
        }, headers=hdrs)
        response.raise_for_status()
        r = await response.json()
        if not r.get('success'):
            print(f"  [{symbol}] {level_name} entry failed: {r}")
            return False
        entry_id = r['orderId']

    state.pending_entries[key] = {
        'order_id': entry_id,
        'entry_price': entry_price,
        'side': side, 'contract_id': contract_id, 'level': level_name,
    }
    print(f"  [{symbol}] {side_str} {level_name} @ {entry_price} ({ref_stop_str}) — attach stop manually on TopstepX")
    return True


async def cancel_pending_entry(client, account, key):
    """Forget an entry only after a confirmed cancellation (never in doubt)."""
    import aiohttp
    entry = state.pending_entries[key]
    if not state.dry_run:
        async with aiohttp.ClientSession() as http:
            response = await http.post(
                f'{client.base_url}/Order/cancel',
                json={'orderId': entry['order_id'], 'accountId': account.id},
                headers={'Authorization': f'Bearer {client.get_session_token()}',
                         'Content-Type': 'application/json'},
            )
            response.raise_for_status()
            result = await response.json()
        if result.get('success') is not True:
            print(f"  [{key[0]}] cancel unconfirmed for {entry['order_id']}; no replacement")
            return False
    del state.pending_entries[key]
    return True


def broker_rows(payload, field):
    """Do not treat HTTP-200 broker errors/malformed payloads as empty state."""
    if isinstance(payload, list):
        return payload
    if (not isinstance(payload, dict) or payload.get('success') is False
            or not isinstance(payload.get(field), list)):
        raise ValueError(f"Invalid broker {field} response")
    return payload[field]


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
    """Local tick bars are a price hint only, NEVER an input to mean levels."""
    state.current_price[symbol] = bar_data['close']


async def refresh_and_reprice(symbol, client, account, now_utc=None):
    """One serialized 60s cycle: broker means first, order reconciliation second."""
    async with state.refresh_locks[symbol]:
        await asyncio.to_thread(refresh_broker_means, client, symbol, now_utc)
        price = state.current_price.get(symbol)
        if price is not None:
            await _scan_and_place(symbol, price, client, account, source="1m_tick")


async def _scan_and_place(symbol, close, client, account, source: str = "tick"):
    """Reconcile broker orders and reprice only after a successful mean refresh."""
    try:
        tick_size = CONTRACT_MAP[symbol][2]
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

        # Count open limit orders for this instrument (entry orders only, type=1=Limit)
        open_order_count = 0
        ord_query_ok = False
        try:
            async with aiohttp.ClientSession() as http:
                resp = await http.post(f'{base_url}/Order/searchOpen',
                                       json={'accountId': account.id}, headers=hdrs)
                resp.raise_for_status()
                ord_data = await resp.json()
            orders = broker_rows(ord_data, 'orders')
            for o in orders:
                if o.get('contractId', '') == contract_id and o.get('type') == 1:
                    open_order_count += abs(o.get('size', 1))
            ord_query_ok = True
        except Exception as e:
            print(f"  [{symbol}] SKIP — order query failed: {e!r}")

        # Read positions AFTER orders. If an entry filled between these reads,
        # it is counted conservatively in both snapshots, not missed in both.
        open_pos_count = 0
        pos_query_ok = False
        try:
            async with aiohttp.ClientSession() as http:
                resp = await http.post(f'{base_url}/Position/searchOpen',
                                       json={'accountId': account.id}, headers=hdrs)
                resp.raise_for_status()
                pos_data = await resp.json()
            positions = broker_rows(pos_data, 'positions')
            for p in positions:
                if p.get('contractId', '') == contract_id:
                    open_pos_count += abs(p.get('size', 0))
            pos_query_ok = True
        except Exception as e:
            print(f"  [{symbol}] SKIP — position query failed: {e!r}")

        # Hard rule: if EITHER query failed, do not place new orders this bar.
        # The cap is only meaningful when we can see both positions and orders.
        if not (pos_query_ok and ord_query_ok):
            return

        # Filled/cancelled/expired orders disappear from searchOpen. Prune
        # before adoption AND price dedup, using the same validated snapshot.
        open_by_id = {str(o['id']): o for o in orders}
        for key, entry in list(state.pending_entries.items()):
            if key[0] != symbol:
                continue
            if state.dry_run and entry['order_id'] == 'DRY':
                open_order_count += CONTRACTS_PER_ORDER
                continue
            broker_order = open_by_id.get(str(entry['order_id']))
            if broker_order is None:
                del state.pending_entries[key]
            elif broker_order.get('limitPrice') is not None:
                entry['entry_price'] = float(broker_order['limitPrice'])

        # RECONCILE state.pending_entries from broker reality on every tick.
        # Prior bug: engine tracked orders only in memory; on restart, the
        # broker still had old resting orders but engine thought there were
        # none, so it either (a) placed duplicates (rejected by contract cap)
        # or (b) failed to reprice because the 2-tick dedup only fires when
        # state.pending_entries has an entry.
        #
        # Rebuild: for each mean level, find any broker limit order for this
        # symbol/side within 20 ticks of the level's price. Attach it as our
        # order for that level so cancel+replace works.
        # `eligible` stays in LEVEL_STRENGTH order: it drives PLACEMENT, so the
        # contract cap is spent on the strongest levels. Adoption below walks a
        # distance-sorted copy instead, so a resting broker order is claimed by
        # the level it actually sits closest to.
        eligible = get_all_eligible_levels(symbol, mode, close, tick_size)
        by_distance = sorted(eligible, key=lambda level: abs(level[1] - close))
        eligible_names = {name for name, _ in eligible}
        # A disappeared/warm-up/wrong-side mean must not leave an old limit.
        for key in list(state.pending_entries):
            if key[0] == symbol and key[1] not in eligible_names:
                order_id = str(state.pending_entries[key]['order_id'])
                size = open_by_id.get(order_id, {}).get('size', CONTRACTS_PER_ORDER)
                if await cancel_pending_entry(client, account, key):
                    open_order_count -= size
                    orders = [o for o in orders if str(o['id']) != order_id]
        for level_name, entry_price in by_distance:
            key = (symbol, level_name)
            if key in state.pending_entries:
                continue  # already tracked
            # Find nearest broker order to this level within 20 ticks
            best = None
            best_dist = tick_size * 20  # 20-tick tolerance
            for bo in orders:
                if bo.get('contractId') != contract_id: continue
                if bo.get('type') != 1: continue
                if bo.get('side') != side: continue
                lp = bo.get('limitPrice')
                if lp is None: continue
                d = abs(float(lp) - entry_price)
                if d <= best_dist:
                    # Also require this broker order isn't already claimed by
                    # a different level in state.pending_entries.
                    already_claimed = any(
                        pe.get('order_id') == bo.get('id')
                        for pe in state.pending_entries.values()
                    )
                    if not already_claimed:
                        best = bo
                        best_dist = d
            if best is not None:
                state.pending_entries[key] = {
                    'order_id': best.get('id'),
                    'entry_price': float(best.get('limitPrice')),
                    'side': side, 'contract_id': contract_id, 'level': level_name,
                }
                print(f"  [{symbol}] {level_name} adopted broker order {best.get('id')} @ {best.get('limitPrice')}")

        # Now reprice every eligible level.
        for level_name, entry_price in eligible:
            key = (symbol, level_name)
            if key in state.active_positions:
                continue  # Already filled at this level
            has_existing_order = key in state.pending_entries
            # If no existing order tracked AND we've hit position cap, skip
            # placing a brand-new order (would be rejected anyway).
            if (not has_existing_order and
                    open_pos_count + open_order_count + CONTRACTS_PER_ORDER > MAX_CONTRACTS_PER_INSTRUMENT):
                continue
            placed = await place_or_update_entry(client, account, symbol, level_name,
                                                 contract_id, side, entry_price, tick_size)
            if placed and not has_existing_order:
                open_order_count += CONTRACTS_PER_ORDER

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
    now_ct = now_utc.astimezone(CT)
    today = get_futures_day(now_ct)
    this_month = get_futures_month(now_ct)

    active = list(state.modes.keys()) if hasattr(state, 'modes') and state.modes else SYMBOLS
    for sym in active:
        if sym not in CONTRACT_MAP:
            print(f"  {sym}: unknown contract, skipping")
            continue
        curr, prior, tick, tick_val = CONTRACT_MAP[sym]

        values = refresh_broker_means(client, sym, now_utc)
        for name, value in values.items():
            print(f"  {sym} {name}: {value:.2f}" if value is not None else f"  {sym} {name}: warm-up/no data")

        # ATR from last 3 trading days (adapts to recent volatility)
        daily = sync_requests.post(f'{base_url}/History/retrieveBars', json={
            "contractId": curr, "live": False,
            "startTime": (now_utc - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Daily bars can keep partial for the current day. Partial daily bar is
            # only used for today's H/L display, not for the running mean.
            "unit": 4, "unitNumber": 1, "limit": 500, "includePartialBar": True,
        }, headers=headers).json().get('bars') or []
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

        # Resolve live contract months BEFORE any bars or orders. MCL rolls
        # monthly, so a stale CONTRACT_MAP would route to an expired contract.
        print(f"\n  Resolving contract months...")
        resolved, failures = resolve_active_contracts(client, active_syms)
        changes = apply_resolved_contracts(resolved)
        for sym in active_syms:
            if sym in resolved:
                cid, expiry, flagged = resolved[sym]
                exp_s = expiry.strftime('%Y-%m-%d') if expiry else 'unknown'
                tag = '' if flagged else '  [not broker-flagged active]'
                moved = f"  (was {changes[sym][0].split('.')[-1]})" if sym in changes else ''
                print(f"    {sym}: {cid}  expires {exp_s}{moved}{tag}")
        for sym, reason in failures.items():
            print(f"    {sym}: UNRESOLVED — {reason}")
        if failures:
            # Refuse the symbol rather than trade a guessed month.
            for sym in failures:
                state.modes.pop(sym, None)
            active_syms = list(state.modes.keys())
            print(f"    Skipping {', '.join(failures)} this session.")
            if not active_syms:
                print("\n  No tradeable symbols after contract resolution. Exiting.")
                return

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

        # Monotonic cadence avoids adding fetch duration to every 60s period.
        next_refresh = loop.time() + 60
        while True:
            await asyncio.sleep(max(0, next_refresh - loop.time()))
            next_refresh += 60

            # Refresh all four means from completed broker bars BEFORE each
            # order scan, even when no local ticks/bars arrived this minute.
            tick_ts = datetime.now(CT).strftime("%H:%M:%S CT")
            tick_summary = []
            for sym in list(state.modes.keys()):
                try:
                    await refresh_and_reprice(sym, client, account)
                    px = state.current_price.get(sym)
                    cdm = state.cdm.get(sym)
                    tick_summary.append(f"{sym}={px:.2f}/CDM={cdm:.2f}" if cdm else f"{sym}={px:.2f}/CDM=?")
                except Exception as e:
                    print(f"  [{sym}] 60s reprice error: {e!r}")
                    tick_summary.append(f"{sym}=err")
            print(f"  ⏱  60s tick {tick_ts} — {' | '.join(tick_summary)}")
            # Do not burst catch-up requests if the broker was slow.
            if next_refresh <= loop.time():
                next_refresh = loop.time() + 60

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
