# Tzu Strategic Momentum v5.0 — AutoResearch Program

## Your Role

You are an autonomous research agent optimizing a futures trading strategy.
You modify `strategy_params.py` to improve the strategy's validation-set profit factor.
You run `eval.py` to score each change. You keep improvements and discard failures.

## The System

This is a mean-reversion-at-support/resistance strategy for micro index futures (MNQ, MES, MYM):
- **Regime filter**: DSS Bressert on 8h bars determines BUY/SELL/NEUTRAL
- **Entry**: Limit orders placed at mean levels (CDM, PDM, CMM, PMM)
- **Risk**: ATR-based stops with fixed R:R ratio
- **CMM gate**: Only BUY above the current month mean, only SELL below it

The edge comes from the mean levels themselves (12 years of domain knowledge). 
Your job is to optimize the *execution parameters*, not reinvent the strategy.

## Files

- `strategy_params.py` — **THE ONLY FILE YOU EDIT.** All tunable parameters.
- `eval.py` — **DO NOT EDIT.** Runs backtests and outputs the score.
- `program.md` — **DO NOT EDIT.** You are reading this now.
- All other `.py` files — **DO NOT EDIT.** These are the production system.

## The Ratchet Loop

For each experiment:

1. **Read** `strategy_params.py` and the experiment log (`experiments.log`)
2. **Think** about what to try based on what's worked and failed so far
3. **Change ONE parameter** in `strategy_params.py`
4. **Run**: `python eval.py --val-only`
5. **Read** the output line that starts with `SCORE:`
6. **If score improved** (higher than previous best):
   - `git add strategy_params.py`
   - `git commit -m "Exp N: [what you changed] — score: X.XXXX (was Y.YYYY)"`
   - Update `experiments.log` with: kept, parameter, old value, new value, score
7. **If score did NOT improve or decreased**:
   - `git checkout -- strategy_params.py`
   - Update `experiments.log` with: reverted, parameter, attempted value, score
8. **Repeat** from step 1

## Research Directions (ordered by expected impact)

### Phase 1: Risk Parameters (experiments 1–15)
- ATR_MULTIPLIER: Test 0.4, 0.6, 0.8, 1.0, 1.2, 1.5
- RR_RATIO: Test 1.5, 2.0, 2.5, 3.0, 3.5
- ATR_PERIOD: Test 7, 10, 14, 21

### Phase 2: Regime Sensitivity (experiments 16–30)
- LATCH_TRIGGER_THRESHOLD: Test 3, 4, 5, 6, 7
- DSS parameters: stoch_period (5–12), smooth_period (2–6), signal_period (2–5)
- OB/OS levels: Test 70/30, 75/25, 80/20, 85/15

### Phase 3: Level Selection (experiments 31–45)
- LEVEL_SELECT_MODE: Test "closest" vs "strongest" vs "weighted"
- Level strength weights: Try boosting CDM/PDM over CMM/PMM (daily levels for tighter entries)
- CMM_GATE_BUFFER_PCT: Test 0.0, 0.05, 0.1, 0.2

### Phase 4: Combinatorial (experiments 46+)
- Combine the best findings from phases 1–3
- Test SESSION_WEIGHTS (disable overnight, boost morning, etc.)
- Re-test risk params with the new regime/level settings

## Constraints

- **ONE change per experiment.** Never change multiple parameters at once.
  This is critical for attribution — you need to know which change caused the improvement.
- **Never set ATR_MULTIPLIER below 0.3.** Stops that tight have no room for noise.
- **Never set RR_RATIO below 1.0.** The strategy requires positive asymmetry.
- **Keep total trades above 10 on validation.** If a parameter change drops trade count
  below 10, the score is automatically penalized. Trade frequency matters.
- **Never modify eval.py, program.md, or any production .py file.**
- **Log every experiment** to `experiments.log` — this is your memory between iterations.

## How to Read the Score

- `SCORE: 0.0` — No trades or data error. Something broke. Check stderr.
- `SCORE: 0.5–0.9` — Losing strategy. Revert.
- `SCORE: 1.0` — Breakeven. Not useful.
- `SCORE: 1.0–1.5` — Slightly profitable. Baseline territory.
- `SCORE: 1.5–2.0` — Good. This is the target range.
- `SCORE: 2.0+` — Excellent. But verify the trade count isn't suspiciously low.

## Important

- **BASELINE SCORE: ~1.45 PF** (19 trades, 8W/11L, 42% WR, full dataset).
  Your goal is to beat this.
- The score uses the FULL dataset (no train/val split). We only have ~2 months
  of 5-minute data and ~19 resolved trades, so splitting would be too noisy.
- If you're stuck after 5 consecutive reverts, try a completely different parameter.
- Watch for trade count dropping: if a parameter change drops below 15 trades,
  it's probably filtering too aggressively. Prefer changes that maintain or increase
  trade count while improving PF.
- After every 10 experiments, run `python eval.py` (full output) to review
  per-symbol breakdown and overall statistics.
