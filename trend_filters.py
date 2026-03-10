"""
Trend Filters for Mean Levels Futures Strategy
================================================
DSS Bressert (Double Smoothed Stochastic) and Lyapunov HP
(Hodrick-Prescott + Lyapunov Divergence) filters.

These are used to determine trend bias before executing mean level
breaks — only take break_above when bullish, break_below when bearish.

Each filter returns a signal from -10 (strong bearish) to +10 (strong bullish).
The combined signal determines whether a break is tradeable.

Author: Matthew Foster
"""

import math
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
# Lyapunov HP — Hodrick-Prescott + Lyapunov Divergence
# ─────────────────────────────────────────────────────────────

class LyapunovHP:
    """
    Hodrick-Prescott filter with Lyapunov divergence detection.

    Steps:
      1. Compute HP filter lambda from filter_period
      2. Apply HP filter via pentadiagonal matrix solver
      3. Compute Lyapunov divergence = log(|hpf[i] / hpf[i-1]|) * 100000

    Signals:
      - Primary buy: filter_period bars all negative → flips positive
      - Primary sell: filter_period bars all positive → flips negative
      - Secondary: simple zero crossing
      - State-based: direction + magnitude of lyapunov value
    """

    def __init__(self, filter_period: int = 7, lyapunov_period: int = 525):
        self.filter_period = filter_period
        self.lyapunov_period = lyapunov_period
        self.lyapunov_values = []
        self.hpf_values = []

    def _hp_filter(self, prices: list[float], lam: float) -> list[float]:
        """
        Hodrick-Prescott filter using scipy sparse solver.

        The HP filter minimizes:
          sum((y_t - tau_t)^2) + lambda * sum((tau_{t+1} - 2*tau_t + tau_{t-1})^2)

        This is solved via: (I + lambda * K'K) * tau = y
        where K is the second-difference matrix.
        """
        from scipy import sparse
        from scipy.sparse.linalg import spsolve

        n = len(prices)
        if n < 5:
            return prices[:]

        y = np.array(prices, dtype=np.float64)

        # Build second-difference matrix K (n-2 x n)
        # K[i] = [0...0, 1, -2, 1, 0...0] with 1 at position i, -2 at i+1, 1 at i+2
        e = np.ones(n)
        K = sparse.diags([e[:-2], -2*e[:-2], e[:-2]], [0, 1, 2], shape=(n-2, n))

        # Solve (I + lambda * K'K) * tau = y
        I = sparse.eye(n)
        A = I + lam * (K.T @ K)
        tau = spsolve(A, y)

        return tau.tolist()

    def calculate(self, prices: list[float]) -> dict:
        """
        Calculate Lyapunov HP from close prices.

        Args:
            prices: Array of close prices

        Returns:
            dict with 'hpf', 'lyapunov' arrays and latest values
        """
        n = len(prices)
        if n < max(self.filter_period + 2, 10):
            return {'hpf': [], 'lyapunov': [], 'lyap_last': 0}

        # Lambda for HP filter
        sin_val = math.sin(math.pi / self.filter_period)
        if sin_val == 0:
            lam = 1.0
        else:
            lam = 0.0625 / (sin_val ** 4)

        # Apply HP filter
        hpf = self._hp_filter(prices, lam)
        self.hpf_values = hpf

        # Lyapunov divergence
        lyapunov = [0.0]
        for i in range(1, len(hpf)):
            if abs(hpf[i - 1]) < 1e-12:
                lyapunov.append(0.0)
            else:
                ratio = abs(hpf[i] / hpf[i - 1])
                if ratio > 0:
                    lyapunov.append(math.log(ratio) * 100000)
                else:
                    lyapunov.append(0.0)

        self.lyapunov_values = lyapunov

        return {
            'hpf': hpf,
            'lyapunov': lyapunov,
            'lyap_last': lyapunov[-1] if lyapunov else 0,
        }

    def get_signal(self) -> int:
        """
        Get the current Lyapunov signal from -10 to +10.

        Returns:
            int: Signal strength (-10 strong bearish to +10 strong bullish)
        """
        lyap = self.lyapunov_values
        n = len(lyap)
        fp = self.filter_period

        if n < fp + 1:
            return 0

        current = lyap[-1]
        recent = lyap[-(fp + 1):-1]  # Previous filter_period values

        # Primary buy: all recent values negative, current flips positive
        if all(v < 0 for v in recent) and current > 0:
            return 10

        # Primary sell: all recent values positive, current flips negative
        if all(v > 0 for v in recent) and current < 0:
            return -10

        # Secondary: simple zero crossing
        if len(lyap) >= 2:
            prev = lyap[-2]
            if prev <= 0 and current > 0:
                return 6
            if prev >= 0 and current < 0:
                return -6

        # State-based: direction + magnitude
        if current > 0:
            # Positive and increasing
            if len(lyap) >= 2 and current > lyap[-2]:
                return min(4, max(1, int(abs(current) / 50)))
            else:
                return min(2, max(1, int(abs(current) / 100)))
        elif current < 0:
            if len(lyap) >= 2 and current < lyap[-2]:
                return -min(4, max(1, int(abs(current) / 50)))
            else:
                return -min(2, max(1, int(abs(current) / 100)))

        return 0


# ─────────────────────────────────────────────────────────────
# Combined Trend Filter
# ─────────────────────────────────────────────────────────────

def compute_trend_bias(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    dss_params: dict = None,
    lyap_params: dict = None,
) -> dict:
    """
    Compute combined trend bias from DSS Bressert and Lyapunov HP.

    Args:
        highs: Array of high prices
        lows: Array of low prices
        closes: Array of close prices
        dss_params: Override DSS parameters (optional)
        lyap_params: Override Lyapunov parameters (optional)

    Returns:
        dict with:
          - dss_signal: int (-10 to +10)
          - lyap_signal: int (-10 to +10)
          - combined_signal: int (-20 to +20)
          - bias: 'BULLISH', 'BEARISH', or 'NEUTRAL'
          - strength: 'STRONG', 'MODERATE', or 'WEAK'
          - can_break_above: bool (should we trade break above?)
          - can_break_below: bool (should we trade break below?)
    """
    # Initialize filters
    dss = DSSBressert(**(dss_params or {}))
    lyap = LyapunovHP(**(lyap_params or {}))

    # Calculate
    dss.calculate(highs, lows, closes)
    lyap.calculate(closes)

    # Get signals
    dss_signal = dss.get_signal()
    lyap_signal = lyap.get_signal()
    combined = dss_signal + lyap_signal

    # Determine bias
    if combined >= 6:
        bias = 'BULLISH'
    elif combined <= -6:
        bias = 'BEARISH'
    else:
        bias = 'NEUTRAL'

    # Determine strength
    abs_combined = abs(combined)
    if abs_combined >= 14:
        strength = 'STRONG'
    elif abs_combined >= 8:
        strength = 'MODERATE'
    else:
        strength = 'WEAK'

    # Trading rules:
    # - BULLISH bias → can trade break_above, skip break_below
    # - BEARISH bias → can trade break_below, skip break_above
    # - NEUTRAL → skip both (no clear edge)
    # - STRONG signal → trade with conviction
    # - With at least one filter agreeing (signal > 0 for bullish), allow the trade
    can_break_above = combined > 0 and (dss_signal > 0 or lyap_signal > 0)
    can_break_below = combined < 0 and (dss_signal < 0 or lyap_signal < 0)

    return {
        'dss_signal': dss_signal,
        'lyap_signal': lyap_signal,
        'combined_signal': combined,
        'bias': bias,
        'strength': strength,
        'can_break_above': can_break_above,
        'can_break_below': can_break_below,
        'dss_last': dss.dss_values[-1] if dss.dss_values else 50,
        'dss_signal_line': dss.signal_values[-1] if dss.signal_values else 50,
        'lyap_last': lyap.lyapunov_values[-1] if lyap.lyapunov_values else 0,
    }
