"""
Mean Levels Futures Scanner for TopstepX
=========================================
Scans micro index futures (MNQ, MES, MYM, M2K) for mean level
break setups with confluence scoring.

Uses the project-x-py SDK to fetch market data and compute
CDM/PDM/CMM/PMM levels, then identifies high-confluence break zones.

Usage:
    python futures_scanner.py              # Scan all instruments
    python futures_scanner.py --symbols MNQ MES  # Scan specific instruments
    python futures_scanner.py --min-score 5       # Only show score >= 5
    python futures_scanner.py --json               # Output as JSON

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

# Add parent dir for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mean_levels_calc import compute_mean_levels, find_confluences, classify_setup, compute_atr
from regime_filter import get_cached_regime

# Default instruments: micro index futures
DEFAULT_SYMBOLS = ["MNQ", "MES", "MYM", "M2K"]

# Tick sizes and tick values for each instrument
INSTRUMENT_SPECS = {
    "MNQ": {"tick_size": 0.25, "tick_value": 0.50, "name": "Micro E-mini Nasdaq-100"},
    "MES": {"tick_size": 0.25, "tick_value": 1.25, "name": "Micro E-mini S&P 500"},
    "MYM": {"tick_size": 1.0, "tick_value": 0.50, "name": "Micro E-mini Dow"},
    "M2K": {"tick_size": 0.10, "tick_value": 0.50, "name": "Micro E-mini Russell 2000"},
    # Full-size (for future expansion)
    "NQ":  {"tick_size": 0.25, "tick_value": 5.00, "name": "E-mini Nasdaq-100"},
    "ES":  {"tick_size": 0.25, "tick_value": 12.50, "name": "E-mini S&P 500"},
    "YM":  {"tick_size": 1.0, "tick_value": 5.00, "name": "E-mini Dow"},
    "RTY": {"tick_size": 0.10, "tick_value": 5.00, "name": "E-mini Russell 2000"},
}


async def scan_instrument(client, symbol: str, days: int = 35) -> Optional[dict]:
    """
    Scan a single instrument for mean level setups.

    Args:
        client: Authenticated ProjectX client
        symbol: Futures symbol (e.g., "MNQ")
        days: Days of historical data to fetch (need ~35 for monthly levels)

    Returns:
        dict with scan results or None if no data
    """
    try:
        # Get instrument info
        instrument = await client.get_instrument(symbol)
        contract_id = instrument.id
        tick_size = instrument.tickSize
        tick_value = instrument.tickValue

        # Fetch historical bars — 5-minute for intraday levels, enough days for monthly
        bars = await client.get_bars(symbol, days=days, interval=5, unit=2)

        if bars.is_empty() or len(bars) < 100:
            print(f"  [{symbol}] Insufficient data ({len(bars)} bars)")
            return None

        # Compute mean levels
        levels_data = compute_mean_levels(bars, timezone="America/Chicago")

        # Compute Daily ATR (14-period on daily bars) for stop sizing
        # Daily ATR better matches mean levels (daily/monthly S&R zones)
        # than 5-min ATR which was too tight for futures
        try:
            daily_bars = await client.get_bars(symbol, days=max(days, 35), interval=1, unit=4)
            if daily_bars.is_empty() or len(daily_bars) < 15:
                # Fallback: aggregate 5-min bars to daily
                import polars as pl
                daily_bars = bars.with_columns(
                    pl.col("timestamp").dt.date().alias("date")
                ).group_by("date").agg([
                    pl.col("timestamp").first(),
                    pl.col("open").first(),
                    pl.col("high").max(),
                    pl.col("low").min(),
                    pl.col("close").last(),
                    pl.col("volume").sum(),
                ]).sort("date")
                print(f"  [{symbol}] Daily bars via aggregation ({len(daily_bars)} days)")
            else:
                print(f"  [{symbol}] Daily bars from API ({len(daily_bars)} days)")
            atr_value = compute_atr(daily_bars, period=14)
        except Exception as e:
            print(f"  [{symbol}] Daily bars failed ({e}), aggregating 5-min bars")
            import polars as pl
            daily_bars = bars.with_columns(
                pl.col("timestamp").dt.date().alias("date")
            ).group_by("date").agg([
                pl.col("timestamp").first(),
                pl.col("open").first(),
                pl.col("high").max(),
                pl.col("low").min(),
                pl.col("close").last(),
                pl.col("volume").sum(),
            ]).sort("date")
            atr_value = compute_atr(daily_bars, period=14)

        if not levels_data["levels"]:
            print(f"  [{symbol}] Could not compute mean levels")
            return None

        current_price = levels_data["current_price"]

        # Read cached regime from 8h bar DSS + Lyapunov filter
        # (regime_filter.py runs at each 8h bar close: 10 AM, 6 PM, 2 AM ET)
        regime = get_cached_regime(symbol)
        if regime:
            mode_icon = {"BUY": "▲", "SELL": "▼", "NEUTRAL": "●"}.get(regime["mode"], "?")
            print(f"  [{symbol}] Regime: {mode_icon} {regime['mode']} MODE ({regime['strength']}) "
                  f"DSS={regime['dss_signal']:+d} Lyap={regime['lyap_signal']:+d} "
                  f"Combined={regime['combined_signal']:+d}")
        else:
            print(f"  [{symbol}] No cached regime — run regime_filter.py first")

        # Find confluences
        confluences = find_confluences(levels_data["levels"], threshold_pct=0.3)

        # Classify each level and each confluence zone
        setups = []

        # Check individual levels
        for level in levels_data["levels"]:
            signal = classify_setup(
                current_price=current_price,
                level_price=level["price"],
                yesterday_high=levels_data.get("yesterday_high", current_price),
                yesterday_low=levels_data.get("yesterday_low", current_price),
                tick_size=tick_size,
            )
            if signal:
                setups.append({
                    "type": signal,
                    "level_name": level["name"],
                    "level_price": level["price"],
                    "distance_pct": round(abs(current_price - level["price"]) / current_price * 100, 3),
                    "score": 3,  # Single level = 3 points
                })

        # Check confluence zones
        for zone in confluences:
            signal = classify_setup(
                current_price=current_price,
                level_price=zone["center"],
                yesterday_high=levels_data.get("yesterday_high", current_price),
                yesterday_low=levels_data.get("yesterday_low", current_price),
                tick_size=tick_size,
            )
            if signal:
                setups.append({
                    "type": signal,
                    "level_name": "+".join(zone["levels"]),
                    "level_price": zone["center"],
                    "distance_pct": round(abs(current_price - zone["center"]) / current_price * 100, 3),
                    "score": zone["score"],
                    "confluence_count": zone["level_count"],
                })

        # Sort setups by score descending
        setups.sort(key=lambda x: x["score"], reverse=True)

        return {
            "symbol": symbol,
            "contract_id": contract_id,
            "instrument_name": instrument.name,
            "tick_size": tick_size,
            "tick_value": tick_value,
            "current_price": current_price,
            "atr": atr_value,
            "cdm": levels_data["cdm"],
            "pdm": levels_data["pdm"],
            "cmm": levels_data["cmm"],
            "pmm": levels_data["pmm"],
            "cdm_dir": levels_data.get("cdm_dir"),
            "cmm_dir": levels_data.get("cmm_dir"),
            "today_high": levels_data.get("today_high"),
            "today_low": levels_data.get("today_low"),
            "confluences": confluences,
            "setups": setups,
            "best_score": setups[0]["score"] if setups else 0,
            "setup_count": len(setups),
            "regime": regime,  # Cached 8h regime from DSS + Lyapunov filter
            "timestamp": datetime.now(pytz.timezone("America/New_York")).isoformat(),
        }

    except Exception as e:
        print(f"  [{symbol}] Error: {e}")
        return None


async def run_scanner(
    symbols: list[str] = None,
    min_score: int = 0,
    days: int = 35,
) -> list[dict]:
    """
    Run the mean levels scanner across all instruments.

    Args:
        symbols: List of symbols to scan (default: micro indexes)
        min_score: Minimum score to include in results
        days: Days of history to fetch

    Returns:
        List of scan results sorted by best score
    """
    from project_x_py import ProjectX

    symbols = symbols or DEFAULT_SYMBOLS

    print(f"\n{'='*60}")
    print(f"  MEAN LEVELS FUTURES SCANNER")
    print(f"  {datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d %H:%M:%S ET')}")
    print(f"  Instruments: {', '.join(symbols)}")
    print(f"{'='*60}\n")

    results = []

    async with ProjectX.from_env() as client:
        await client.authenticate()
        account = client.get_account_info()
        print(f"  Account: {account.name} | Balance: ${account.balance:,.2f}\n")

        for symbol in symbols:
            print(f"  Scanning {symbol}...")
            result = await scan_instrument(client, symbol, days=days)
            if result:
                results.append(result)

    # Sort by best score
    results.sort(key=lambda x: x["best_score"], reverse=True)

    # Filter by min score
    if min_score > 0:
        results = [r for r in results if r["best_score"] >= min_score]

    # Print results
    print(f"\n{'='*60}")
    print(f"  SCAN RESULTS")
    print(f"{'='*60}\n")

    if not results:
        print("  No setups found.\n")
        return results

    for r in results:
        print(f"  {r['symbol']} ({r['instrument_name']}) — Price: {r['current_price']}  |  Daily ATR(14): {r['atr']}")
        cdm_dir = r.get('cdm_dir', '')
        cmm_dir = r.get('cmm_dir', '')
        print(f"    CDM: {r['cdm']} ({cdm_dir})  |  PDM: {r['pdm']}  |  CMM: {r['cmm']} ({cmm_dir})  |  PMM: {r['pmm']}")

        # Show 8h regime from cached DSS + Lyapunov filter
        regime = r.get('regime')
        if regime:
            mode_icon = {"BUY": "▲", "SELL": "▼", "NEUTRAL": "●"}.get(regime['mode'], '?')
            print(f"    REGIME: {mode_icon} {regime['mode']} MODE ({regime['strength']}) "
                  f"DSS={regime['dss_signal']:+d} Lyap={regime['lyap_signal']:+d} "
                  f"Combined={regime['combined_signal']:+d}")
        else:
            print(f"    REGIME: N/A (run regime_filter.py)")

        if r["confluences"]:
            for c in r["confluences"]:
                print(f"    CONFLUENCE: {'+'.join(c['levels'])} @ {c['center']} (score: {c['score']})")

        if r["setups"]:
            for s in r["setups"]:
                direction = "▲ BREAK ABOVE" if s["type"] == "break_above" else "▼ BREAK BELOW"
                print(f"    {direction} {s['level_name']} @ {s['level_price']} | "
                      f"Score: {s['score']} | Dist: {s['distance_pct']}%")
        else:
            print("    No break setups (price near levels)")

        print()

    return results


def main():
    parser = argparse.ArgumentParser(description="Mean Levels Futures Scanner")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS, help="Symbols to scan")
    parser.add_argument("--min-score", type=int, default=0, help="Minimum setup score")
    parser.add_argument("--days", type=int, default=35, help="Days of history")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--output", type=str, default=None, help="Save results to file")

    args = parser.parse_args()

    results = asyncio.run(run_scanner(
        symbols=args.symbols,
        min_score=args.min_score,
        days=args.days,
    ))

    if args.json:
        print(json.dumps(results, indent=2, default=str))

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\n  Results saved to {args.output}")

    return results


if __name__ == "__main__":
    main()
