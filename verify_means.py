"""Read-only broker audit of CDM/PDM/CMM/PMM.

    python verify_means.py --symbol MES --compare-engine

Uses independent bucket/average math and the same completed-bar snapshot for
the reference and engine comparison. Never starts a stream or places orders.
The optional --hourly-days can extend, but never truncate, the prior month.
"""
import argparse
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import pytz
from dotenv import load_dotenv

load_dotenv()
from topstep_api import from_env
import realtime_engine as engine

CT = pytz.timezone("America/Chicago")
# One contract configuration, including the existing MNQ/MES Z26 rollover.
CONTRACT_MAP = {sym: config[0] for sym, config in engine.CONTRACT_MAP.items()}


def get_futures_day(ct_time):
    return ct_time.date() + timedelta(days=ct_time.hour >= 17)


def get_futures_month(ct_time):
    day = get_futures_day(ct_time)
    return day.year, day.month


def reference_window_start(now_utc):
    """Independent calendar implementation for the audit's full-month pull."""
    day = get_futures_day(now_utc.astimezone(CT))
    previous_last = day.replace(day=1) - timedelta(days=1)
    previous_first = previous_last.replace(day=1)
    open_date = previous_first - timedelta(days=1)
    return CT.localize(datetime.combine(open_date, datetime.min.time()).replace(
        hour=17)).astimezone(timezone.utc)


def reference_means(bars_5m, hourly, now_utc):
    """Independent audit: raw averages plus counts (before warm-up gates)."""
    day_groups, month_groups = defaultdict(dict), defaultdict(dict)
    starts = [now_utc - timedelta(days=7), reference_window_start(now_utc)]
    for bars, duration, groups, start in zip(
            [bars_5m, hourly], [5, 60], [day_groups, month_groups], starts):
        for bar in bars:
            ts = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < start or ts + timedelta(minutes=duration) > now_utc:
                continue
            local = ts.astimezone(CT)
            key = get_futures_day(local) if duration == 5 else get_futures_month(local)
            groups[key][ts] = float(bar["c"])

    day = get_futures_day(now_utc.astimezone(CT))
    month = (day.year, day.month)
    previous_date = day.replace(day=1) - timedelta(days=1)
    previous_month = previous_date.year, previous_date.month
    prior_days = sorted(key for key in day_groups if key < day)
    buckets = {
        "CDM": day_groups[day],
        "PDM": day_groups[prior_days[-1]] if prior_days else {},
        "CMM": month_groups[month],
        "PMM": month_groups[previous_month],
    }
    return {
        name: {"value": math.fsum(bucket.values()) / len(bucket) if bucket else None,
               "count": len(bucket)}
        for name, bucket in buckets.items()
    }


class BarSnapshot:
    """Read-only adapter: compare against identical data, not a later fetch."""
    def __init__(self, contract_id, bars_5m, hourly):
        self.contract_id = contract_id
        self.bars = {(2, 5): bars_5m, (3, 1): hourly}

    def get_bars(self, **kwargs):
        if kwargs["contract_id"] != self.contract_id or kwargs["include_partial"]:
            raise AssertionError("Audit must use the same contract and completed bars")
        return self.bars[(kwargs["unit"], kwargs["unit_number"])]


def compare_engine(symbol, bars_5m, hourly, now_utc):
    reference = reference_means(bars_5m, hourly, now_utc)
    calculated = engine.refresh_broker_means(
        BarSnapshot(CONTRACT_MAP[symbol], bars_5m, hourly), symbol, now_utc)
    for name, result in reference.items():
        minimum = {"CDM": engine.MIN_SAMPLES_CDM, "CMM": engine.MIN_SAMPLES_CMM}.get(name, 1)
        expected = result["value"] if result["count"] >= minimum else None
        actual = calculated[name]
        if expected is None:
            assert actual is None, f"{symbol} {name}: expected warm-up/no data, got {actual}"
        else:
            assert actual is not None and math.isclose(actual, expected, abs_tol=1e-8, rel_tol=0), (
                f"{symbol} {name}: engine={actual}, broker reference={expected}")
    return calculated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=list(CONTRACT_MAP), type=str.upper)
    parser.add_argument("--hourly-days", type=int, default=None,
                        help="optional extra history; never less than full prior month")
    parser.add_argument("--compare-engine", action="store_true",
                        help="assert engine matches independent broker calculation; read-only")
    args = parser.parse_args()
    api = from_env()
    now_utc = datetime.now(timezone.utc)

    # Audit the month the broker says is live, not a hardcoded one. Without
    # this the audit can silently pass against an expired contract.
    wanted = [args.symbol] if args.symbol else list(CONTRACT_MAP)
    resolved, failures = engine.resolve_active_contracts(api, wanted, now_utc)
    for sym, (cid, expiry, flagged) in resolved.items():
        if CONTRACT_MAP[sym] != cid:
            print(f"  {sym}: contract month {CONTRACT_MAP[sym]} -> {cid}")
        CONTRACT_MAP[sym] = cid
        if not flagged:
            print(f"  {sym}: {cid} is not broker-flagged active "
                  f"(expires {expiry.strftime('%Y-%m-%d') if expiry else 'unknown'})")
    if failures:
        for sym, reason in failures.items():
            print(f"  {sym}: UNRESOLVED - {reason}")
        raise RuntimeError(
            "cannot audit without a live contract month: " + ", ".join(failures))

    month_start = reference_window_start(now_utc)
    if args.hourly_days is not None:
        if args.hourly_days <= 0:
            parser.error("--hourly-days must be positive")
        month_start = min(month_start, now_utc - timedelta(days=args.hourly_days))
    print(f"Audit as of {now_utc.isoformat()} | futures day {get_futures_day(now_utc.astimezone(CT))}")
    print(f"Hourly history begins {month_start.isoformat()} (includes entire prior futures month)")
    for symbol in [args.symbol] if args.symbol else CONTRACT_MAP:
        cid = CONTRACT_MAP[symbol]
        bars_5m = api.get_bars(
            contract_id=cid, unit=2, unit_number=5,
            start_time=now_utc - timedelta(days=7), end_time=now_utc,
            limit=5000, include_partial=False)
        hourly = api.get_bars(
            contract_id=cid, unit=3, unit_number=1,
            start_time=month_start, end_time=now_utc,
            limit=5000, include_partial=False)
        if not bars_5m or not hourly:
            raise RuntimeError(f"{symbol}: broker returned empty history; audit cannot pass")
        print(f"\n{symbol} ({cid})")
        for name, result in reference_means(bars_5m, hourly, now_utc).items():
            value = result["value"]
            display = f"{value:.8f}" if value is not None else "no data"
            minimum = {"CDM": engine.MIN_SAMPLES_CDM, "CMM": engine.MIN_SAMPLES_CMM}.get(name, 1)
            note = " [engine warm-up/no data]" if result["count"] < minimum else ""
            print(f"  {name}: {display} ({result['count']} completed bars){note}")
        if args.compare_engine:
            compare_engine(symbol, bars_5m, hourly, now_utc)
            print("  PASS: all four engine means match the independent broker-bar calculation")


if __name__ == "__main__":
    main()
