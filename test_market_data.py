"""Offline tests for the market data store. No broker, no network."""
import contextlib
import io
import math
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import collect_data
from market_data import Collector, RateLimiter, build_root, contracts_between, load
from market_data.contracts import Contract, MONTH_CODES
from market_data.continuous import futures_day, plan_rolls
from market_data.store import MAX_BARS_PER_REQUEST, TIMEFRAMES, bars_to_frame

UTC = timezone.utc
TICK_OFFSET = 2.0  # each later contract trades this much above the previous


def path_price(ts: datetime) -> float:
    """Shared underlying price path. Contract prices = path + fixed offset."""
    hours = ts.timestamp() / 3600
    raw = 1000 + 25 * math.sin(hours / 40) + 0.01 * (hours % 7)
    return round(raw / 0.25) * 0.25  # on every root's tick grid


class FakeBroker:
    """Mimics /History/retrieveBars: newest-first, capped at the request limit."""

    def __init__(self, roots=("MCL",), fail=(), rate_limit_once=False, forming_bar=False):
        self.calls, self.fail = [], set(fail)
        self.rate_limit_once, self.forming_bar = rate_limit_once, forming_bar
        self.now = None
        self.chains = {r: contracts_between(r, date(2025, 1, 1), date(2027, 6, 1)) for r in roots}

    def contract(self, cid):
        for chain in self.chains.values():
            for i, c in enumerate(chain):
                if c.contract_id == cid:
                    return c, i, chain
        raise KeyError(cid)

    def volume(self, cid, ts):
        c, i, chain = self.contract(cid)
        prev_exp = chain[i - 1].expiry if i else c.expiry - timedelta(days=31)
        front_from = prev_exp - timedelta(days=5)
        front_to = c.expiry - timedelta(days=5)
        return 1000 if front_from <= ts.date() < front_to else 50

    def get_bars(self, contract_id, unit, unit_number, start_time, end_time,
                 limit, live, include_partial):
        self.calls.append(dict(contract_id=contract_id, unit=unit, unit_number=unit_number,
                               start_time=start_time, end_time=end_time, limit=limit,
                               live=live, include_partial=include_partial))
        if self.rate_limit_once:
            self.rate_limit_once = False
            raise RuntimeError("/History/retrieveBars returned 429: Too Many Requests")
        if contract_id in self.fail:
            raise RuntimeError("/History/retrieveBars returned 400: bad contract")
        c, i, _ = self.contract(contract_id)
        minutes = {(2, 1): 1, (2, 5): 5, (2, 15): 15, (3, 1): 60, (3, 4): 240, (4, 1): 1440}[(unit, unit_number)]
        step = timedelta(minutes=minutes)
        first = datetime(2020, 1, 1, tzinfo=UTC)
        t = first + ((start_time - first) // step) * step
        if t < start_time:
            t += step
        bars = []
        while t < end_time:
            closes = t + step
            if closes <= self.now and t.weekday() != 5:
                o, cl = path_price(t) + i * TICK_OFFSET, path_price(closes) + i * TICK_OFFSET
                bars.append({"t": t.isoformat().replace("+00:00", "Z"), "o": o, "h": max(o, cl) + 0.5,
                             "l": min(o, cl) - 0.5, "c": cl, "v": self.volume(contract_id, t)})
            t += step
        if self.forming_bar:
            fb = self.now - step / 2
            bars.append({"t": fb.isoformat(), "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1})
        assert len(bars) <= limit, "request would have exceeded the broker's bar limit"
        return list(reversed(bars))


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class ContractTests(unittest.TestCase):
    def test_expiries_match_exchange_calendars(self):
        cases = {("MCL", "V", 2026): date(2026, 9, 21), ("MCL", "X", 2026): date(2026, 10, 19),
                 ("MNQ", "Z", 2026): date(2026, 12, 18), ("MES", "U", 2026): date(2026, 9, 18),
                 ("MGC", "V", 2026): date(2026, 10, 28), ("MGC", "V", 2025): date(2025, 10, 29)}
        for (root, code, year), expected in cases.items():
            self.assertEqual(Contract(root, code, year).expiry, expected, (root, code, year))

    def test_cycles(self):
        mcl = contracts_between("MCL", date(2026, 3, 1), date(2026, 9, 1))
        codes = [c.month_code for c in mcl]
        for a, b in zip(codes, codes[1:]):
            self.assertEqual((MONTH_CODES.index(a) + 1) % 12, MONTH_CODES.index(b) % 12)
        self.assertTrue(all(c.month_code in "GJMQVZ"
                            for c in contracts_between("MGC", date(2026, 1, 1), date(2026, 12, 1))))
        self.assertTrue(all(c.month_code in "HMUZ"
                            for c in contracts_between("MNQ", date(2026, 1, 1), date(2026, 12, 1))))

    def test_ids_and_symbols(self):
        c = Contract("MNQ", "Z", 2026)
        self.assertEqual((c.contract_id, c.symbol), ("CON.F.US.MNQ.Z26", "MNQZ6"))

    def test_unknown_root_rejected(self):
        with self.assertRaises(ValueError):
            contracts_between("ZZZ", date(2026, 1, 1), date(2026, 2, 1))


class RateLimiterTests(unittest.TestCase):
    def test_never_exceeds_window(self):
        clock = Clock()
        limiter = RateLimiter(max_calls=45, period=30, clock=clock, sleep=clock.sleep)
        stamps = []
        for _ in range(200):
            limiter.wait()
            stamps.append(clock.t)
        for i, s in enumerate(stamps):
            self.assertLessEqual(sum(1 for x in stamps[i:] if x - s < 30), 45)


class CollectorTests(unittest.TestCase):
    NOW = datetime(2026, 9, 25, 21, 0, tzinfo=UTC)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)

    def collector(self, broker, now=None):
        now = now or self.NOW
        broker.now = now
        clock = Clock()
        return Collector(broker, self.store, limiter=RateLimiter(clock=clock, sleep=clock.sleep),
                         now=lambda: now, sleep=lambda s: None, log=lambda *a: None)

    def test_requests_stay_within_broker_limit_and_never_ask_for_partial(self):
        broker = FakeBroker()
        c = Contract("MCL", "X", 2026)
        self.collector(broker).collect(c, "1m")
        self.assertGreater(len(broker.calls), 1, "1m history must be paged")
        for call in broker.calls:
            self.assertEqual(call["limit"], MAX_BARS_PER_REQUEST)
            self.assertFalse(call["include_partial"])
            self.assertFalse(call["live"])
            span = (call["end_time"] - call["start_time"]).total_seconds() / 60
            self.assertLessEqual(span, MAX_BARS_PER_REQUEST)

    def test_stored_bars_sorted_unique_and_closed(self):
        broker = FakeBroker(forming_bar=True)
        c = Contract("MCL", "X", 2026)
        col = self.collector(broker)
        col.collect(c, "5m")
        frame = pd.read_parquet(col.raw_path(c, "5m"))
        self.assertTrue(frame.ts_event.is_monotonic_increasing)
        self.assertFalse(frame.ts_event.duplicated().any())
        self.assertTrue((frame.ts_event + pd.Timedelta(minutes=5) <= self.NOW).all())
        self.assertEqual(list(frame.columns), ["ts_event", "open", "high", "low", "close", "volume"])

    def test_bad_bars_rejected(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        bars = [{"t": "2025-12-31T00:00:00Z", "o": 10, "h": 9, "l": 8, "c": 10, "v": 1},
                {"t": "2025-12-31T00:01:00Z", "o": 10, "h": 11, "l": 9, "c": 10, "v": 1},
                {"t": "2025-12-31T00:02:00Z", "o": None, "h": 11, "l": 9, "c": 10, "v": 1}]
        frame, rejected = bars_to_frame(bars, 1, now)
        self.assertEqual((len(frame), rejected), (1, 2))

    def test_incremental_run_only_fetches_new_bars(self):
        c = Contract("MCL", "X", 2026)
        broker = FakeBroker()
        self.collector(broker).collect(c, "1h")
        first_rows = len(pd.read_parquet(self.store / "raw/MCL" / c.contract_id / "1h.parquet"))
        later = self.NOW + timedelta(hours=6)
        broker.calls.clear()
        self.collector(broker, later).collect(c, "1h")
        self.assertEqual(len(broker.calls), 1)
        self.assertGreaterEqual(broker.calls[0]["start_time"], self.NOW - timedelta(hours=2))
        frame = pd.read_parquet(self.store / "raw/MCL" / c.contract_id / "1h.parquet")
        self.assertFalse(frame.ts_event.duplicated().any())
        self.assertGreater(len(frame), first_rows)

    def test_expired_and_complete_contracts_are_not_refetched(self):
        c = Contract("MCL", "Q", 2026)  # expired July 2026
        broker = FakeBroker()
        entry = self.collector(broker).collect(c, "1h")
        self.assertTrue(entry["complete"])
        broker.calls.clear()
        self.collector(broker, self.NOW + timedelta(days=1)).collect(c, "1h")
        self.assertEqual(broker.calls, [])

    def test_rate_limit_is_retried(self):
        broker = FakeBroker(rate_limit_once=True)
        entry = self.collector(broker).collect(Contract("MCL", "X", 2026), "1d")
        self.assertNotIn("error", entry)
        self.assertGreater(entry["bars"], 0)

    def test_failure_is_recorded_and_other_contracts_continue(self):
        bad = Contract("MCL", "V", 2026)
        broker = FakeBroker(fail={bad.contract_id})
        col = self.collector(broker)
        self.assertIn("error", col.collect_safe(bad, "1h"))
        self.assertNotIn("error", col.collect_safe(Contract("MCL", "X", 2026), "1h"))
        self.assertFalse(col.manifest[bad.contract_id]["1h"].get("complete"))


class ContinuousTests(unittest.TestCase):
    NOW = datetime(2026, 9, 25, 21, 0, tzinfo=UTC)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)
        broker = FakeBroker(roots=("MCL",))
        broker.now = self.NOW
        clock = Clock()
        col = Collector(broker, self.store, limiter=RateLimiter(clock=clock, sleep=clock.sleep),
                        now=lambda: self.NOW, sleep=lambda s: None, log=lambda *a: None)
        self.contracts = contracts_between("MCL", date(2026, 5, 1), self.NOW.date())
        for c in self.contracts:
            for tf in ("1h", "1d"):
                col.collect(c, tf)
        with quiet():
            self.summary = build_root(self.store, "MCL", self.contracts, ["1h", "1d"])

    def test_back_adjusted_series_is_seamless(self):
        # Every contract = shared path + constant offset, so a correct additive
        # back-adjustment reproduces the path + the NEWEST contract's offset.
        adj = load("MCL", "1h", store=self.store)
        rolls = pd.read_csv(self.store / "continuous/mcl-rolls.csv")
        last_idx = [c.contract_id for c in FakeBroker(("MCL",)).chains["MCL"]].index(rolls.contract.iloc[-1])
        expected = adj.ts_event.map(lambda t: path_price(t.to_pydatetime() + timedelta(hours=1))) + last_idx * TICK_OFFSET
        self.assertLess((adj.close - expected).abs().max(), 1e-9)
        self.assertGreaterEqual(len(rolls), 3, "a monthly contract must roll several times")

    def test_unadjusted_keeps_real_prices(self):
        raw = load("MCL", "1h", adjusted=False, store=self.store)
        chain = [c.symbol for c in FakeBroker(("MCL",)).chains["MCL"]]
        idx = raw.symbol.map(chain.index)
        expected = raw.ts_event.map(lambda t: path_price(t.to_pydatetime() + timedelta(hours=1))) + idx * TICK_OFFSET
        self.assertLess((raw.close - expected).abs().max(), 1e-9)

    def test_roll_takes_effect_after_the_crossover_day(self):
        raw = load("MCL", "1h", adjusted=False, store=self.store)
        broker = FakeBroker(("MCL",))
        days = futures_day(raw.ts_event)
        for sym, day in zip(raw.symbol, days):
            c = next(c for c in broker.chains["MCL"] if c.symbol == sym)
            self.assertLessEqual(day, c.expiry, f"{sym} used after its expiry")
        # Volume in the fake flips 5 days before expiry; the switch must come after.
        switches = raw[raw.symbol != raw.symbol.shift()].iloc[1:]
        for _, row in switches.iterrows():
            prev = raw[raw.ts_event < row.ts_event].iloc[-1]
            c = next(c for c in broker.chains["MCL"] if c.symbol == prev.symbol)
            flip = c.expiry - timedelta(days=5)
            self.assertGreater(futures_day(pd.Series([row.ts_event])).iloc[0], flip - timedelta(days=1))

    def test_roll_uses_only_completed_days(self):
        # The day volume crosses over is the DECISION day. It must still trade
        # the old contract; the new one starts the following futures day.
        rolls = pd.read_csv(self.store / "continuous/mcl-rolls.csv",
                            parse_dates=["first_day", "last_day"])
        raw = load("MCL", "1h", adjusted=False, store=self.store)
        days = pd.to_datetime(futures_day(raw.ts_event))
        for k in range(len(rolls) - 1):
            decision = rolls.last_day[k]
            self.assertGreater(rolls.first_day[k + 1], decision)
            on_decision_day = raw[days == decision].symbol.unique().tolist()
            self.assertEqual(on_decision_day, [rolls.symbol[k]], f"look-ahead at {decision.date()}")

    def test_schema_matches_tarric_files(self):
        adj = load("MCL", "1h", store=self.store)
        self.assertEqual(list(adj.columns), ["ts_event", "open", "high", "low", "close", "volume", "symbol"])
        self.assertEqual(str(adj.ts_event.dt.tz), "UTC")
        self.assertEqual(adj.volume.dtype, "int64")
        self.assertTrue(adj.ts_event.is_monotonic_increasing)
        self.assertFalse(adj.ts_event.duplicated().any())

    def test_no_hourly_data_builds_nothing(self):
        with quiet():
            self.assertEqual(build_root(self.store, "MES", contracts_between(
                "MES", date(2026, 5, 1), self.NOW.date()), ["1h"]), {})


class TickGridTests(unittest.TestCase):
    def test_back_adjustment_stays_on_grid_despite_float_drift(self):
        # Adding float gaps and multiplying by ticks like 0.01 leaves noise such
        # as 61.370000000000005. The backtester rejects off-grid prices, so the
        # snapped value must equal the exact decimal price.
        from decimal import Decimal
        from market_data.continuous import snap
        import random
        rng = random.Random(7)
        for tick in (0.01, 0.10, 0.25, 1.0):
            cents = [rng.randint(1000, 900000) for _ in range(2000)]
            gaps = [rng.randint(-500, 500) for _ in range(2000)]
            raw = pd.Series([c * tick for c in cents])
            adj = pd.Series([sum([g * tick] * 3) for g in gaps])
            out = snap(raw + adj, tick)
            places = Decimal(str(tick))
            for value, c, g in zip(out, cents, gaps):
                exact = (Decimal(c) + 3 * Decimal(g)) * places
                self.assertEqual(Decimal(repr(float(value))), exact.normalize()
                                 if exact == exact.to_integral() else exact,
                                 f"tick {tick}: {value!r} != {exact}")

    def test_mnq_output_loads_in_the_backtester(self):
        import sys
        bt = Path("/home/user/workspace/mean-levels-backtest")
        if not (bt / "run_backtest.py").exists():
            self.skipTest("backtest workspace not present")
        sys.path.insert(0, str(bt))
        try:
            import run_backtest
        except ImportError:
            self.skipTest("topstep_backtest not installed")
        with tempfile.TemporaryDirectory() as tmp:
            broker = FakeBroker(roots=("MNQ",))
            now = datetime(2026, 9, 25, 21, 0, tzinfo=UTC)
            broker.now = now
            with quiet():
                collect_data.main(["--symbols", "MNQ", "--since", "2026-06-01",
                                   "--timeframes", "5m", "--store", tmp], api=broker, now=now)
            frame, bars = run_backtest.load_bars(
                Path(tmp) / "continuous/mnq-continuous-5m.parquet", "MNQ", 5)
            self.assertGreater(len(bars), 1000)


class CliTests(unittest.TestCase):
    def test_end_to_end_with_fake_broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = FakeBroker(roots=("MCL", "MNQ"))
            now = datetime(2026, 9, 25, 21, 0, tzinfo=UTC)
            broker.now = now
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = collect_data.main(["--symbols", "MCL", "MNQ", "--since", "2026-07-01",
                                          "--timeframes", "1h", "1d", "--store", tmp],
                                         api=broker, now=now)
            self.assertEqual(code, 0, out.getvalue())
            for name in ("mcl-continuous-1h", "mnq-continuous-1d", "mnq-unadjusted-1h"):
                self.assertTrue((Path(tmp) / "continuous" / f"{name}.parquet").exists(), name)
            self.assertIn("All requests succeeded", out.getvalue())

    def test_failures_set_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 9, 25, 21, 0, tzinfo=UTC)
            broker = FakeBroker(roots=("MCL",), fail={"CON.F.US.MCL.X26"})
            broker.now = now
            with quiet():
                code = collect_data.main(["--symbols", "MCL", "--since", "2026-08-01",
                                          "--timeframes", "1d", "--store", tmp], api=broker, now=now)
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
