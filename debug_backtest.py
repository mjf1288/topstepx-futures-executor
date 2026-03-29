"""
Diagnostic tool — run this to see WHY trades are winning/losing.
Usage: python debug_backtest.py
"""
import json, os, sys, math

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import strategy_params as params
from eval import compute_dss, compute_means_from_bars, compute_atr_from_bars

DATA_DIR = os.path.join(SCRIPT_DIR, "backtest_data")

def load_bar_data(symbol):
    files = {
        "8h": os.path.join(DATA_DIR, f"{symbol}_8h.json"),
        "5m": os.path.join(DATA_DIR, f"{symbol}_5m.json"),
        "daily": os.path.join(DATA_DIR, f"{symbol}_daily.json"),
    }
    data = {}
    for tf, fp in files.items():
        if os.path.exists(fp):
            with open(fp) as f:
                data[tf] = json.load(f)
        else:
            data[tf] = []
    return data


for symbol in params.SYMBOLS:
    print(f"\n{'='*60}")
    print(f"  {symbol} DIAGNOSTIC")
    print(f"{'='*60}")
    
    data = load_bar_data(symbol)
    bars_8h = data["8h"]
    bars_5m = data["5m"]
    daily_bars = data["daily"]
    
    if not bars_5m:
        print(f"  No 5m data for {symbol}")
        continue
    
    print(f"  5m range: {bars_5m[0]['timestamp'][:10]} to {bars_5m[-1]['timestamp'][:10]}")
    print(f"  8h range: {bars_8h[0]['timestamp'][:10]} to {bars_8h[-1]['timestamp'][:10]}")
    first_5m_date = bars_5m[0]["timestamp"][:10]
    eligible_8h = [b for b in bars_8h if b["timestamp"][:10] >= first_5m_date]
    print(f"  8h bars with 5m coverage: {len(eligible_8h)}")
    
    # Show ATR for context
    atr = compute_atr_from_bars(daily_bars, params.ATR_PERIOD)
    stop_dist = atr * params.ATR_MULTIPLIER
    target_dist = stop_dist * params.RR_RATIO
    last_price = bars_5m[-1]["close"] if bars_5m else 0
    print(f"\n  Last price:  {last_price:.2f}")
    print(f"  14-day ATR:  {atr:.2f}")
    print(f"  Stop dist:   {stop_dist:.2f} ({stop_dist/last_price*100:.3f}%)")
    print(f"  Target dist: {target_dist:.2f} ({target_dist/last_price*100:.3f}%)")
    
    # Show mean levels at end of data
    means = compute_means_from_bars(bars_5m)
    print(f"\n  Mean levels (end of data):")
    for k in ["cdm", "pdm", "cmm", "pmm"]:
        v = means[k]
        if v:
            dist = abs(last_price - v)
            pct = dist / last_price * 100
            above_below = "above" if v > last_price else "below"
            print(f"    {k.upper()}: {v:.2f} ({pct:.2f}% {above_below} price)")
    
    # Walk through and trace decisions
    latched_mode = None
    position_free_after = None
    stats = {"neutral": 0, "no_5m": 0, "cmm_gate": 0, "no_atr": 0, 
             "no_candidates": 0, "no_fill": 0, "trade_win": 0, "trade_loss": 0, 
             "position_locked": 0, "no_resolution": 0}
    
    level_strength = {
        "CMM": params.LEVEL_STRENGTH_CMM, "PMM": params.LEVEL_STRENGTH_PMM,
        "CDM": params.LEVEL_STRENGTH_CDM, "PDM": params.LEVEL_STRENGTH_PDM,
    }
    
    print(f"\n  Trade-by-trade log:")
    print(f"  {'-'*56}")
    
    for i in range(30, len(bars_8h)):
        current_bar = bars_8h[i]
        bar_date = current_bar["timestamp"][:10]
        current_price = current_bar["close"]
        
        # Only bars with 5m coverage
        if bar_date < first_5m_date:
            continue
        
        if position_free_after is not None:
            if current_bar["timestamp"] < position_free_after:
                stats["position_locked"] += 1
                continue
            else:
                position_free_after = None
        
        h_slice = [b["high"] for b in bars_8h[:i+1]]
        l_slice = [b["low"] for b in bars_8h[:i+1]]
        c_slice = [b["close"] for b in bars_8h[:i+1]]
        dss_signal = compute_dss(h_slice, l_slice, c_slice)
        
        if dss_signal >= params.LATCH_TRIGGER_THRESHOLD:
            latched_mode = "BUY"
        elif dss_signal <= params.LATCH_FLIP_THRESHOLD:
            latched_mode = "SELL"
        
        if latched_mode is None:
            stats["neutral"] += 1
            continue
        
        mode = latched_mode
        
        bars_5m_slice = [b for b in bars_5m if b["timestamp"][:10] <= bar_date]
        if len(bars_5m_slice) < 50:
            stats["no_5m"] += 1
            continue
        
        means = compute_means_from_bars(bars_5m_slice)
        
        if params.CMM_GATE_ENABLED and means["cmm"] is not None:
            buffer = means["cmm"] * params.CMM_GATE_BUFFER_PCT / 100
            if mode == "BUY" and current_price < means["cmm"] - buffer:
                stats["cmm_gate"] += 1
                continue
            if mode == "SELL" and current_price > means["cmm"] + buffer:
                stats["cmm_gate"] += 1
                continue
        
        daily_slice = [b for b in daily_bars if b["timestamp"][:10] <= bar_date]
        atr_val = compute_atr_from_bars(daily_slice, params.ATR_PERIOD)
        if atr_val == 0:
            stats["no_atr"] += 1
            continue
        
        stop_d = atr_val * params.ATR_MULTIPLIER
        stop_d = max(stop_d, 1.0)
        
        level_map = {"CDM": means["cdm"], "PDM": means["pdm"], 
                     "CMM": means["cmm"], "PMM": means["pmm"]}
        candidates = []
        for lname, lprice in level_map.items():
            if lprice is None:
                continue
            dist_pct = abs(current_price - lprice) / current_price * 100
            if mode == "BUY" and lprice < current_price:
                candidates.append({"level": lname, "price": lprice, "dist_pct": dist_pct, "side": "BUY"})
            elif mode == "SELL" and lprice > current_price:
                candidates.append({"level": lname, "price": lprice, "dist_pct": dist_pct, "side": "SELL"})
        
        if not candidates:
            stats["no_candidates"] += 1
            continue
        
        candidates.sort(key=lambda x: x["dist_pct"])
        best = candidates[0]
        entry_price = best["price"]
        
        if best["side"] == "BUY":
            stop_price = entry_price - stop_d
            target_price = entry_price + stop_d * params.RR_RATIO
        else:
            stop_price = entry_price + stop_d
            target_price = entry_price - stop_d * params.RR_RATIO
        
        future_5m = [b for b in bars_5m if b["timestamp"] > current_bar["timestamp"]]
        entry_window = future_5m[:96]
        full_window = future_5m[:576]
        
        entry_filled = False
        entry_bar_idx = None
        for idx, fb in enumerate(entry_window):
            if best["side"] == "BUY" and fb["low"] <= entry_price:
                entry_filled = True
                entry_bar_idx = idx
                break
            elif best["side"] == "SELL" and fb["high"] >= entry_price:
                entry_filled = True
                entry_bar_idx = idx
                break
        
        if not entry_filled:
            stats["no_fill"] += 1
            print(f"    {bar_date} {mode:4s} | entry={entry_price:.2f} ({best['level']}) "
                  f"price={current_price:.2f} dist={best['dist_pct']:.2f}% | NO FILL in {len(entry_window)} bars")
            continue
        
        post_fill_bars = full_window[entry_bar_idx + 1:]
        resolved = False
        for fb in post_fill_bars:
            if best["side"] == "BUY":
                if fb["high"] >= target_price:
                    stats["trade_win"] += 1
                    position_free_after = fb["timestamp"]
                    resolved = True
                    print(f"    {bar_date} {mode:4s} | entry={entry_price:.2f} ({best['level']}) "
                          f"stop={stop_price:.2f} target={target_price:.2f} | WIN  "
                          f"atr={atr_val:.2f} stop_d={stop_d:.2f}")
                    break
                if fb["low"] <= stop_price:
                    stats["trade_loss"] += 1
                    position_free_after = fb["timestamp"]
                    resolved = True
                    print(f"    {bar_date} {mode:4s} | entry={entry_price:.2f} ({best['level']}) "
                          f"stop={stop_price:.2f} target={target_price:.2f} | LOSS "
                          f"atr={atr_val:.2f} stop_d={stop_d:.2f}")
                    break
            else:
                if fb["low"] <= target_price:
                    stats["trade_win"] += 1
                    position_free_after = fb["timestamp"]
                    resolved = True
                    print(f"    {bar_date} {mode:4s} | entry={entry_price:.2f} ({best['level']}) "
                          f"stop={stop_price:.2f} target={target_price:.2f} | WIN  "
                          f"atr={atr_val:.2f} stop_d={stop_d:.2f}")
                    break
                if fb["high"] >= stop_price:
                    stats["trade_loss"] += 1
                    position_free_after = fb["timestamp"]
                    resolved = True
                    print(f"    {bar_date} {mode:4s} | entry={entry_price:.2f} ({best['level']}) "
                          f"stop={stop_price:.2f} target={target_price:.2f} | LOSS "
                          f"atr={atr_val:.2f} stop_d={stop_d:.2f}")
                    break
        
        if not resolved:
            stats["no_resolution"] += 1
            if full_window:
                position_free_after = full_window[-1]["timestamp"]
            print(f"    {bar_date} {mode:4s} | entry={entry_price:.2f} ({best['level']}) "
                  f"stop={stop_price:.2f} target={target_price:.2f} | UNRESOLVED "
                  f"post_fill_bars={len(post_fill_bars)}")
    
    print(f"\n  Decision breakdown:")
    for k, v in stats.items():
        print(f"    {k:20s}: {v}")

print(f"\n{'='*60}")
print("  DONE — Paste all this output back to Computer")
print(f"{'='*60}")
