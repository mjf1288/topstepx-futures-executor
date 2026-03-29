"""
Strategy Parameters — THE ONLY FILE THE AGENT MODIFIES
========================================================
All tunable parameters for the Tzu Strategic Momentum v5.0 system.
The autoresearch agent proposes changes HERE, runs eval.py, and
keeps or reverts based on the score.

DO NOT move logic into this file. This is purely configuration.
The agent may change any value below.

Author: Matthew Foster
"""

# ─────────────────────────────────────────────────────────────
# REGIME FILTER — DSS Bressert Parameters
# ─────────────────────────────────────────────────────────────
DSS_STOCH_PERIOD = 8          # Lookback for raw stochastic (range: 5–20)
DSS_SMOOTH_PERIOD = 4         # EMA smoothing period (range: 2–8)
DSS_SIGNAL_PERIOD = 3         # Signal line EMA period (range: 2–6)
DSS_OB_LEVEL = 80             # Overbought threshold (range: 70–90)
DSS_OS_LEVEL = 20             # Oversold threshold (range: 10–30)

# ─────────────────────────────────────────────────────────────
# REGIME TRIGGER THRESHOLDS
# ─────────────────────────────────────────────────────────────
LATCH_TRIGGER_THRESHOLD = 4   # DSS signal >= this → BUY latch (range: 3–7)
LATCH_FLIP_THRESHOLD = -4     # DSS signal <= this → SELL latch (symmetric)

# ─────────────────────────────────────────────────────────────
# RISK MANAGEMENT
# ─────────────────────────────────────────────────────────────
ATR_MULTIPLIER = 1.0          # Stop distance as multiple of daily ATR (range: 0.3–2.0)
RR_RATIO = 2.0                # Reward-to-risk ratio (range: 1.5–4.0)
MIN_ATR_MULTIPLIER = 0.4      # Floor for dynamic ATR scaling (range: 0.2–0.6)
ATR_PERIOD = 14               # ATR lookback period in daily bars (range: 7–21)

# ─────────────────────────────────────────────────────────────
# LEVEL SELECTION
# ─────────────────────────────────────────────────────────────
# Strength weights: higher = preferred when multiple levels qualify
LEVEL_STRENGTH_CMM = 4        # Current Month Mean weight (range: 1–6)
LEVEL_STRENGTH_PMM = 3        # Previous Month Mean weight (range: 1–6)
LEVEL_STRENGTH_CDM = 2        # Current Day Mean weight (range: 1–6)
LEVEL_STRENGTH_PDM = 1        # Previous Day Mean weight (range: 1–6)

# Level selection mode:
#   "closest"  — pick the level closest to current price
#   "strongest" — pick the level with the highest strength weight
#   "weighted"  — score by (strength / distance_pct), pick highest
LEVEL_SELECT_MODE = "closest"

# ─────────────────────────────────────────────────────────────
# CMM STRUCTURE GATE
# ─────────────────────────────────────────────────────────────
CMM_GATE_ENABLED = True       # Require price above CMM for BUY, below for SELL
CMM_GATE_BUFFER_PCT = 0.0     # Buffer % around CMM (0.0 = exact, 0.1 = 0.1% buffer)

# ─────────────────────────────────────────────────────────────
# INSTRUMENTS
# ─────────────────────────────────────────────────────────────
SYMBOLS = ["MNQ", "MES", "MYM"]  # Active instruments
MAX_CONTRACTS_PER_SYMBOL = 1     # Max lots per symbol (range: 1–2)
MAX_TOTAL_CONTRACTS = 4          # Max total open contracts (range: 3–6)

# ─────────────────────────────────────────────────────────────
# SESSION TIMING (for future session-weighting experiments)
# ─────────────────────────────────────────────────────────────
SESSION_WEIGHTS = {
    "overnight": 1.0,   # 2:00 AM ET session weight (range: 0.0–1.0)
    "morning": 1.0,     # 10:00 AM ET session weight (range: 0.0–1.0)
    "rth": 1.0,         # 6:00 PM ET session weight (range: 0.0–1.0)
}
