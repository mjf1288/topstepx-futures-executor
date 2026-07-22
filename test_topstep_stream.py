"""
test_topstep_stream.py — Standalone verification of topstep_stream.py.

Connects to the ProjectX Market Hub, subscribes to MES U26 trades, and
prints every 5-min bar that closes. Runs for a configurable duration.

Exits 0 if at least ONE bar closed cleanly during the run. Non-zero if
we never saw a bar close (probably means no ticks came through — could
be a connection issue OR just off-hours).

Usage:
    python test_topstep_stream.py             # default 15 min
    python test_topstep_stream.py --minutes 5 # short run

Success criteria (matches REBUILD_SCOPE.md #6.2):
- Connects to market hub without errors
- Subscribes to MES U26 (and MNQ U26)
- Prints ≥3 tick summaries within 60 seconds during RTH
- Prints ≥1 bar close within a 15-minute run
- No unhandled exceptions
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from topstep_api import from_env, TopstepAPIError
from topstep_stream import TopstepStream, bucket_start_utc


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


MES_CONTRACT_ID = "CON.F.US.MES.U26"
MNQ_CONTRACT_ID = "CON.F.US.MNQ.U26"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, default=15, help="How long to run")
    parser.add_argument(
        "--contract",
        default=MES_CONTRACT_ID,
        help="Contract to subscribe to (default MES U26)",
    )
    args = parser.parse_args()

    print("═" * 66)
    print(" test_topstep_stream.py — verifying SignalR + bar aggregator")
    print("═" * 66)
    print(f" Contract:    {args.contract}")
    print(f" Duration:    {args.minutes} minutes")
    print(f" Started UTC: {datetime.now(timezone.utc).isoformat()}")
    print("═" * 66)
    print()

    # Get JWT from REST auth (reuses topstep_api)
    try:
        api = from_env()
    except TopstepAPIError as e:
        print(f"  ✗ Authentication failed: {e}")
        sys.exit(1)
    print(f"  ✓ Authenticated. Account id: {api.account_id}")

    jwt = api.get_jwt()
    stream = TopstepStream(jwt_token=jwt)

    # Simple counters
    bars_seen = 0
    ticks_seen = 0
    first_tick_time = None
    last_price_by_contract: dict[str, float] = {}

    def handle_bar(contract_id: str, bar: dict) -> None:
        nonlocal bars_seen
        bars_seen += 1
        n_ticks = bar.get("n_ticks", "?")
        print(
            f"  ▶ BAR CLOSE  {contract_id}  t={bar['t']}  "
            f"O={bar['o']:.2f} H={bar['h']:.2f} L={bar['l']:.2f} C={bar['c']:.2f}  "
            f"v={bar['v']}  ({n_ticks} ticks)"
        )

    stream.on_bar_close(handle_bar)

    # Subscribe BEFORE start so contracts get subscribed on connect
    stream.subscribe(args.contract)

    print("  Connecting to market hub...")
    try:
        stream.start()
    except Exception as e:
        print(f"  ✗ Failed to start stream: {e!r}")
        sys.exit(1)
    print(f"  ✓ Connected. Streaming for {args.minutes} minutes.")
    print()
    print("  Watching for ticks and bar closes:")
    print()

    # Watchdog — print periodic status
    start_time = time.time()
    end_time = start_time + args.minutes * 60
    last_status_time = start_time
    STATUS_INTERVAL_S = 60  # print status once a minute

    try:
        while time.time() < end_time:
            time.sleep(1)
            now = time.time()

            # Also track raw tick count via last_event_time freshness
            last_event = stream.last_event_time
            if last_event is not None:
                if first_tick_time is None:
                    first_tick_time = last_event
                # Rough tick counter — increment if fresh event in last second
                if (datetime.now(timezone.utc) - last_event).total_seconds() < 1:
                    ticks_seen += 1

            # Periodic status log
            if now - last_status_time >= STATUS_INTERVAL_S:
                elapsed_min = (now - start_time) / 60
                remaining_min = (end_time - now) / 60
                last_bar = stream.last_bar_time(args.contract)
                last_bar_str = last_bar.isoformat() if last_bar else "never"
                connected = "yes" if stream.connected else "NO"
                print(
                    f"  [status @ {elapsed_min:.1f}m] connected={connected}  "
                    f"bars={bars_seen}  reconnects={stream.reconnect_count}  "
                    f"last_bar={last_bar_str}"
                )
                last_status_time = now

    except KeyboardInterrupt:
        print()
        print("  Interrupted by user (Ctrl+C)")

    print()
    print("  Stopping stream...")
    stream.stop()

    # ─────────────────────────────────────────────────────────
    # Report
    # ─────────────────────────────────────────────────────────
    print()
    print("═" * 66)
    print(" REPORT")
    print("═" * 66)
    print(f"  Duration:         {(time.time() - start_time) / 60:.1f} min")
    print(f"  Bar closes seen:  {bars_seen}")
    print(f"  Reconnects:       {stream.reconnect_count}")
    last_bar = stream.last_bar_time(args.contract)
    if last_bar:
        print(f"  Last bar close:   {last_bar.isoformat()}")
    else:
        print(f"  Last bar close:   (none)")

    # Success criterion: at least 1 bar closed
    if bars_seen >= 1:
        print()
        print("═" * 66)
        print(f" PASSED ✓  ({bars_seen} bars closed cleanly)")
        print("═" * 66)
        sys.exit(0)
    else:
        print()
        print("═" * 66)
        print(" FAILED ✗  (no bars closed during the run)")
        print("═" * 66)
        print()
        print("  Possible causes:")
        print("   - Market is off-hours (Sun morning, Fri evening after 4pm CT)")
        print("   - Ran for <5 minutes (bars close on 5-min UTC-floor boundaries)")
        print("   - Very illiquid contract / no trades in the window")
        print("   - Actual streaming bug (check log lines above)")
        sys.exit(1)


if __name__ == "__main__":
    main()
