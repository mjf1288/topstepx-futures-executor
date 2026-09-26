"""Collect completed TopstepX bars into a local parquet store.

Layout under the store root (default ~/topstep-data):

    raw/<ROOT>/<CONTRACT_ID>/<tf>.parquet     one file per contract month
    continuous/<root>-continuous-<tf>.parquet back-adjusted front month
    continuous/<root>-unadjusted-<tf>.parquet front month, real prices
    continuous/<root>-rolls.csv               every roll and its price gap
    manifest.json                             what has been collected

Collection is incremental: rerunning only asks for bars after the last one
stored, and contract months that expired and were fully fetched are skipped.
Only completed bars are stored; the forming bar is never requested or kept.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .contracts import Contract

# name -> (ProjectX unit, unit_number, minutes per bar)
# ProjectX units: 1=second, 2=minute, 3=hour, 4=day
TIMEFRAMES = {
    "1m": (2, 1, 1),
    "5m": (2, 5, 5),
    "15m": (2, 15, 15),
    "1h": (3, 1, 60),
    "4h": (3, 4, 240),
    "1d": (4, 1, 1440),
}

MAX_BARS_PER_REQUEST = 20_000  # ProjectX retrieveBars hard limit
COLUMNS = ["ts_event", "open", "high", "low", "close", "volume"]


def default_store() -> Path:
    return Path(os.environ.get("TOPSTEP_DATA_DIR", "~/topstep-data")).expanduser()


class RateLimiter:
    """Sliding window. ProjectX allows 50 retrieveBars calls per 30 seconds."""

    def __init__(self, max_calls=45, period=30.0, clock=time.monotonic, sleep=time.sleep):
        self.max_calls, self.period = max_calls, period
        self.clock, self.sleep = clock, sleep
        self.calls: deque[float] = deque()

    def wait(self):
        now = self.clock()
        while self.calls and now - self.calls[0] >= self.period:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            self.sleep(self.period - (now - self.calls[0]) + 0.05)
            now = self.clock()
            while self.calls and now - self.calls[0] >= self.period:
                self.calls.popleft()
        self.calls.append(now)


def bars_to_frame(bars: list[dict], minutes: int, fetched_at: datetime) -> tuple[pd.DataFrame, int]:
    """Broker bar dicts -> clean frame. Returns (frame, rejected_count)."""
    if not bars:
        return pd.DataFrame(columns=COLUMNS), 0
    raw = pd.DataFrame(bars)
    frame = pd.DataFrame({
        "ts_event": pd.to_datetime(raw.get("t"), utc=True, errors="coerce"),
        "open": pd.to_numeric(raw.get("o"), errors="coerce"),
        "high": pd.to_numeric(raw.get("h"), errors="coerce"),
        "low": pd.to_numeric(raw.get("l"), errors="coerce"),
        "close": pd.to_numeric(raw.get("c"), errors="coerce"),
        "volume": pd.to_numeric(raw.get("v"), errors="coerce"),
    })
    before = len(frame)
    frame = frame.dropna()
    body_hi = frame[["open", "close"]].max(axis=1)
    body_lo = frame[["open", "close"]].min(axis=1)
    frame = frame[(frame.high >= body_hi) & (frame.low <= body_lo) & (frame.volume >= 0)]
    # Belt and braces: never keep a bar that had not closed when fetched.
    frame = frame[frame.ts_event + pd.Timedelta(minutes=minutes) <= fetched_at]
    frame = frame.astype({"volume": "int64"})
    frame = frame.drop_duplicates("ts_event", keep="last").sort_values("ts_event")
    return frame.reset_index(drop=True), before - len(frame)


def _write_atomic(frame: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    frame.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def merge_into(path: Path, new: pd.DataFrame) -> pd.DataFrame:
    """Merge new bars into an existing file; newer rows win on the same stamp."""
    if path.exists():
        old = pd.read_parquet(path)
        merged = pd.concat([old, new], ignore_index=True) if len(new) else old
    else:
        merged = new
    merged = merged.drop_duplicates("ts_event", keep="last").sort_values("ts_event")
    merged = merged.reset_index(drop=True)
    _write_atomic(merged[COLUMNS], path)
    return merged


class Collector:
    def __init__(self, api, store: Path, limiter: RateLimiter | None = None,
                 now=lambda: datetime.now(timezone.utc), sleep=time.sleep, log=print):
        self.api, self.store = api, Path(store)
        self.limiter = limiter or RateLimiter()
        self.now, self.sleep, self.log = now, sleep, log
        self.manifest_path = self.store / "manifest.json"
        self.manifest = (json.loads(self.manifest_path.read_text())
                         if self.manifest_path.exists() else {})

    def raw_path(self, contract: Contract, tf: str) -> Path:
        return self.store / "raw" / contract.root / contract.contract_id / f"{tf}.parquet"

    def _save_manifest(self):
        self.store.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.manifest, indent=2, sort_keys=True))
        os.replace(tmp, self.manifest_path)

    def _request(self, contract_id, unit, unit_number, start, end):
        for attempt in range(5):
            self.limiter.wait()
            try:
                return self.api.get_bars(
                    contract_id=contract_id, unit=unit, unit_number=unit_number,
                    start_time=start, end_time=end, limit=MAX_BARS_PER_REQUEST,
                    live=False, include_partial=False)
            except Exception as exc:
                if " 429" in str(exc) and attempt < 4:
                    self.log(f"    rate limited; waiting 30s")
                    self.sleep(30)
                    continue
                raise

    def collect(self, contract: Contract, tf: str) -> dict:
        """Fetch any bars not yet stored for one contract month and timeframe."""
        unit, unit_number, minutes = TIMEFRAMES[tf]
        entry = self.manifest.setdefault(contract.contract_id, {}).setdefault(tf, {})
        if entry.get("complete"):
            return entry

        now = self.now()
        win_start, win_end = contract.fetch_window()
        end = min(win_end, now)
        start = win_start
        if entry.get("last_ts"):
            # Re-request the last stored bar so a late correction overwrites it.
            start = max(win_start, datetime.fromisoformat(entry["last_ts"]))
        if start >= end:
            entry["complete"] = win_end <= now
            return entry

        page = timedelta(minutes=minutes * MAX_BARS_PER_REQUEST)
        frames, rejected, requests_made = [], 0, 0
        cursor = start
        while cursor < end:
            page_end = min(cursor + page, end)
            bars = self._request(contract.contract_id, unit, unit_number, cursor, page_end)
            requests_made += 1
            frame, bad = bars_to_frame(bars or [], minutes, now)
            rejected += bad
            if len(frame):
                frames.append(frame)
            cursor = page_end

        new = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
        merged = merge_into(self.raw_path(contract, tf), new) if (frames or self.raw_path(contract, tf).exists()) else new

        entry.update({
            "bars": int(len(merged)),
            "complete": win_end <= now,
            "rejected": int(entry.get("rejected", 0)) + rejected,
            "updated": now.isoformat(),
        })
        if len(merged):
            entry["first_ts"] = merged.ts_event.iloc[0].isoformat()
            entry["last_ts"] = merged.ts_event.iloc[-1].isoformat()
        entry.pop("error", None)
        self._save_manifest()
        self.log(f"  {contract.contract_id:<18} {tf:>3}  +{len(new):>6} bars "
                 f"({len(merged)} stored, {requests_made} req"
                 + (f", {rejected} rejected" if rejected else "") + ")")
        return entry

    def collect_safe(self, contract: Contract, tf: str) -> dict:
        """collect(), but record a failure and carry on with the next contract."""
        try:
            return self.collect(contract, tf)
        except Exception as exc:
            entry = self.manifest.setdefault(contract.contract_id, {}).setdefault(tf, {})
            entry["error"] = repr(exc)[:300]
            self._save_manifest()
            self.log(f"  {contract.contract_id:<18} {tf:>3}  FAILED: {exc!r}"[:200])
            return entry
