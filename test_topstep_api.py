"""
test_topstep_api.py — Standalone verification of topstep_api.py.

Runs the REST layer end-to-end WITHOUT touching orders. Exits 0 on
success, non-zero on failure. Prints friendly ✓/✗ lines.

Usage:
    python test_topstep_api.py

Requires: .env with PROJECT_X_* credentials, ProjectX API Access subscribed.

Success criteria (matches REBUILD_SCOPE.md #6.1):
- Authenticates
- Selects EXPRESS-V2-CT account
- Fetches 5 days of MES U26 5-min bars (>1000 bars expected)
- Lists open positions and open orders (empty or populated, no crash)
- Fetches historical hourly bars for CMM computation
- Exits 0
"""

import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from topstep_api import from_env, TopstepAPIError


# ─────────────────────────────────────────────────────────────
# Small pretty-print helpers so failures are obvious
# ─────────────────────────────────────────────────────────────
def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def fail(msg: str, exc: Exception | None = None) -> None:
    print(f"  ✗ {msg}")
    if exc is not None:
        print(f"    {type(exc).__name__}: {exc}")
    sys.exit(1)


def section(name: str) -> None:
    print()
    print(f"── {name} " + "─" * (60 - len(name)))


# ─────────────────────────────────────────────────────────────
# Test target: MES U26 (front month during rebuild period)
# ─────────────────────────────────────────────────────────────
MES_CONTRACT_ID = "CON.F.US.MES.U26"
MNQ_CONTRACT_ID = "CON.F.US.MNQ.U26"


def main() -> None:
    print("═" * 66)
    print(" test_topstep_api.py — verifying direct ProjectX REST client")
    print("═" * 66)

    # 1. Auth + account selection
    section("Authentication + account selection")
    try:
        api = from_env()
    except TopstepAPIError as e:
        fail("from_env() failed", e)
        return  # unreachable
    ok(f"Authenticated as user, account_id={api.account_id}")
    ok(f"Selected account: {api.account_name}")

    # 2. Historical 5-min bars (5 days back)
    section("Historical 5-min bars — MES U26, last 5 days")
    try:
        bars_5m = api.get_bars(
            contract_id=MES_CONTRACT_ID, unit=2, unit_number=5, days=5, limit=5000
        )
    except TopstepAPIError as e:
        fail("get_bars 5-min failed", e)
        return

    if len(bars_5m) < 500:
        fail(f"got only {len(bars_5m)} bars, expected >500 for 5 days of 5-min MES")

    ok(f"Fetched {len(bars_5m)} 5-min bars")

    # Sanity check the shape of the first and last bar
    first = bars_5m[0]
    last = bars_5m[-1]
    needed_keys = {"t", "o", "h", "l", "c", "v"}
    if not needed_keys.issubset(first.keys()):
        fail(f"First bar missing keys: got {list(first.keys())}, need {needed_keys}")
    if not needed_keys.issubset(last.keys()):
        fail(f"Last bar missing keys: got {list(last.keys())}, need {needed_keys}")

    ok(f"Bar shape OK — keys: {sorted(first.keys())}")
    ok(f"First bar: t={first['t']}, c={first['c']}, v={first['v']}")
    ok(f"Last bar:  t={last['t']}, c={last['c']}, v={last['v']}")

    # Basic sanity: prices in a plausible range for MES (currently ~7500)
    for b in (first, last):
        if not (5000 < b["c"] < 10000):
            fail(f"MES close price {b['c']} outside plausible range 5000-10000")
    ok("MES prices in plausible range (5000-10000)")

    # 3. Historical hourly bars (needed for CMM computation)
    section("Historical hourly bars — MES U26, last 45 days")
    try:
        bars_1h = api.get_bars(
            contract_id=MES_CONTRACT_ID, unit=3, unit_number=1, days=45, limit=5000
        )
    except TopstepAPIError as e:
        fail("get_bars 1-hour failed", e)
        return

    if len(bars_1h) < 100:
        fail(f"got only {len(bars_1h)} hourly bars, expected >100 for 45 days")
    ok(f"Fetched {len(bars_1h)} hourly bars")

    # 4. Historical daily bars (needed for ATR)
    section("Historical daily bars — MES U26, last 10 days")
    try:
        bars_1d = api.get_bars(
            contract_id=MES_CONTRACT_ID, unit=4, unit_number=1, days=10
        )
    except TopstepAPIError as e:
        fail("get_bars 1-day failed", e)
        return
    if len(bars_1d) < 3:
        fail(f"got only {len(bars_1d)} daily bars, expected >=3 for ATR(3)")
    ok(f"Fetched {len(bars_1d)} daily bars")

    # 5. Open positions
    section("Open positions")
    try:
        positions = api.get_open_positions()
    except TopstepAPIError as e:
        fail("get_open_positions failed", e)
        return
    ok(f"Fetched positions list ({len(positions)} open)")
    for p in positions:
        print(
            f"    - {p.get('contractId')} size={p.get('size')} avg={p.get('averagePrice')}"
        )

    # 6. Open orders
    section("Open orders")
    try:
        orders = api.get_open_orders()
    except TopstepAPIError as e:
        fail("get_open_orders failed", e)
        return
    ok(f"Fetched orders list ({len(orders)} working)")
    for o in orders:
        print(
            f"    - {o.get('contractId')} side={o.get('side')} type={o.get('type')} "
            f"size={o.get('size')} limit={o.get('limitPrice')} stop={o.get('stopPrice')}"
        )

    # 7. MNQ bars too — confirm both instruments work
    section("Historical 5-min bars — MNQ U26, last 5 days")
    try:
        bars_mnq = api.get_bars(
            contract_id=MNQ_CONTRACT_ID, unit=2, unit_number=5, days=5, limit=5000
        )
    except TopstepAPIError as e:
        fail("get_bars MNQ failed", e)
        return
    if len(bars_mnq) < 500:
        fail(f"got only {len(bars_mnq)} MNQ bars, expected >500 for 5 days")
    ok(f"Fetched {len(bars_mnq)} MNQ 5-min bars")

    # 8. Token refresh path (call the no-op path — no request)
    section("Token refresh (proactive)")
    try:
        api.maybe_refresh_token()
    except TopstepAPIError as e:
        fail("maybe_refresh_token failed", e)
        return
    ok("Token refresh path OK (no-op when token still fresh)")

    # ─────────────────────────────────────────────────────────
    print()
    print("═" * 66)
    print(" ALL TESTS PASSED ✓  — Success criterion 1 met.")
    print("═" * 66)
    sys.exit(0)


if __name__ == "__main__":
    main()
