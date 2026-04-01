"""
Trend Filters for Mean Levels Futures Strategy
================================================
DSS Bressert (Double Smoothed Stochastic) filter.

Used to determine trend bias before executing mean level breaks —
only take break_above when bullish, break_below when bearish.

Signal range: -10 (strong bearish) to +10 (strong bullish).

Author: Matthew Foster
"""

import numpy as np
import polars as pl
from typing import Optional


# ─────────────────────────────────────────────────────────────
# DSS Bressert — Double Smoothed Stochastic
# ─────────────────────────────────────────────────────────────

class DSSBressert:
    """
    Double Smoothed Stochastic oscillator.

    Steps:
      1. Raw stochastic over stoch_period bars
      2. EMA smooth (smooth_period)
      3. Second stochastic on the smoothed line
      4. Second EMA → DSS line
      5. Signal = EMA of DSS (signal_period)

    Signals:
      - Primary buy: DSS crosses above signal, BOTH below os_level
      - Primary sell: DSS crosses below signal, BOTH above ob_level
      - Secondary: crossover at one extreme only
      - Exhaustion hooks: both lines at extreme, starting to reverse
    """

    def __init__(self, stoch_period: int = 8, smooth_period: int = 4,
                 signal_period: int = 3, ob_level: float = 80, os_level: float = 20):
        self.stoch_period = stoch_period
        self.smooth_period = smooth_period
        self.signal_period = signal_period
        self.ob_level = ob_level
        self.os_level = os_level
        self.dss_values = []
        self.signal_values = []

    def _ema(self, values: list[float], period: int) -> list[float]:
        """Compute EMA of a list."""
        if not values:
            return []
        k = 2.0 / (period + 1)
        result = [values[0]]
        for i in range(1, len(values)):
            result.append(values[i] * k + result[-1] * (1 - k))
        return result

    def calculate(self, highs: list[float], lows: list[float],
                  closes: list[float]) -> dict:
        """
        Calculate DSS Bressert from price arrays.

        Args:
            highs: Array of high prices
            lows: Array of low prices
            closes: Array of close prices

        Returns:
            dict with 'dss', 'signal' arrays and latest values
        """
        n = len(closes)
        if n < self.stoch_period + self.smooth_period * 2:
            return {'dss': [], 'signal': [], 'dss_last': 50, 'signal_last': 50}

        # Step 1: Raw stochastic
        raw_stoch = []
        for i in range(n):
            start = max(0, i - self.stoch_period + 1)
            period_highs = highs[start:i + 1]
            period_lows = lows[start:i + 1]
            hh = max(period_highs)
            ll = min(period_lows)
            if hh == ll:
                raw_stoch.append(50.0)
            else:
                raw_stoch.append((closes[i] - ll) / (hh - ll) * 100)

        # Step 2: First EMA smooth
        smooth1 = self._ema(raw_stoch, self.smooth_period)

        # Step 3: Second stochastic on smooth1
        raw_stoch2 = []
        for i in range(len(smooth1)):
            start = max(0, i - self.stoch_period + 1)
            window = smooth1[start:i + 1]
            hi = max(window)
            lo = min(window)
            if hi == lo:
                raw_stoch2.append(50.0)
            else:
                raw_stoch2.append((smooth1[i] - lo) / (hi - lo) * 100)

        # Step 4: Second EMA → DSS line
        dss = self._ema(raw_stoch2, self.smooth_period)

        # Step 5: Signal = EMA of DSS
        signal = self._ema(dss, self.signal_period)

        self.dss_values = dss
        self.signal_values = signal

        return {
            'dss': dss,
            'signal': signal,
            'dss_last': dss[-1] if dss else 50,
            'signal_last': signal[-1] if signal else 50,
        }

    def get_signal(self) -> int:
        """
        Get the current DSS signal from -10 to +10.

        Returns:
            int: Signal strength (-10 strong bearish to +10 strong bullish)
        """
        if len(self.dss_values) < 3 or len(self.signal_values) < 3:
            return 0

        dss = self.dss_values[-1]
        dss_prev = self.dss_values[-2]
        sig = self.signal_values[-1]
        sig_prev = self.signal_values[-2]

        # Crossover detection
        cross_above = dss_prev <= sig_prev and dss > sig
        cross_below = dss_prev >= sig_prev and dss < sig

        # Primary buy: DSS crosses above signal, BOTH below oversold
        if cross_above and dss < self.os_level and sig < self.os_level:
            return 10

        # Primary sell: DSS crosses below signal, BOTH above overbought
        if cross_below and dss > self.ob_level and sig > self.ob_level:
            return -10

        # Secondary buy: crossover above, at least one below oversold
        if cross_above and (dss < self.os_level or sig < self.os_level):
            return 7

        # Secondary sell: crossover below, at least one above overbought
        if cross_below and (dss > self.ob_level or sig > self.ob_level):
            return -7

        # Exhaustion hooks — both at extreme, starting to reverse
        if dss > self.ob_level and sig > self.ob_level and dss < dss_prev:
            return -5  # Bearish exhaustion

        if dss < self.os_level and sig < self.os_level and dss > dss_prev:
            return 5  # Bullish exhaustion

        # Simple crossover (not at extremes)
        if cross_above:
            return 4
        if cross_below:
            return -4

        # Trend continuation — DSS above signal = bullish bias
        if dss > sig:
            strength = min(3, max(1, int((dss - sig) / 10)))
            return strength
        elif dss < sig:
            strength = min(3, max(1, int((sig - dss) / 10)))
            return -strength

        return 0



# ─────────────────────────────────────────────────────────────
# Combined Trend Filter
# ─────────────────────────────────────────────────────────────

def compute_trend_bias(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    dss_params: dict = None,
    latched_mode: str = None,
) -> dict:
    """
    Compute trend bias from DSS Bressert with latching regime.

    v5.0 LATCHING MODE:
    Once DSS triggers BUY (>= +4) or SELL (<= -4), that mode LATCHES
    and persists across all subsequent sessions until an opposite
    trigger fires.  The +1 to +3 continuation values keep the existing
    bias alive instead of resetting to NEUTRAL.

    Latch rules:
      - DSS >= +4  →  latch BULLISH (regardless of prior mode)
      - DSS <= -4  →  latch BEARISH (regardless of prior mode)
      - DSS between -3 and +3  →  KEEP prior latched mode
      - If no prior mode exists (first run), treat as NEUTRAL

    Args:
        highs: Array of high prices
        lows: Array of low prices
        closes: Array of close prices
        dss_params: Override DSS parameters (optional)
        latched_mode: Prior latched mode ('BULLISH', 'BEARISH', or None)

    Returns:
        dict with:
          - dss_signal: int (-10 to +10)
          - combined_signal: int (same as dss_signal)
          - bias: 'BULLISH', 'BEARISH', or 'NEUTRAL'
          - strength: 'STRONG', 'MODERATE', or 'WEAK'
          - can_break_above: bool
          - can_break_below: bool
          - latched: bool (True if mode was carried from prior session)
          - trigger: str ('new_trigger', 'latched', or 'no_latch')
    """
    # DSS Bressert
    dss = DSSBressert(**(dss_params or {}))
    dss.calculate(highs, lows, closes)
    dss_signal = dss.get_signal()
    combined = dss_signal

    # Determine bias with latching
    latched = False
    trigger = 'no_latch'

    if combined >= 4:
        # Fresh BUY trigger — latch BULLISH
        bias = 'BULLISH'
        trigger = 'new_trigger'
    elif combined <= -4:
        # Fresh SELL trigger — latch BEARISH
        bias = 'BEARISH'
        trigger = 'new_trigger'
    elif latched_mode in ('BULLISH', 'BEARISH'):
        # No new trigger — keep prior latched mode
        bias = latched_mode
        latched = True
        trigger = 'latched'
    else:
        # No prior mode and no trigger — truly neutral (first run)
        bias = 'NEUTRAL'
        trigger = 'no_latch'

    # Determine strength
    abs_combined = abs(combined)
    if abs_combined >= 8:
        strength = 'STRONG'
    elif abs_combined >= 5:
        strength = 'MODERATE'
    else:
        strength = 'WEAK'

    can_break_above = combined > 0
    can_break_below = combined < 0

    return {
        'dss_signal': dss_signal,
        'combined_signal': combined,
        'bias': bias,
        'strength': strength,
        'can_break_above': can_break_above,
        'can_break_below': can_break_below,
        'dss_last': dss.dss_values[-1] if dss.dss_values else 50,
        'dss_signal_line': dss.signal_values[-1] if dss.signal_values else 50,
        'latched': latched,
        'trigger': trigger,
    }
