# CLAUDE.md — Agent Instructions for Tzu AutoResearch

## Project Overview
This is the Tzu Strategic Momentum v5.0 futures trading system.
An autoresearch loop is being run to optimize strategy parameters.

## Critical Rules
1. **ONLY modify `strategy_params.py`** — never touch eval.py, program.md, or any other .py file
2. Read `program.md` for full research directions before starting
3. Log every experiment to `experiments.log`
4. Run `python eval.py --val-only` to score each change
5. Parse the `SCORE: X.XXXX` line from stdout to get the score
6. ONE parameter change per experiment — never change multiple at once
7. git commit on improvements, git checkout on failures

## Quick Start
```bash
# Read the research program
cat program.md

# Run baseline score
python eval.py

# Start the optimization loop per program.md instructions
```

## File Map
- `strategy_params.py` — Tunable parameters (AGENT EDITS THIS)
- `eval.py` — Backtesting harness (DO NOT EDIT)
- `program.md` — Research directions (DO NOT EDIT)
- `experiments.log` — Experiment history (append only)
- `run_regime_session.py` — Production executor (DO NOT EDIT)
- `trend_filters.py` — DSS Bressert + Lyapunov (DO NOT EDIT)
- `mean_levels_calc.py` — Mean level computation (DO NOT EDIT)
