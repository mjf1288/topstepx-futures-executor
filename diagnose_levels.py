#!/usr/bin/env python3
"""
Diagnostic tool for MES/MNQ mean level bugs.

Prints EVERYTHING needed to figure out why levels are wrong:
  - Raw bars pulled (first + last few)
  - Timezone / DST state
  - Day boundary bucketing (which bars go into 'today' vs 'yesterday')
  - Running CDM step-by-step for the last 10 bars
  - Final CDM/PDM/CMM/PMM from the engine
  - What levels the executor WOULD place orders at

Run this and paste the entire output.
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import polars as pl
import pytz
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

from mean_levels_calc import compute_mean_levels, calc_period_mean


async def fetch_bars(symbol: str, days: int = 3):
    """Try to fetch bars via project-x-py; fall back to a helpful message."""
    try:
        from project_x_py import TradingSuite
    except ImportError:
        print(f"  ! project_x_py not installed in this venv")
        return None

    api_key = os.environ.get("PROJECT_X_API_KEY")
    username = os.environ.get("PROJECT_X_USERNAME")
    if not api_key or not username:
        print(f"  ! PROJECT_X_API_KEY / PROJECT_X_USERNAME not in .env")
        return None

    try:
        suite = await TradingSuite.create(
            instruments=[symbol],
            timeframes=["5min"],
            initial_days=days,
        )
        client = suite[symbol]
        bars = await client.data.get_data("5min")
        await suite.disconnect()
        return bars
    except Exception as e:
        print(f"  ! fetch failed: {e!r}")
        return None


def print_bar_bucketing(bars: pl.DataFrame, symbol: str, tz_name: str = "America/Chicago"):
    """Show which bars go into which day/month group under the +7h shift."""
    tz = pytz.timezone(tz_name)
    df = bars.sort("timestamp")

    ct = pl.col("timestamp").dt.convert_time_zone(tz_name)
    rolled = ct + pl.duration(hours=7)
    df = df.with_columns([
        ct.alias("ct_wall"),
        rolled.dt.date().alias("session_date"),
        rolled.dt.hour().alias("rolled_hour"),
    ])

    # last 30 bars
    tail = df.tail(30).select(["timestamp", "ct_wall", "session_date", "close"])
    print(f"\n  ── LAST 30 BARS OF {symbol} (5m) ──")
    print(f"  {'UTC timestamp':<26} {'CT wall clock':<26} {'session':<12} {'close':>10}")
    for row in tail.iter_rows(named=True):
        print(f"  {str(row['timestamp']):<26} {str(row['ct_wall']):<26} "
              f"{str(row['session_date']):<12} {row['close']:>10.2f}")


def print_running_cdm(bars: pl.DataFrame, symbol: str, tz_name: str = "America/Chicago"):
    """Recompute CDM step by step for TODAY only and print each bar's running mean."""
    tz = pytz.timezone(tz_name)
    df = bars.sort("timestamp")

    ct = pl.col("timestamp").dt.convert_time_zone(tz_name)
    rolled = ct + pl.duration(hours=7)
    df = df.with_columns([
        ct.alias("ct_wall"),
        rolled.dt.date().alias("session_date"),
    ])

    # Find today's session
    dates = df.select("session_date").unique().sort("session_date").to_series().to_list()
    if not dates:
        return
    today = dates[-1]
    today_bars = df.filter(pl.col("session_date") == today)
    print(f"\n  ── RUNNING CDM FOR {symbol} · session {today} · {len(today_bars)} bars ──")
    print(f"  {'CT wall clock':<26} {'close':>10} {'cum':>12} {'count':>6} {'running_mean':>14}")
    cum, count = 0.0, 0
    for row in today_bars.iter_rows(named=True):
        cum += float(row['close'])
        count += 1
        rmean = cum / count
        print(f"  {str(row['ct_wall']):<26} {row['close']:>10.2f} {cum:>12.2f} {count:>6d} {rmean:>14.4f}")
    print(f"  ═══ FINAL CDM = {cum/count:.4f} ═══" if count else "  no bars")


async def diagnose(symbol: str):
    print(f"\n{'═' * 78}")
    print(f"  {symbol}")
    print(f"{'═' * 78}")

    now_ct = datetime.now(pytz.timezone("America/Chicago"))
    print(f"  now (CT): {now_ct.isoformat()}")
    print(f"  DST active: {bool(now_ct.dst())}  (offset from UTC = {now_ct.utcoffset()})")

    bars = await fetch_bars(symbol, days=3)
    if bars is None or bars.is_empty():
        print(f"  ! no bars fetched — cannot diagnose")
        return

    print(f"  bars fetched: {len(bars)}")
    print(f"  first bar timestamp: {bars['timestamp'][0]}")
    print(f"  last  bar timestamp: {bars['timestamp'][-1]}")
    print(f"  last close: {bars['close'][-1]}")

    print_bar_bucketing(bars, symbol)
    print_running_cdm(bars, symbol)

    print(f"\n  ── ENGINE OUTPUT ({symbol}) ──")
    result = compute_mean_levels(bars)
    print(f"  current_price = {result['current_price']}")
    print(f"  CDM = {result['cdm']}   (dir={result.get('cdm_dir')})")
    print(f"  PDM = {result['pdm']}")
    print(f"  CMM = {result['cmm']}   (dir={result.get('cmm_dir')})")
    print(f"  PMM = {result['pmm']}")
    print(f"  today  H/L: {result.get('today_high')} / {result.get('today_low')}")
    print(f"  yday   H/L: {result.get('yesterday_high')} / {result.get('yesterday_low')}")

    print(f"\n  ── LEVELS THE EXECUTOR WOULD USE ──")
    price = result['current_price']
    for lvl in result['levels']:
        side = "BELOW (buy zone)" if lvl['price'] < price else "ABOVE (sell zone)"
        print(f"    {lvl['name']}: {lvl['price']:>10.4f}  ({side}, distance={abs(price - lvl['price']):.2f})")


async def main():
    for sym in ("MES", "MNQ"):
        try:
            await diagnose(sym)
        except Exception as e:
            print(f"\n  ! {sym} failed: {e!r}")


if __name__ == "__main__":
    asyncio.run(main())
