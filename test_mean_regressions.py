"""Offline regression suite. Never authenticates, starts engines or trades.

Run: PYTHON_DOTENV_DISABLED=1 python -m unittest -v test_mean_regressions
Fixtures go through verify_means.py's independent math AND CLI entry point.
"""
import asyncio
import contextlib
import copy
import io
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
import realtime_engine as engine
import verify_means as verifier
from topstep_api import TopstepAPI, TopstepAPIError

NOW = datetime(2026, 9, 21, 16, 2, tzinfo=timezone.utc)
CID = engine.CONTRACT_MAP["MES"][0]
ACCOUNT = SimpleNamespace(id=123)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def history(start, end, minutes):
    bars = []
    ts = start.replace(second=0, microsecond=0)
    ts -= timedelta(minutes=ts.minute % minutes)
    while ts + timedelta(minutes=minutes) <= end:
        local = ts.astimezone(engine.CT)
        # CME-like weekday sessions, including Sunday evening and daily break.
        closed = (local.weekday() == 5
                  or (local.weekday() == 4 and local.hour >= 16)
                  or (local.weekday() == 6 and local.hour < 17)
                  or local.hour == 16)
        if ts >= start and not closed:
            close = 7000 + local.day * 3 + local.hour / 4
            bars.append({"t": ts.isoformat(), "c": close,
                         "o": close, "h": close + 1, "l": close - 1, "v": 10})
        ts += timedelta(minutes=minutes)
    return bars


class FixtureClient:
    base_url = "https://broker.invalid/api"

    def __init__(self, now=NOW):
        self.day = history(now - timedelta(days=7), now, 5)
        self.hour = history(verifier.reference_window_start(now), now, 60)
        self.calls = []

    def get_session_token(self):
        return "offline-test-token"

    def search_contract(self, symbol):
        # Mirrors /Contract/search: the live month for this root, unexpired.
        return [{
            "id": engine.CONTRACT_MAP[symbol][0],
            "activeContract": True,
            "lastTradingDate": (NOW + timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }]

    def get_bars(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["contract_id"] == CID
        assert kwargs["include_partial"] is False
        return copy.deepcopy(self.day if kwargs["unit"] == 2 else self.hour)


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return copy.deepcopy(self.body)


class Broker:
    def __init__(self, orders=None, positions=None):
        self.orders = orders or []
        self.positions = positions or []
        self.calls = []
        self.cancel_success = True
        self.place_success = True
        self.query_overrides = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, json, headers):
        path = url.removeprefix(FixtureClient.base_url)
        self.calls.append((path, json))
        if path in self.query_overrides:
            return self.query_overrides[path]
        if path == "/Position/searchOpen":
            return Response({"success": True, "positions": self.positions})
        if path == "/Order/searchOpen":
            return Response({"success": True, "orders": self.orders})
        if path == "/Order/cancel":
            if self.cancel_success:
                self.orders = [o for o in self.orders if o["id"] != json["orderId"]]
            return Response({"success": self.cancel_success})
        if path == "/Order/place":
            if not self.place_success:
                return Response({"success": False})
            order_id = 1000 + len(self.calls)
            self.orders.append(dict(json, id=order_id))
            return Response({"success": True, "orderId": order_id})
        raise AssertionError(f"Unexpected broker call: {path}")


def order(order_id=10, price=99, side=0, size=1, contract=CID):
    return {"id": order_id, "contractId": contract, "type": 1,
            "side": side, "size": size, "limitPrice": price}


def pending(order_id=10, price=99):
    return {"order_id": order_id, "entry_price": price, "side": 0,
            "contract_id": CID, "level": "CDM"}


class BaseTests(unittest.TestCase):
    def setUp(self):
        engine.state = engine.State()
        engine.state.modes = {"MES": "BUY"}
        # Any unmocked network access is a hard failure.
        self.network = patch("requests.sessions.Session.request",
                             side_effect=AssertionError("Network prohibited in offline tests"))
        self.network.start()
        self.addCleanup(self.network.stop)


class MeansTests(BaseTests):
    def test_all_four_means_match_verify_means(self):
        client = FixtureClient()
        values = engine.refresh_broker_means(client, "MES", NOW)
        reference = verifier.reference_means(client.day, client.hour, NOW)
        for name, value in values.items():
            self.assertAlmostEqual(value, reference[name]["value"])
        self.assertEqual([c["unit"] for c in client.calls], [2, 3])
        self.assertTrue(all(c["include_partial"] is False for c in client.calls))
        self.assertEqual(client.calls[1]["start_time"],
                         datetime(2026, 7, 31, 22, tzinfo=timezone.utc))

    def test_45_day_window_demonstrably_changes_pmm(self):
        client = FixtureClient()
        full = engine.refresh_broker_means(client, "MES", NOW)["PMM"]
        clipped = [b for b in client.hour
                   if datetime.fromisoformat(b["t"]) >= NOW - timedelta(days=45)]
        wrong = verifier.reference_means(client.day, clipped, NOW)["PMM"]["value"]
        self.assertGreater(abs(full - wrong), 1)

    def test_calendar_window_year_leap_month_roll_and_dst(self):
        cases = [
            ("2026-01-31T23:01:00+00:00", "2025-12-31T23:00:00+00:00"),
            ("2026-01-21T16:00:00+00:00", "2025-11-30T23:00:00+00:00"),
            ("2024-03-31T21:00:00+00:00", "2024-01-31T23:00:00+00:00"),
            ("2026-04-20T16:00:00+00:00", "2026-02-28T23:00:00+00:00"),
            ("2026-12-20T16:00:00+00:00", "2026-10-31T22:00:00+00:00"),
            ("2026-09-30T22:01:00+00:00", "2026-08-31T22:00:00+00:00"),
        ]
        for now, expected in cases:
            with self.subTest(now=now):
                instant = datetime.fromisoformat(now)
                self.assertEqual(engine.prior_month_start_utc(instant), datetime.fromisoformat(expected))
                self.assertEqual(engine.prior_month_start_utc(instant),
                                 verifier.reference_window_start(instant))

    def test_repeated_refresh_replaces_drift_and_never_double_counts(self):
        client = FixtureClient()
        first = engine.refresh_broker_means(client, "MES", NOW)
        before = copy.deepcopy(engine.state.day_closes)
        engine.state.cdm["MES"] = 999999
        engine.state.pmm["MES"] = 999999
        second = engine.refresh_broker_means(client, "MES", NOW)
        self.assertEqual(first, second)
        self.assertEqual(before, engine.state.day_closes)

    def test_changed_broker_closes_update_all_four_means(self):
        client = FixtureClient()
        first = engine.refresh_broker_means(client, "MES", NOW)
        for bar in client.day + client.hour:
            bar["c"] += 10
        second = engine.refresh_broker_means(client, "MES", NOW)
        for name in first:
            self.assertAlmostEqual(second[name] - first[name], 10)

    def test_partial_duplicate_future_and_naive_timestamps(self):
        client = FixtureClient()
        expected = engine.refresh_broker_means(client, "MES", NOW)
        client.day += [copy.deepcopy(client.day[-1]),
                       {"t": "2026-09-21T16:00:00Z", "c": 999999},
                       {"t": "2026-09-22T16:00:00Z", "c": 999999}]
        client.hour += [{"t": "2026-09-21T16:00:00Z", "c": 999999}]
        client.day[0]["t"] = datetime.fromisoformat(client.day[0]["t"]).replace(tzinfo=None).isoformat()
        self.assertEqual(expected, engine.refresh_broker_means(client, "MES", NOW))
        verifier.compare_engine("MES", client.day, client.hour, NOW)

    def test_pdm_skips_weekend(self):
        client = FixtureClient()
        engine.refresh_broker_means(client, "MES", NOW)
        friday = [b["c"] for b in client.day if
                  verifier.get_futures_day(datetime.fromisoformat(b["t"]).astimezone(engine.CT)).isoformat() == "2026-09-18"]
        self.assertAlmostEqual(engine.state.pdm["MES"], sum(friday) / len(friday))

    def test_warmup_and_absent_month_clear_cached_values(self):
        now = datetime(2026, 10, 1, 22, 10, tzinfo=timezone.utc)
        client = FixtureClient(now)
        for name in ("cdm", "pdm", "cmm", "pmm"):
            getattr(engine.state, name)["MES"] = 999999
        # Remove the previous month, but retain some valid current-hour bars.
        client.hour = [b for b in client.hour if verifier.get_futures_month(
            datetime.fromisoformat(b["t"]).astimezone(engine.CT)) == (2026, 10)]
        values = engine.refresh_broker_means(client, "MES", now)
        self.assertIsNone(values["CDM"])
        self.assertIsNone(values["CMM"])
        self.assertIsNone(values["PMM"])
        self.assertNotEqual(values["PDM"], 999999)

    def test_failed_second_pull_does_not_partially_publish(self):
        client = FixtureClient()
        engine.refresh_broker_means(client, "MES", NOW)
        original = engine.state.cdm["MES"]
        client.day[-1]["c"] = 999999
        client.hour = None
        with self.assertRaises(ValueError):
            engine.refresh_broker_means(client, "MES", NOW)
        self.assertEqual(engine.state.cdm["MES"], original)

    def test_nonfinite_bar_fails_closed(self):
        client = FixtureClient()
        client.day[-1]["c"] = float("nan")
        with self.assertRaises(ValueError):
            engine.refresh_broker_means(client, "MES", NOW)
        self.assertNotIn("MES", engine.state.cdm)

    def test_verifier_cli_compares_engine_with_read_only_fixture(self):
        client = FixtureClient()
        output = io.StringIO()
        with patch.object(verifier, "from_env", return_value=client), \
             patch.object(verifier, "datetime", Clock), \
             patch("sys.argv", ["verify_means.py", "--symbol", "MES", "--compare-engine",
                                "--hourly-days", "45"]), contextlib.redirect_stdout(output):
            verifier.main()
        self.assertIn("PASS: all four", output.getvalue())
        self.assertEqual(client.calls[1]["start_time"], engine.prior_month_start_utc(NOW))

    def test_verifier_detects_engine_mismatch(self):
        client = FixtureClient()
        bad = {"CDM": -1, "PDM": -1, "CMM": -1, "PMM": -1}
        with patch.object(engine, "refresh_broker_means", return_value=bad), \
             self.assertRaises(AssertionError):
            verifier.compare_engine("MES", client.day, client.hour, NOW)

    def test_contracts_preserve_confirmed_z26_roll(self):
        for symbol in ("MNQ", "MES"):
            self.assertTrue(engine.CONTRACT_MAP[symbol][0].endswith(".Z26"))
            self.assertEqual(verifier.CONTRACT_MAP[symbol], engine.CONTRACT_MAP[symbol][0])

    def test_api_rejects_error_null_and_missing_bars(self):
        api = TopstepAPI("offline", "offline", "offline")
        for payload in ({"success": False, "bars": []}, {"bars": None}, {}, []):
            with self.subTest(payload=payload), patch.object(api, "_post", return_value=payload), \
                 self.assertRaises(TopstepAPIError):
                api.get_bars(CID, 2, 5, days=7, include_partial=False)

    def test_startup_uses_same_broker_refresh_and_warmup(self):
        client = FixtureClient()
        # ATR is not a mean input, and is the only legacy direct HTTP seed call.
        daily_response = SimpleNamespace(json=lambda: {"bars": []})
        with patch.object(engine, "datetime", Clock), \
             patch("requests.post", return_value=daily_response) as post:
            engine.seed_historical(client)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["unit"], 4)
        values = verifier.compare_engine("MES", client.day, client.hour, NOW)
        for name, value in values.items():
            self.assertEqual(value, getattr(engine.state, name.lower())["MES"])

    def test_broker_payload_excludes_partial_bars_and_uses_exact_bounds(self):
        api = TopstepAPI("offline", "offline", "offline")
        start = engine.prior_month_start_utc(NOW)
        with patch.object(api, "_post", return_value={"success": True, "bars": []}) as post:
            api.get_bars(CID, 3, 1, start_time=start, end_time=NOW, include_partial=False)
        payload = post.call_args.args[1]
        self.assertFalse(payload["includePartialBar"])
        self.assertEqual(payload["startTime"], "2026-07-31T22:00:00Z")
        self.assertEqual(payload["endTime"], "2026-09-21T16:02:00Z")


class OrderTests(BaseTests, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        engine.state.cdm["MES"] = 99
        self.client = FixtureClient()
        self.clock = patch.object(engine, "datetime", Clock)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def scan(self, broker):
        with patch("aiohttp.ClientSession", return_value=broker):
            await engine._scan_and_place("MES", 100, self.client, ACCOUNT)

    def mutations(self, broker):
        return [path for path, _ in broker.calls if path in ("/Order/place", "/Order/cancel")]

    async def test_cancelled_order_replaced_even_at_unchanged_price(self):
        engine.state.pending_entries[("MES", "CDM")] = pending()
        broker = Broker()
        await self.scan(broker)
        self.assertEqual(self.mutations(broker), ["/Order/place"])
        self.assertNotEqual(engine.state.pending_entries[("MES", "CDM")]["order_id"], 10)

    async def test_filled_order_clears_but_position_cap_blocks_reentry(self):
        engine.state.pending_entries[("MES", "CDM")] = pending()
        broker = Broker(positions=[{"contractId": CID, "size": 2}])
        await self.scan(broker)
        self.assertNotIn(("MES", "CDM"), engine.state.pending_entries)
        self.assertEqual(self.mutations(broker), [])
        self.assertEqual([path for path, _ in broker.calls],
                         ["/Order/searchOpen", "/Position/searchOpen"])

    async def test_partial_fill_still_working_is_not_pruned(self):
        engine.state.pending_entries[("MES", "CDM")] = pending()
        broker = Broker([order()], [{"contractId": CID, "size": 1}])
        await self.scan(broker)
        self.assertIn(("MES", "CDM"), engine.state.pending_entries)
        self.assertEqual(self.mutations(broker), [])

    async def test_sell_one_tick_reprice(self):
        engine.state.modes["MES"] = "SELL"
        engine.state.cdm["MES"] = 100.75
        entry = pending(price=101)
        entry["side"] = 1
        engine.state.pending_entries[("MES", "CDM")] = entry
        broker = Broker([order(price=101, side=1)])
        await self.scan(broker)
        self.assertEqual(self.mutations(broker), ["/Order/cancel", "/Order/place"])
        self.assertEqual(broker.orders[0]["limitPrice"], 100.75)
        self.assertEqual(broker.orders[0]["side"], 1)

    async def test_working_order_same_price_is_not_duplicated(self):
        engine.state.pending_entries[("MES", "CDM")] = pending()
        broker = Broker([order()])
        await self.scan(broker)
        self.assertEqual(self.mutations(broker), [])
        self.assertEqual(sum(path == "/Order/searchOpen" for path, _ in broker.calls), 1)

    async def test_one_tick_move_cancels_and_replaces(self):
        engine.state.pending_entries[("MES", "CDM")] = pending()
        engine.state.cdm["MES"] = 99.25
        broker = Broker([order()])
        await self.scan(broker)
        self.assertEqual(self.mutations(broker), ["/Order/cancel", "/Order/place"])
        self.assertEqual(broker.orders[0]["limitPrice"], 99.25)

    async def test_cancel_failure_never_places_replacement(self):
        engine.state.pending_entries[("MES", "CDM")] = pending()
        engine.state.cdm["MES"] = 99.25
        broker = Broker([order()])
        broker.cancel_success = False
        await self.scan(broker)
        self.assertEqual(self.mutations(broker), ["/Order/cancel"])
        self.assertEqual(engine.state.pending_entries[("MES", "CDM")]["order_id"], 10)

    async def test_placement_failure_after_cancel_leaves_no_phantom(self):
        engine.state.pending_entries[("MES", "CDM")] = pending()
        engine.state.cdm["MES"] = 99.25
        broker = Broker([order()])
        broker.place_success = False
        await self.scan(broker)
        self.assertNotIn(("MES", "CDM"), engine.state.pending_entries)

    async def test_failed_queries_never_clear_or_place(self):
        for path, body, status in [
            ("/Order/searchOpen", {"success": False, "orders": []}, 200),
            ("/Order/searchOpen", {"orders": None}, 200),
            ("/Position/searchOpen", {"success": False, "positions": []}, 200),
            ("/Position/searchOpen", {"positions": []}, 500),
        ]:
            with self.subTest(path=path, body=body, status=status):
                engine.state.pending_entries[("MES", "CDM")] = pending()
                broker = Broker()
                broker.query_overrides[path] = Response(body, status)
                await self.scan(broker)
                self.assertIn(("MES", "CDM"), engine.state.pending_entries)
                self.assertEqual(self.mutations(broker), [])

    async def test_restart_adopts_once_and_reprices(self):
        engine.state.cdm["MES"] = 99.25
        broker = Broker([order()])
        await self.scan(broker)
        self.assertEqual(self.mutations(broker), ["/Order/cancel", "/Order/place"])
        self.assertEqual(len(broker.orders), 1)

    async def test_other_symbol_pending_is_untouched(self):
        engine.state.pending_entries[("MNQ", "CDM")] = pending(55)
        await self.scan(Broker())
        self.assertIn(("MNQ", "CDM"), engine.state.pending_entries)

    async def test_cap_counts_positions_working_sizes_and_new_placements(self):
        engine.state.pdm["MES"] = 98
        engine.state.cmm["MES"] = 97
        broker = Broker(positions=[{"contractId": CID, "size": 1}])
        await self.scan(broker)
        self.assertEqual(len(broker.orders), 1)
        # One slot left, so it goes to the STRONGEST eligible level (CMM 97),
        # not the nearest one (CDM 99). See LEVEL_STRENGTH.
        self.assertEqual(broker.orders[0]["limitPrice"], 97)
        # Opposite-side, multi-contract working order consumes cap too.
        engine.state.pending_entries.clear()
        broker = Broker([order(side=1, size=2)])
        await self.scan(broker)
        self.assertEqual(self.mutations(broker), [])

    def test_eligible_levels_ordered_by_documented_strength(self):
        engine.state.pdm["MES"] = 98
        engine.state.cmm["MES"] = 97
        engine.state.pmm["MES"] = 96
        levels = engine.get_all_eligible_levels("MES", "BUY", 100, 0.25)
        self.assertEqual([name for name, _ in levels], ["CMM", "PMM", "CDM", "PDM"])
        sells = engine.get_all_eligible_levels("MES", "SELL", 90, 0.25)
        self.assertEqual([name for name, _ in sells], ["CMM", "PMM", "CDM", "PDM"])

    async def test_contract_cap_is_spent_on_strongest_levels(self):
        # All four means eligible below price; the 2-contract per-symbol cap must
        # buy the two STRONGEST (CMM, PMM), never the two nearest (CDM, PDM).
        engine.state.pdm["MES"] = 98
        engine.state.cmm["MES"] = 97
        engine.state.pmm["MES"] = 96
        broker = Broker()
        await self.scan(broker)
        self.assertEqual(sorted(o["limitPrice"] for o in broker.orders), [96, 97])
        self.assertEqual(
            sorted(key[1] for key in engine.state.pending_entries), ["CMM", "PMM"]
        )

    async def test_total_cap_counts_other_symbols(self):
        # Exposure on OTHER contracts must consume the account-wide cap, or five
        # symbols each spend their own allowance and breach the combine together.
        engine.state.cmm["MES"] = 97
        other = "CON.F.US.MCL.X26"
        broker = Broker(
            [order(contract=other, size=2)],
            [{"contractId": "CON.F.US.MYM.Z26", "size": 3}],
        )
        await self.scan(broker)
        self.assertEqual(self.mutations(broker), [])

    async def test_total_cap_allows_placement_with_room_left(self):
        engine.state.cmm["MES"] = 97
        broker = Broker(positions=[{"contractId": "CON.F.US.MYM.Z26", "size": 2}])
        await self.scan(broker)
        # 2 lots elsewhere leaves 3 of 5; per-symbol cap of 2 is the tighter one.
        self.assertEqual(len(broker.orders), 2)

    async def test_total_cap_is_enforced_per_placement_not_once_per_cycle(self):
        # 4 lots held elsewhere leaves room for exactly ONE more. Two levels are
        # eligible and the per-symbol cap would allow both, so a single cycle
        # must still stop at one or it breaches the combine.
        engine.state.cmm["MES"] = 97
        broker = Broker(positions=[{"contractId": "CON.F.US.MYM.Z26", "size": 4}])
        await self.scan(broker)
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(broker.orders[0]["limitPrice"], 97)  # strongest first

    async def test_at_total_cap_existing_orders_still_reprice(self):
        # Being at the account cap must not freeze working orders: a reprice is
        # cancel+replace and adds no exposure.
        engine.state.pending_entries[("MES", "CDM")] = pending()
        engine.state.cdm["MES"] = 99.25
        broker = Broker([order()], [{"contractId": "CON.F.US.MYM.Z26", "size": 4}])
        await self.scan(broker)
        self.assertEqual(self.mutations(broker), ["/Order/cancel", "/Order/place"])
        self.assertEqual(len(broker.orders), 1)

    def test_total_cap_matches_combine_and_bounds_a_single_symbol(self):
        # Per-symbol x symbol count intentionally EXCEEDS the account cap, so the
        # account cap must be the binding constraint and cannot be removed.
        self.assertEqual(engine.MAX_TOTAL_CONTRACTS, 5)
        self.assertGreaterEqual(engine.MAX_TOTAL_CONTRACTS, engine.MAX_CONTRACTS_PER_INSTRUMENT)
        self.assertGreater(
            engine.MAX_CONTRACTS_PER_INSTRUMENT * len(engine.SYMBOLS),
            engine.MAX_TOTAL_CONTRACTS,
        )

    async def test_reprice_at_cap_does_not_add_exposure(self):
        engine.state.pending_entries[("MES", "CDM")] = pending()
        engine.state.cdm["MES"] = 99.25
        broker = Broker([order()], [{"contractId": CID, "size": 1}])
        await self.scan(broker)
        self.assertEqual(self.mutations(broker), ["/Order/cancel", "/Order/place"])
        self.assertEqual(len(broker.orders), 1)

    async def test_ineligible_level_cancels_without_readopting_stale_snapshot(self):
        engine.state.pending_entries[("MES", "CDM")] = pending()
        engine.state.cdm["MES"] = None
        engine.state.pdm["MES"] = 98.75
        broker = Broker([order()])
        await self.scan(broker)
        self.assertNotIn(("MES", "CDM"), engine.state.pending_entries)
        self.assertEqual(self.mutations(broker), ["/Order/cancel", "/Order/place"])

    async def test_dry_run_performs_no_order_mutations(self):
        engine.state.dry_run = True
        engine.state.pending_entries[("MES", "CDM")] = pending("DRY", 98)
        broker = Broker()
        await self.scan(broker)
        self.assertEqual(self.mutations(broker), [])
        self.assertEqual(engine.state.pending_entries[("MES", "CDM")]["entry_price"], 99)

    async def test_local_tick_bar_cannot_change_means_or_place(self):
        before = copy.deepcopy(engine.state.day_closes)
        with patch.object(engine, "_scan_and_place", new_callable=AsyncMock) as scan:
            await engine.on_new_bar("MES", {"close": 999999, "timestamp": NOW}, self.client, ACCOUNT)
        self.assertEqual(engine.state.cdm["MES"], 99)
        self.assertEqual(engine.state.day_closes, before)
        scan.assert_not_awaited()

    async def test_60s_cycle_refreshes_before_scan_without_tick_data(self):
        observations = []

        async def scan(*args, **kwargs):
            observations.append(engine.state.cdm["MES"])

        with patch.object(engine, "_scan_and_place", side_effect=scan):
            await engine.refresh_and_reprice("MES", self.client, ACCOUNT, NOW)
            for bar in self.client.day:
                bar["c"] += 5
            await engine.refresh_and_reprice("MES", self.client, ACCOUNT, NOW)
        self.assertEqual(len(self.client.calls), 4)
        self.assertAlmostEqual(observations[1] - observations[0], 5)

    async def test_history_failure_skips_placement(self):
        self.client.hour = None
        with patch.object(engine, "_scan_and_place", new_callable=AsyncMock) as scan:
            with self.assertRaises(ValueError):
                await engine.refresh_and_reprice("MES", self.client, ACCOUNT, NOW)
            scan.assert_not_awaited()

    async def test_concurrent_refreshes_serialize_through_placement(self):
        running, maximum = 0, 0

        async def scan(*args, **kwargs):
            nonlocal running, maximum
            running += 1
            maximum = max(maximum, running)
            await asyncio.sleep(0.01)
            running -= 1

        with patch.object(engine, "_scan_and_place", side_effect=scan):
            await asyncio.gather(*[
                engine.refresh_and_reprice("MES", self.client, ACCOUNT, NOW)
                for _ in range(3)])
        self.assertEqual(maximum, 1)



class ContractResolutionTests(unittest.TestCase):
    """The engine must never route an order to an expired or foreign contract."""

    NOW = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.original = dict(engine.CONTRACT_MAP)
        self.addCleanup(lambda: engine.CONTRACT_MAP.update(self.original))

    @staticmethod
    def client(contracts, raises=False):
        class C:
            def search_contract(self, symbol):
                if raises:
                    raise RuntimeError("boom")
                return contracts
        return C()

    @staticmethod
    def contract(cid, expiry, active=False):
        return {"id": cid, "activeContract": active, "lastTradingDate": expiry}

    def test_expired_month_is_never_selected(self):
        client = self.client([
            self.contract("CON.F.US.MCL.V26", "2026-09-21T18:30:00Z", active=True),
            self.contract("CON.F.US.MCL.X26", "2026-10-19T18:30:00Z"),
        ])
        resolved, failures = engine.resolve_active_contracts(client, ["MCL"], self.NOW)
        self.assertEqual(failures, {})
        self.assertEqual(resolved["MCL"][0], "CON.F.US.MCL.X26")

    def test_broker_active_flag_wins_over_nearer_expiry(self):
        client = self.client([
            self.contract("CON.F.US.MGC.V26", "2026-10-28T18:30:00Z"),
            self.contract("CON.F.US.MGC.Z26", "2026-12-29T18:30:00Z", active=True),
        ])
        resolved, _ = engine.resolve_active_contracts(client, ["MGC"], self.NOW)
        self.assertEqual(resolved["MGC"][0], "CON.F.US.MGC.Z26")
        self.assertTrue(resolved["MGC"][2])

    def test_nearest_expiry_when_nothing_flagged(self):
        client = self.client([
            self.contract("CON.F.US.MYM.H27", "2027-03-19T14:30:00Z"),
            self.contract("CON.F.US.MYM.Z26", "2026-12-18T14:30:00Z"),
        ])
        resolved, _ = engine.resolve_active_contracts(client, ["MYM"], self.NOW)
        self.assertEqual(resolved["MYM"][0], "CON.F.US.MYM.Z26")

    def test_foreign_roots_and_spreads_are_ignored(self):
        client = self.client([
            self.contract("CON.F.US.MNQ.Z26", "2026-12-18T14:30:00Z", active=True),
            self.contract("CON.F.US.MYM.Z26", "2026-12-18T14:30:00Z", active=True),
            self.contract("CON.S.US.MYM.Z26.H27", "2027-03-19T14:30:00Z", active=True),
            self.contract("CON.F.US.MYM.BADMONTH", "2027-03-19T14:30:00Z", active=True),
        ])
        resolved, failures = engine.resolve_active_contracts(client, ["MYM"], self.NOW)
        self.assertEqual(failures, {})
        self.assertEqual(resolved["MYM"][0], "CON.F.US.MYM.Z26")

    def test_all_expired_is_a_failure_not_a_guess(self):
        client = self.client([
            self.contract("CON.F.US.MYM.U26", "2026-09-18T14:30:00Z", active=True),
        ])
        resolved, failures = engine.resolve_active_contracts(client, ["MYM"], self.NOW)
        self.assertEqual(resolved, {})
        self.assertIn("MYM", failures)
        self.assertIn("expired", failures["MYM"])

    def test_search_failure_is_reported_not_swallowed(self):
        resolved, failures = engine.resolve_active_contracts(
            self.client([], raises=True), ["MCL"], self.NOW)
        self.assertEqual(resolved, {})
        self.assertIn("contract search failed", failures["MCL"])

    def test_empty_result_is_a_failure(self):
        resolved, failures = engine.resolve_active_contracts(
            self.client([]), ["MCL"], self.NOW)
        self.assertEqual(resolved, {})
        self.assertIn("nothing", failures["MCL"])

    def test_missing_expiry_does_not_beat_a_dated_contract(self):
        client = self.client([
            self.contract("CON.F.US.MCL.Z26", None),
            self.contract("CON.F.US.MCL.X26", "2026-10-19T18:30:00Z"),
        ])
        resolved, _ = engine.resolve_active_contracts(client, ["MCL"], self.NOW)
        self.assertEqual(resolved["MCL"][0], "CON.F.US.MCL.X26")

    def test_apply_preserves_ticks_and_demotes_old_month(self):
        engine.CONTRACT_MAP["MCL"] = ("CON.F.US.MCL.V26", "CON.F.US.MCL.U26", 0.01, 1.00)
        changes = engine.apply_resolved_contracts(
            {"MCL": ("CON.F.US.MCL.X26", self.NOW, True)})
        self.assertEqual(engine.CONTRACT_MAP["MCL"],
                         ("CON.F.US.MCL.X26", "CON.F.US.MCL.V26", 0.01, 1.00))
        self.assertEqual(changes["MCL"], ("CON.F.US.MCL.V26", "CON.F.US.MCL.X26"))

    def test_apply_is_a_noop_when_month_already_correct(self):
        before = engine.CONTRACT_MAP["MNQ"]
        changes = engine.apply_resolved_contracts(
            {"MNQ": (before[0], self.NOW, True)})
        self.assertEqual(changes, {})
        self.assertEqual(engine.CONTRACT_MAP["MNQ"], before)

    def test_shipped_fallback_map_has_sane_tick_data(self):
        expected = {"MNQ": (0.25, 0.50), "MES": (0.25, 1.25), "MYM": (1.0, 0.50),
                    "MGC": (0.10, 1.00), "MCL": (0.01, 1.00)}
        for sym, (tick, value) in expected.items():
            active, prior, got_tick, got_value = engine.CONTRACT_MAP[sym]
            self.assertEqual((got_tick, got_value), (tick, value), sym)
            self.assertTrue(active.startswith(f"CON.F.US.{sym}."), sym)
            self.assertNotEqual(active, prior, sym)

if __name__ == "__main__":
    unittest.main()
