"""
Regime Session — 8h Bar Regime Filter → Limit Orders at Mean Levels
====================================================================
Single unified pipeline that runs 3x/day at each 8h bar close:
  - 2:00 AM ET  (overnight bar closes)
  - 10:00 AM ET (morning bar closes)
  - 6:00 PM ET  (RTH bar closes)

Each run:
  1. Fetch 8h bars, compute DSS Bressert + Lyapunov HP → BUY / SELL / NEUTRAL
  2. Fetch 5-min + daily bars, compute mean levels (CDM, PDM, CMM, PMM)
  3. Cancel any stale pending limit orders from prior session
  4. If regime is BUY or SELL, place LIMIT orders at mean levels:
       BUY MODE:  buy limits at levels below price (support)
       SELL MODE:  sell limits at levels above price (resistance)
  5. Each limit gets a stop (1x daily ATR) and take-profit (2x ATR)

Risk structure:
  - 1 contract max per symbol (4 symbols = 4 max)
  - ATR-based stops on daily bars (1x ATR)
  - MLL gate: skip if stop-out would breach drawdown floor
  - Daily profit cap: $1,400 (consistency rule)
  - No $400 hard cap — 1 lot micro risk IS the risk, MLL is the safety net

Usage:
    python run_regime_session.py              # Live
    python run_regime_session.py --dry-run    # Show what would happen
    python run_regime_session.py --symbols MNQ MES

Author: Matthew Foster
"""

import asyncio
import argparse
import json
import math
import os
import sys
from datetime import datetime

import pytz
from dotenv import load_dotenv

# Load .env from script directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
sys.path.insert(0, SCRIPT_DIR)

from trend_filters import compute_trend_bias
from mean_levels_calc import compute_mean_levels, compute_atr

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
DEFAULT_SYMBOLS = ["MNQ", "MES", "MYM", "M2K"]

COMBINE_STARTING_BALANCE = 50_000
COMBINE_MLL = 2_000
COMBINE_MAX_LOTS = 5
DAILY_PROFIT_CAP = 1_400

MAX_CONTRACTS_PER_SYMBOL = 1
MAX_TOTAL_CONTRACTS = 4

DEFAULT_ATR_MULTIPLIER = 1.0
DEFAULT_RR_RATIO = 2.0

# Level strength (monthly > daily)
LEVEL_STRENGTH = {"CMM": 4, "PMM": 3, "CDM": 2, "PDM": 1}

# Files
REGIME_FILE = os.path.join(SCRIPT_DIR, "regime_state.json")
ORDERS_FILE = os.path.join(SCRIPT_DIR, "orders_state.json")
TRACKER_FILE = os.path.join(SCRIPT_DIR, "combine_tracker.json")
RESULTS_FILE = os.path.join(SCRIPT_DIR, "futures_scan_results.json")


# ─────────────────────────────────────────────────────────────
# COMBINE TRACKER
# ─────────────────────────────────────────────────────────────

def load_tracker() -> dict:
    if os.path.exists(TRACKER_FILE):
        with open(TRACKER_FILE, "r") as f:
            return json.load(f)
    return {
        "starting_balance": COMBINE_STARTING_BALANCE,
        "peak_eod_balance": COMBINE_STARTING_BALANCE,
        "current_mll": COMBINE_STARTING_BALANCE - COMBINE_MLL,
        "mll_locked": False,
        "total_realized_pnl": 0.0,
        "daily_pnl": {},
        "best_day_pnl": 0.0,
    }


def save_tracker(tracker: dict):
    tracker["last_updated"] = datetime.now().isoformat()
    with open(TRACKER_FILE, "w") as f:
        json.dump(tracker, f, indent=2)


def update_eod_balance(tracker: dict, balance: float) -> dict:
    if balance > tracker["peak_eod_balance"]:
        tracker["peak_eod_balance"] = balance
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
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        async with aiohttp.ClientSession() as http:
            async with http.post(f'{base_url}/Trade/search',
                                 json={'accountId': account.id}, headers=headers) as resp:
                result = await resp.json()
                trades = result.get('trades', [])
        pnl = sum((t.get('profitAndLoss', 0) or 0) - (t.get('fees', 0) or 0)
                   for t in trades if t.get('profitAndLoss') is not None)
        return round(pnl, 2)
    except Exception as e:
        print(f"  WARNING: Daily P&L fetch failed: {e}")
        return 0.0


# ─────────────────────────────────────────────────────────────
# ORDER MANAGEMENT
# ─────────────────────────────────────────────────────────────

def load_orders() -> dict:
    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r") as f:
            return json.load(f)
    return {"pending_limits": {}, "last_updated": None}


def save_orders(state: dict):
    state["last_updated"] = datetime.now().isoformat()
    with open(ORDERS_FILE, "w") as f:
        json.dump(state, f, indent=2)


async def cancel_all_pending(client, orders_state: dict) -> int:
    """Cancel stale UNFILLED limit entries (+ their brackets) from prior session.
    
    CRITICAL: If the entry order was FILLED, we must KEEP the stop and target
    alive to protect the open position. Only cancel entries that are still
    pending (unfilled).
    """
    import aiohttp

    pending = orders_state.get("pending_limits", {})
    if not pending:
        return 0

    token = client.get_session_token()
    base_url = client.base_url
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    cancelled = 0
    kept = 0

    # Get open positions to check which entries have filled
    open_positions = await client.search_open_positions()
    positioned_symbols = set()
    for p in open_positions:
        cid = getattr(p, 'contractId', '')
        parts = cid.split('.')
        sym = parts[3] if len(parts) >= 4 else cid
        positioned_symbols.add(sym)

    async with aiohttp.ClientSession() as http:
        for key, info in list(pending.items()):
            symbol = info.get("symbol", key.split("_")[0])

            # If this symbol has an open position, the entry FILLED.
            # Keep the stop and target orders alive to protect it.
            if symbol in positioned_symbols:
                print(f"  KEEPING brackets for {symbol} — entry filled, position open")
                kept += 1
                continue

            # Entry did NOT fill (or position already closed) — cancel everything
            for oid_key in ["entry_order_id", "stop_order_id", "target_order_id"]:
                oid = info.get(oid_key)
                if not oid:
                    continue
                try:
                    async with http.post(f'{base_url}/Order/cancel',
                                         json={'orderId': oid}, headers=headers) as resp:
                        res = await resp.json()
                        if res.get('success'):
                            cancelled += 1
                except Exception:
                    pass
            del pending[key]

    if kept:
        print(f"  Kept {kept} bracket(s) for open positions")
    return cancelled


async def place_limit_bracket(client, account, contract_id: str,
                               side: int, entry_price: float,
                               stop_price: float, target_price: float) -> dict:
    """Place a limit entry + stop loss + take profit bracket."""
    import aiohttp

    token = client.get_session_token()
    base_url = client.base_url
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    entry_id = stop_id = target_id = None
    stop_side = 1 if side == 0 else 0

    async with aiohttp.ClientSession() as http:
        # 1. LIMIT entry
        payload = {
            'accountId': account.id,
            'contractId': contract_id,
            'type': 1,  # Limit
            'side': side,
            'size': 1,
            'limitPrice': entry_price,
        }
        async with http.post(f'{base_url}/Order/place', json=payload, headers=headers) as resp:
            res = await resp.json()
            if res.get('success'):
                entry_id = res.get('orderId')
            else:
                raise Exception(f"Entry limit failed: {res}")

        # 2. STOP LOSS
        payload = {
            'accountId': account.id,
            'contractId': contract_id,
            'type': 4,  # Stop
            'side': stop_side,
            'size': 1,
            'stopPrice': stop_price,
        }
        async with http.post(f'{base_url}/Order/place', json=payload, headers=headers) as resp:
            res = await resp.json()
            if res.get('success'):
                stop_id = res.get('orderId')
            else:
                print(f"      Stop failed: {res}")

        # 3. TAKE PROFIT
        payload = {
            'accountId': account.id,
            'contractId': contract_id,
            'type': 1,  # Limit
            'side': stop_side,
            'size': 1,
            'limitPrice': target_price,
        }
        async with http.post(f'{base_url}/Order/place', json=payload, headers=headers) as resp:
            res = await resp.json()
            if res.get('success'):
                target_id = res.get('orderId')
            else:
                print(f"      Target failed: {res}")

    return {"entry_order_id": entry_id, "stop_order_id": stop_id, "target_order_id": target_id}


# ─────────────────────────────────────────────────────────────
# CORE PIPELINE
# ─────────────────────────────────────────────────────────────

async def run_regime_session(
    symbols: list[str] = None,
    live: bool = True,
    atr_mult: float = DEFAULT_ATR_MULTIPLIER,
    rr_ratio: float = DEFAULT_RR_RATIO,
) -> dict:
    """
    Full pipeline: regime filter → mean levels → limit orders.
    """
    from project_x_py import ProjectX

    symbols = symbols or DEFAULT_SYMBOLS
    tz = pytz.timezone("America/New_York")
    now = datetime.now(tz)
    mode_str = "LIVE" if live else "DRY RUN"

    print(f"\n{'='*65}")
    print(f"  REGIME SESSION [{mode_str}]")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Instruments: {', '.join(symbols)}")
    print(f"  Stops: {atr_mult}x ATR (daily) | R:R 1:{rr_ratio}")
    print(f"  Max: {MAX_CONTRACTS_PER_SYMBOL} lot/symbol, {MAX_TOTAL_CONTRACTS} total")
    print(f"{'='*65}")

    tracker = load_tracker()
    orders_state = load_orders()
    all_results = []

    async with ProjectX.from_env() as client:
        await client.authenticate()
        account = client.get_account_info()
        balance = account.balance

        tracker = update_eod_balance(tracker, balance)
        save_tracker(tracker)

        daily_pnl = await get_daily_pnl(client) if live else 0.0
        mll_floor = tracker["current_mll"]
        cushion = balance - mll_floor

        print(f"\n  Account:   {account.name}")
        print(f"  Balance:   ${balance:,.2f}")
        print(f"  MLL Floor: ${mll_floor:,.0f} {'(LOCKED)' if tracker.get('mll_locked') else '(trailing)'}")
        print(f"  Cushion:   ${cushion:,.2f}")
        if live:
            print(f"  Daily P&L: ${daily_pnl:+,.2f} (cap: ${DAILY_PROFIT_CAP:,})")

        # Daily profit cap check
        if live and daily_pnl >= DAILY_PROFIT_CAP:
            print(f"\n  DAILY PROFIT CAP REACHED — no new orders today.")
            return {"status": "daily_cap_reached"}

        # ── Step 1: Cancel stale pending limits ──
        if live:
            print(f"\n  ── CANCELLING STALE ORDERS ──")
            cancelled = await cancel_all_pending(client, orders_state)
            # NOTE: cancel_all_pending already removes cancelled entries from
            # orders_state["pending_limits"] via `del pending[key]`.  Brackets
            # kept for open positions remain in the dict — do NOT wipe it.
            save_orders(orders_state)
            print(f"  Cancelled {cancelled} order(s) from prior session.")
            kept = len(orders_state.get("pending_limits", {}))
            if kept:
                print(f"  Retained {kept} bracket(s) for open positions.")

        # ── Step 2: Compute regime + levels for each symbol ──
        print(f"\n  ── SCANNING ──")

        for symbol in symbols:
            print(f"\n  [{symbol}] ---")

            try:
                instrument = await client.get_instrument(symbol)
                contract_id = instrument.id
                tick_size = instrument.tickSize
                tick_value = instrument.tickValue

                # Fetch 8h bars for regime
                bars_8h = await client.get_bars(symbol, days=60, interval=8, unit=3)
                if bars_8h.is_empty() or len(bars_8h) < 20:
                    print(f"  [{symbol}] Not enough 8h bars ({len(bars_8h)})")
                    continue

                highs_8h = bars_8h["high"].to_list()
                lows_8h = bars_8h["low"].to_list()
                closes_8h = bars_8h["close"].to_list()

                # Compute regime
                bias = compute_trend_bias(highs_8h, lows_8h, closes_8h)
                if bias["bias"] == "BULLISH":
                    mode = "BUY"
                elif bias["bias"] == "BEARISH":
                    mode = "SELL"
                else:
                    mode = "NEUTRAL"

                mode_icon = {"BUY": "▲", "SELL": "▼", "NEUTRAL": "●"}[mode]
                print(f"  [{symbol}] Regime: {mode_icon} {mode} "
                      f"(DSS={bias['dss_signal']:+d} Lyap={bias['lyap_signal']:+d} "
                      f"Combined={bias['combined_signal']:+d})")

                # Save regime state
                regime_data = {
                    "mode": mode, "regime": bias["bias"],
                    "strength": bias["strength"],
                    "dss_signal": bias["dss_signal"],
                    "lyap_signal": bias["lyap_signal"],
                    "combined_signal": bias["combined_signal"],
                    "computed_at": now.isoformat(),
                }

                if mode == "NEUTRAL":
                    all_results.append({"symbol": symbol, "regime": regime_data,
                                        "action": "skip_neutral"})
                    continue

                # Fetch 5-min bars for mean levels
                bars_5m = await client.get_bars(symbol, days=35, interval=5, unit=2)
                if bars_5m.is_empty() or len(bars_5m) < 100:
                    print(f"  [{symbol}] Not enough 5m bars")
                    continue

                levels_data = compute_mean_levels(bars_5m, timezone="America/Chicago")
                current_price = levels_data["current_price"]

                cdm = levels_data["cdm"]
                pdm = levels_data["pdm"]
                cmm = levels_data["cmm"]
                pmm = levels_data["pmm"]

                print(f"  [{symbol}] Price: {current_price}")
                print(f"    CDM: {cdm}  PDM: {pdm}")
                print(f"    CMM: {cmm}  PMM: {pmm}")

                # Fetch daily bars for ATR
                daily_bars = await client.get_bars(symbol, days=35, interval=1, unit=4)
                if daily_bars.is_empty() or len(daily_bars) < 15:
                    import polars as pl
                    daily_bars = bars_5m.with_columns(
                        pl.col("timestamp").dt.date().alias("date")
                    ).group_by("date").agg([
                        pl.col("timestamp").first(), pl.col("open").first(),
                        pl.col("high").max(), pl.col("low").min(),
                        pl.col("close").last(), pl.col("volume").sum(),
                    ]).sort("date")

                atr = compute_atr(daily_bars, period=14)
                stop_dist = atr * atr_mult
                stop_dist = max(stop_dist, tick_size * 4)

                # Dollar risk for 1 contract
                ticks_to_stop = stop_dist / tick_size
                dollar_risk = ticks_to_stop * tick_value
                dollar_target = dollar_risk * rr_ratio

                print(f"    ATR(14d): {atr:.2f} | Stop: {stop_dist:.2f} pts | "
                      f"Risk/lot: ${dollar_risk:.0f} | Target: ${dollar_target:.0f}")

                # Build candidate levels
                level_map = {"CDM": cdm, "PDM": pdm, "CMM": cmm, "PMM": pmm}
                candidates = []

                for lname, lprice in level_map.items():
                    if lprice is None:
                        continue

                    if mode == "BUY" and lprice < current_price:
                        # Support: buy limit below price
                        entry = round(round(lprice / tick_size) * tick_size, 6)
                        stop = round(round((entry - stop_dist) / tick_size) * tick_size, 6)
                        target = round(round((entry + stop_dist * rr_ratio) / tick_size) * tick_size, 6)
                        candidates.append({
                            "level": lname, "strength": LEVEL_STRENGTH[lname],
                            "side": 0, "side_str": "BUY",
                            "entry": entry, "stop": stop, "target": target,
                            "dollar_risk": dollar_risk, "dollar_target": dollar_target,
                            "dist_pct": round(abs(current_price - entry) / current_price * 100, 2),
                        })

                    elif mode == "SELL" and lprice > current_price:
                        # Resistance: sell limit above price
                        entry = round(round(lprice / tick_size) * tick_size, 6)
                        stop = round(round((entry + stop_dist) / tick_size) * tick_size, 6)
                        target = round(round((entry - stop_dist * rr_ratio) / tick_size) * tick_size, 6)
                        candidates.append({
                            "level": lname, "strength": LEVEL_STRENGTH[lname],
                            "side": 1, "side_str": "SELL",
                            "entry": entry, "stop": stop, "target": target,
                            "dollar_risk": dollar_risk, "dollar_target": dollar_target,
                            "dist_pct": round(abs(current_price - entry) / current_price * 100, 2),
                        })

                # Sort by strength (monthly first)
                candidates.sort(key=lambda x: x["strength"], reverse=True)

                if not candidates:
                    print(f"  [{symbol}] No eligible levels for {mode} mode")
                    all_results.append({"symbol": symbol, "regime": regime_data,
                                        "action": "no_eligible_levels"})
                    continue

                # Pick the strongest level
                best = candidates[0]

                # MLL gate
                balance_after_loss = balance - best["dollar_risk"]
                if balance_after_loss < mll_floor:
                    print(f"  [{symbol}] MLL BLOCK — stop-out would drop to "
                          f"${balance_after_loss:,.0f} < floor ${mll_floor:,.0f}")
                    all_results.append({"symbol": symbol, "regime": regime_data,
                                        "action": "mll_blocked",
                                        "dollar_risk": best["dollar_risk"]})
                    continue

                # Check existing positions
                existing = await client.search_open_positions()
                positioned_symbols = set()
                open_lots = 0
                for p in existing:
                    cid = getattr(p, 'contractId', '')
                    parts = cid.split('.')
                    sym = parts[3] if len(parts) >= 4 else cid
                    positioned_symbols.add(sym)
                    open_lots += getattr(p, 'size', 1)

                if symbol in positioned_symbols:
                    print(f"  [{symbol}] Already has open position — skip")
                    all_results.append({"symbol": symbol, "regime": regime_data,
                                        "action": "already_positioned"})
                    continue

                if open_lots >= MAX_TOTAL_CONTRACTS:
                    print(f"  [{symbol}] At max contracts ({open_lots}/{MAX_TOTAL_CONTRACTS})")
                    all_results.append({"symbol": symbol, "regime": regime_data,
                                        "action": "max_contracts"})
                    continue

                # Display the order
                icon = "▲" if best["side"] == 0 else "▼"
                print(f"\n  [{symbol}] {icon} {best['side_str']} LIMIT 1 @ {best['entry']}")
                print(f"    Level: {best['level']} (strength {best['strength']})")
                print(f"    Stop:   {best['stop']} — ${best['dollar_risk']:.0f} risk")
                print(f"    Target: {best['target']} — ${best['dollar_target']:.0f}")
                print(f"    R:R = 1:{rr_ratio} | Dist: {best['dist_pct']}%")

                if len(candidates) > 1:
                    others = [f"{c['level']}@{c['entry']}" for c in candidates[1:]]
                    print(f"    Other levels: {', '.join(others)}")

                # Place the order (live only)
                if live:
                    try:
                        ids = await place_limit_bracket(
                            client, account, contract_id,
                            best["side"], best["entry"],
                            best["stop"], best["target"],
                        )
                        print(f"    LIMIT PLACED — Entry: {ids['entry_order_id']}")
                        if ids["stop_order_id"]:
                            print(f"    STOP PLACED  — {ids['stop_order_id']} @ {best['stop']}")
                        if ids["target_order_id"]:
                            print(f"    TARGET PLACED — {ids['target_order_id']} @ {best['target']}")

                        order_key = f"{symbol}_{best['level']}"
                        orders_state["pending_limits"][order_key] = {
                            **ids, **best,
                            "symbol": symbol, "contract_id": contract_id,
                            "placed_at": now.isoformat(),
                        }
                        save_orders(orders_state)

                        all_results.append({"symbol": symbol, "regime": regime_data,
                                            "action": "limit_placed", "order": best,
                                            "order_ids": ids})
                    except Exception as e:
                        print(f"    ERROR: {e}")
                        all_results.append({"symbol": symbol, "regime": regime_data,
                                            "action": "error", "error": str(e)})
                else:
                    all_results.append({"symbol": symbol, "regime": regime_data,
                                        "action": "dry_run", "order": best})

            except Exception as e:
                print(f"  [{symbol}] Error: {e}")
                import traceback
                traceback.print_exc()
                all_results.append({"symbol": symbol, "action": "error", "error": str(e)})

    # Save regime state
    regime_out = {}
    for r in all_results:
        if "regime" in r:
            regime_out[r["symbol"]] = r["regime"]
    regime_state = {"instruments": regime_out, "last_updated": now.isoformat()}
    with open(REGIME_FILE, "w") as f:
        json.dump(regime_state, f, indent=2)

    # Summary
    print(f"\n{'='*65}")
    print(f"  SUMMARY")
    print(f"{'='*65}")

    placed = [r for r in all_results if r["action"] == "limit_placed"]
    dry = [r for r in all_results if r["action"] == "dry_run"]
    skipped = [r for r in all_results if r["action"] in ("skip_neutral", "no_eligible_levels",
                                                           "mll_blocked", "already_positioned",
                                                           "max_contracts")]
    errors = [r for r in all_results if r["action"] == "error"]

    if placed:
        total_risk = sum(r["order"]["dollar_risk"] for r in placed)
        print(f"  PLACED: {len(placed)} limit(s) — ${total_risk:,.0f} total risk")
        for r in placed:
            o = r["order"]
            print(f"    {r['symbol']} {o['side_str']} @ {o['entry']} ({o['level']})")
    if dry:
        total_risk = sum(r["order"]["dollar_risk"] for r in dry)
        print(f"  DRY RUN: {len(dry)} limit(s) would be placed — ${total_risk:,.0f} risk")
        for r in dry:
            o = r["order"]
            print(f"    {r['symbol']} {o['side_str']} @ {o['entry']} ({o['level']})")
    if skipped:
        print(f"  SKIPPED: {len(skipped)}")
        for r in skipped:
            print(f"    {r['symbol']}: {r['action']}")
    if errors:
        print(f"  ERRORS: {len(errors)}")

    if not placed and not dry:
        print(f"  No orders — all instruments NEUTRAL or blocked")

    print(f"{'='*65}\n")

    # Save results
    with open(RESULTS_FILE, "w") as f:
        json.dump({"timestamp": now.isoformat(), "mode": mode_str,
                    "results": all_results}, f, indent=2, default=str)

    return {"results": all_results, "regime": regime_state}


def main():
    parser = argparse.ArgumentParser(description="Regime Session — 8h Filter → Limit Orders")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--atr-mult", type=float, default=DEFAULT_ATR_MULTIPLIER)
    parser.add_argument("--rr-ratio", type=float, default=DEFAULT_RR_RATIO)
    args = parser.parse_args()

    asyncio.run(run_regime_session(
        symbols=args.symbols,
        live=not args.dry_run,
        atr_mult=args.atr_mult,
        rr_ratio=args.rr_ratio,
    ))


if __name__ == "__main__":
    main()
