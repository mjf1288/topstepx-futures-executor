# TopstepX Mean Levels Regime Trading System

Automated futures trading system for TopstepX 50K Combine using micro index futures (MNQ, MES, MYM, M2K).

## Architecture

Unified pipeline runs 3x/day at each CME 8-hour bar close (2 AM, 10 AM, 6 PM ET):

1. **Regime Filter** — DSS Bressert + Lyapunov HP on 8h bars → BUY / SELL / NEUTRAL
2. **Mean Levels** — Running cumulative close averages (CDM, PDM, CMM, PMM)
3. **Execution** — Limit orders at mean levels with ATR-based bracket stops

### Regime Logic
- DSS Bressert (stochastic variant) and Lyapunov HP (Hodrick-Prescott with Lyapunov exponent) must agree
- Both bullish → BUY mode (limits at support levels below price)
- Both bearish → SELL mode (limits at resistance levels above price)
- Otherwise → NEUTRAL (no orders)

### Mean Level Priority
| Level | Description | Strength |
|-------|-------------|----------|
| CMM | Current Month Mean | 4 (strongest) |
| PMM | Previous Month Mean | 3 |
| CDM | Current Day Mean | 2 |
| PDM | Previous Day Mean | 1 |

### Risk Management
- 1 contract max per symbol (4 symbols = 4 max)
- 1x daily ATR stops, 2:1 reward-to-risk targets
- MLL gate: blocks trades if stop-out would breach drawdown floor
- Daily profit cap: $1,400 (consistency rule buffer)

## Files

| File | Description |
|------|-------------|
| `run_regime_session.py` | **Main entry point** — unified regime → limit orders pipeline |
| `trend_filters.py` | DSS Bressert + Lyapunov HP computation |
| `mean_levels_calc.py` | Running cumulative close average + ATR |
| `regime_state.json` | Cached regime state (BUY/SELL/NEUTRAL per instrument) |
| `.env` | TopstepX API credentials (not tracked) |

### Legacy (superseded)
| File | Description |
|------|-------------|
| `run_session.py` | Old looping session runner |
| `futures_executor.py` | Old limit order executor |
| `futures_scanner.py` | Old regime-reading scanner |
| `regime_filter.py` | Old standalone regime filter |

## Usage

```bash
# Live run
python run_regime_session.py

# Dry run (no orders placed)
python run_regime_session.py --dry-run

# Specific symbols only
python run_regime_session.py --symbols MNQ MES

# Custom ATR multiplier and R:R ratio
python run_regime_session.py --atr-mult 1.5 --rr-ratio 3.0
```

## Requirements

- Python 3.10+
- `project-x-py` (TopstepX SDK)
- `polars`, `numpy`, `scipy`, `aiohttp`, `python-dotenv`, `pytz`

## Setup

1. Copy `.env.example` to `.env` and fill in your TopstepX credentials:
   ```
   TOPSTEP_API_KEY=your_api_key
   TOPSTEP_USERNAME=your_username
   ```

2. Install dependencies:
   ```bash
   pip install project-x-py polars numpy scipy aiohttp python-dotenv pytz
   ```

3. Run a dry test:
   ```bash
   python run_regime_session.py --dry-run
   ```
