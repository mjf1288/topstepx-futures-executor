"""End-to-end execution proof for realtime_engine.py, with no broker account.

This imports the REAL engine and drives its real 60-second cycle
(`refresh_and_reprice`) against a simulated ProjectX broker fed with recorded
MNQ bars. Nothing in the mean maths, eligibility, placement, dedup or pruning
logic is reimplemented here - only the network edge is replaced.

What it demonstrates, against the spec:
  1. all four means recomputed from COMPLETED BROKER BARS every cycle
  2. a limit order resting at each eligible mean, on the correct side of price
  3. orders repriced when a mean moves
  4. `pending_entries` cleared when an order fills or is cancelled, so the
     level can be re-armed instead of being blocked by the 2-tick dedup

Run:
    python sim_execution_harness.py

Live trading is never touched: there is no session token, no real base_url and
no real account. Every order goes into an in-memory book.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# Recorded MNQ bars (Tarric's parquet set). Override with MNQ_BAR_DIR.
DATA_DIR = Path(os.environ.get("MNQ_BAR_DIR", "/home/user/workspace/mean-levels-backtest"))
SYMBOL = "MNQ"


# ─────────────────────────────────────────────────────────────
# Simulated broker book
# ─────────────────────────────────────────────────────────────
class SimBroker:
    """In-memory stand-in for the ProjectX order/position endpoints."""

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, int] = {}
        self.next_id = 1000
        self.log: list[str] = []

    def place(self, payload: dict) -> str:
        self.next_id += 1
        order_id = f"SIM{self.next_id}"
        self.orders[order_id] = {
            "id": order_id,
            "contractId": payload["contractId"],
            "type": payload["type"],
            "side": payload["side"],
            "size": payload["size"],
            "limitPrice": float(payload["limitPrice"]),
            "status": "open",
        }
        side = "BUY" if payload["side"] == 0 else "SELL"
        self.log.append(f"PLACE {order_id} {side} {payload['limitPrice']}")
        return order_id

    def cancel(self, order_id: str) -> bool:
        order = self.orders.get(order_id)
        if order is None or order["status"] != "open":
            return False
        order["status"] = "cancelled"
        self.log.append(f"CANCEL {order_id}")
        return True

    def open_orders(self) -> list[dict]:
        return [o for o in self.orders.values() if o["status"] == "open"]

    def open_positions(self) -> list[dict]:
        return [
            {"contractId": cid, "size": size}
            for cid, size in self.positions.items()
            if size
        ]

    def fill(self, order_id: str) -> None:
        """Force a fill, as a touched resting limit would."""
        order = self.orders[order_id]
        order["status"] = "filled"
        signed = order["size"] if order["side"] == 0 else -order["size"]
        self.positions[order["contractId"]] = (
            self.positions.get(order["contractId"], 0) + signed
        )
        self.log.append(f"FILL {order_id} @ {order['limitPrice']}")


# ─────────────────────────────────────────────────────────────
# Fake aiohttp: routes the engine's POSTs into SimBroker
# ─────────────────────────────────────────────────────────────
class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> dict:
        return self._payload

    # The engine uses BOTH `await http.post(...)` and `async with http.post(...)`.
    def __await__(self):
        async def _self():
            return self
        return _self().__await__()

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _Session:
    def __init__(self, broker: SimBroker) -> None:
        self.broker = broker

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def post(self, url: str, json: dict | None = None, headers=None) -> _Response:
        body = json or {}
        if url.endswith("/Order/place"):
            return _Response({"success": True, "orderId": self.broker.place(body)})
        if url.endswith("/Order/cancel"):
            ok = self.broker.cancel(body["orderId"])
            return _Response({"success": ok})
        if url.endswith("/Order/searchOpen"):
            return _Response({"success": True, "orders": self.broker.open_orders()})
        if url.endswith("/Position/searchOpen"):
            return _Response({"success": True, "positions": self.broker.open_positions()})
        raise AssertionError(f"unexpected broker call: {url}")


def install_fake_aiohttp(broker: SimBroker) -> None:
    module = types.ModuleType("aiohttp")
    module.ClientSession = lambda *a, **k: _Session(broker)
    sys.modules["aiohttp"] = module


# ─────────────────────────────────────────────────────────────
# Bar feed from recorded data
# ─────────────────────────────────────────────────────────────
class BarFeed:
    """Serves recorded 5-minute and hourly bars in ProjectX bar shape."""

    def __init__(self) -> None:
        self.five = self._load("mnq-continuous-5m.parquet")
        self.hour = self._load("mnq-continuous-1h.parquet")

    @staticmethod
    def _load(name: str) -> pd.DataFrame:
        path = DATA_DIR / name
        if not path.exists():
            raise SystemExit(
                f"missing {path}\nSet MNQ_BAR_DIR to the folder holding "
                "mnq-continuous-5m.parquet and mnq-continuous-1h.parquet"
            )
        frame = pd.read_parquet(path).sort_values("ts_event")
        frame["ts"] = pd.to_datetime(frame.ts_event, utc=True)
        return frame.reset_index(drop=True)

    def bars(self, unit: int, unit_number: int, start, end) -> list[dict]:
        if unit == 2 and unit_number == 5:
            frame, minutes = self.five, 5
        elif unit == 3 and unit_number == 1:
            frame, minutes = self.hour, 60
        else:
            raise AssertionError(f"unexpected bar request unit={unit}/{unit_number}")
        window = frame[(frame.ts >= start) & (frame.ts <= end)]
        return [
            {
                "t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "o": float(o), "h": float(h), "l": float(low), "c": float(c),
                "v": int(v),
            }
            for ts, o, h, low, c, v in zip(
                window.ts, window.open, window.high, window.low,
                window.close, window.volume,
            )
        ]


class SimClient:
    """Stands in for TopstepAPI: bars plus the auth surface the engine reads."""

    base_url = "https://sim.invalid/api"

    def __init__(self, feed: BarFeed) -> None:
        self.feed = feed
        self.bar_calls = 0

    def get_session_token(self) -> str:
        return "SIM-NO-REAL-TOKEN"

    def get_bars(self, contract_id, unit, unit_number, start_time, end_time,
                 limit=5000, include_partial=False):
        self.bar_calls += 1
        assert include_partial is False, "engine must never request partial bars"
        return self.feed.bars(unit, unit_number, start_time, end_time)


class SimAccount:
    id = 99999999
    name = "SIM-NO-REAL-ACCOUNT"


# ─────────────────────────────────────────────────────────────
# Drive the engine
# ─────────────────────────────────────────────────────────────
async def main() -> int:
    broker = SimBroker()
    install_fake_aiohttp(broker)

    import realtime_engine as engine

    feed = BarFeed()
    client = SimClient(feed)
    account = SimAccount()

    engine.state.dry_run = False  # exercise the real placement path
    engine.state.modes = {SYMBOL: "BUY"}

    contract_id, _, tick_size = engine.CONTRACT_MAP[SYMBOL][:3]
    last_bar = feed.five.ts.iloc[-1].to_pydatetime()
    print(f"engine     {engine.__file__}")
    print(f"contract   {contract_id}   tick {tick_size}")
    print(f"bar feed   5m to {last_bar:%Y-%m-%d %H:%M} UTC, "
          f"{len(feed.five):,} bars | 1h {len(feed.hour):,} bars")
    print(f"broker     simulated in-memory book (account {account.name})\n")

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(f"  {'PASS' if condition else 'FAIL'}  {label}"
              + (f"   {detail}" if detail else ""))
        if not condition:
            failures.append(label)

    # ── Cycle 1: means from broker bars, orders placed at eligible levels ──
    now = last_bar - timedelta(minutes=5)
    price_row = feed.five[feed.five.ts <= now].iloc[-1]
    engine.state.current_price[SYMBOL] = float(price_row.close)

    print("── cycle 1: recompute means, arm eligible levels ──")
    await engine.refresh_and_reprice(SYMBOL, client, account, now_utc=now)
    means = {
        name: getattr(engine.state, name.lower()).get(SYMBOL)
        for name in ("CDM", "PDM", "CMM", "PMM")
    }
    price = engine.state.current_price[SYMBOL]
    print(f"  price {price:,.2f}   " + "  ".join(
        f"{k} {v:,.2f}" if v is not None else f"{k} None" for k, v in means.items()))
    check("all four means computed from broker bars",
          all(v is not None for v in means.values()),
          f"{sum(v is not None for v in means.values())}/4")
    check("broker bar requests issued", client.bar_calls >= 2,
          f"{client.bar_calls} calls")

    eligible = engine.get_all_eligible_levels(SYMBOL, "BUY", price, tick_size)
    resting = broker.open_orders()
    cap = engine.MAX_CONTRACTS_PER_INSTRUMENT
    per_order = engine.CONTRACTS_PER_ORDER
    expected_orders = min(len(eligible), cap // per_order)
    print(f"  eligible BUY levels {[(n, p) for n, p in eligible]}")
    print(f"  resting orders {[(o['id'], o['limitPrice']) for o in resting]}")
    print(f"  cap {cap} contracts / {per_order} per order -> {expected_orders} orders")
    check("orders rest at eligible levels up to the contract cap",
          len(resting) == expected_orders and len(resting) > 0,
          f"{len(resting)} orders / {len(eligible)} eligible / cap {expected_orders}")

    # The cap must be spent on the STRONGEST levels, per LEVEL_STRENGTH.
    armed = {key[1] for key in engine.state.pending_entries if key[0] == SYMBOL}
    strongest = {name for name, _ in eligible[:expected_orders]}
    print(f"  armed {sorted(armed)}   strongest eligible {sorted(strongest)}")
    check("cap is spent on the strongest eligible levels", armed == strongest,
          f"armed {sorted(armed)} vs expected {sorted(strongest)}")
    check("every BUY limit is below price",
          all(o["limitPrice"] < price for o in resting))
    check("pending_entries tracks each resting order",
          len(engine.state.pending_entries) == len(resting),
          f"{len(engine.state.pending_entries)} tracked")

    # ── Cycle 2: no change, so no churn ──
    print("\n── cycle 2: means unchanged, expect no new orders ──")
    before = set(o["id"] for o in broker.open_orders())
    await engine.refresh_and_reprice(SYMBOL, client, account, now_utc=now)
    after = set(o["id"] for o in broker.open_orders())
    check("identical means do not churn orders", before == after,
          f"{len(before)} -> {len(after)}")

    # ── Cycle 3: a mean moves, so the order is repriced ──
    print("\n── cycle 3: move a mean, expect a reprice ──")
    tracked = [k for k in engine.state.pending_entries if k[0] == SYMBOL]
    key = tracked[0]
    old = engine.state.pending_entries[key]
    old_id, old_price = old["order_id"], old["entry_price"]
    shifted = old_price - 25 * float(tick_size)
    # Move the level itself, exactly as a new broker bar would.
    getattr(engine.state, key[1].lower())[SYMBOL] = shifted
    await engine._scan_and_place(SYMBOL, price, client, account, source="test")
    new = engine.state.pending_entries.get(key)
    print(f"  moved {key[1]} {old_price} -> {shifted}")
    print(f"  order {old_id} -> {new['order_id'] if new else None}")
    check("moved mean produced a new order", new is not None and new["order_id"] != old_id)
    check("repriced order sits at the new level",
          new is not None and abs(new["entry_price"] - shifted) < float(tick_size))
    check("stale order was cancelled at the broker",
          broker.orders[old_id]["status"] == "cancelled",
          broker.orders[old_id]["status"])

    # ── Cycle 4: a fill clears pending_entries so the level can re-arm ──
    print("\n── cycle 4: fill an entry, expect pending_entries to clear ──")
    fill_key = new is not None and key or tracked[0]
    fill_id = engine.state.pending_entries[fill_key]["order_id"]
    broker.fill(fill_id)
    print(f"  filled {fill_id}; broker position "
          f"{broker.positions.get(contract_id, 0)}")
    await engine._scan_and_place(SYMBOL, price, client, account, source="test")
    still_tracked = fill_key in engine.state.pending_entries
    tracked_id = (engine.state.pending_entries.get(fill_key) or {}).get("order_id")
    check("filled order pruned from pending_entries",
          not still_tracked or tracked_id != fill_id,
          f"tracked={tracked_id}")
    check("dedup no longer blocks the level",
          not still_tracked or tracked_id != fill_id)

    # ── Cycle 5: a cancellation also clears it ──
    print("\n── cycle 5: cancel an entry, expect pending_entries to clear ──")
    remaining = [k for k in engine.state.pending_entries if k[0] == SYMBOL]
    if remaining:
        ckey = remaining[0]
        cid_order = engine.state.pending_entries[ckey]["order_id"]
        broker.cancel(cid_order)
        await engine._scan_and_place(SYMBOL, price, client, account, source="test")
        gone = engine.state.pending_entries.get(ckey, {}).get("order_id") != cid_order
        check("cancelled order pruned from pending_entries", gone,
              f"{ckey[1]} {cid_order}")
    else:
        check("cancelled order pruned from pending_entries", False, "nothing tracked")

    # ── Sixty-second cadence over a run of consecutive minutes ──
    print("\n── cadence: ten consecutive 60s cycles ──")
    calls_before = client.bar_calls
    for step in range(10):
        moment = now + timedelta(minutes=step)
        row = feed.five[feed.five.ts <= moment].iloc[-1]
        await engine.on_new_bar(
            SYMBOL, {"close": float(row.close)}, client, account
        )
        await engine.refresh_and_reprice(SYMBOL, client, account, now_utc=moment)
    per_cycle = (client.bar_calls - calls_before) / 10
    print(f"  broker bar requests per cycle: {per_cycle:.1f}")
    check("every cycle refetches broker bars", per_cycle >= 2.0,
          f"{per_cycle:.1f} per cycle")
    check("orders still reconciled after ten cycles",
          len(engine.state.pending_entries) == len(broker.open_orders()),
          f"{len(engine.state.pending_entries)} tracked / "
          f"{len(broker.open_orders())} open")

    print(f"\nbroker event log ({len(broker.log)} events):")
    for line in broker.log[-12:]:
        print(f"  {line}")

    print()
    if failures:
        print(f"FAILED {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED — engine executes the spec against a simulated broker")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
