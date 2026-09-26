"""Stitch per-contract bars into one front-month series per root.

Roll rule: switch to the next contract at the start of the futures day after
the first completed day on which its volume beats the current contract's, or
after the current contract's last trading day, whichever comes first. The
decision only uses a day that has already closed, so there is no look-ahead.

Two outputs per timeframe:
  <root>-continuous-<tf>.parquet  additively back-adjusted: the newest contract
                                  keeps real prices, older history is shifted by
                                  the price gap at each roll (same convention as
                                  Tarric's files, so the backtest reads it as is)
  <root>-unadjusted-<tf>.parquet  real traded prices, with jumps at each roll
Both carry a `symbol` column naming the contract each bar came from.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pandas as pd

from .contracts import TICK_SIZE, Contract

ROLL_TF = "1h"  # timeframe used to measure daily volume and the roll gap


def futures_day(ts: pd.Series) -> pd.Series:
    """CME futures day: rolls at 17:00 Chicago time."""
    ct = ts.dt.tz_convert("America/Chicago")
    return (ct + pd.Timedelta(hours=7)).dt.date


def snap(values, tick: float):
    """Round to the tick grid, then trim the float noise the multiply adds back
    (e.g. 6137 * 0.01 = 61.370000000000005)."""
    if not tick:
        return values
    decimals = max(0, -Decimal(str(tick)).as_tuple().exponent)
    return ((values / tick).round() * tick).round(decimals)


def _load(path: Path) -> pd.DataFrame | None:
    return pd.read_parquet(path) if path.exists() else None


def plan_rolls(contracts: list[Contract], hourly: dict[str, pd.DataFrame]) -> list[dict]:
    """Return one segment per contract: {contract, first_day, last_day, gap}."""
    chain = [c for c in contracts if hourly.get(c.contract_id) is not None
             and len(hourly[c.contract_id])]
    if not chain:
        return []
    daily = {}
    for c in chain:
        h = hourly[c.contract_id]
        daily[c.contract_id] = h.groupby(futures_day(h.ts_event))["volume"].sum()
    all_days = sorted(set().union(*[set(v.index) for v in daily.values()]))

    segments, i = [], 0
    current = chain[0]
    seg_start = daily[current.contract_id].index.min()
    for day in all_days:
        if day < seg_start:
            continue
        if i + 1 >= len(chain):
            continue
        nxt = chain[i + 1]
        cur_vol = daily[current.contract_id].get(day, 0)
        nxt_vol = daily[nxt.contract_id].get(day, 0)
        expired = day >= current.expiry
        if nxt_vol > cur_vol or expired:
            segments.append({"contract": current, "first_day": seg_start, "last_day": day})
            current, i = nxt, i + 1
            later = [d for d in all_days if d > day]
            seg_start = later[0] if later else day
    segments.append({"contract": current, "first_day": seg_start, "last_day": all_days[-1]})

    # Price gap at each roll: next close minus current close at the last hour
    # both traded on the decision day.
    for k in range(len(segments) - 1):
        a = hourly[segments[k]["contract"].contract_id]
        b = hourly[segments[k + 1]["contract"].contract_id]
        day = segments[k]["last_day"]
        a_day = a[futures_day(a.ts_event) == day].set_index("ts_event").close
        b_day = b[futures_day(b.ts_event) == day].set_index("ts_event").close
        common = a_day.index.intersection(b_day.index)
        if len(common):
            t = common.max()
            segments[k]["gap"] = float(b_day[t] - a_day[t])
        elif len(a_day) and len(b_day):
            segments[k]["gap"] = float(b_day.iloc[-1] - a_day.iloc[-1])
        else:
            segments[k]["gap"] = 0.0
            segments[k]["gap_missing"] = True
    segments[-1]["gap"] = 0.0
    return segments


def build_root(store: Path, root: str, contracts: list[Contract], timeframes: list[str],
               log=print) -> dict:
    raw_dir = Path(store) / "raw" / root
    out_dir = Path(store) / "continuous"
    out_dir.mkdir(parents=True, exist_ok=True)

    hourly = {c.contract_id: _load(raw_dir / c.contract_id / f"{ROLL_TF}.parquet")
              for c in contracts}
    segments = plan_rolls(contracts, hourly)
    if not segments:
        log(f"  {root}: no hourly data yet, continuous series not built")
        return {}

    # Cumulative adjustment for each segment = sum of gaps at every later roll.
    adjust, running = [0.0] * len(segments), 0.0
    for k in range(len(segments) - 1, -1, -1):
        adjust[k] = running
        running += segments[k - 1]["gap"] if k > 0 else 0.0

    pd.DataFrame([{
        "contract": s["contract"].contract_id,
        "symbol": s["contract"].symbol,
        "first_day": s["first_day"],
        "last_day": s["last_day"],
        "gap_to_next": s["gap"],
        "gap_missing": s.get("gap_missing", False),
        "adjustment": adjust[k],
    } for k, s in enumerate(segments)]).to_csv(out_dir / f"{root.lower()}-rolls.csv", index=False)

    summary = {}
    for tf in timeframes:
        parts_adj, parts_raw = [], []
        for k, seg in enumerate(segments):
            frame = _load(raw_dir / seg["contract"].contract_id / f"{tf}.parquet")
            if frame is None or not len(frame):
                continue
            days = futures_day(frame.ts_event)
            frame = frame[(days >= seg["first_day"]) & (days <= seg["last_day"])].copy()
            frame["symbol"] = seg["contract"].symbol
            parts_raw.append(frame)
            shifted = frame.copy()
            tick = TICK_SIZE.get(root)
            for col in ("open", "high", "low", "close"):
                shifted[col] = snap(shifted[col] + adjust[k], tick)
            parts_adj.append(shifted)
        if not parts_raw:
            continue
        for name, parts in (("continuous", parts_adj), ("unadjusted", parts_raw)):
            series = (pd.concat(parts, ignore_index=True)
                      .drop_duplicates("ts_event", keep="last")
                      .sort_values("ts_event").reset_index(drop=True))
            series["symbol"] = series["symbol"].astype("string")
            series.to_parquet(out_dir / f"{root.lower()}-{name}-{tf}.parquet", index=False)
        summary[tf] = (len(series), series.ts_event.iloc[0], series.ts_event.iloc[-1])
    return summary


def load(root: str, tf: str, adjusted: bool = True, store: Path | None = None) -> pd.DataFrame:
    """Read a continuous series for a backtest."""
    from .store import default_store
    kind = "continuous" if adjusted else "unadjusted"
    path = Path(store or default_store()) / "continuous" / f"{root.lower()}-{kind}-{tf}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run collect_data.py first")
    return pd.read_parquet(path)
