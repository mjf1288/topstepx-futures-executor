# Broker mean refresh fix

Branch: `rebuild-data-layer`. This patch does not start engines, change launch
defaults, enable trading, attach brackets, or alter account selection.

## Changes

- PMM history begins at 17:00 America/Chicago on the calendar day before the
  first day of the previous futures month. The boundary is localized after
  calendar arithmetic, preserving DST and December/January handling.
- Startup and every 60-second cycle use the same broker-only refresh function.
  CDM/PDM use completed 5-minute closes; CMM/PMM use completed hourly closes.
  All requests set `includePartialBar=false`. Timestamp filtering independently
  excludes incomplete bars and deduplicates repeated timestamps.
- Locally aggregated streaming bars update the price hint only. They never
  contribute closes to any mean or independently trigger order placement.
- Each successful refresh replaces all four values. Missing period buckets
  clear old values. Existing warm-up gates remain six 5-minute bars for CDM
  and 24 hourly bars for CMM.
- Broker fetch failures skip that symbol's order scan rather than trade on
  cached means. Missing/null/error history responses fail closed.
- A validated open-order snapshot clears filled/cancelled/expired pending IDs
  before adoption and dedup. Positions are queried after working orders to
  conservatively account for fills between the two reads.
- Any change in tick-rounded entry price, including one tick, triggers
  cancel/replace. An unconfirmed cancellation never triggers replacement.
  A confirmed cancellation clears local state even if replacement fails.
- Exposure accounting includes working limit quantities on both sides,
  positions, and successful new placements within the current scan. Resting
  orders may still reprice at cap without adding exposure. Tracked levels that
  become ineligible are cancelled.
- MNQ/MES preserve the previously confirmed local Z26 rollover, now committed
  on this branch. Other contract mappings remain unchanged.

## Offline verification

```bash
PYTHON_DOTENV_DISABLED=1 python -m unittest -v test_mean_regressions
```

The fixtures exercise `verify_means.py` independently, including its
`--compare-engine` CLI path. Network access is blocked in the suite. These are
deterministic broker-shaped fixtures, not a recorded or current broker snapshot.
Before pushing, all 35 offline tests passed, all four Python files passed
`py_compile`, and `git diff --check` passed. No engine or trading process was
started; authenticated live broker-data verification remains pending.

Coverage includes full-prior-month versus clipped-45-day PMM, year/month rolls,
DST, leap years, weekends, duplicate/partial bars, warm-up, repeated refreshes,
60-second refresh-before-placement, startup, concurrency, missing/null/error
responses, buy/sell one-tick repricing, filled/cancelled/partially filled orders,
restart adoption, cancel/replace failures, dry-run, and exposure caps.

## Remaining read-only broker acceptance

With current broker credentials configured locally, and engines still paused:

```bash
python verify_means.py --symbol MES --compare-engine
python verify_means.py --symbol MNQ --compare-engine
```

The audit retrieves only historical bars after authentication/account selection.
It never calls order endpoints or starts the engine. It compares independent
averages with engine values using the identical broker-bar snapshot and cutoff.

The old verifier also used 45 days and included partial bars by default; both
are corrected. `--hourly-days` can extend but cannot shorten the calendar window.

Offline PASS does not establish availability/completeness of native Z26 history,
current broker parity, stream freshness, or full-session stability. Requesting
the full month cannot manufacture history the broker does not retain. Keep
engines paused until the read-only live-data audit is accepted. Existing broker
orders are not cancelled merely by stopping an engine.
