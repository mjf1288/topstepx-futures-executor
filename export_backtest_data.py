"""
Export Historical Bar Data for AutoResearch Backtesting
========================================================
Pulls 8h, 5m, and daily bars from TopstepX for each symbol
and saves them as JSON in backtest_data/.

Run once to populate the data, then the eval harness uses it.

Usage:
    python export_backtest_data.py
    python export_backtest_data.py --days 180  # Override lookback
    python export_backtest_data.py --symbols MNQ MES

Author: Matthew Foster
"""

import asyncio
import argparse
import json
import os
import sys
from datetime import datetime

import pytz

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "backtest_data")
sys.path.insert(0, SCRIPT_DIR)

# Import roll stitching from the main system
from run_regime_session import fetch_bars_with_rollstitch

from dotenv import load_dotenv
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))


def bars_to_json(bars) -> list[dict]:
    """Convert a Polars DataFrame of bars to a JSON-serializable list."""
    records = []
    for row in bars.iter_rows(named=True):
        ts = row["timestamp"]
        if hasattr(ts, "isoformat"):
            ts_str = ts.isoformat()
        else:
            ts_str = str(ts)

        records.append({
            "timestamp": ts_str,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": int(row.get("volume", 0)),
        })
    return records


async def export_data(symbols: list[str], days: int = 120):
    """Export bar data for all symbols and timeframes."""
    from project_x_py import ProjectX

    os.makedirs(DATA_DIR, exist_ok=True)

    tz = pytz.timezone("America/New_York")
    now = datetime.now(tz)

    print(f"{'='*60}")
    print(f"  EXPORTING BACKTEST DATA")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"  Lookback: {days} days")
    print(f"  Output:   {DATA_DIR}/")
    print(f"{'='*60}")

    async with ProjectX.from_env() as client:
        await client.authenticate()
        account = client.get_account_info()
        print(f"\n  Account: {account.name}")
        print(f"  Balance: ${account.balance:,.2f}\n")

        for symbol in symbols:
            print(f"\n  [{symbol}] Fetching data...")

            try:
                instrument = await client.get_instrument(symbol)
                contract_id = instrument.id
                print(f"    Contract: {contract_id}")

                # ── 8-hour bars (for DSS regime filter) ──
                print(f"    8h bars...", end=" ", flush=True)
                bars_8h = await fetch_bars_with_rollstitch(
                    client, symbol, contract_id,
                    days=days, interval=8, unit=3, min_bars=20,
                )
                data_8h = bars_to_json(bars_8h)
                filepath_8h = os.path.join(DATA_DIR, f"{symbol}_8h.json")
                with open(filepath_8h, "w") as f:
                    json.dump(data_8h, f, indent=2)
                print(f"{len(data_8h)} bars saved")

                # ── 5-minute bars (for mean levels + trade simulation) ──
                # Note: 5m bars over long periods can be large. We fetch in chunks.
                print(f"    5m bars...", end=" ", flush=True)
                bars_5m = await fetch_bars_with_rollstitch(
                    client, symbol, contract_id,
                    days=days, interval=5, unit=2, min_bars=100,
                )
                data_5m = bars_to_json(bars_5m)
                filepath_5m = os.path.join(DATA_DIR, f"{symbol}_5m.json")
                with open(filepath_5m, "w") as f:
                    json.dump(data_5m, f, indent=2)
                print(f"{len(data_5m)} bars saved")

                # ── Daily bars (for ATR calculation) ──
                print(f"    Daily bars...", end=" ", flush=True)
                bars_daily = await fetch_bars_with_rollstitch(
                    client, symbol, contract_id,
                    days=days, interval=1, unit=4, min_bars=15,
                )
                data_daily = bars_to_json(bars_daily)
                filepath_daily = os.path.join(DATA_DIR, f"{symbol}_daily.json")
                with open(filepath_daily, "w") as f:
                    json.dump(data_daily, f, indent=2)
                print(f"{len(data_daily)} bars saved")

                print(f"    [{symbol}] Done — {len(data_8h)} 8h + {len(data_5m)} 5m + {len(data_daily)} daily bars")

            except Exception as e:
                print(f"    [{symbol}] ERROR: {e}")
                import traceback
                traceback.print_exc()

    # Summary
    print(f"\n{'='*60}")
    print(f"  EXPORT COMPLETE")
    print(f"{'='*60}")
    
    total_files = 0
    total_size = 0
    for fname in sorted(os.listdir(DATA_DIR)):
        if fname.endswith(".json"):
            fpath = os.path.join(DATA_DIR, fname)
            fsize = os.path.getsize(fpath)
            total_files += 1
            total_size += fsize
            print(f"  {fname}: {fsize / 1024:.0f} KB")
    
    print(f"\n  Total: {total_files} files, {total_size / 1024 / 1024:.1f} MB")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Export TopstepX bar data for backtesting")
    parser.add_argument("--symbols", nargs="+", default=["MNQ", "MES", "MYM"])
    parser.add_argument("--days", type=int, default=120,
                        help="Lookback in days (default: 120 = ~4 months)")
    args = parser.parse_args()

    asyncio.run(export_data(symbols=args.symbols, days=args.days))


if __name__ == "__main__":
    main()
