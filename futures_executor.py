"""
Mean Levels Futures Executor for TopstepX — 50K Combine Edition
================================================================
Places LIMIT orders at mean levels (CDM, PDM, CMM, PMM) as support/
resistance entries, guided by the 8h DSS+Lyapunov regime filter.

** EXECUTION MODEL **

  The regime filter (8h bars) determines BUY or SELL mode:
    - BUY MODE:  mean levels below price = support → place BUY LIMIT orders
    - SELL MODE: mean levels above price = resistance → place SELL LIMIT orders
    - NEUTRAL:   no trades

  For each symbol, we place limit entries at the mean levels with:
    - Entry: LIMIT order at the mean level price
    - Stop:  1x Daily ATR behind the entry
    - Target: 2x ATR in the trade direction (1:2 R:R)

  Level priority (strongest first): CMM, PMM (monthly) > CDM, PDM (daily)

** RISK CONTROLS **

  - 1 contract per symbol (4 symbols × 1 lot = 4 max)
  - $400 max risk per trade hard cap
  - ATR-based stops (1x ATR on daily bars)
  - Daily profit cap: $1,400 (consistency rule)
  - MLL gate: skip if potential loss would breach drawdown floor
  - Order management: cancel/replace stale limits each scan cycle

** ORDER MANAGEMENT **

  Each scan cycle:
    1. Cancel any pending limit orders from prior scans (levels shift)
    2. Re-place limits at the updated mean levels
    3. Skip symbols that already have a filled position
    4. Track all pending/filled orders in orders_state.json

Usage:
    python futures_executor.py                        # Dry run
    python futures_executor.py --live                  # Place real orders
    python futures_executor.py --min-score 5           # Higher conviction only

Author: Matthew Foster
"""

import asyncio
import argparse
import json
import os
import sys
from datetime import datetime, date
from typing import Optional

import pytz
from dotenv import load_dotenv

# Load .env from script directory
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from futures_scanner import run_scanner, INSTRUMENT_SPECS, DEFAULT_SYMBOLS

# ─────────────────────────────────────────────────────────────
# COMBINE PARAMETERS (50K Trading Combine)
# ─────────────────────────────────────────────────────────────
COMBINE_STARTING_BALANCE = 50_000
COMBINE_PROFIT_TARGET = 3_000       # $3,000 to pass
COMBINE_MLL = 2_000                 # $2,000 max loss limit (EOD trailing)
COMBINE_MAX_LOTS = 5                # Micros count as FULL lots
COMBINE_CONSISTENCY_PCT = 50        # Best day must be < 50% of total profits
COMBINE_CONSISTENCY_CAP = 1_500     # Hard cap = 50% of $3,000
DAILY_PROFIT_CAP = 1_400            # Auto-stop at $1,400/day

# ─────────────────────────────────────────────────────────────
# TRADING SESSIONS (all times ET)
# ─────────────────────────────────────────────────────────────
TRADING_WINDOWS = {
    "morning_open": {"start": (9, 30), "end": (11, 30), "label": "Morning Open", "min_score": 5},
    "midday":       {"start": (11, 30), "end": (14, 0),  "label": "Midday",       "min_score": 6},
    "power_hour":   {"start": (14, 0),  "end": (15, 30), "label": "Power Hour",   "min_score": 5},
}
FLATTEN_DEADLINE_ET = (15, 50)
TOPSTEP_HARD_CUTOFF_ET = (16, 10)

# ─────────────────────────────────────────────────────────────
# TRADE DEFAULTS
# ─────────────────────────────────────────────────────────────
DEFAULT_RISK_PCT = 2.0
DEFAULT_MIN_SCORE = 5
DEFAULT_RR_RATIO = 2.0
DEFAULT_ATR_MULTIPLIER = 1.0
MAX_RISK_PER_TRADE = 400
MAX_CONTRACTS_PER_SYMBOL = 1       # 1 limit order per symbol max
MAX_TOTAL_CONTRACTS = 4            # 4 symbols × 1 = 4 total

# Level strength ranking (monthly > daily)
LEVEL_STRENGTH = {
    "CMM": 4,  # Current Month Mean — strongest
    "PMM": 3,  # Previous Month Mean — strong
    "CDM": 2,  # Current Day Mean
    "PDM": 1,  # Previous Day Mean
}

# ─────────────────────────────────────────────────────────────
# TRACKING FILES
# ─────────────────────────────────────────────────────────────
TRACKING_DIR = os.path.dirname(os.path.abspath(__file__))
TRACKING_FILE = os.path.join(TRACKING_DIR, "combine_tracker.json")
ORDERS_FILE = os.path.join(TRACKING_DIR, "orders_state.json")


def is_in_trading_window(now_et: datetime = None) -> tuple[bool, str, int]:
    if now_et is None:
        now_et = datetime.now(pytz.timezone("America/New_York"))
    current_minutes = now_et.hour * 60 + now_et.minute
    for key, window in TRADING_WINDOWS.items():
        start_min = window["start"][0] * 60 + window["start"][1]
        end_min = window["end"][0] * 60 + window["end"][1]
        if start_min <= current_minutes <= end_min:
            return True, window["label"], window.get("min_score", 5)
    return False, "Outside trading windows", 5


def should_flatten_now(now_et: datetime = None) -> bool:
    if now_et is None:
        now_et = datetime.now(pytz.timezone("America/New_York"))
    flatten_min = FLATTEN_DEADLINE_ET[0] * 60 + FLATTEN_DEADLINE_ET[1]
    current_min = now_et.hour * 60 + now_et.minute
    return current_min >= flatten_min


def minutes_until_flatten(now_et: datetime = None) -> int:
    if now_et is None:
        now_et = datetime.now(pytz.timezone("America/New_York"))
    flatten_min = FLATTEN_DEADLINE_ET[0] * 60 + FLATTEN_DEADLINE_ET[1]
    current_min = now_et.hour * 60 + now_et.minute
    return flatten_min - current_min


# ─────────────────────────────────────────────────────────────
# COMBINE TRACKER
# ─────────────────────────────────────────────────────────────

def load_combine_tracker() -> dict:
    if os.path.exists(TRACKING_FILE):
        with open(TRACKING_FILE, "r") as f:
            return json.load(f)
    return {
        "starting_balance": COMBINE_STARTING_BALANCE,
        "peak_eod_balance": COMBINE_STARTING_BALANCE,
        "current_mll": COMBINE_STARTING_BALANCE - COMBINE_MLL,
        "mll_locked": False,
        "total_realized_pnl": 0.0,
        "daily_pnl": {},
        "best_day_pnl": 0.0,
        "trades_today": 0,
        "today_pnl": 0.0,
        "today_date": None,
        "total_lots_open": 0,
        "created": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat(),
    }


def save_combine_tracker(tracker: dict):
    tracker["last_updated"] = datetime.now().isoformat()
    with open(TRACKING_FILE, "w") as f:
        json.dump(tracker, f, indent=2)


def update_eod_balance(tracker: dict, current_balance: float) -> dict:
    if current_balance > tracker["peak_eod_balance"]:
        tracker["peak_eod_balance"] = current_balance
    new_mll = tracker["peak_eod_balance"] - COMBINE_MLL
    if new_mll >= COMBINE_STARTING_BALANCE:
        tracker["current_mll"] = COMBINE_STARTING_BALANCE
        tracker["mll_locked"] = True
    else:
        tracker["current_mll"] = new_mll
    return tracker


async def get_daily_pnl(client) -> float:
    import aiohttp
    try:
        account = client.get_account_info()
        token = client.get_session_token()
        base_url = client.base_url
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f'{base_url}/Trade/search',
                json={'accountId': account.id},
                headers=headers
            ) as resp:
                result = await resp.json()
                trades = result.get('trades', [])
        daily_pnl = 0.0
        for t in trades:
            pnl = t.get('profitAndLoss')
            fees = t.get('fees', 0) or 0
            if pnl is not None:
                daily_pnl += (pnl - fees)
        return round(daily_pnl, 2)
    except Exception as e:
        print(f"  WARNING: Could not fetch daily P&L: {e}")
        try:
            return round(client.get_account_info().balance - COMBINE_STARTING_BALANCE, 2)
        except Exception:
            return 0.0


# ─────────────────────────────────────────────────────────────
# ORDER STATE MANAGEMENT
# ─────────────────────────────────────────────────────────────

def load_orders_state() -> dict:
    """Load tracked pending/filled orders."""
    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r") as f:
            return json.load(f)
    return {"pending_limits": {}, "filled_positions": {}, "last_updated": None}


def save_orders_state(state: dict):
    state["last_updated"] = datetime.now().isoformat()
    with open(ORDERS_FILE, "w") as f:
        json.dump(state, f, indent=2)


async def cancel_pending_limits(client, orders_state: dict) -> int:
    """
    Cancel all pending limit orders from prior scans.
    Mean levels shift each bar, so stale limits need refreshing.
    Returns count of successfully cancelled orders.
    """
    import aiohttp

    cancelled = 0
    pending = orders_state.get("pending_limits", {})

    if not pending:
        return 0

    token = client.get_session_token()
    base_url = client.base_url
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }

    async with aiohttp.ClientSession() as http:
        for key, order_info in list(pending.items()):
            order_id = order_info.get("entry_order_id")
            if not order_id:
                del pending[key]
                continue

            try:
                # Cancel the entry limit order
                async with http.post(
                    f'{base_url}/Order/cancel',
                    json={'orderId': order_id},
                    headers=headers
                ) as resp:
                    result = await resp.json()
                    if result.get('success'):
                        print(f"    Cancelled limit: {key} (Order {order_id})")
                        cancelled += 1
                    else:
                        # Order may have been filled or already cancelled
                        print(f"    Cancel failed for {key}: {result.get('message', 'unknown')}")

                # Also cancel associated stop/target if they exist
                for otype in ["stop_order_id", "target_order_id"]:
                    oid = order_info.get(otype)
                    if oid:
                        try:
                            async with http.post(
                                f'{base_url}/Order/cancel',
                                json={'orderId': oid},
                                headers=headers
                            ) as resp:
                                await resp.json()
                        except Exception:
                            pass

                del pending[key]

            except Exception as e:
                print(f"    Error cancelling {key}: {e}")

    return cancelled


# ─────────────────────────────────────────────────────────────
# CORE: COMPUTE LIMIT ORDER PARAMS AT MEAN LEVELS
# ─────────────────────────────────────────────────────────────

def compute_limit_orders_for_symbol(
    symbol: str,
    current_price: float,
    levels: dict,
    regime: dict,
    tick_size: float,
    tick_value: float,
    atr: float,
    account_balance: float,
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER,
    rr_ratio: float = DEFAULT_RR_RATIO,
) -> list[dict]:
    """
    Compute limit order parameters at mean levels for one symbol.

    In BUY MODE:  levels below price → BUY LIMIT (support bounce)
    In SELL MODE: levels above price → SELL LIMIT (resistance rejection)

    Args:
        symbol: Instrument symbol
        current_price: Current market price
        levels: dict with 'cdm', 'pdm', 'cmm', 'pmm'
        regime: Cached regime from 8h filter
        tick_size: Instrument tick size
        tick_value: Dollar value per tick
        atr: Daily ATR(14) value
        account_balance: Current account balance
        atr_multiplier: Multiplier for ATR stop distance
        rr_ratio: Risk:Reward ratio

    Returns:
        List of order parameter dicts, sorted by level strength (strongest first)
    """
    mode = regime.get("mode", "NEUTRAL") if regime else "NEUTRAL"

    if mode == "NEUTRAL":
        return []

    # ATR-based stop distance
    if atr <= 0:
        stop_distance = current_price * 0.001
    else:
        stop_distance = atr * atr_multiplier

    min_stop = tick_size * 4
    stop_distance = max(stop_distance, min_stop)

    # Check dollar risk for 1 contract
    ticks_to_stop = stop_distance / tick_size
    dollar_risk = ticks_to_stop * tick_value * 1  # always 1 contract
    dollar_target = ticks_to_stop * rr_ratio * tick_value * 1

    # Hard cap on risk
    if dollar_risk > MAX_RISK_PER_TRADE:
        print(f"  [{symbol}] ATR risk ${dollar_risk:.0f} exceeds ${MAX_RISK_PER_TRADE} cap — skipping")
        return []

    orders = []

    # Build list of mean levels to place orders at
    level_map = {
        "CDM": levels.get("cdm"),
        "PDM": levels.get("pdm"),
        "CMM": levels.get("cmm"),
        "PMM": levels.get("pmm"),
    }

    for level_name, level_price in level_map.items():
        if level_price is None:
            continue

        # Determine if this level is actionable given the regime
        if mode == "BUY":
            # BUY MODE: place BUY LIMIT at levels BELOW current price (support)
            if level_price >= current_price:
                continue  # Level is above price — not support in buy mode

            side = 0  # Buy
            entry_price = level_price
            stop_price = entry_price - stop_distance
            target_price = entry_price + (stop_distance * rr_ratio)

        elif mode == "SELL":
            # SELL MODE: place SELL LIMIT at levels ABOVE current price (resistance)
            if level_price <= current_price:
                continue  # Level is below price — not resistance in sell mode

            side = 1  # Sell
            entry_price = level_price
            stop_price = entry_price + stop_distance
            target_price = entry_price - (stop_distance * rr_ratio)

        else:
            continue

        # Align to tick size
        entry_price = round(round(entry_price / tick_size) * tick_size, 6)
        stop_price = round(round(stop_price / tick_size) * tick_size, 6)
        target_price = round(round(target_price / tick_size) * tick_size, 6)

        # Distance from current price to the level
        distance_pts = abs(current_price - entry_price)
        distance_pct = round(distance_pts / current_price * 100, 3)

        orders.append({
            "symbol": symbol,
            "level_name": level_name,
            "level_strength": LEVEL_STRENGTH.get(level_name, 0),
            "side": side,
            "side_str": "BUY" if side == 0 else "SELL",
            "size": 1,  # Always 1 contract
            "entry_price": entry_price,
            "stop_price": stop_price,
            "target_price": target_price,
            "stop_distance": round(stop_distance, 4),
            "stop_type": f"ATR({atr_multiplier}x) = {atr:.2f}",
            "dollar_risk": round(dollar_risk, 2),
            "dollar_target": round(dollar_target, 2),
            "rr_ratio": rr_ratio,
            "distance_from_price": round(distance_pts, 4),
            "distance_pct": distance_pct,
            "mode": mode,
        })

    # Sort by level strength (monthly first)
    orders.sort(key=lambda x: x["level_strength"], reverse=True)

    return orders


# ─────────────────────────────────────────────────────────────
# EXECUTE: PLACE LIMIT ORDERS
# ─────────────────────────────────────────────────────────────

async def execute_limit_orders(
    scan_results: list[dict],
    live: bool = False,
    rr_ratio: float = DEFAULT_RR_RATIO,
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER,
) -> list[dict]:
    """
    Place limit orders at mean levels based on 8h regime.

    Each scan cycle:
      1. Cancel stale pending limits from prior scans
      2. Check which symbols already have filled positions
      3. Place new limit orders at updated mean levels
      4. Attach stop + target for each limit

    Returns:
        List of order results
    """
    tz = pytz.timezone("America/New_York")
    now_et = datetime.now(tz)
    mode_label = "LIVE" if live else "DRY RUN"
    tracker = load_combine_tracker()
    orders_state = load_orders_state()

    print(f"\n{'='*65}")
    print(f"  MEAN LEVELS LIMIT EXECUTOR [{mode_label}] — 50K COMBINE")
    print(f"  {now_et.strftime('%Y-%m-%d %H:%M:%S ET')}")
    print(f"  Stops: {atr_multiplier}x ATR (daily) | R:R = 1:{rr_ratio}")
    print(f"  Max: {MAX_CONTRACTS_PER_SYMBOL} contract/symbol, {MAX_TOTAL_CONTRACTS} total")
    print(f"{'='*65}")

    # ── Session Gate ──
    in_window, window_label, window_min_score = is_in_trading_window(now_et)
    mins_to_flatten = minutes_until_flatten(now_et)

    print(f"\n  ── SESSION ──")
    print(f"  Window: {window_label}")
    print(f"  Flatten deadline: {FLATTEN_DEADLINE_ET[0]}:{FLATTEN_DEADLINE_ET[1]:02d} ET ({mins_to_flatten} min)")

    if should_flatten_now(now_et):
        print(f"  PAST FLATTEN DEADLINE — cancelling all pending, no new orders.")
        if live:
            from project_x_py import ProjectX
            async with ProjectX.from_env() as client:
                await client.authenticate()
                cancelled = await cancel_pending_limits(client, orders_state)
                if cancelled:
                    print(f"  Cancelled {cancelled} stale limit(s).")
                    orders_state["pending_limits"] = {}
                    save_orders_state(orders_state)
        return []

    if not in_window and live:
        print(f"  OUTSIDE TRADING WINDOWS — no new orders in live mode.")
        return []

    if mins_to_flatten < 30 and live:
        print(f"  Less than 30 min to flatten — too close for new limit orders.")
        return []

    # ── Combine Status ──
    print(f"\n  ── COMBINE STATUS ──")
    print(f"  MLL Floor:       ${tracker['current_mll']:,.0f} "
          f"{'(LOCKED)' if tracker['mll_locked'] else '(trailing)'}")
    print(f"  Profit Target:   ${COMBINE_PROFIT_TARGET:,.0f}")

    all_orders = []

    if live:
        from project_x_py import ProjectX
        import aiohttp

        async with ProjectX.from_env() as client:
            await client.authenticate()
            account = client.get_account_info()
            balance = account.balance

            tracker = update_eod_balance(tracker, balance)
            save_combine_tracker(tracker)

            daily_pnl = await get_daily_pnl(client)
            print(f"\n  Account: {account.name} | Balance: ${balance:,.2f}")
            print(f"  Daily P&L: ${daily_pnl:+,.2f} (cap: ${DAILY_PROFIT_CAP:,})")
            print(f"  Cushion above MLL: ${balance - tracker['current_mll']:,.2f}")

            # Hard stop: daily profit cap
            if daily_pnl >= DAILY_PROFIT_CAP:
                print(f"\n  DAILY PROFIT CAP REACHED — no new orders today.")
                return []

            # ── Step 1: Cancel stale pending limits ──
            print(f"\n  ── CANCEL STALE LIMITS ──")
            cancelled = await cancel_pending_limits(client, orders_state)
            if cancelled:
                print(f"  Cancelled {cancelled} stale limit(s).")
            else:
                print(f"  No stale limits to cancel.")
            orders_state["pending_limits"] = {}

            # ── Step 2: Check existing positions ──
            existing_positions = await client.search_open_positions()
            open_lots = sum(getattr(p, 'size', 1) for p in existing_positions)
            symbols_with_positions = set()
            for p in existing_positions:
                cid = getattr(p, 'contractId', '')
                parts = cid.split('.')
                sym = parts[3] if len(parts) >= 4 else cid
                symbols_with_positions.add(sym)

            if symbols_with_positions:
                print(f"\n  Already positioned in: {', '.join(symbols_with_positions)}")

            # ── Step 3: Place new limit orders ──
            print(f"\n  ── PLACING LIMIT ORDERS ──")

            token = client.get_session_token()
            base_url = client.base_url
            api_headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            }

            for result in scan_results:
                symbol = result["symbol"]
                regime = result.get("regime")

                if not regime:
                    print(f"\n  [{symbol}] No regime — skipping")
                    continue

                mode = regime.get("mode", "NEUTRAL")
                if mode == "NEUTRAL":
                    print(f"\n  [{symbol}] Regime NEUTRAL — no orders")
                    continue

                if symbol in symbols_with_positions:
                    print(f"\n  [{symbol}] Already has position — skipping")
                    continue

                # Compute limit orders at mean levels
                limit_orders = compute_limit_orders_for_symbol(
                    symbol=symbol,
                    current_price=result["current_price"],
                    levels={"cdm": result["cdm"], "pdm": result["pdm"],
                            "cmm": result["cmm"], "pmm": result["pmm"]},
                    regime=regime,
                    tick_size=result["tick_size"],
                    tick_value=result["tick_value"],
                    atr=result.get("atr", 0),
                    account_balance=balance,
                    atr_multiplier=atr_multiplier,
                    rr_ratio=rr_ratio,
                )

                if not limit_orders:
                    print(f"\n  [{symbol}] No eligible levels for {mode} mode")
                    continue

                # Pick the strongest level (1 order per symbol)
                best = limit_orders[0]

                # MLL gate
                balance_after_loss = balance - best["dollar_risk"]
                mll_floor = tracker["current_mll"]
                if balance_after_loss < mll_floor:
                    print(f"\n  [{symbol}] MLL BLOCK — loss would drop to "
                          f"${balance_after_loss:,.0f} < floor ${mll_floor:,.0f}")
                    continue

                # Total lots check
                if open_lots >= MAX_TOTAL_CONTRACTS:
                    print(f"\n  [{symbol}] At max contracts ({open_lots}/{MAX_TOTAL_CONTRACTS})")
                    continue

                mode_icon = "▲" if best["side"] == 0 else "▼"
                print(f"\n  [{symbol}] {mode_icon} {best['side_str']} LIMIT 1 contract "
                      f"@ {best['entry_price']} | {best['level_name']} ({mode} MODE)")
                print(f"    Stop:   {best['stop_price']} ({best['stop_type']}) — "
                      f"${best['dollar_risk']:.0f} risk")
                print(f"    Target: {best['target_price']} — ${best['dollar_target']:.0f}")
                print(f"    R:R = 1:{best['rr_ratio']} | Dist from price: "
                      f"{best['distance_pct']}%")

                # Show other eligible levels
                if len(limit_orders) > 1:
                    others = [f"{o['level_name']}@{o['entry_price']}" for o in limit_orders[1:]]
                    print(f"    (Other eligible levels: {', '.join(others)})")

                # Place the limit entry
                try:
                    entry_id = None
                    stop_id = None
                    target_id = None

                    async with aiohttp.ClientSession() as http:
                        # 1. LIMIT entry order
                        entry_payload = {
                            'accountId': account.id,
                            'contractId': result['contract_id'],
                            'type': 1,  # Limit order
                            'side': best['side'],
                            'size': 1,
                            'limitPrice': best['entry_price'],
                        }
                        async with http.post(
                            f'{base_url}/Order/place',
                            json=entry_payload,
                            headers=api_headers
                        ) as resp:
                            res = await resp.json()
                            if res.get('success'):
                                entry_id = res.get('orderId')
                                print(f"    LIMIT PLACED — Order ID: {entry_id}")
                            else:
                                raise Exception(f"Limit failed: {res}")

                        # 2. STOP LOSS (opposite side, stop order)
                        stop_side = 1 if best['side'] == 0 else 0
                        stop_payload = {
                            'accountId': account.id,
                            'contractId': result['contract_id'],
                            'type': 4,  # Stop order
                            'side': stop_side,
                            'size': 1,
                            'stopPrice': best['stop_price'],
                        }
                        async with http.post(
                            f'{base_url}/Order/place',
                            json=stop_payload,
                            headers=api_headers
                        ) as resp:
                            res = await resp.json()
                            if res.get('success'):
                                stop_id = res.get('orderId')
                                print(f"    STOP PLACED — Order ID: {stop_id} "
                                      f"@ {best['stop_price']}")
                            else:
                                print(f"    Stop failed: {res}")

                        # 3. TAKE PROFIT (limit order, opposite side)
                        tp_payload = {
                            'accountId': account.id,
                            'contractId': result['contract_id'],
                            'type': 1,  # Limit
                            'side': stop_side,
                            'size': 1,
                            'limitPrice': best['target_price'],
                        }
                        async with http.post(
                            f'{base_url}/Order/place',
                            json=tp_payload,
                            headers=api_headers
                        ) as resp:
                            res = await resp.json()
                            if res.get('success'):
                                target_id = res.get('orderId')
                                print(f"    TARGET PLACED — Order ID: {target_id} "
                                      f"@ {best['target_price']}")
                            else:
                                print(f"    Target failed: {res}")

                    # Track the pending order
                    order_key = f"{symbol}_{best['level_name']}"
                    orders_state["pending_limits"][order_key] = {
                        "entry_order_id": entry_id,
                        "stop_order_id": stop_id,
                        "target_order_id": target_id,
                        **best,
                        "placed_at": now_et.isoformat(),
                    }

                    open_lots += 1
                    all_orders.append({
                        "symbol": symbol,
                        "status": "limit_placed",
                        "entry_order_id": entry_id,
                        "stop_order_id": stop_id,
                        "target_order_id": target_id,
                        **best,
                    })

                except Exception as e:
                    print(f"    ERROR: {e}")
                    all_orders.append({
                        "symbol": symbol,
                        "status": "error",
                        "error": str(e),
                        **best,
                    })

            save_orders_state(orders_state)

    else:
        # ── DRY RUN ──
        placeholder_balance = COMBINE_STARTING_BALANCE
        daily_pnl = 0.0
        open_lots = 0
        symbols_with_positions = set()

        try:
            from project_x_py import ProjectX
            async with ProjectX.from_env() as client:
                await client.authenticate()
                account = client.get_account_info()
                placeholder_balance = account.balance
                daily_pnl = await get_daily_pnl(client)
                existing_positions = await client.search_open_positions()
                open_lots = sum(getattr(p, 'size', 1) for p in existing_positions)
                for p in existing_positions:
                    cid = getattr(p, 'contractId', '')
                    parts = cid.split('.')
                    sym = parts[3] if len(parts) >= 4 else cid
                    symbols_with_positions.add(sym)
                print(f"\n  Account: {account.name} | Balance: ${placeholder_balance:,.2f}")
                print(f"  Daily P&L: ${daily_pnl:+,.2f} (cap: ${DAILY_PROFIT_CAP:,})")
                print(f"  Open lots: {open_lots}/{MAX_TOTAL_CONTRACTS}")
                print(f"  Cushion above MLL: ${placeholder_balance - tracker['current_mll']:,.2f}")
        except Exception:
            print(f"\n  (No credentials — using ${placeholder_balance:,.0f})")

        print(f"\n  ── LIMIT ORDER PLAN ──")

        for result in scan_results:
            symbol = result["symbol"]
            regime = result.get("regime")

            if not regime:
                print(f"\n  [{symbol}] No regime — would skip")
                continue

            mode = regime.get("mode", "NEUTRAL")
            if mode == "NEUTRAL":
                print(f"\n  [{symbol}] Regime NEUTRAL — no orders")
                continue

            if symbol in symbols_with_positions:
                print(f"\n  [{symbol}] Already has position — would skip")
                continue

            limit_orders = compute_limit_orders_for_symbol(
                symbol=symbol,
                current_price=result["current_price"],
                levels={"cdm": result["cdm"], "pdm": result["pdm"],
                        "cmm": result["cmm"], "pmm": result["pmm"]},
                regime=regime,
                tick_size=result["tick_size"],
                tick_value=result["tick_value"],
                atr=result.get("atr", 0),
                account_balance=placeholder_balance,
                atr_multiplier=atr_multiplier,
                rr_ratio=rr_ratio,
            )

            if not limit_orders:
                print(f"\n  [{symbol}] No eligible levels for {mode} mode")
                continue

            best = limit_orders[0]

            # MLL gate
            balance_after_loss = placeholder_balance - best["dollar_risk"]
            mll_floor = tracker["current_mll"]
            if balance_after_loss < mll_floor:
                print(f"\n  [{symbol}] MLL BLOCK — loss would drop to "
                      f"${balance_after_loss:,.0f} < floor ${mll_floor:,.0f}")
                continue

            mode_icon = "▲" if best["side"] == 0 else "▼"
            print(f"\n  [{symbol}] {mode_icon} {best['side_str']} LIMIT 1 @ "
                  f"{best['entry_price']} | {best['level_name']} ({mode} MODE)")
            print(f"    Stop:   {best['stop_price']} ({best['stop_type']}) — "
                  f"${best['dollar_risk']:.0f} risk")
            print(f"    Target: {best['target_price']} — ${best['dollar_target']:.0f}")
            print(f"    R:R = 1:{best['rr_ratio']} | Dist: {best['distance_pct']}%")

            if len(limit_orders) > 1:
                print(f"    Other eligible levels:")
                for o in limit_orders[1:]:
                    print(f"      {o['level_name']} @ {o['entry_price']} "
                          f"(dist: {o['distance_pct']}%)")

            all_orders.append({
                "symbol": symbol,
                "status": "dry_run",
                **best,
            })

    # ── Summary ──
    print(f"\n{'='*65}")
    placed = [o for o in all_orders if o["status"] == "limit_placed"]
    dry = [o for o in all_orders if o["status"] == "dry_run"]
    errors = [o for o in all_orders if o["status"] == "error"]

    if placed:
        total_risk = sum(o["dollar_risk"] for o in placed)
        print(f"  PLACED: {len(placed)} limit order(s) | Total risk: ${total_risk:,.0f}")
        for o in placed:
            print(f"    {o['symbol']} {o['side_str']} @ {o['entry_price']} "
                  f"({o['level_name']})")
    if dry:
        total_risk = sum(o["dollar_risk"] for o in dry)
        print(f"  DRY RUN: {len(dry)} limit(s) would be placed | Risk: ${total_risk:,.0f}")
        for o in dry:
            print(f"    {o['symbol']} {o['side_str']} @ {o['entry_price']} "
                  f"({o['level_name']})")
    if errors:
        print(f"  ERRORS: {len(errors)}")

    if not all_orders:
        print(f"  No orders — regime is NEUTRAL or all symbols positioned")

    print(f"{'='*65}\n")

    return all_orders


async def run_full_pipeline(
    symbols: list[str] = None,
    live: bool = False,
    rr_ratio: float = DEFAULT_RR_RATIO,
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER,
    days: int = 35,
    save_results: bool = True,
) -> dict:
    """Run the full scanner → limit executor pipeline."""

    # Step 1: Scan
    scan_results = await run_scanner(
        symbols=symbols or DEFAULT_SYMBOLS,
        min_score=0,
        days=days,
    )

    # Step 2: Place limit orders
    order_results = await execute_limit_orders(
        scan_results=scan_results,
        live=live,
        rr_ratio=rr_ratio,
        atr_multiplier=atr_multiplier,
    )

    output = {
        "timestamp": datetime.now(pytz.timezone("America/New_York")).isoformat(),
        "mode": "live" if live else "dry_run",
        "combine": "50K",
        "config": {
            "rr_ratio": rr_ratio,
            "atr_multiplier": atr_multiplier,
            "symbols": symbols or DEFAULT_SYMBOLS,
            "max_contracts_per_symbol": MAX_CONTRACTS_PER_SYMBOL,
            "max_total_contracts": MAX_TOTAL_CONTRACTS,
        },
        "scan_results": scan_results,
        "orders": order_results,
    }

    if save_results:
        results_dir = os.path.dirname(os.path.abspath(__file__))
        results_file = os.path.join(results_dir, "futures_scan_results.json")
        with open(results_file, "w") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"  Results saved to {results_file}\n")

    return output


def main():
    parser = argparse.ArgumentParser(
        description="Mean Levels Limit Executor — 50K Combine")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--rr-ratio", type=float, default=DEFAULT_RR_RATIO)
    parser.add_argument("--atr-mult", type=float, default=DEFAULT_ATR_MULTIPLIER)
    parser.add_argument("--days", type=int, default=35)
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    output = asyncio.run(run_full_pipeline(
        symbols=args.symbols,
        live=args.live,
        rr_ratio=args.rr_ratio,
        atr_multiplier=args.atr_mult,
        days=args.days,
    ))

    if args.json:
        print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
