"""Collect TopstepX bars into the local data store, then rebuild continuous series.

    python collect_data.py

Safe to rerun as often as you like: it only fetches bars it doesn't already
have. First run backfills from --since; later runs just top up. Read-only
against the broker: it requests history and never touches orders or positions.

Options:
    --symbols MNQ MES MGC MCL     roots to collect (MYM also supported)
    --since 2024-01-01            how far back to backfill
    --timeframes 1m 5m 15m 1h 4h 1d
    --store ~/topstep-data        or set TOPSTEP_DATA_DIR
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from market_data import TIMEFRAMES, Collector, build_root, contracts_between, default_store
from market_data.continuous import ROLL_TF


def make_api():
    """Authenticate only. Collection never needs an account selected."""
    from topstep_api import TopstepAPI
    username = os.environ.get("PROJECT_X_USERNAME")
    api_key = os.environ.get("PROJECT_X_API_KEY")
    if not username or not api_key:
        sys.exit("Missing PROJECT_X_USERNAME / PROJECT_X_API_KEY (check .env)")
    # The client requires an account name, but history calls never use it.
    api = TopstepAPI(username=username, api_key=api_key,
                     account_name=os.environ.get("PROJECT_X_ACCOUNT_NAME") or "history-only")
    api.authenticate()
    return api


def main(argv=None, api=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", nargs="+", default=["MNQ", "MES", "MGC", "MCL"],
                        type=str.upper)
    parser.add_argument("--since", type=date.fromisoformat, default=date(2024, 1, 1))
    parser.add_argument("--timeframes", nargs="+", default=list(TIMEFRAMES),
                        choices=list(TIMEFRAMES))
    parser.add_argument("--store", type=Path, default=None)
    args = parser.parse_args(argv)

    load_dotenv()
    store = (args.store or default_store()).expanduser()
    now = now or datetime.now(timezone.utc)
    timeframes = list(dict.fromkeys(args.timeframes + [ROLL_TF]))  # rolls need 1h
    api = api or make_api()
    collector = Collector(api, store, now=lambda: now)

    print(f"Store: {store}")
    print(f"Symbols: {' '.join(args.symbols)} | since {args.since} | {' '.join(timeframes)}")
    failures = 0
    for root in args.symbols:
        contracts = contracts_between(root, args.since, now.date())
        print(f"\n{root}: {len(contracts)} contract months "
              f"({contracts[0].symbol} .. {contracts[-1].symbol})")
        for contract in contracts:
            for tf in timeframes:
                entry = collector.collect_safe(contract, tf)
                failures += "error" in entry
        summary = build_root(store, root, contracts, args.timeframes)
        for tf, (n, first, last) in summary.items():
            print(f"  continuous {tf:>3}: {n:>9,} bars  {first:%Y-%m-%d} -> {last:%Y-%m-%d %H:%M} UTC")

    print(f"\nDone. {failures} failed request group(s)." if failures
          else "\nDone. All requests succeeded.")
    print(f"Continuous files: {store / 'continuous'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
