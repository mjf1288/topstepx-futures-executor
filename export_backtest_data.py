"""
Export Historical Bar Data for AutoResearch Backtesting
========================================================
Pulls 8h, 5m, and daily bars from TopstepX for each symbol
and saves them as JSON in backtest_data/.

This version fetches BOTH the current and prior quarterly contracts
and stitches them together to get 4+ months of continuous data.

Usage:
    python export_backtest_data.py
    python export_backtest_data.py --days 180
    python export_backtest_data.py --symbols MNQ MES

Author: Matthew Foster
"""

import asyncio
import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import pytz
import aiohttp

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "backtest_data")
sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

# CME quarterly months: H=Mar, M=Jun, U=Sep, Z=Dec
QUARTERLY_MONTHS = ["H", "M", "U", "Z"]
QUARTERLY_MONTH_NUMS = {"H": 3, "M": 6, "U": 9, "Z": 12}

# Unit codes for TopstepX API
# 1=second, 2=minute, 3=hour, 4=day
UNIT_NAMES = {2: "min", 3: "hour", 4: "day"}


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


def get_contract_chain(symbol: str, current_contract_id: str, num_prior: int = 2) -> list[str]:
    """Build a list of contract IDs: [oldest_prior, ..., current].
    
    Example: current = CON.F.US.MNQ.M26
    Returns: [CON.F.US.MNQ.Z25, CON.F.US.MNQ.H26, CON.F.US.MNQ.M26]
    """
    parts = current_contract_id.split(".")
    sym = parts[3]
    month_code = parts[4][0]
    year_suffix = int(parts[4][1:])
    
    idx = QUARTERLY_MONTHS.index(month_code)
    
    contracts = []
    for step in range(num_prior, 0, -1):
        prior_idx = idx - step
        prior_year = year_suffix
        while prior_idx < 0:
            prior_idx += 4
            prior_year -= 1
        prior_month = QUARTERLY_MONTHS[prior_idx]
        contracts.append(f"CON.F.US.{sym}.{prior_month}{prior_year:02d}")
    
    contracts.append(current_contract_id)
    return contracts


async def fetch_contract_bars(client, contract_id: str, days: int,
                               interval: int, unit: int) -> list[dict]:
    """Fetch bars for a specific contract ID via raw API."""
    token = client.get_session_token()
    base_url = client.base_url
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    tz = pytz.timezone("America/Chicago")
    now = datetime.now(tz)
    start = now - timedelta(days=days)
    
    async with aiohttp.ClientSession() as http:
        payload = {
            "contractId": contract_id,
            "live": False,
            "startTime": start.astimezone(pytz.UTC).isoformat(),
            "endTime": now.astimezone(pytz.UTC).isoformat(),
            "unit": unit,
            "unitNumber": interval,
            "limit": 10000,
            "includePartialBar": True,
        }
        async with http.post(f"{base_url}/History/retrieveBars",
                             json=payload, headers=headers) as resp:
            result = await resp.json()
    
    if not result.get("success") or not result.get("bars"):
        return []
    
    return result["bars"]


async def fetch_and_stitch(client, symbol: str, contract_chain: list[str],
                            days: int, interval: int, unit: int) -> "pl.DataFrame":
    """Fetch bars from multiple contracts and stitch them into a continuous series."""
    import polars as pl
    
    all_dfs = []
    
    for contract_id in contract_chain:
        print(f"      Fetching {contract_id}...", end=" ", flush=True)
        raw_bars = await fetch_contract_bars(client, contract_id, days, interval, unit)
        
        if not raw_bars:
            print("no data")
            continue
        
        df = (
            pl.DataFrame(raw_bars)
            .sort("t")
            .rename({"t": "timestamp", "o": "open", "h": "high",
                     "l": "low", "c": "close", "v": "volume"})
        )
        
        # Parse timestamps
        try:
            df = df.with_columns(
                pl.col("timestamp").str.to_datetime()
                .dt.replace_time_zone("UTC")
                .dt.convert_time_zone("America/Chicago")
            )
        except Exception:
            try:
                df = df.with_columns(
                    pl.col("timestamp").cast(pl.Datetime)
                    .dt.replace_time_zone("UTC")
                    .dt.convert_time_zone("America/Chicago")
                )
            except Exception:
                print(f"timestamp parse error")
                continue
        
        print(f"{len(df)} bars")
        all_dfs.append((contract_id, df))
    
    if not all_dfs:
        return pl.DataFrame()
    
    if len(all_dfs) == 1:
        return all_dfs[0][1]
    
    # Stitch: adjust older contracts by the roll gap
    # Work backwards from the newest contract
    stitched = all_dfs[-1][1]  # Start with newest (current contract)
    
    for i in range(len(all_dfs) - 2, -1, -1):
        old_contract_id, old_df = all_dfs[i]
        
        # Find the overlap point: last old bar before first new bar
        first_new_ts = stitched["timestamp"].min()
        old_before = old_df.filter(pl.col("timestamp") < first_new_ts)
        
        if old_before.is_empty():
            continue
        
        # Roll gap = first new bar open - last old bar close
        last_old_close = float(old_before["close"][-1])
        first_new_open = float(stitched["open"][0])
        gap = first_new_open - last_old_close
        
        # Adjust old prices
        old_adjusted = old_before.with_columns([
            (pl.col("open") + gap).alias("open"),
            (pl.col("high") + gap).alias("high"),
            (pl.col("low") + gap).alias("low"),
            (pl.col("close") + gap).alias("close"),
        ])
        
        stitched = pl.concat([old_adjusted, stitched]).sort("timestamp")
        print(f"      Stitched {old_contract_id}: +{len(old_before)} bars (gap: {gap:+.2f} pts)")
    
    return stitched


async def export_data(symbols: list[str], days: int = 180):
    """Export bar data for all symbols and timeframes."""
    from project_x_py import ProjectX

    os.makedirs(DATA_DIR, exist_ok=True)

    tz = pytz.timezone("America/New_York")
    now = datetime.now(tz)

    print(f"{'='*60}")
    print(f"  EXPORTING BACKTEST DATA")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"  Lookback: {days} days (with contract stitching)")
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

                # Build contract chain (current + 2 prior quarters)
                chain = get_contract_chain(symbol, contract_id, num_prior=2)
                print(f"    Chain: {' → '.join(chain)}")

                # ── 8-hour bars (for DSS regime filter) ──
                print(f"\n    8h bars:")
                bars_8h = await fetch_and_stitch(
                    client, symbol, chain, days=days, interval=8, unit=3
                )
                if not bars_8h.is_empty():
                    data_8h = bars_to_json(bars_8h)
                    with open(os.path.join(DATA_DIR, f"{symbol}_8h.json"), "w") as f:
                        json.dump(data_8h, f, indent=2)
                    print(f"    → {len(data_8h)} total 8h bars saved")
                else:
                    print(f"    → No 8h bars!")

                # ── 5-minute bars (for mean levels + trade simulation) ──
                print(f"\n    5m bars:")
                bars_5m = await fetch_and_stitch(
                    client, symbol, chain, days=days, interval=5, unit=2
                )
                if not bars_5m.is_empty():
                    data_5m = bars_to_json(bars_5m)
                    with open(os.path.join(DATA_DIR, f"{symbol}_5m.json"), "w") as f:
                        json.dump(data_5m, f, indent=2)
                    print(f"    → {len(data_5m)} total 5m bars saved")
                else:
                    print(f"    → No 5m bars!")

                # ── Daily bars (for ATR calculation) ──
                print(f"\n    Daily bars:")
                bars_daily = await fetch_and_stitch(
                    client, symbol, chain, days=days, interval=1, unit=4
                )
                if not bars_daily.is_empty():
                    data_daily = bars_to_json(bars_daily)
                    with open(os.path.join(DATA_DIR, f"{symbol}_daily.json"), "w") as f:
                        json.dump(data_daily, f, indent=2)
                    print(f"    → {len(data_daily)} total daily bars saved")
                else:
                    print(f"    → No daily bars!")

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
    parser.add_argument("--days", type=int, default=180,
                        help="Lookback in days (default: 180 = ~6 months)")
    args = parser.parse_args()

    asyncio.run(export_data(symbols=args.symbols, days=args.days))


if __name__ == "__main__":
    main()
