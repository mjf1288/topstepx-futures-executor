# Market data store

Collects completed TopstepX bars for MNQ, MES, MGC and MCL (MYM optional) into
local parquet files for backtesting. Read-only against the broker: it requests
history and never touches orders or positions.

## Run

    python collect_data.py

First run backfills from 2024-01-01. Every later run only fetches bars it doesn't
already have, so run it daily (or after each session) to keep the store current.

## What it produces

Default location `~/topstep-data` (override with `--store` or `TOPSTEP_DATA_DIR`):

    raw/<ROOT>/<CONTRACT_ID>/<tf>.parquet     every contract month, real prices
    continuous/<root>-continuous-<tf>.parquet back-adjusted front month
    continuous/<root>-unadjusted-<tf>.parquet front month, real prices
    continuous/<root>-rolls.csv               each roll, date and price gap
    manifest.json                             coverage, bar counts, errors

Timeframes: 1m 5m 15m 1h 4h 1d, all pulled natively from the broker. The engine
computes means from broker 5m and 1h bars, so the backtest sees the same bars.

`continuous` files use the same columns as Tarric's parquet
(`ts_event, open, high, low, close, volume, symbol`), so the existing backtest
reads them directly:

    python run_backtest.py parity --data ~/topstep-data/continuous/mnq-continuous-5m.parquet

## Rules it follows

- Only completed bars. The forming bar is never requested or stored.
- Rolls to the next contract the futures day AFTER its volume first beats the
  current contract's (or after last trade). No look-ahead.
- Back-adjustment is additive: the newest contract keeps real prices; older
  history shifts by each roll gap. Prices are snapped to the tick grid.
- Respects the broker limits: 20,000 bars per request, 50 history calls per
  30 seconds (it uses 45), with a wait-and-retry on HTTP 429.
- A failed contract is recorded in manifest.json and retried next run; the rest
  still collect. Exit code 1 if anything failed.

How far back TopstepX serves history isn't documented. Months with no data are
simply empty; `manifest.json` shows real coverage after the first run.
