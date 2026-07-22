# TopstepX Futures Engine Rebuild — Scope Document

**Branch:** `rebuild-data-layer` off `main` @ `05bcbee`
**Date:** 2026-07-22
**Status:** DRAFT — awaiting user approval before Phase 1 begins

---

## 1. Purpose

Remove the abandoned `project-x-py` third-party wrapper library from the futures execution engine and rewrite the data layer to call the ProjectX Gateway API directly. Preserve the mean-level execution algorithm (CDM / PDM / CMM / PMM) exactly as-is — this is the proven alpha, and it is NOT being modified.

## 2. Background

- Engine ran stably April–early June 2026 on `project-x-py` v3.5.x
- Roll-week fixes (June 16–24) introduced regressions we later rolled back
- July 1: `project-x-py` began crashing on real-time bars (`unable to append DataFrame of width 8 with width 6`)
- Library maintainer has been silent for 9+ months (last release Sept 2025). PyPI shows no patches.
- Every SDK downgrade attempt (3.5.9 → 3.5.8 → 3.5.7 → 3.4.0 → 3.3.2) hit either the same schema bug or a new API mismatch or a polars incompatibility
- Verdict: the library is dead. We need to own the data layer.

Meanwhile, half the engine already bypasses the library — order placement, order cancel, position search, and cap enforcement all POST directly to ProjectX. And `build2.py` in this repo already contains working direct-API code for authentication and historical bar retrieval. The rebuild is finishing what's already half-done, not inventing new architecture.

## 3. What We Keep (UNTOUCHED)

The following are proven working code and MUST NOT be modified in this rebuild:

- **Mean-level algorithm** (`get_all_eligible_levels()`, CDM/PDM/CMM/PMM ordering, tick rounding)
- **Stop math** (0.382 × 3-day ATR)
- **Target math** (2.618 R:R)
- **Cap enforcement** (broker-verified position + working order count, currently 2/instrument)
- **Bracket-on-fill pattern** (`check_and_bracket_fills`)
- **Quarterly roll handling** (`CONTRACT_MAP` with front + prior_expired contracts)
- **Trade log SQLite schema**
- **All fixes from commits** `76c298b` through `05bcbee`:
  - GET→POST cap query fix
  - Cap=2 limit
  - CMM re-enabled, PMM disabled until Aug 1
  - Canonical roll spread (already inert since we're on U26 with native data)

## 4. What We Replace

Only the following functions are replaced. Everything else stays.

### 4.1. Historical bar fetching

**Current:** `client.get_bars(symbol, days, interval)` via `project-x-py`
**New:** Direct POST to `https://api.topstepx.com/api/History/retrieveBars`
**Payload shape (confirmed in existing `build2.py`):**
```python
{
    "contractId": "CON.F.US.MES.U26",
    "live": False,
    "startTime": "2026-07-01T00:00:00Z",
    "endTime": "2026-07-22T00:00:00Z",
    "unit": 2,          # 1=sec, 2=min, 3=hour, 4=day
    "unitNumber": 5,    # e.g. 5 for 5-minute
    "limit": 5000,
    "includePartialBar": True,
}
```
**Response:** `{"bars": [{t, o, h, l, c, v}, ...]}`

### 4.2. Real-time bar streaming

**Current:** `TradingSuite.data` bar-close events via `project-x-py`
**New:** Direct SignalR WebSocket to `wss://rtc.topstepx.com/hubs/market`

**CRITICAL DESIGN NOTE:** The ProjectX market hub emits ONLY tick-level events:
- `GatewayQuote(contractId, data)` — bid/ask updates
- `GatewayTrade(contractId, data)` — executed trades
- `GatewayDepth(contractId, data)` — order book depth

**There is no native 5-min bar close event.** We must aggregate ticks into 5-min bars in our own code.

**Aggregator design:**
- Subscribe to `SubscribeContractTrades(contract_id)` on the market hub
- Buffer each trade's price + volume into the current 5-min bucket (keyed on UTC minute floor)
- When wall-clock crosses a 5-min boundary, "close" the current bucket → fire callback with the closed bar → start next bucket
- Bar has: `t` (bucket start UTC ISO), `o` (first trade price in bucket), `h`/`l` (min/max), `c` (last trade price), `v` (sum of trade sizes)

**Reconnect handling:**
- SignalR `withAutomaticReconnect()` (Python `signalrcore` lib equivalent)
- On reconnect, we re-invoke `SubscribeContractTrades` (SignalR does not auto-resubscribe)
- On any disconnect > 30 seconds, we backfill the missed 5-min bars via `/History/retrieveBars` before resuming live aggregation

### 4.3. Order/position operations

**No change.** Existing direct POSTs to `/Order/place`, `/Order/cancel`, `/Order/searchOpen`, `/Position/searchOpen`, and `/Position/closeContract` remain as-is.

### 4.4. Authentication

**Current:** `project-x-py` internal auth flow
**New:** POST to `https://api.topstepx.com/api/Auth/loginKey` (already used by `build2.py`)

```python
POST /api/Auth/loginKey
Body: {"userName": "...", "apiKey": "..."}
Response: {"token": "eyJ..."}
```

Token used as `Authorization: Bearer {token}` in all REST calls, and passed as `?access_token={token}` in WebSocket connection URL.

**Token refresh:** JWT expiry appears to be ~24hrs per Topstep docs. We fetch a fresh token on engine start; if any REST call returns 401, we re-authenticate and retry once.

## 5. File Structure

### New files (created by this rebuild)

- `topstep_api.py` — REST client. Auth, historical bars, order operations, position queries. Consolidates the direct-API code that's currently scattered across `realtime_engine.py`, `build2.py`, and `mean_levels_calc.py`.
- `topstep_stream.py` — SignalR real-time client. Handles WebSocket lifecycle, subscription, tick→5min aggregation, reconnect+backfill.
- `test_topstep_api.py` — Standalone script that exercises `topstep_api.py` end-to-end: auth, fetch bars, get positions. Passes = REST layer works.
- `test_topstep_stream.py` — Standalone script that connects, subscribes, and prints 5-min bars for 15 minutes. Passes = streaming layer works.

### Existing files modified

- `realtime_engine.py` — Only the `main()` function is modified:
  - Replace `from project_x_py import ProjectX, TradingSuite` with `from topstep_api import ...` and `from topstep_stream import ...`
  - Replace `TradingSuite.create()` block with `TopstepStream(...)` initialization
  - Replace bar-close callback wiring to call our aggregator's callback instead of the SDK's
  - Estimated ~50 lines changed, all in `main()`
- `requirements.txt` (or `setup_mac.sh`) — Remove `project-x-py`, add `signalrcore`, `aiohttp`, `requests`

### Existing files untouched

- `mean_levels_calc.py`, `trend_filters.py`, `check_atr.py`, `build2.py`, `run_regime_session.py` — no changes
- All algo code in `realtime_engine.py` outside `main()` — no changes

## 6. Success Criteria (Merge Gate)

All five MUST pass before `rebuild-data-layer` merges to `main`:

1. **`test_topstep_api.py` passes**: authenticates, fetches 5 days of MES 5-min bars (>1400 bars expected), gets current positions (empty list), completes cleanly with exit code 0.

2. **`test_topstep_stream.py` passes**: connects to market hub, subscribes to MES U26 trades, aggregates ticks into 5-min bars, prints ≥3 consecutive bar closes over ~15 minutes with reasonable OHLCV values (no width-mismatch errors, no missing bar boundaries).

3. **Full dry-run session clean**: `python realtime_engine.py --mes buy --dry-run` runs from Globex open (Sunday 5pm CT) through daily maintenance break (Monday 4pm CT) with zero unhandled exceptions.

4. **Seeded means match TradingView**: MES CDM, PDM, CMM values within 0.05% of TV. (PMM stays disabled until Aug 1.)

5. **Cap enforcement holds across restart**: manually place a 1-contract MES position via TopstepX UI, start the engine, verify it sees the position (logs "1 pos + 0 working"), and confirms it will not place a second entry if that would breach cap=2.

## 7. Non-Goals

Explicitly OUT OF SCOPE for this rebuild. If any of these are needed, they are separate work AFTER this rebuild is stable:

- ❌ No changes to the mean-level algorithm
- ❌ No changes to cap logic (2 stays 2)
- ❌ No changes to bracket/stop/target math
- ❌ No re-enabling PMM (waits until Aug 1 regardless)
- ❌ No new instruments (MNQ + MES only, MYM + MGC stay dormant unless user requests)
- ❌ No dashboard changes
- ❌ No Tzu Equities changes (different repo, different engine)
- ❌ No performance optimization
- ❌ No new logging/telemetry beyond what's needed to debug failures
- ❌ No credential rotation or auth mechanism changes

## 8. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| SignalR reconnect drops ticks → wrong 5-min bar OHLCV | Medium | High | On any disconnect >30s, refetch missed bars from `/History/retrieveBars` before resuming live. Pause order placement during any gap. |
| Tick→bar aggregation off-by-one on minute boundary | Medium | Medium | Bar buckets keyed on `UTC floor(timestamp, 5min)`. Test explicitly with `test_topstep_stream.py`. |
| JWT token expires mid-session | Medium | High | Refresh on 401. Also proactively re-auth if uptime >20hrs. |
| ProjectX rate-limits us | Low | High | Batch calls, respect `/History/retrieveBars` limits (5000 bars/call). Do not exceed 10 REST calls/second. |
| Market hub subscription silently stops emitting | Medium | High | Watchdog: if no `GatewayTrade` event for a contract in >60 seconds during RTH, log warning and re-subscribe. |
| Historical + live bar boundary mismatch on engine startup | Medium | Medium | On engine start: fetch history up to "now-5min", then start live aggregation for the current 5-min bucket. Sync point is the last completed 5-min boundary. |
| Cap query race on engine restart with existing broker positions | Low | High | Cap logic already fixed (`bf23719`). Restart with existing positions is Test Criterion 5. |
| Quarterly roll boundary (Sept 11 U26→Z26) breaks streaming | Low | High | `CONTRACT_MAP` in the algo handles roll. Rebuild does not change roll logic. Retest before Sept 11. |
| We introduce a bug that costs money on live test | Low but real | High | Live test uses cap=1 for one MES contract only. Max exposure = 1 × ~$80 stop = ~$100. |

## 9. Timeline (Realistic, Not Optimistic)

- **Wed 7/22 (today)** — Scope doc written, committed, awaiting user approval
- **Thu 7/23** — User reviews scope, red-lines what changes are needed
- **Fri 7/24** — Phase 1: build `topstep_api.py` + `test_topstep_api.py`. Success criterion 1 must pass.
- **Sat 7/25** — Phase 2: build `topstep_stream.py` + `test_topstep_stream.py`. Success criterion 2 must pass.
- **Sun 7/26** — Phase 3: wire into `realtime_engine.py` main(). Start dry-run at Globex open (Sunday 5pm CT).
- **Mon 7/27** — Success criteria 3 and 4 evaluated end-of-day. If pass, prep for live test.
- **Tue 7/28** — Success criterion 5 tested. If pass, single-contract MES live for the session.
- **Wed 7/29** — If Tuesday clean, full production (both instruments, cap=2, all enabled levels).

Any failure at any step pushes everything downstream by at least a day. No skipping steps.

## 10. Rollback Plan

If the rebuild fails or misbehaves after merging to main, revert to `05bcbee` with:
```bash
git checkout main && git reset --hard 05bcbee
```
This restores the previous (broken-but-known) state. The old `project-x-py` code path becomes usable again once the library is fixed (though we don't expect that to happen).

## 11. What This Rebuild Does NOT Solve

Being explicit about what still won't be true even after this ships:

- Does NOT recover the $2,248 already lost. The account starts at ~$2,752 whenever we go live.
- Does NOT prevent all future infrastructure failures. It removes one specific dependency risk (abandoned library). Other risks remain: ProjectX outages, network issues, exchange halts.
- Does NOT change the trading strategy's edge or risk profile. Same algo, same means, same stops.
- Does NOT prevent human error in configuration (wrong contract, wrong side, wrong cap).

## 12. Approval

**Before Phase 1 begins, user (mjf1288) must approve this scope in the thread.**

Red-line changes acceptable. Add / remove / rescope any section. But: no code gets written on this branch until this document is signed off.

---

**End of scope document.**
