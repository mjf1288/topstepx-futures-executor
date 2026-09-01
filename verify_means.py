"""Direct-from-broker mean computation for auditing engine CDM/PDM/CMM/PMM.

Runs the exact same math the engine uses, but standalone. If the engine
reports a mean that doesn't match what this script says, we know either
the engine's cached state has drifted, or the input data window is wrong.

Usage:
    python verify_means.py
    python verify_means.py --symbol MES
"""
import argparse
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import pytz
load_dotenv()
from topstep_api import from_env

CT = pytz.timezone("America/Chicago")


def get_futures_day(ct_time):
    """Futures trading day rolls at 5pm CT."""
    if ct_time.hour >= 17:
        return ct_time.date() + timedelta(days=1)
    return ct_time.date()


def get_futures_month(ct_time):
    d = get_futures_day(ct_time)
    return (d.year, d.month)


CONTRACT_MAP = {
    "MNQ": "CON.F.US.MNQ.U26",
    "MES": "CON.F.US.MES.U26",
    "MYM": "CON.F.US.MYM.U26",
    "MGC": "CON.F.US.MGC.V26",
    "MCL": "CON.F.US.MCL.V26",   # front month rolls monthly
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", help="restrict to one symbol (MNQ/MES/MYM/MGC)")
    parser.add_argument("--hourly-days", type=int, default=45,
                        help="how many days of hourly history to pull (default 45)")
    args = parser.parse_args()

    api = from_env()
    now_utc = datetime.now(timezone.utc)
    now_ct = now_utc.astimezone(CT)
    today = get_futures_day(now_ct)
    this_month = get_futures_month(now_ct)
    prev_m = (this_month[0], this_month[1] - 1) if this_month[1] > 1 else (this_month[0] - 1, 12)

    print(f"Now UTC: {now_utc.isoformat()}")
    print(f"Now CT:  {now_ct.isoformat()}")
    print(f"Futures day: {today}")
    print(f"This month: {this_month}   Prev month: {prev_m}")

    symbols = [args.symbol.upper()] if args.symbol else list(CONTRACT_MAP.keys())

    for sym in symbols:
        cid = CONTRACT_MAP.get(sym)
        if cid is None:
            print(f"\nUnknown symbol {sym}, skipping")
            continue

        print()
        print("=" * 66)
        print(f" {sym}  ({cid})")
        print("=" * 66)

        # ── CDM + PDM from 5-min bars ────────────────────────────────
        # Session start = most recent 5pm CT rollover
        if now_ct.hour >= 17:
            session_start_ct = CT.localize(datetime(now_ct.year, now_ct.month, now_ct.day, 17, 0))
        else:
            session_start_ct = CT.localize(datetime(now_ct.year, now_ct.month, now_ct.day, 17, 0)) - timedelta(days=1)
        session_start_utc = session_start_ct.astimezone(timezone.utc)

        all_bars = api.get_bars(contract_id=cid, unit=2, unit_number=5, days=5, limit=5000)

        bars_today = []
        bars_by_day = defaultdict(list)
        for b in all_bars:
            ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
            if ts >= session_start_utc:
                bars_today.append(b)
            else:
                ts_ct = ts.astimezone(CT)
                bars_by_day[get_futures_day(ts_ct)].append(b)

        if bars_today:
            cdm = sum(b["c"] for b in bars_today) / len(bars_today)
            print(f"  CDM: {cdm:.2f}  ({len(bars_today)} bars since {session_start_ct})")
        else:
            print(f"  CDM: (no bars since {session_start_ct})")

        prior_days = sorted([d for d in bars_by_day.keys() if d < today], reverse=True)
        if prior_days:
            pdm_day = prior_days[0]
            closes = [b["c"] for b in bars_by_day[pdm_day]]
            pdm = sum(closes) / len(closes)
            print(f"  PDM: {pdm:.2f}  ({len(closes)} bars on {pdm_day})")

        # ── CMM + PMM from hourly bars ───────────────────────────────
        hourly = api.get_bars(
            contract_id=cid, unit=3, unit_number=1, days=args.hourly_days, limit=5000
        )
        month_data = defaultdict(list)
        ts_by_month = defaultdict(list)
        for b in hourly:
            ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(CT)
            fm = get_futures_month(ts)
            month_data[fm].append(b["c"])
            ts_by_month[fm].append(ts)

        print(f"  Hourly bars across {args.hourly_days}d: {len(hourly)}")
        for m in sorted(month_data.keys()):
            n = len(month_data[m])
            mean = sum(month_data[m]) / n
            first = min(ts_by_month[m])
            last = max(ts_by_month[m])
            label = ""
            if m == this_month:
                label = "  ← CMM"
            elif m == prev_m:
                label = "  ← PMM"
            print(
                f"  Month {m[0]}-{m[1]:02d}: n={n:4d}  mean={mean:10.2f}  "
                f"({first.strftime('%Y-%m-%d %H:%M')} → {last.strftime('%Y-%m-%d %H:%M')} CT){label}"
            )


if __name__ == "__main__":
    main()
