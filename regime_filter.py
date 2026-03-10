"""
Regime Filter — DSS Bressert + Lyapunov HP on 8h Bars
======================================================
Determines BUY MODE, SELL MODE, or NEUTRAL for each instrument
based on 8-hour bar data from the CME futures session.

8h bar boundaries (CME Globex):
  - 10:00 AM ET → 6:00 PM ET
  -  6:00 PM ET → 2:00 AM ET
  -  2:00 AM ET → 10:00 AM ET

This script runs at each 8h bar close to update the regime.
The regime is cached to regime_state.json so the scanner/executor
can read it without recomputing every 10 minutes.

Usage:
    python regime_filter.py              # Update regime for all instruments
    python regime_filter.py --symbols MNQ MES  # Specific instruments
    python regime_filter.py --dry-run    # Show regime without saving

Author: Matthew Foster
"""

import asyncio
import argparse
import json
import os
import sys
from datetime import datetime
from typing import Optional

import pytz
from dotenv import load_dotenv

# Load .env from script directory
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trend_filters import compute_trend_bias

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
DEFAULT_SYMBOLS = ["MNQ", "MES", "MYM", "M2K"]
REGIME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regime_state.json")

# 8h bar parameters for DSS + Lyapunov
# Need ~65 bars (65 × 8h = ~21 days of 3 bars/day) for reliable DSS + HP filter
BARS_8H_DAYS = 60  # Fetch 60 days to get plenty of 8h bars


def load_regime_state() -> dict:
    """Load the current regime state from file."""
    if os.path.exists(REGIME_FILE):
        with open(REGIME_FILE, "r") as f:
            return json.load(f)
    return {"instruments": {}, "last_updated": None}


def save_regime_state(state: dict):
    """Save regime state to file."""
    with open(REGIME_FILE, "w") as f:
        json.dump(state, f, indent=2)


async def compute_instrument_regime(client, symbol: str) -> Optional[dict]:
    """
    Compute the regime (BUY/SELL/NEUTRAL) for a single instrument
    using DSS Bressert + Lyapunov HP on 8-hour bars.

    Args:
        client: Authenticated ProjectX client
        symbol: Futures symbol

    Returns:
        dict with regime data or None
    """
    try:
        # Fetch 8-hour bars (unit=3 = hours, interval=8)
        bars_8h = await client.get_bars(symbol, days=BARS_8H_DAYS, interval=8, unit=3)

        if bars_8h.is_empty() or len(bars_8h) < 20:
            print(f"  [{symbol}] Insufficient 8h bars ({len(bars_8h)})")
            return None

        print(f"  [{symbol}] Loaded {len(bars_8h)} 8h bars")

        # Extract HLC arrays
        highs = bars_8h["high"].to_list()
        lows = bars_8h["low"].to_list()
        closes = bars_8h["close"].to_list()

        # Latest bar info
        last_bar = bars_8h[-1]
        last_ts = last_bar["timestamp"].item()
        last_close = closes[-1]

        # Compute trend bias on 8h bars
        bias = compute_trend_bias(highs, lows, closes)

        # Determine regime
        # BUY MODE:  mean levels below price = support → buy bounces
        # SELL MODE: mean levels above price = resistance → sell rejections
        # NEUTRAL:   skip — no clear directional edge
        regime = bias["bias"]  # BULLISH / BEARISH / NEUTRAL

        # Map to trading mode
        if regime == "BULLISH":
            mode = "BUY"
        elif regime == "BEARISH":
            mode = "SELL"
        else:
            mode = "NEUTRAL"

        result = {
            "symbol": symbol,
            "mode": mode,                          # BUY / SELL / NEUTRAL
            "regime": regime,                       # BULLISH / BEARISH / NEUTRAL
            "strength": bias["strength"],           # STRONG / MODERATE / WEAK
            "dss_signal": bias["dss_signal"],
            "lyap_signal": bias["lyap_signal"],
            "combined_signal": bias["combined_signal"],
            "can_break_above": bias["can_break_above"],
            "can_break_below": bias["can_break_below"],
            "dss_last": bias["dss_last"],
            "dss_signal_line": bias["dss_signal_line"],
            "lyap_last": bias["lyap_last"],
            "last_8h_close": last_close,
            "last_8h_bar_ts": str(last_ts),
            "bars_used": len(bars_8h),
            "computed_at": datetime.now(pytz.timezone("America/New_York")).isoformat(),
        }

        mode_icon = {"BUY": "▲", "SELL": "▼", "NEUTRAL": "●"}[mode]
        print(f"  [{symbol}] {mode_icon} {mode} MODE ({bias['strength']}) "
              f"DSS={bias['dss_signal']:+d} Lyap={bias['lyap_signal']:+d} "
              f"Combined={bias['combined_signal']:+d} "
              f"| Last 8h close: {last_close}")

        return result

    except Exception as e:
        print(f"  [{symbol}] Error computing regime: {e}")
        import traceback
        traceback.print_exc()
        return None


async def update_regime(
    symbols: list[str] = None,
    dry_run: bool = False,
) -> dict:
    """
    Update the regime state for all instruments.

    Args:
        symbols: List of symbols (default: micro indexes)
        dry_run: If True, compute but don't save

    Returns:
        dict with regime state
    """
    from project_x_py import ProjectX

    symbols = symbols or DEFAULT_SYMBOLS
    tz = pytz.timezone("America/New_York")
    now = datetime.now(tz)

    print(f"\n{'='*65}")
    print(f"  REGIME FILTER — DSS + Lyapunov HP (8h bars)")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Instruments: {', '.join(symbols)}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE UPDATE'}")
    print(f"{'='*65}\n")

    state = load_regime_state()
    state["last_updated"] = now.isoformat()

    async with ProjectX.from_env() as client:
        await client.authenticate()
        account = client.get_account_info()
        print(f"  Account: {account.name} | Balance: ${account.balance:,.2f}\n")

        for symbol in symbols:
            result = await compute_instrument_regime(client, symbol)
            if result:
                state["instruments"][symbol] = result

    # Summary
    print(f"\n{'='*65}")
    print(f"  REGIME SUMMARY")
    print(f"{'='*65}")
    for sym, data in state.get("instruments", {}).items():
        mode = data.get("mode", "UNKNOWN")
        strength = data.get("strength", "?")
        combined = data.get("combined_signal", 0)
        icon = {"BUY": "▲", "SELL": "▼", "NEUTRAL": "●"}.get(mode, "?")
        print(f"  {icon} {sym}: {mode} ({strength}) Combined={combined:+d}")
    print(f"{'='*65}\n")

    if not dry_run:
        save_regime_state(state)
        print(f"  Regime saved to {REGIME_FILE}\n")
    else:
        print(f"  (Dry run — not saved)\n")

    return state


def get_cached_regime(symbol: str) -> Optional[dict]:
    """
    Read the cached regime for a symbol.
    Called by the scanner/executor to get the current BUY/SELL/NEUTRAL mode.

    Args:
        symbol: Instrument symbol

    Returns:
        dict with regime data, or None if not cached
    """
    state = load_regime_state()
    return state.get("instruments", {}).get(symbol)


def main():
    parser = argparse.ArgumentParser(description="Regime Filter — DSS + Lyapunov on 8h Bars")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS, help="Symbols to scan")
    parser.add_argument("--dry-run", action="store_true", help="Show regime without saving")
    args = parser.parse_args()

    asyncio.run(update_regime(
        symbols=args.symbols,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    main()
