"""
Mean Levels Session Runner
===========================
Runs the scanner+executor in a loop every SCAN_INTERVAL minutes
for the duration of the current trading window.

Called by the scheduled task at the start of each window.
Exits cleanly when the window ends or the flatten deadline approaches.

Usage:
    python run_session.py                  # Live mode (default)
    python run_session.py --dry-run        # Dry run mode
    python run_session.py --interval 5     # Scan every 5 min

Author: Matthew Foster
"""

import asyncio
import argparse
import os
import sys
import time
from datetime import datetime

import pytz
from dotenv import load_dotenv

# Load .env
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from futures_executor import (
    run_full_pipeline,
    is_in_trading_window,
    should_flatten_now,
    minutes_until_flatten,
    FLATTEN_DEADLINE_ET,
    TRADING_WINDOWS,
)

DEFAULT_INTERVAL = 10  # minutes


async def run_session(
    interval_min: int = DEFAULT_INTERVAL,
    live: bool = True,
):
    """
    Run the scan+execute loop for the current trading window.
    """
    tz = pytz.timezone("America/New_York")

    print(f"\n{'='*65}")
    print(f"  MEAN LEVELS SESSION RUNNER")
    print(f"  {datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S ET')}")
    print(f"  Mode: {'LIVE' if live else 'DRY RUN'}")
    print(f"  Scan interval: {interval_min} min")
    print(f"{'='*65}\n")

    scan_count = 0

    while True:
        now = datetime.now(tz)

        # Check if we should stop
        if should_flatten_now(now):
            print(f"\n  [{now.strftime('%H:%M')}] Past flatten deadline. Stopping session.")
            break

        # Check if we're still in a trading window
        in_window, window_label, _window_min_score = is_in_trading_window(now)
        mins_to_flatten = minutes_until_flatten(now)

        if not in_window:
            print(f"\n  [{now.strftime('%H:%M')}] Outside trading windows ({window_label}). Stopping session.")
            break

        # Don't start a new scan if less than 15 min to flatten
        if mins_to_flatten < 15:
            print(f"\n  [{now.strftime('%H:%M')}] Only {mins_to_flatten} min to flatten. Stopping session.")
            break

        # Run the pipeline
        scan_count += 1
        print(f"\n  ── Scan #{scan_count} [{window_label}] {now.strftime('%H:%M:%S ET')} ──\n")

        try:
            await run_full_pipeline(live=live)
        except Exception as e:
            print(f"\n  ERROR during scan: {e}\n")

        # Wait for next interval
        now_after = datetime.now(tz)
        in_window_after, _, _ = is_in_trading_window(now_after)
        if not in_window_after or should_flatten_now(now_after):
            break

        next_scan = interval_min * 60
        print(f"\n  Next scan in {interval_min} min...")
        await asyncio.sleep(next_scan)

    print(f"\n  Session complete. Ran {scan_count} scan(s).")
    print(f"  {datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S ET')}\n")


def main():
    parser = argparse.ArgumentParser(description="Mean Levels Session Runner")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Scan interval in minutes")
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode (no live orders)")
    args = parser.parse_args()

    asyncio.run(run_session(
        interval_min=args.interval,
        live=not args.dry_run,
    ))


if __name__ == "__main__":
    main()
