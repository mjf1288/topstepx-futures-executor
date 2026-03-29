"""
Evaluation Harness — DO NOT MODIFY (locked evaluator)
======================================================
This is the autoresearch scoring function. The agent CANNOT touch this file.
It backtests the strategy using parameters from strategy_params.py against
historical price data, with a train/validation split.

Outputs a single score to stdout: the validation-set profit factor.
The agent reads this score to decide keep or revert.

Usage:
    python eval.py                  # Full eval (train + validation scores)
    python eval.py --val-only       # Validation score only (for the ratchet loop)

Author: Matthew Foster
"""

import json
import os
import sys
import math
from datetime import datetime, timedelta
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# ─────────────────────────────────────────────────────────────
# IMPORT STRATEGY PARAMS (the file the agent modifies)
# ─────────────────────────────────────────────────────────────
import importlib
import strategy_params as params


def reload_params():
    """Reload strategy_params in case the agent just modified it."""
    importlib.reload(params)


# ─────────────────────────────────────────────────────────────
# SIMULATED PRICE DATA
# ─────────────────────────────────────────────────────────────
# In production, this loads real historical data from files.
# For the autoresearch loop, we use exported bar data.

DATA_DIR = os.path.join(SCRIPT_DIR, "backtest_data")


def load_bar_data(symbol: str) -> list[dict]:
    """Load historical bar data for a symbol from JSON files.
    
    Expected format: list of dicts with keys:
        timestamp, open, high, low, close, volume
    
    Files should be exported from TopstepX or another data source.
    Naming: {DATA_DIR}/{symbol}_8h.json for 8h bars
             {DATA_DIR}/{symbol}_5m.json for 5m bars
             {DATA_DIR}/{symbol}_daily.json for daily bars
    """
    files = {
        "8h": os.path.join(DATA_DIR, f"{symbol}_8h.json"),
        "5m": os.path.join(DATA_DIR, f"{symbol}_5m.json"),
        "daily": os.path.join(DATA_DIR, f"{symbol}_daily.json"),
    }
    
    data = {}
    for timeframe, filepath in files.items():
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                data[timeframe] = json.load(f)
        else:
            data[timeframe] = []
    
    return data


# ─────────────────────────────────────────────────────────────
# DSS BRESSERT (standalone, uses strategy_params values)
# ─────────────────────────────────────────────────────────────

def compute_dss(highs, lows, closes):
    """Compute DSS Bressert using current strategy_params values."""
    stoch_period = params.DSS_STOCH_PERIOD
    smooth_period = params.DSS_SMOOTH_PERIOD
    signal_period = params.DSS_SIGNAL_PERIOD
    ob_level = params.DSS_OB_LEVEL
    os_level = params.DSS_OS_LEVEL

    n = len(closes)
    if n < stoch_period + smooth_period * 2:
        return 0

    def ema(values, period):
        if not values:
            return []
        k = 2.0 / (period + 1)
        result = [values[0]]
        for i in range(1, len(values)):
            result.append(values[i] * k + result[-1] * (1 - k))
        return result

    # Raw stochastic
    raw_stoch = []
    for i in range(n):
        start = max(0, i - stoch_period + 1)
        hh = max(highs[start:i+1])
        ll = min(lows[start:i+1])
        if hh == ll:
            raw_stoch.append(50.0)
        else:
            raw_stoch.append((closes[i] - ll) / (hh - ll) * 100)

    smooth1 = ema(raw_stoch, smooth_period)
    
    raw_stoch2 = []
    for i in range(len(smooth1)):
        start = max(0, i - stoch_period + 1)
        window = smooth1[start:i+1]
        hi, lo = max(window), min(window)
        if hi == lo:
            raw_stoch2.append(50.0)
        else:
            raw_stoch2.append((smooth1[i] - lo) / (hi - lo) * 100)

    dss = ema(raw_stoch2, smooth_period)
    signal = ema(dss, signal_period)

    if len(dss) < 3 or len(signal) < 3:
        return 0

    d, dp = dss[-1], dss[-2]
    s, sp = signal[-1], signal[-2]
    cross_above = dp <= sp and d > s
    cross_below = dp >= sp and d < s

    if cross_above and d < os_level and s < os_level:
        return 10
    if cross_below and d > ob_level and s > ob_level:
        return -10
    if cross_above and (d < os_level or s < os_level):
        return 7
    if cross_below and (d > ob_level or s > ob_level):
        return -7
    if d > ob_level and s > ob_level and d < dp:
        return -5
    if d < os_level and s < os_level and d > dp:
        return 5
    if cross_above:
        return 4
    if cross_below:
        return -4
    if d > s:
        return min(3, max(1, int((d - s) / 10)))
    if d < s:
        return -min(3, max(1, int((s - d) / 10)))
    return 0


# ─────────────────────────────────────────────────────────────
# MEAN LEVELS (standalone)
# ─────────────────────────────────────────────────────────────

def compute_means_from_bars(bars_5m: list[dict]) -> dict:
    """Compute CDM, PDM, CMM, PMM from 5-min bar data."""
    if not bars_5m:
        return {"cdm": None, "pdm": None, "cmm": None, "pmm": None}

    closes = [b["close"] for b in bars_5m]
    dates = [b["timestamp"][:10] for b in bars_5m]  # YYYY-MM-DD
    months = [b["timestamp"][:7] for b in bars_5m]   # YYYY-MM

    def calc_mean(closes_list, groups):
        cum, count = 0.0, 0
        buff, prev_tf_buff = 0.0, None
        current_group = None
        for i in range(len(closes_list)):
            grp = groups[i]
            if i == 0 or grp != current_group:
                if i > 0:
                    prev_tf_buff = buff
                cum, count, current_group = closes_list[i], 1, grp
            else:
                cum += closes_list[i]
                count += 1
            buff = cum / count
        return {"current": round(buff, 4), "prev": round(prev_tf_buff, 4) if prev_tf_buff else round(buff, 4)}

    daily = calc_mean(closes, dates)
    monthly = calc_mean(closes, months)

    return {
        "cdm": daily["current"],
        "pdm": daily["prev"],
        "cmm": monthly["current"],
        "pmm": monthly["prev"],
        "current_price": closes[-1],
    }


def compute_atr_from_bars(daily_bars: list[dict], period: int = None) -> float:
    """Compute ATR from daily bar data."""
    period = period or params.ATR_PERIOD
    if len(daily_bars) < period + 1:
        return 0.0

    trs = []
    for i in range(1, len(daily_bars)):
        h = daily_bars[i]["high"]
        l = daily_bars[i]["low"]
        pc = daily_bars[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    recent = trs[-period:]
    return sum(recent) / len(recent) if recent else 0.0


# ─────────────────────────────────────────────────────────────
# BACKTESTER
# ─────────────────────────────────────────────────────────────

def backtest_symbol(symbol: str, data: dict, start_date: str = None, 
                     end_date: str = None) -> list[dict]:
    """
    Simulate the Tzu v5.0 strategy on historical data for one symbol.
    
    Returns a list of trade dicts:
        {entry_price, exit_price, side, pnl, level, date, ...}
    """
    bars_8h = data.get("8h", [])
    bars_5m = data.get("5m", [])
    daily_bars = data.get("daily", [])

    if not bars_8h or not bars_5m or not daily_bars:
        return []

    # Filter by date range if specified
    if start_date:
        bars_8h = [b for b in bars_8h if b["timestamp"][:10] >= start_date]
        bars_5m = [b for b in bars_5m if b["timestamp"][:10] >= start_date]
        daily_bars = [b for b in daily_bars if b["timestamp"][:10] >= start_date]
    if end_date:
        bars_8h = [b for b in bars_8h if b["timestamp"][:10] <= end_date]
        bars_5m = [b for b in bars_5m if b["timestamp"][:10] <= end_date]
        daily_bars = [b for b in daily_bars if b["timestamp"][:10] <= end_date]

    if len(bars_8h) < 30 or len(daily_bars) < 20:
        return []

    # Level strength mapping from params
    level_strength = {
        "CMM": params.LEVEL_STRENGTH_CMM,
        "PMM": params.LEVEL_STRENGTH_PMM,
        "CDM": params.LEVEL_STRENGTH_CDM,
        "PDM": params.LEVEL_STRENGTH_PDM,
    }

    trades = []
    latched_mode = None

    # Walk through 8h bars, simulating the 3x/day session logic
    for i in range(30, len(bars_8h)):
        # Compute DSS on the 8h bars up to this point
        h_slice = [b["high"] for b in bars_8h[:i+1]]
        l_slice = [b["low"] for b in bars_8h[:i+1]]
        c_slice = [b["close"] for b in bars_8h[:i+1]]

        dss_signal = compute_dss(h_slice, l_slice, c_slice)

        # Latching logic
        if dss_signal >= params.LATCH_TRIGGER_THRESHOLD:
            latched_mode = "BUY"
            trigger = "new"
        elif dss_signal <= params.LATCH_FLIP_THRESHOLD:
            latched_mode = "SELL"
            trigger = "new"
        else:
            trigger = "latched"

        if latched_mode is None:
            continue  # NEUTRAL, no trade

        mode = latched_mode
        current_bar = bars_8h[i]
        bar_date = current_bar["timestamp"][:10]
        current_price = current_bar["close"]

        # Get 5m bars up to this bar's date for mean levels
        bars_5m_slice = [b for b in bars_5m if b["timestamp"][:10] <= bar_date]
        if len(bars_5m_slice) < 50:
            continue

        means = compute_means_from_bars(bars_5m_slice)
        
        # CMM structure gate
        if params.CMM_GATE_ENABLED and means["cmm"] is not None:
            buffer = means["cmm"] * params.CMM_GATE_BUFFER_PCT / 100
            if mode == "BUY" and current_price < means["cmm"] - buffer:
                continue
            if mode == "SELL" and current_price > means["cmm"] + buffer:
                continue

        # Get daily bars for ATR
        daily_slice = [b for b in daily_bars if b["timestamp"][:10] <= bar_date]
        atr = compute_atr_from_bars(daily_slice, params.ATR_PERIOD)
        if atr == 0:
            continue

        stop_dist = atr * params.ATR_MULTIPLIER
        stop_dist = max(stop_dist, 1.0)  # Minimum 1 point

        # Build candidate levels
        level_map = {"CDM": means["cdm"], "PDM": means["pdm"], 
                     "CMM": means["cmm"], "PMM": means["pmm"]}
        candidates = []

        for lname, lprice in level_map.items():
            if lprice is None:
                continue

            dist_pct = abs(current_price - lprice) / current_price * 100 if current_price else 999

            if mode == "BUY" and lprice < current_price:
                candidates.append({
                    "level": lname, "price": lprice,
                    "strength": level_strength.get(lname, 1),
                    "dist_pct": dist_pct, "side": "BUY",
                })
            elif mode == "SELL" and lprice > current_price:
                candidates.append({
                    "level": lname, "price": lprice,
                    "strength": level_strength.get(lname, 1),
                    "dist_pct": dist_pct, "side": "SELL",
                })

        if not candidates:
            continue

        # Select level based on mode
        if params.LEVEL_SELECT_MODE == "closest":
            candidates.sort(key=lambda x: x["dist_pct"])
        elif params.LEVEL_SELECT_MODE == "strongest":
            candidates.sort(key=lambda x: -x["strength"])
        elif params.LEVEL_SELECT_MODE == "weighted":
            candidates.sort(key=lambda x: -(x["strength"] / max(x["dist_pct"], 0.01)))

        best = candidates[0]
        entry_price = best["price"]

        # Calculate stop and target
        if best["side"] == "BUY":
            stop_price = entry_price - stop_dist
            target_price = entry_price + stop_dist * params.RR_RATIO
        else:
            stop_price = entry_price + stop_dist
            target_price = entry_price - stop_dist * params.RR_RATIO

        # Simulate fill: look forward in 5m bars to see if entry is hit,
        # then if stop or target is hit first
        future_5m = [b for b in bars_5m if b["timestamp"] > current_bar["timestamp"]]
        
        # Look forward up to ~2 full sessions (576 bars = 48 hours)
        # Mean level limit orders often take time to fill
        future_5m = future_5m[:576]

        entry_filled = False
        for fb in future_5m:
            if not entry_filled:
                # Check if entry level is reached
                if best["side"] == "BUY" and fb["low"] <= entry_price:
                    entry_filled = True
                elif best["side"] == "SELL" and fb["high"] >= entry_price:
                    entry_filled = True
                continue

            # Entry filled — check stop and target
            if best["side"] == "BUY":
                if fb["low"] <= stop_price:
                    # Stopped out
                    pnl = stop_price - entry_price
                    trades.append({
                        "symbol": symbol, "side": best["side"],
                        "entry": entry_price, "exit": stop_price,
                        "pnl_points": round(pnl, 4),
                        "level": best["level"], "date": bar_date,
                        "result": "loss", "trigger": trigger,
                    })
                    break
                elif fb["high"] >= target_price:
                    # Target hit
                    pnl = target_price - entry_price
                    trades.append({
                        "symbol": symbol, "side": best["side"],
                        "entry": entry_price, "exit": target_price,
                        "pnl_points": round(pnl, 4),
                        "level": best["level"], "date": bar_date,
                        "result": "win", "trigger": trigger,
                    })
                    break
            else:  # SELL
                if fb["high"] >= stop_price:
                    pnl = entry_price - stop_price
                    trades.append({
                        "symbol": symbol, "side": best["side"],
                        "entry": entry_price, "exit": stop_price,
                        "pnl_points": round(pnl, 4),
                        "level": best["level"], "date": bar_date,
                        "result": "loss", "trigger": trigger,
                    })
                    break
                elif fb["low"] <= target_price:
                    pnl = entry_price - target_price
                    trades.append({
                        "symbol": symbol, "side": best["side"],
                        "entry": entry_price, "exit": target_price,
                        "pnl_points": round(pnl, 4),
                        "level": best["level"], "date": bar_date,
                        "result": "win", "trigger": trigger,
                    })
                    break

    return trades


# ─────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────

def compute_score(trades: list[dict]) -> dict:
    """
    Compute performance metrics from a list of trades.
    
    Primary metric: PROFIT FACTOR (gross wins / gross losses)
    Secondary: win rate, total trades, avg R, max consecutive losses
    """
    if not trades:
        return {
            "profit_factor": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "avg_r": 0.0,
            "max_consec_losses": 0,
            "total_pnl_points": 0.0,
        }

    wins = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]

    gross_wins = sum(abs(t["pnl_points"]) for t in wins)
    gross_losses = sum(abs(t["pnl_points"]) for t in losses)

    profit_factor = gross_wins / gross_losses if gross_losses > 0 else (
        999.0 if gross_wins > 0 else 0.0
    )
    win_rate = len(wins) / len(trades) * 100 if trades else 0

    # Average R (each win = +RR_RATIO R, each loss = -1 R)
    total_r = len(wins) * params.RR_RATIO - len(losses)
    avg_r = total_r / len(trades) if trades else 0

    # Max consecutive losses
    max_consec = 0
    current_consec = 0
    for t in trades:
        if t["result"] == "loss":
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0

    total_pnl = sum(t["pnl_points"] for t in trades)

    return {
        "profit_factor": round(profit_factor, 4),
        "win_rate": round(win_rate, 2),
        "total_trades": len(trades),
        "avg_r": round(avg_r, 4),
        "max_consec_losses": max_consec,
        "total_pnl_points": round(total_pnl, 4),
        "wins": len(wins),
        "losses": len(losses),
    }


# ─────────────────────────────────────────────────────────────
# MAIN — THE RATCHET READS THIS OUTPUT
# ─────────────────────────────────────────────────────────────

def main():
    reload_params()

    val_only = "--val-only" in sys.argv

    # Date split configuration
    # Training:   everything before SPLIT_DATE
    # Validation: SPLIT_DATE through end of data
    # Using March 1 as split — gives ~2 months training (Jan–Feb) and
    # ~1 month validation (March), matching the 5m data availability.
    SPLIT_DATE = "2026-03-01"  # Train: Jan-Feb, Validate: March

    symbols = params.SYMBOLS

    # Check for data
    if not os.path.exists(DATA_DIR):
        print("ERROR: No backtest_data/ directory found.", file=sys.stderr)
        print("Export your historical data to backtest_data/{SYMBOL}_8h.json, "
              "{SYMBOL}_5m.json, {SYMBOL}_daily.json", file=sys.stderr)
        print("SCORE: 0.0")
        sys.exit(1)

    all_train_trades = []
    all_val_trades = []

    for symbol in symbols:
        data = load_bar_data(symbol)
        if not any(data.values()):
            print(f"  [{symbol}] No data found, skipping", file=sys.stderr)
            continue

        # Show data range
        for tf, bars in data.items():
            if bars:
                first = bars[0]["timestamp"][:10]
                last = bars[-1]["timestamp"][:10]
                print(f"  [{symbol}] {tf}: {len(bars)} bars ({first} to {last})", file=sys.stderr)

        # Full backtest (no date filter) to see total capacity
        all_trades = backtest_symbol(symbol, data)
        print(f"  [{symbol}] Total trades (unfiltered): {len(all_trades)}", file=sys.stderr)

        # Training set: everything before split date
        train_trades = [t for t in all_trades if t["date"] < SPLIT_DATE]
        all_train_trades.extend(train_trades)

        # Validation set: split date through end
        val_trades = [t for t in all_trades if t["date"] >= SPLIT_DATE]
        all_val_trades.extend(val_trades)

    train_score = compute_score(all_train_trades)
    val_score = compute_score(all_val_trades)

    if not val_only:
        print("=" * 50, file=sys.stderr)
        print("  BACKTEST RESULTS", file=sys.stderr)
        print("=" * 50, file=sys.stderr)
        print(f"\n  TRAINING SET (before {SPLIT_DATE}):", file=sys.stderr)
        print(f"    Trades:        {train_score['total_trades']}", file=sys.stderr)
        print(f"    Profit Factor: {train_score['profit_factor']}", file=sys.stderr)
        print(f"    Win Rate:      {train_score['win_rate']}%", file=sys.stderr)
        print(f"    Avg R:         {train_score['avg_r']}", file=sys.stderr)
        print(f"    Max Consec L:  {train_score['max_consec_losses']}", file=sys.stderr)
        print(f"\n  VALIDATION SET (from {SPLIT_DATE}):", file=sys.stderr)
        print(f"    Trades:        {val_score['total_trades']}", file=sys.stderr)
        print(f"    Profit Factor: {val_score['profit_factor']}", file=sys.stderr)
        print(f"    Win Rate:      {val_score['win_rate']}%", file=sys.stderr)
        print(f"    Avg R:         {val_score['avg_r']}", file=sys.stderr)
        print(f"    Max Consec L:  {val_score['max_consec_losses']}", file=sys.stderr)
        print("=" * 50, file=sys.stderr)

    # THE SINGLE SCORE — this is what the ratchet reads
    # Using validation profit factor as the primary metric
    score = val_score["profit_factor"]

    # Penalize if too few trades (not statistically meaningful)
    if val_score["total_trades"] < 10:
        score *= 0.5  # Halve the score if under 10 trades
        print(f"  WARNING: Only {val_score['total_trades']} validation trades — "
              f"score penalized", file=sys.stderr)

    # Penalize extreme max consecutive losses (risk of ruin)
    if val_score["max_consec_losses"] > 8:
        score *= 0.8
        print(f"  WARNING: {val_score['max_consec_losses']} consecutive losses — "
              f"score penalized", file=sys.stderr)

    print(f"SCORE: {score:.4f}")


if __name__ == "__main__":
    main()
