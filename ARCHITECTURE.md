# Mean-Level Executor — Architecture Guide

Branch `rebuild-data-layer` @ `03b9769`. Line references are to that commit.

---

## 1. Scope in one paragraph

`realtime_engine.py` is an **execution-only** limit-order placer. You pick a direction per
instrument (`BUY` or `SELL`). Every 60 seconds it recomputes four mean levels from
**completed broker bars**, then keeps limit orders resting at the strongest eligible levels,
repricing them as the means move. It places **entry limits only** — no stops, no targets, no
brackets. You attach risk manually in the TopstepX UI.

It does **not** decide direction. Nothing in this file reads DSS, Lyapunov, or any regime
signal.

---

## 2. Two entry points, often confused

| File | Cadence | Picks direction? | Places orders |
|---|---|---|---|
| `realtime_engine.py` | continuous, 60s loop | **No** — you pass `--mnq buy` etc. | entry limits at means |
| `run_regime_session.py` | 3x/day at 8h bar close (2 AM, 10 AM, 6 PM ET) | **Yes** — DSS Bressert latching | entry limits + ATR bracket |

They are separate programs with separate risk behaviour. This guide covers
`realtime_engine.py`, the one you run with `--mnq buy --mes buy`.

---

## 3. Runtime topology

```
   TopstepX REST (api.topstepx.com/api)      TopstepX SignalR (rtc.topstepx.com/hubs/market)
              │                                              │
     topstep_api.TopstepAPI                        topstep_stream.TopstepStream
     auth / bars / orders / positions              ticks → local 5-min bars
              │                                              │
              │  get_bars(unit=2,#5)  get_bars(unit=3,#1)     │  on_bar_close(cid, bar)
              ▼                                              ▼
     refresh_broker_means()  ──────────────►  on_new_bar()  ── sets state.current_price ONLY
     CDM PDM CMM PMM (authoritative)                         (never feeds a mean)
              │
              ▼
     _scan_and_place()  ── reconcile broker truth → cancel/place/reprice limits
```

**The key invariant from your spec:** locally aggregated tick bars are a *price hint only*.
They set `state.current_price` and nothing else (`on_new_bar`, line 367). Every mean comes
from broker bars, refetched each cycle.

---

## 4. The 60-second cycle

The loop starts at line 699. Cadence is monotonic (`next_refresh += 60`), so broker latency
doesn't drift the period, and it won't burst catch-up requests if the broker was slow.

Startup order is: authenticate → **resolve contract months (§5a)** → seed history and ATR →
subscribe to the stream → enter the loop.

Per symbol, `refresh_and_reprice()` (line 372) holds a per-symbol lock and runs two phases
in strict order:

**Phase 1 — `refresh_broker_means()` (line 153)**
1. Two REST calls: 5-min bars for the last 7 days, hourly bars from the prior month's open.
2. `completed_bars()` normalises timestamps to UTC, **deduplicates by timestamp**, and drops
   any bar whose close time exceeds `now` — so no partial bar enters a mean.
3. Bucket 5-min closes by futures day, hourly closes by futures month.
4. Publish all four means **atomically**. Both requests must succeed first; a failure raises
   and the caller skips placement rather than trading on stale levels.
5. An empty bucket **clears** that mean instead of leaking the old value across a roll.

**Phase 2 — `_scan_and_place()` (line 381)**
1. Weekend gate (Fri after 18:00 ET, all Saturday, Sunday before 18:00 ET).
2. Session-day roll clears the loss counter.
3. Query **open orders, then open positions**, in that order — if a fill lands between the
   two reads it is double-counted, never missed. If *either* query fails, place nothing.
4. Prune `pending_entries` of any order absent from the broker's open-order snapshot.
5. Adopt unclaimed broker orders within 20 ticks of a level (restart safety).
6. Cancel entries whose level is no longer eligible.
7. Place or reprice, strongest level first, until the contract cap is reached.

---

## 5. The four means — exact definitions

All are **running cumulative averages of closes**, never `(high+low)/2`.

| Level | Source bars | Window | Warm-up gate |
|---|---|---|---|
| **CDM** | 5-min | current futures day | `MIN_SAMPLES_CDM = 6` (~30 min) |
| **PDM** | 5-min | most recent prior futures day present | 1 bar |
| **CMM** | hourly | current futures month | `MIN_SAMPLES_CMM = 24` |
| **PMM** | hourly | **entire** prior futures month | 1 bar |

- **Futures day** rolls at **17:00 CT**: at or after 17:00, the day counts as tomorrow
  (`get_futures_day`, line 112). Futures month derives from the futures day.
- **PMM window** starts at the prior month's 17:00 CT session open, computed with calendar
  arithmetic *before* localising so DST can't shift it (`prior_month_start_utc`, line 126).
  This is the bug fixed earlier — a 45-day lookback could not cover a full prior month.
- Warm-up gates exist because a 1-sample CMM equals current price and would trigger an
  instant fill with no edge (observed 2026-06-01 globex open).

---

## 5a. Contract resolution (startup)

MCL rolls **monthly**, MGC every two months, and the index roots quarterly — so any hardcoded
month eventually becomes an expired contract. Before fetching a single bar,
`resolve_active_contracts()` (line 108) queries `/Contract/search` per root and selects:

1. only ids matching exactly `CON.F.US.<ROOT>.<MonthCode><YY>` — never a spread or another
   product;
2. only contracts whose `lastTradingDate` is still in the future;
3. the broker's `activeContract` flag first, nearest expiry as the tiebreak.

`apply_resolved_contracts()` then rewrites slot 0 of `CONTRACT_MAP`, demotes the previous
month into slot 1, and preserves tick size and tick value.

**A symbol that cannot be resolved is dropped from the session**, with the reason printed. If
none resolve, the engine exits. Routing an order to the wrong month is worse than not trading.
`verify_means.py` resolves the same way, so an audit cannot pass against a dead contract.

`CONTRACT_MAP` remains in the file as a documented fallback and as the source of tick data.

---

## 6. Level eligibility and priority

A **BUY limit must sit strictly below** current price; a **SELL limit strictly above**.
Otherwise it fills instantly at market and the level means nothing.

Levels are returned **strongest first** (`LEVEL_STRENGTH`, line 216):

| Level | Strength |
|---|---|
| CMM | 4 |
| PMM | 3 |
| CDM | 2 |
| PDM | 1 |

This matters because `MAX_CONTRACTS_PER_INSTRUMENT = 2` allows only two resting orders per
symbol. Placement walks strength order so the cap buys your strongest levels. Order
*adoption* separately walks a distance-sorted copy, so a resting broker order is claimed by
the level it actually sits nearest (lines 493–494).

Before `03b9769` this was plain dict order re-sorted by distance, so CDM and PDM took both
slots on essentially every scan and CMM/PMM were never armed.

---

## 7. Order lifecycle

| Event | Behaviour |
|---|---|
| Level becomes eligible | place limit, 1 contract (`CONTRACTS_PER_ORDER`) |
| Level moves ≥ 1 tick | cancel old, place new; **repriced on any tick change** (line 267) |
| Level moves < 1 tick | no action, no churn |
| Cancel not confirmed | **no replacement placed** — never trades into an unknown state |
| Order fills or vanishes | pruned from `pending_entries`, so the level can re-arm |
| Level becomes ineligible | order cancelled, cap credit returned |
| Restart with live orders | adopted from broker by proximity, no duplicates |

`pending_entries` is keyed `(symbol, level_name)`, so each level owns at most one order.

---

## 8. Guards

| Guard | Value / behaviour |
|---|---|
| Contracts per instrument | `2` — shared budget with the VWAP engine, counted from broker truth (positions + working orders, either side) |
| Contracts per order | `1` |
| Broker query failure | place nothing this cycle |
| Mean refresh failure | raises; placement skipped |
| Weekend | Fri ≥18:00 ET through Sun <18:00 ET |
| Warm-up | CDM 6 bars, CMM 24 bars |
| Dry run | `--dry-run` logs intent, mutates no broker state |

---

## 9. State model

Single module-level `state` object (`class State`, line 85). Everything is per-symbol dicts.

| Field | Meaning |
|---|---|
| `modes` | `{symbol: 'BUY'/'SELL'}` from the CLI |
| `current_price` | last local tick-bar close — **price hint only** |
| `cdm/pdm/cmm/pmm` | published means |
| `atr` | 3-day ATR, **seeded once at startup** |
| `pending_entries` | `{(symbol, level): {order_id, entry_price, side, ...}}` |
| `active_positions` | intended filled-position registry — **never written to** (see §11) |
| `session_losses` | consecutive losses per symbol — **never incremented** (see §11) |
| `refresh_locks` | per-symbol lock serialising each cycle |

State is in-memory only; broker reconciliation each cycle is what makes restarts safe.

---

## 10. Alignment with your stated spec

| Your requirement | Status |
|---|---|
| Four means: CDM, PDM, CMM, PMM | ✅ |
| Recomputed every 60s from completed broker bars | ✅ verified, 2 bar requests per cycle |
| Local tick bars never feed a mean | ✅ price hint only |
| Orders updated when the means move | ✅ reprices on ≥1 tick change |
| PMM covers the whole prior month | ✅ full session-open boundary |
| `pending_entries` clears on fill/cancel | ✅ |
| Running cumulative close average | ✅ not H/L midpoint |
| Correct contract month per symbol | ✅ resolved from the broker at startup (§5a) |
| Mean-level strength priority honoured | ✅ as of `03b9769` |
| Execution-only, stops attached manually | ✅ no brackets placed |
| 3 consecutive losses stops the symbol | ❌ **dead code** (§11.1) |

---

## 11. Gaps that need your decision

### 11.1 The three-consecutive-loss stop never fires

`state.active_positions` is **only read and deleted, never written** (lines 539, 740–762, 778). The code that populated it lived in `check_and_bracket_fills()`, which became a no-op
on 2026-07-23 when bracket placement was removed.

Consequences:
- the position-close monitor iterates an always-empty dict, so `session_losses` never
  increments and the `>= 3` stop at line 403 is unreachable;
- the "already filled at this level" guard at line 539 never fires, so a level can re-arm
  immediately after a fill — only the contract cap restrains it;
- the hourly status line always shows `Active:` empty.

Worth noting the old win/loss test was also only a heuristic: it inferred the result from
`current_price` vs entry rather than realized P&L. Rebuilding this properly means reading
fills or trade history, not resurrecting that comparison.

**Decision:** leave the loss-streak stop dead and rely on the cap, or rebuild fill tracking
from broker trade history.

### 11.2 ATR is seeded once and never refreshed

`state.atr` is written only inside `seed_historical()` (line 600). The 60-second loop never
recomputes it, so the reference stop distance printed with every order drifts stale over a
multi-day run. If the broker returns fewer than 4 daily bars it is never set at all and
every order logs `ATR pending`.

Low severity today because ATR is display-only in execution-only mode — but it is the number
you use to size the manual stop.

### 11.3 Contract months — RESOLVED 2026-09-22

Previously MYM's tuple had current and prior inverted (pointing at U26, expired 2026-09-18)
and MCL sat on V26 (expired 2026-09-21). Both are fixed, and the engine no longer trusts the
static map for routing — see §5a. Historical note follows.

#### What was wrong

```python
'MNQ': ('CON.F.US.MNQ.Z26', 'CON.F.US.MNQ.U26', 0.25, 0.50)   # correct: Z26 current
'MES': ('CON.F.US.MES.Z26', 'CON.F.US.MES.U26', 0.25, 1.25)   # correct: Z26 current
'MYM': ('CON.F.US.MYM.U26', 'CON.F.US.MYM.Z26', 1.0,  0.50)   # current/prior INVERTED
'MCL': ('CON.F.US.MCL.V26', 'CON.F.US.MCL.X26', 0.01, 1.00)   # V26 expiring
```

MYM's tuple has current and prior swapped relative to MNQ/MES: it points at **U26
(September 2026)**, which expired on the third Friday, 2026-09-18. Running `--mym` today
would target an expired contract. MCL V26 (October crude) expires around 2026-09-22, so it
needs rolling to X26 now.

MGC V26 (October gold) was still valid, but which of V26/Z26 carries the volume is a
judgement call — which is exactly why the active month now comes from the broker rather than
from a constant in this file.

### 11.4 Module docstring contradicts the code

The header still says "places limit orders at the closest mean level" and "1 lot per symbol,
max 3 positions (MNQ, MES, MYM)". Actual behaviour is strongest level first, a 2-contract
per-instrument cap, and 5 configured symbols.

### 11.5 Silent failure paths

The position monitor and the hourly status block are each wrapped in bare `except: pass`
(lines 764, 786), and `_scan_and_place` catches everything into a non-fatal print (line 553).
Real errors are survivable by design here, but they are invisible in logs.

---

## 12. How to verify without a broker

```bash
# Offline: real engine cycle against a simulated broker, recorded MNQ bars
MNQ_BAR_DIR=/path/to/parquet python sim_execution_harness.py

# Offline: 37 regression tests (month/year/leap/DST boundaries, partial and
# duplicate bars, cancel-failure, cap counting, restart adoption, Z26)
python -m pytest test_mean_regressions.py -q

# Against the broker, read-only: engine means vs an independent calculation
export PROJECT_X_ACCOUNT_NAME='50KTC-V2-DLL-163901-35987599'
python verify_means.py --symbol MES --compare-engine

# Against the broker, no orders
python realtime_engine.py --mes buy --mnq buy --dry-run
```

---

## 13. Outstanding repo hygiene

`.env` is **tracked in this public repository** with real credentials. Untracking it and
adding it to `.gitignore` does not remove it from history — the credentials should be rotated.
