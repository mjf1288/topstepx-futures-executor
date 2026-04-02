"""
Mean Levels Calculator for Futures (TopstepX)
==============================================
Computes CDM, PDM, CMM, PMM mean levels from futures OHLCV data
using the project-x-py SDK.

Mean Levels (Running Cumulative Close Average):
  CDM = Current Day Mean  = sum(all closes today) / count(closes today)
  PDM = Previous Day Mean = final running mean of yesterday's closes
  CMM = Current Month Mean = sum(all closes this month) / count(closes this month)
  PMM = Previous Month Mean = final running mean of last month's closes

Each mean also tracks direction (UP/DOWN) based on whether the running
average is rising or falling bar-to-bar.

Author: Matthew Foster
"""

import polars as pl
from datetime import datetime, timedelta
from typing import Optional
import pytz


def calc_period_mean(closes: list[float], groups: list) -> dict:
    """
    Core mean engine — computes running cumulative mean of close prices,
    grouped by period (date for daily, YYYY-MM for monthly).

    This matches the original Mean Levels indicator logic exactly:
      - For each bar, accumulate close into the current group's running sum
      - When the group changes, save the previous group's final mean as 'prev'
      - Track direction based on whether the running mean is rising or falling

    Args:
        closes: Array of close prices (one per bar)
        groups: Array of group keys (date objects for daily, 'YYYY-MM' strings for monthly)

    Returns:
        dict with 'current' (current period mean), 'prev' (previous period mean),
        'dir' ('UP' or 'DOWN')
    """
    if not closes or not groups:
        return {'current': None, 'prev': None, 'dir': None}

    cum, count = 0.0, 0
    buff, prev_buff, prev_tf_buff = 0.0, 0.0, None
    direction = 0
    current_group = None

    for i in range(len(closes)):
        src = float(closes[i])
        grp = groups[i]

        if i == 0 or grp != current_group:
            if i > 0:
                prev_tf_buff = buff
            cum, count, current_group = src, 1, grp
        else:
            cum += src
            count += 1

        prev_buff = buff
        buff = cum / count

        if buff > prev_buff:
            direction = 1
        elif buff < prev_buff:
            direction = -1

    return {
        'current': round(buff, 4),
        'prev': round(prev_tf_buff, 4) if prev_tf_buff is not None else round(buff, 4),
        'dir': 'UP' if direction == 1 else 'DOWN',
    }


def compute_mean_levels(
    bars: pl.DataFrame,
    timezone: str = "America/Chicago",
) -> dict:
    """
    Compute CDM, PDM, CMM, PMM from OHLCV bars using the running
    cumulative close average method.

    Args:
        bars: Polars DataFrame with columns: timestamp, open, high, low, close, volume
        timezone: Timezone for day/month boundaries (futures use Chicago)

    Returns:
        dict with keys: cdm, pdm, cmm, pmm, current_price, levels,
        cdm_dir, pdm_dir, cmm_dir, pmm_dir (directional context)
    """
    if bars.is_empty() or len(bars) < 2:
        return {"cdm": None, "pdm": None, "cmm": None, "pmm": None,
                "current_price": None, "levels": []}

    tz = pytz.timezone(timezone)
    now = datetime.now(tz)

    # Sort by timestamp
    df = bars.sort("timestamp")

    # Futures trading day rolls at 5 PM CT (17:00).
    # Shift timestamps by +7 hours so that bars after 5 PM CT
    # get grouped into the NEXT calendar date.
    # Example: 5:01 PM CT on Apr 1 → shifted to 12:01 AM Apr 2 → date = Apr 2
    #          4:59 PM CT on Apr 1 → shifted to 11:59 PM Apr 1 → date = Apr 1
    df = df.with_columns([
        (pl.col("timestamp") + pl.duration(hours=7)).dt.date().alias("date"),
        (pl.col("timestamp") + pl.duration(hours=7)).dt.year().cast(pl.Utf8).str.cat(
            (pl.col("timestamp") + pl.duration(hours=7)).dt.month().cast(pl.Utf8).str.pad_start(2, "0"),
            separator="-"
        ).alias("year_month"),
    ])

    current_price = float(df["close"][-1])

    # Extract closes and group arrays
    closes = df["close"].to_list()
    daily_groups = df["date"].to_list()
    monthly_groups = df["year_month"].to_list()

    # --- Daily Mean ---
    daily_mean = calc_period_mean(closes, daily_groups)
    cdm = daily_mean['current']  # Current Day Mean
    pdm = daily_mean['prev']     # Previous Day Mean
    cdm_dir = daily_mean['dir']

    # --- Monthly Mean ---
    monthly_mean = calc_period_mean(closes, monthly_groups)
    cmm = monthly_mean['current']  # Current Month Mean
    pmm = monthly_mean['prev']     # Previous Month Mean
    cmm_dir = monthly_mean['dir']

    # Get today/yesterday high-low for classify_setup
    dates = df.select("date").unique().sort("date").to_series().to_list()
    today = dates[-1] if dates else None
    yesterday = dates[-2] if len(dates) >= 2 else today

    today_bars = df.filter(pl.col("date") == today) if today else df.tail(1)
    yesterday_bars = df.filter(pl.col("date") == yesterday) if yesterday else df.tail(1)

    today_high = float(today_bars["high"].max()) if not today_bars.is_empty() else current_price
    today_low = float(today_bars["low"].min()) if not today_bars.is_empty() else current_price
    yesterday_high = float(yesterday_bars["high"].max()) if not yesterday_bars.is_empty() else current_price
    yesterday_low = float(yesterday_bars["low"].min()) if not yesterday_bars.is_empty() else current_price

    # Collect all valid levels
    levels = []
    if cdm is not None:
        levels.append({"name": "CDM", "price": cdm, "type": "current_day_mean"})
    if pdm is not None:
        levels.append({"name": "PDM", "price": pdm, "type": "previous_day_mean"})
    if cmm is not None:
        levels.append({"name": "CMM", "price": cmm, "type": "current_month_mean"})
    if pmm is not None:
        levels.append({"name": "PMM", "price": pmm, "type": "previous_month_mean"})

    return {
        "cdm": cdm,
        "pdm": pdm,
        "cmm": cmm,
        "pmm": pmm,
        "cdm_dir": cdm_dir,
        "cmm_dir": cmm_dir,
        "current_price": round(current_price, 4),
        "today_high": round(today_high, 4),
        "today_low": round(today_low, 4),
        "yesterday_high": round(yesterday_high, 4),
        "yesterday_low": round(yesterday_low, 4),
        "levels": levels,
    }


def find_confluences(levels: list[dict], threshold_pct: float = 0.3) -> list[dict]:
    """
    Find confluences where multiple mean levels stack within threshold_pct of each other.

    Args:
        levels: List of level dicts with 'name' and 'price'
        threshold_pct: Maximum % distance to consider levels as confluent

    Returns:
        List of confluence zones with stacking levels and scores
    """
    if len(levels) < 2:
        return []

    # Sort by price
    sorted_levels = sorted(levels, key=lambda x: x["price"])
    confluences = []
    used = set()

    for i, level in enumerate(sorted_levels):
        if i in used:
            continue

        zone_levels = [level]
        zone_prices = [level["price"]]

        for j in range(i + 1, len(sorted_levels)):
            if j in used:
                continue
            pct_diff = abs(sorted_levels[j]["price"] - level["price"]) / level["price"] * 100
            if pct_diff <= threshold_pct:
                zone_levels.append(sorted_levels[j])
                zone_prices.append(sorted_levels[j]["price"])
                used.add(j)

        if len(zone_levels) >= 2:
            used.add(i)
            zone_center = sum(zone_prices) / len(zone_prices)
            zone_width = max(zone_prices) - min(zone_prices)

            # Score: 3 points per level in the zone
            score = len(zone_levels) * 3

            confluences.append({
                "levels": [l["name"] for l in zone_levels],
                "center": round(zone_center, 4),
                "width": round(zone_width, 4),
                "score": score,
                "level_count": len(zone_levels),
            })

    return confluences


def compute_atr(
    bars: pl.DataFrame,
    period: int = 14,
) -> float:
    """
    Compute Average True Range (ATR) from OHLCV bars.

    ATR = SMA of True Range over `period` bars.
    True Range = max(H-L, |H-prevC|, |L-prevC|)

    Args:
        bars: Polars DataFrame with high, low, close columns
        period: Lookback period (default 14)

    Returns:
        ATR value as float (in price points), or 0 if insufficient data
    """
    if bars.is_empty() or len(bars) < period + 1:
        return 0.0

    df = bars.sort("timestamp")

    # Compute True Range components
    df = df.with_columns(
        pl.col("close").shift(1).alias("prev_close")
    ).filter(
        pl.col("prev_close").is_not_null()
    ).with_columns(
        pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("prev_close")).abs(),
            (pl.col("low") - pl.col("prev_close")).abs(),
        ).alias("true_range")
    )

    if len(df) < period:
        return 0.0

    # Take the last `period` bars and compute SMA
    recent_tr = df.tail(period)["true_range"].to_list()
    atr = sum(recent_tr) / len(recent_tr)
    return round(atr, 6)


def classify_setup(
    current_price: float,
    level_price: float,
    yesterday_high: float,
    yesterday_low: float,
    tick_size: float = 0.25,
) -> Optional[str]:
    """
    Classify whether price has broken above or below a level.
    Only returns 'break_above' or 'break_below' (no bounces).

    Args:
        current_price: Current market price
        level_price: The mean level price
        yesterday_high: Yesterday's high
        yesterday_low: Yesterday's low
        tick_size: Instrument tick size

    Returns:
        'break_above', 'break_below', or None
    """
    # Price must be beyond the level by at least a few ticks
    buffer = tick_size * 4  # 4-tick buffer to confirm break

    if current_price > level_price + buffer:
        # Price broke above — this level is now support
        return "break_above"
    elif current_price < level_price - buffer:
        # Price broke below — this level is now resistance
        return "break_below"

    return None
