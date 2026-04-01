"""
Tzu Strategic Momentum — Real-Time Execution Engine
====================================================
Continuous process that streams live 5-min bars, recomputes CDM
dynamically, and places/adjusts limit orders at the live mean level.

This replaces the cron-based static limit approach. Instead of placing
a limit at a snapshot CDM, this engine:
  1. Streams live 5-min bars via WebSocket
  2. Recomputes CDM after every new bar close
  3. Checks if price is touching/crossing the CDM
  4. Places limit entry at the live CDM when regime is active
  5. Cancels/replaces the limit as CDM moves
  6. Places bracket (stop + target) immediately on fill

Run on your local Mac:
    python realtime_engine.py              # Live
    python realtime_engine.py --dry-run    # Show what would happen

Author: Matthew Foster
"""

import asyncio
import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict

import pytz
from dotenv import load_dotenv

# Load .env from script directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
sys.path.insert(0, SCRIPT_DIR)

from trend_filters import compute_trend_bias
from mean_levels_calc import compute_atr

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
SYMBOLS = ["MNQ", "MES", "MYM"]

DEFAULT_ATR_MULTIPLIER = 1.0
DEFAULT_RR_RATIO = 2.0
MIN_ATR_MULTIPLIER = 0.4
DAILY_PROFIT_CAP = 1_400

REGIME_FILE = os.path.join(SCRIPT_DIR, "regime_state.json")
ORDERS_FILE = os.path.join(SCRIPT_DIR, "orders_state.json")
TRADE_LOG_FILE = os.path.join(SCRIPT_DIR, "trade_log.json")

# How often to recompute regime (8h bars) — in seconds
REGIME_RECOMPUTE_INTERVAL = 8 * 60 * 60  # 8 hours

# How often to refresh ATR from daily bars — in seconds
ATR_REFRESH_INTERVAL = 4 * 60 * 60  # 4 hours

ET = pytz.timezone("America/New_York")
CT = pytz.timezone("America/Chicago")

# Level strength
LEVEL_STRENGTH = {"CMM": 4, "PMM": 3, "CDM": 2, "PDM": 1}


# ─────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────
class EngineState:
    """Holds all runtime state for the engine."""
    def __init__(self):
        # Per-symbol state
        self.regime = {}        # {symbol: 'BUY'/'SELL'/'NEUTRAL'}
        self.regime_data = {}   # {symbol: full bias dict}
        self.atr = {}           # {symbol: ATR value}
        self.current_price = {} # {symbol: latest price}
        
        # Running mean accumulators (CDM)
        self.day_closes = defaultdict(list)    # {(symbol, date): [closes]}
        self.month_closes = defaultdict(list)  # {(symbol, year, month): [closes]}
        self.prev_day_mean = {}   # {symbol: PDM}
        self.prev_month_mean = {} # {symbol: PMM}
        
        # Current levels
        self.cdm = {}  # {symbol: current CDM}
        self.cmm = {}  # {symbol: current CMM}
        
        # Active orders
        self.pending_entry = {}   # {symbol: {order_id, entry_price, side, ...}}
        self.active_position = {} # {symbol: {entry_price, stop, target, ...}}
        
        # Timing
        self.last_regime_compute = None
        self.last_atr_refresh = None
        self.current_day = None
        self.current_month = None
        
        # Trade log
        self.dry_run = False


state = EngineState()


# ─────────────────────────────────────────────────────────────
# MEAN LEVEL COMPUTATION (running)
# ─────────────────────────────────────────────────────────────
def update_running_means(symbol: str, close: float, timestamp: datetime):
    """Update CDM and CMM with a new bar close. Called on every 5-min bar."""
    ct_time = timestamp.astimezone(CT)
    today = ct_time.date()
    this_month = (ct_time.year, ct_time.month)
    
    # Day roll detection
    day_key = (symbol, today)
    if state.current_day and state.current_day != today:
        # New day — save yesterday's CDM as PDM
        prev_day_key = (symbol, state.current_day)
        if prev_day_key in state.day_closes and state.day_closes[prev_day_key]:
            closes = state.day_closes[prev_day_key]
            state.prev_day_mean[symbol] = sum(closes) / len(closes)
    
    # Month roll detection
    if state.current_month and state.current_month != this_month:
        # New month — save last month's CMM as PMM
        prev_month_key = (symbol, *state.current_month)
        if prev_month_key in state.month_closes and state.month_closes[prev_month_key]:
            closes = state.month_closes[prev_month_key]
            state.prev_month_mean[symbol] = sum(closes) / len(closes)
        # Clear day closes for new month
        state.day_closes[day_key] = []
    
    state.current_day = today
    state.current_month = this_month
    
    # Accumulate
    state.day_closes[day_key].append(close)
    month_key = (symbol, *this_month)
    state.month_closes[month_key].append(close)
    
    # Compute running means
    day_closes = state.day_closes[day_key]
    state.cdm[symbol] = sum(day_closes) / len(day_closes)
    
    month_closes = state.month_closes[month_key]
    state.cmm[symbol] = sum(month_closes) / len(month_closes)


def get_gate_level(symbol: str) -> tuple:
    """Get the structure gate level — PMM for days 1-3, CMM after."""
    ct_now = datetime.now(CT)
    if ct_now.day <= 3 and symbol in state.prev_month_mean:
        return state.prev_month_mean[symbol], "PMM"
    return state.cmm.get(symbol), "CMM"


def get_closest_entry_level(symbol: str, mode: str, price: float, tick_size: float) -> tuple:
    """Get the closest eligible mean level for entry."""
    levels = {
        'CDM': state.cdm.get(symbol),
        'PDM': state.prev_day_mean.get(symbol),
        'CMM': state.cmm.get(symbol),
        'PMM': state.prev_month_mean.get(symbol),
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
        return None, None, None
    
    candidates.sort()
    dist, name, level = candidates[0]
    # Round to tick
    entry = round(round(level / tick_size) * tick_size, 6)
    return entry, name, dist


# ─────────────────────────────────────────────────────────────
# ORDER MANAGEMENT
# ─────────────────────────────────────────────────────────────
async def place_or_update_entry(client, account, symbol: str, contract_id: str,
                                 side: int, entry_price: float, tick_size: float):
    """Place a new entry limit or move existing one to track CDM."""
    import aiohttp
    
    token = client.get_session_token()
    base_url = client.base_url
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    
    existing = state.pending_entry.get(symbol)
    
    if existing:
        # Check if CDM moved enough to warrant moving the order
        old_price = existing['entry_price']
        if abs(entry_price - old_price) < tick_size * 2:
            return  # CDM hasn't moved enough, keep current order
        
        # Cancel old order
        async with aiohttp.ClientSession() as http:
            async with http.post(f'{base_url}/Order/cancel',
                                 json={'orderId': existing['order_id'], 'accountId': account.id},
                                 headers=headers) as resp:
                res = await resp.json()
                if res.get('success'):
                    print(f"  [{symbol}] Cancelled old entry @ {old_price}")
    
    if state.dry_run:
        print(f"  [{symbol}] DRY RUN: Would place {'BUY' if side==0 else 'SELL'} LIMIT @ {entry_price}")
        state.pending_entry[symbol] = {
            'order_id': 'DRY', 'entry_price': entry_price, 'side': side,
            'contract_id': contract_id,
        }
        return
    
    # Place new entry
    async with aiohttp.ClientSession() as http:
        payload = {
            'accountId': account.id, 'contractId': contract_id,
            'type': 1, 'side': side, 'size': 1, 'limitPrice': entry_price,
        }
        async with http.post(f'{base_url}/Order/place', json=payload, headers=headers) as resp:
            res = await resp.json()
            if res.get('success'):
                oid = res.get('orderId')
                state.pending_entry[symbol] = {
                    'order_id': oid, 'entry_price': entry_price, 'side': side,
                    'contract_id': contract_id,
                }
                print(f"  [{symbol}] Entry {'moved' if existing else 'placed'}: "
                      f"{'BUY' if side==0 else 'SELL'} LIMIT @ {entry_price}")
            else:
                print(f"  [{symbol}] Entry failed: {res}")


async def check_and_bracket(client, account, symbol: str):
    """Check if entry filled, place bracket if so."""
    import aiohttp
    
    pending = state.pending_entry.get(symbol)
    if not pending or pending.get('order_id') == 'DRY':
        return
    
    # Check open positions
    token = client.get_session_token()
    base_url = client.base_url
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    
    async with aiohttp.ClientSession() as http:
        async with http.post(f'{base_url}/Position/searchOpen',
                             json={'accountId': account.id},
                             headers=headers) as resp:
            data = await resp.json()
    
    # Check if this symbol has a position
    for p in data.get('positions', []):
        cid = p.get('contractId', '')
        parts = cid.split('.')
        sym = parts[3] if len(parts) >= 4 else ''
        if sym == symbol and symbol not in state.active_position:
            # FILLED! Place bracket immediately
            entry_price = pending['entry_price']
            side = pending['side']
            stop_side = 1 if side == 0 else 0
            atr = state.atr.get(symbol, 100)
            stop_dist = atr * DEFAULT_ATR_MULTIPLIER
            tick_size = 0.25 if symbol in ('MES', 'MNQ') else 1.0
            
            if side == 0:  # BUY
                stop = round(round((entry_price - stop_dist) / tick_size) * tick_size, 6)
                target = round(round((entry_price + stop_dist * DEFAULT_RR_RATIO) / tick_size) * tick_size, 6)
            else:  # SELL
                stop = round(round((entry_price + stop_dist) / tick_size) * tick_size, 6)
                target = round(round((entry_price - stop_dist * DEFAULT_RR_RATIO) / tick_size) * tick_size, 6)
            
            print(f"\n  *** [{symbol}] ENTRY FILLED @ {entry_price} ***")
            print(f"  Placing bracket: stop @ {stop}, target @ {target}")
            
            if not state.dry_run:
                async with aiohttp.ClientSession() as http:
                    # Stop
                    r = await (await http.post(f'{base_url}/Order/place',
                        json={'accountId': account.id, 'contractId': pending['contract_id'],
                              'type': 4, 'side': stop_side, 'size': 1, 'stopPrice': stop},
                        headers=headers)).json()
                    stop_id = r.get('orderId') if r.get('success') else None
                    
                    # Target
                    r = await (await http.post(f'{base_url}/Order/place',
                        json={'accountId': account.id, 'contractId': pending['contract_id'],
                              'type': 1, 'side': stop_side, 'size': 1, 'limitPrice': target},
                        headers=headers)).json()
                    target_id = r.get('orderId') if r.get('success') else None
                    
                    print(f"  STOP: {stop_id} @ {stop}")
                    print(f"  TARGET: {target_id} @ {target}")
            
            state.active_position[symbol] = {
                'entry': entry_price, 'stop': stop, 'target': target,
                'side': side, 'contract_id': pending['contract_id'],
            }
            del state.pending_entry[symbol]
            return


# ─────────────────────────────────────────────────────────────
# REGIME COMPUTATION
# ─────────────────────────────────────────────────────────────
async def compute_regime(client):
    """Compute DSS regime for all symbols using 8h bars. Run periodically."""
    print(f"\n{'='*60}")
    print(f"  REGIME RECOMPUTE — {datetime.now(ET).strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"{'='*60}")
    
    # Load prior latched state
    prior_regime = {}
    if os.path.exists(REGIME_FILE):
        with open(REGIME_FILE, "r") as f:
            prior_regime = json.load(f).get("instruments", {})
    
    # Month-boundary reset
    ct_now = datetime.now(CT)
    if ct_now.day == 1:
        for sym_key in list(prior_regime.keys()):
            old = prior_regime[sym_key].get('regime', 'NEUTRAL')
            if old != 'NEUTRAL':
                prior_regime[sym_key]['regime'] = None
                print(f"  MONTH RESET: {sym_key} latch cleared (was {old})")
    
    for symbol in SYMBOLS:
        try:
            instrument = await client.get_instrument(symbol)
            contract_id = instrument.id
            
            # Fetch 8h bars
            bars_8h = await client.get_bars(symbol, days=60, interval=8, unit=3)
            if bars_8h.is_empty() or len(bars_8h) < 20:
                print(f"  [{symbol}] Not enough 8h bars")
                continue
            
            highs = bars_8h["high"].to_list()
            lows = bars_8h["low"].to_list()
            closes = bars_8h["close"].to_list()
            
            prior_sym = prior_regime.get(symbol, {})
            latched_mode = prior_sym.get("regime")
            
            bias = compute_trend_bias(highs, lows, closes, latched_mode=latched_mode)
            
            if bias["bias"] == "BULLISH":
                mode = "BUY"
            elif bias["bias"] == "BEARISH":
                mode = "SELL"
            else:
                mode = "NEUTRAL"
            
            state.regime[symbol] = mode
            state.regime_data[symbol] = bias
            
            latch_tag = " [LATCHED]" if bias.get("latched") else ""
            if bias.get("trigger") == "new_trigger":
                latch_tag = " [NEW TRIGGER]"
            
            print(f"  [{symbol}] {mode}{latch_tag} (DSS={bias['dss_signal']:+d})")
            
            # Save regime state
            prior_regime[symbol] = {
                "mode": mode, "regime": bias["bias"],
                "strength": bias["strength"],
                "dss_signal": bias["dss_signal"],
                "latched": bias.get("latched", False),
                "trigger": bias.get("trigger", "no_latch"),
                "computed_at": datetime.now(ET).isoformat(),
            }
            
            # Also refresh ATR
            daily_bars = await client.get_bars(symbol, days=35, interval=1, unit=4)
            if not daily_bars.is_empty() and len(daily_bars) >= 15:
                state.atr[symbol] = compute_atr(daily_bars, period=14)
                print(f"  [{symbol}] ATR(14d): {state.atr[symbol]:.2f}")
            
        except Exception as e:
            print(f"  [{symbol}] Error: {e}")
    
    # Save regime file
    with open(REGIME_FILE, "w") as f:
        json.dump({"instruments": prior_regime, "last_updated": datetime.now(ET).isoformat()}, f, indent=2)
    
    state.last_regime_compute = datetime.now()


# ─────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────
async def on_new_bar(symbol: str, bar_data: dict, client, account):
    """Called on every new 5-min bar close. Core execution logic."""
    close = bar_data['close']
    timestamp = bar_data.get('timestamp', datetime.now(CT))
    tick_size = 0.25 if symbol in ('MES', 'MNQ') else 1.0
    
    state.current_price[symbol] = close
    
    # Update running means
    update_running_means(symbol, close, timestamp)
    
    cdm = state.cdm.get(symbol)
    mode = state.regime.get(symbol, 'NEUTRAL')
    
    if not cdm or mode == 'NEUTRAL':
        return
    
    # Time filter: no orders before 10 PM ET on new day
    et_now = datetime.now(ET)
    if 17 <= et_now.hour < 22:
        return  # Day roll window, CDM resetting
    
    # Friday after 6 PM / Saturday — markets closing
    if (et_now.weekday() == 4 and et_now.hour >= 18) or et_now.weekday() == 5:
        return
    
    # Sunday before 10 PM
    if et_now.weekday() == 6 and et_now.hour < 22:
        return
    
    # Structure gate (CMM or PMM)
    gate_level, gate_name = get_gate_level(symbol)
    if gate_level is not None:
        if mode == 'BUY' and close < gate_level:
            return  # Below gate, skip
        elif mode == 'SELL' and close > gate_level:
            return  # Above gate, skip
    
    # Skip if already have active position in this symbol
    if symbol in state.active_position:
        # Check if position is still open
        await check_and_bracket(client, account, symbol)
        return
    
    # Get closest entry level
    entry_price, level_name, dist = get_closest_entry_level(symbol, mode, close, tick_size)
    if not entry_price:
        return
    
    # Get instrument info
    try:
        instrument = await client.get_instrument(symbol)
        contract_id = instrument.id
    except:
        return
    
    side = 0 if mode == 'BUY' else 1
    
    # Place or update the entry limit to track CDM
    await place_or_update_entry(client, account, symbol, contract_id, side, entry_price, tick_size)
    
    # Check for fills
    await check_and_bracket(client, account, symbol)


async def main(dry_run: bool = False):
    """Main entry point — initialize and run the real-time engine."""
    from project_x_py import ProjectX, TradingSuite, EventType
    
    state.dry_run = dry_run
    mode_str = "DRY RUN" if dry_run else "LIVE"
    
    print(f"""
╔══════════════════════════════════════════════════════════╗
║  Tzu Strategic Momentum — Real-Time Execution Engine    ║
║  Mode: {mode_str:<50}║
║  Instruments: {', '.join(SYMBOLS):<43}║
║  Stops: {DEFAULT_ATR_MULTIPLIER}x ATR | R:R 1:{DEFAULT_RR_RATIO}                              ║
╚══════════════════════════════════════════════════════════╝
""")
    
    try:
        async with ProjectX.from_env() as client:
            await client.authenticate()
            account = client.get_account_info()
            
            print(f"  Account: {account.name}")
            print(f"  Balance: ${account.balance:,.2f}")
            
            # Initial regime computation
            await compute_regime(client)
            
            # Initialize TradingSuite for real-time streaming
            suite = TradingSuite(client, symbols=SYMBOLS, timeframes=["5min"])
            
            # Register bar callback
            async def bar_callback(event):
                """Handle new 5-min bar events."""
                try:
                    symbol = event.get('symbol', '')
                    bar = event.get('bar', {})
                    if symbol in SYMBOLS and bar:
                        close = bar.get('close', bar.get('c'))
                        if close:
                            bar_data = {
                                'close': close,
                                'high': bar.get('high', bar.get('h', close)),
                                'low': bar.get('low', bar.get('l', close)),
                                'open': bar.get('open', bar.get('o', close)),
                                'timestamp': datetime.now(CT),
                            }
                            await on_new_bar(symbol, bar_data, client, account)
                except Exception as e:
                    print(f"  Bar callback error: {e}")
            
            await suite.on(EventType.NEW_BAR, bar_callback)
            await suite.connect()
            
            print(f"\n  📡 STREAMING LIVE — watching {', '.join(SYMBOLS)}")
            print(f"  CDM updates on every 5-min bar close")
            print(f"  Press Ctrl+C to stop\n")
            
            # Main loop — keep alive + periodic regime recompute
            while True:
                await asyncio.sleep(60)
                
                # Recompute regime every 8 hours
                if (state.last_regime_compute is None or 
                    (datetime.now() - state.last_regime_compute).seconds > REGIME_RECOMPUTE_INTERVAL):
                    await compute_regime(client)
                
                # Periodic status
                now = datetime.now(ET)
                if now.minute == 0:  # Print status every hour
                    print(f"\n  [{now.strftime('%H:%M')}] Status:")
                    for sym in SYMBOLS:
                        cdm = state.cdm.get(sym)
                        price = state.current_price.get(sym)
                        mode = state.regime.get(sym, '?')
                        pending = 'ENTRY' if sym in state.pending_entry else ''
                        active = 'POSITION' if sym in state.active_position else ''
                        print(f"    {sym}: {mode} | price={price} cdm={cdm:.2f if cdm else '?'} {pending} {active}")
    
    except KeyboardInterrupt:
        print(f"\n\n  Shutting down...")
    except Exception as e:
        print(f"\n  Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print(f"  Engine stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tzu Strategic Momentum — Real-Time Engine")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without placing orders")
    args = parser.parse_args()
    
    asyncio.run(main(dry_run=args.dry_run))
