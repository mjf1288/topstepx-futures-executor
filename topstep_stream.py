"""
topstep_stream.py — Direct SignalR real-time client for ProjectX Market Hub.

Replaces the abandoned `project-x-py` real-time data manager. Connects to
wss://rtc.topstepx.com/hubs/market, subscribes to contract-level trade
events, and aggregates ticks into synthetic 5-min bars locally.

WHY WE AGGREGATE OURSELVES:
The ProjectX market hub emits ONLY tick-level events (GatewayQuote,
GatewayTrade, GatewayDepth). There is NO native 5-min bar-close event.
We build 5-min bars from GatewayTrade events, which gives us:
  - Full control over bar boundaries (aligned to 5-min UTC floor)
  - No dependency on library-specific bar semantics
  - Explicit backfill on reconnect

BAR SEMANTICS:
  - Bars keyed on 5-min UTC floor (e.g. 14:00, 14:05, 14:10, ...)
  - A bar is "closed" (callback fired) when the FIRST trade of the NEXT
    bucket arrives, OR when the wall-clock closes it via periodic tick
  - This means a bar can be delayed by a few seconds after wall-clock
    close — but the timestamp is exact (aligned floor)
  - No lookahead: bar close only fires after the boundary has passed

RECONNECT + BACKFILL:
  - signalrcore handles auto-reconnect with exponential backoff
  - On disconnect, we mark the connection as "gap" and stop firing bar
    callbacks until reconnect completes
  - On reconnect, we re-subscribe (SignalR does not persist subscriptions)
  - The engine is responsible for backfill: on any disconnect >30s, the
    engine's main loop can call TopstepAPI.get_bars() to fetch missed
    bars before resuming
  - We also expose a `last_bar_time` property so the engine can compare
    against expected wall-clock and detect silent stalls

USAGE:
    stream = TopstepStream(jwt_token=api.get_jwt())
    stream.on_bar_close(handle_bar)  # handle_bar(contract_id, bar_dict)
    stream.subscribe("CON.F.US.MES.U26")
    stream.subscribe("CON.F.US.MNQ.U26")
    stream.start()
    # ... blocks/runs in background thread ...
    stream.stop()

The bar callback receives:
    contract_id: "CON.F.US.MES.U26"
    bar: {"t": "2026-07-22T18:35:00Z", "o": 7500.5, "h": 7502.0,
          "l": 7500.25, "c": 7501.75, "v": 47, "n_ticks": 33}
"""

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Callable

from signalrcore.hub_connection_builder import HubConnectionBuilder


MARKET_HUB_URL = "https://rtc.topstepx.com/hubs/market"
USER_HUB_URL = "https://rtc.topstepx.com/hubs/user"

# 5-minute bars. If we ever want to change granularity, this is the ONE
# place to change it. Everything else is derived.
BAR_SECONDS = 5 * 60

# How often we sweep to close stale bars (bars whose bucket has passed but
# no new-bucket trade has arrived yet). 1s = 1s max delay past close.
SWEEP_INTERVAL_S = 1.0

log = logging.getLogger("topstep_stream")


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def bucket_start_utc(dt: datetime) -> datetime:
    """Return the 5-min UTC-floor bucket start for `dt`."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    total_seconds = int(dt.timestamp())
    floored = total_seconds - (total_seconds % BAR_SECONDS)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def parse_gateway_timestamp(ts: str) -> datetime:
    """Parse a ProjectX ISO 8601 timestamp string to UTC-aware datetime."""
    # Handle "2024-07-21T13:45:00Z" and "2024-07-21T13:45:00.123+00:00" both
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


# ──────��──────────────────────────────────────────────────────
# Tick → 5-min bar aggregator
# ─────────────────────────────────────────────────────────────
class BarAggregator:
    """Aggregates tick trades into 5-min bars for a SINGLE contract.

    Not thread-safe on its own — TopstepStream serializes callbacks
    through a lock before calling into here.
    """

    def __init__(self, contract_id: str):
        self.contract_id = contract_id
        self.current_bucket_start: datetime | None = None
        self.o: float | None = None
        self.h: float | None = None
        self.l: float | None = None
        self.c: float | None = None
        self.v: int = 0
        self.n_ticks: int = 0
        self.last_tick_time: datetime | None = None

    def add_tick(self, price: float, volume: int, ts: datetime) -> dict | None:
        """Add a tick. If it crosses into a new bucket, returns the CLOSED
        prior bar dict. Otherwise returns None.
        """
        bucket = bucket_start_utc(ts)
        closed_bar: dict | None = None

        if self.current_bucket_start is None:
            # First-ever tick for this contract
            self.current_bucket_start = bucket
            self.o = price
            self.h = price
            self.l = price
            self.c = price
            self.v = volume
            self.n_ticks = 1
        elif bucket > self.current_bucket_start:
            # Crossed a bucket boundary — CLOSE the prior bar
            closed_bar = self._snapshot()
            # Start new bucket with this tick
            self.current_bucket_start = bucket
            self.o = price
            self.h = price
            self.l = price
            self.c = price
            self.v = volume
            self.n_ticks = 1
        else:
            # Same bucket — update running OHLCV
            self.h = max(self.h, price)
            self.l = min(self.l, price)
            self.c = price
            self.v += volume
            self.n_ticks += 1

        self.last_tick_time = ts
        return closed_bar

    def force_close_if_stale(self, now_utc: datetime) -> dict | None:
        """Called by the sweep loop. If wall-clock has moved past the
        current bucket's end and we still have a bucket open, close it.
        Returns the closed bar or None.
        """
        if self.current_bucket_start is None:
            return None
        bucket_end = self.current_bucket_start + timedelta(seconds=BAR_SECONDS)
        if now_utc >= bucket_end:
            closed = self._snapshot()
            # Reset — next tick will start a fresh bucket
            self.current_bucket_start = None
            self.o = self.h = self.l = self.c = None
            self.v = 0
            self.n_ticks = 0
            return closed
        return None

    def _snapshot(self) -> dict:
        """Snapshot the current bucket as a bar dict."""
        return {
            "t": self.current_bucket_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "o": self.o,
            "h": self.h,
            "l": self.l,
            "c": self.c,
            "v": self.v,
            "n_ticks": self.n_ticks,
        }


# ─────────────────────────────────────────────────────────────
# TopstepStream — market hub client
# ─────────────────────────────────────────────────────────────
class TopstepStream:
    """SignalR client for ProjectX Market Hub with local 5-min bar aggregation.

    Threading: signalrcore fires callbacks on its own thread. We serialize
    all state mutations through self._lock. Bar callbacks are invoked
    synchronously — the engine's callback should be fast (queue work, don't
    do heavy computation in the callback).
    """

    def __init__(self, jwt_token: str):
        if not jwt_token:
            raise ValueError("jwt_token required")

        self._token = jwt_token
        self._connection = None
        self._connected = False
        self._subscribed_contracts: set[str] = set()
        self._aggregators: dict[str, BarAggregator] = {}
        self._bar_callback: Callable[[str, dict], None] | None = None
        # Additional callbacks invoked on every raw trade (for tick-based
        # aggregators like tick-count / range / dollar bars). Signature:
        #   callback(contract_id: str, price: float, volume: int, ts: datetime)
        self._tick_callbacks: list[Callable[[str, float, int, datetime], None]] = []
        self._lock = threading.RLock()
        self._sweep_thread: threading.Thread | None = None
        self._sweep_stop = threading.Event()

        # Metrics for the engine's watchdog
        self._last_event_time: datetime | None = None
        self._last_bar_time: dict[str, datetime] = {}
        self._connection_gap_start: datetime | None = None
        self._reconnect_count = 0

    # ─────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────
    def on_bar_close(self, callback: Callable[[str, dict], None]) -> None:
        """Register a callback invoked when a 5-min bar closes.

        Signature: callback(contract_id: str, bar: dict) -> None

        Bar dict keys: t (ISO string), o, h, l, c, v, n_ticks
        """
        self._bar_callback = callback

    def on_trade_tick(
        self,
        callback: Callable[[str, float, int, datetime], None],
    ) -> None:
        """Register a callback invoked on EVERY trade tick.

        Signature: callback(contract_id: str, price: float, volume: int, ts: datetime)

        Multiple callbacks may be registered. Used by tick-count / range /
        dollar-bar aggregators (e.g. anchored-VWAP on 2000-tick bars).
        The existing 5-min bar aggregation continues to run in parallel.
        """
        self._tick_callbacks.append(callback)

    def subscribe(self, contract_id: str) -> None:
        """Subscribe to trades for a contract. Idempotent.

        If already connected, subscribes immediately. If not yet started,
        the contract is queued and subscribed on start().
        """
        with self._lock:
            self._subscribed_contracts.add(contract_id)
            if contract_id not in self._aggregators:
                self._aggregators[contract_id] = BarAggregator(contract_id)
            if self._connected and self._connection is not None:
                self._invoke_subscribe(contract_id)

    def unsubscribe(self, contract_id: str) -> None:
        with self._lock:
            self._subscribed_contracts.discard(contract_id)
            if self._connected and self._connection is not None:
                try:
                    self._connection.send("UnsubscribeContractTrades", [contract_id])
                except Exception as e:
                    log.warning(f"UnsubscribeContractTrades({contract_id}) failed: {e!r}")

    def start(self) -> None:
        """Start the WebSocket connection and begin streaming.

        Blocks until connected (up to 30 seconds). Raises on failure.
        Runs the sweep thread in the background.
        """
        url = f"{MARKET_HUB_URL}?access_token={self._token}"
        self._connection = (
            HubConnectionBuilder()
            .with_url(
                url,
                options={
                    "skip_negotiation": True,
                    "access_token_factory": lambda: self._token,
                },
            )
            .with_automatic_reconnect(
                {
                    "type": "interval",
                    "keep_alive_interval": 10,
                    # Retry every 5s indefinitely
                    "intervals": [5, 5, 5, 10, 10, 30],
                }
            )
            .build()
        )

        # Wire up connection events
        self._connection.on_open(self._on_open)
        self._connection.on_close(self._on_close)
        self._connection.on_error(self._on_error)
        self._connection.on_reconnect(self._on_reconnect)

        # Wire up market hub events. GatewayTrade signature: (contractId, data)
        # signalrcore passes them as a single list arg.
        self._connection.on("GatewayTrade", self._on_gateway_trade)
        self._connection.on("GatewayQuote", self._on_gateway_quote)
        # Depth events subscribed but ignored — we don't use them for bars
        self._connection.on("GatewayDepth", lambda args: None)

        # Start
        self._connection.start()

        # Wait for open (poll up to 30s)
        deadline = time.time() + 30
        while time.time() < deadline and not self._connected:
            time.sleep(0.1)
        if not self._connected:
            raise RuntimeError("Timed out waiting for market hub connection")

        # Subscribe to any queued contracts
        with self._lock:
            for cid in list(self._subscribed_contracts):
                self._invoke_subscribe(cid)

        # Start the sweep thread that closes stale buckets
        self._sweep_stop.clear()
        self._sweep_thread = threading.Thread(
            target=self._sweep_loop, daemon=True, name="topstep-stream-sweep"
        )
        self._sweep_thread.start()

    def stop(self) -> None:
        """Stop the stream. Safe to call multiple times."""
        self._sweep_stop.set()
        if self._sweep_thread is not None:
            self._sweep_thread.join(timeout=5)
        if self._connection is not None:
            try:
                self._connection.stop()
            except Exception:
                pass
        self._connected = False

    # ─────────────────────────────────────────────────────────
    # Introspection (for engine watchdog)
    # ─────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_event_time(self) -> datetime | None:
        """Wall-clock UTC of the most recent event of any kind."""
        return self._last_event_time

    def last_bar_time(self, contract_id: str) -> datetime | None:
        """Wall-clock UTC of the most recent bar CLOSE for a contract."""
        return self._last_bar_time.get(contract_id)

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    # ─────────────────────────────────────────────────────────
    # Internal — connection event handlers
    # ─────────────────────────────────────────────────────────
    def _on_open(self) -> None:
        with self._lock:
            self._connected = True
            if self._connection_gap_start is not None:
                gap = datetime.now(timezone.utc) - self._connection_gap_start
                log.info(f"Market hub reconnected after gap of {gap.total_seconds():.1f}s")
                self._connection_gap_start = None
            else:
                log.info("Market hub connected")

    def _on_close(self) -> None:
        with self._lock:
            was_connected = self._connected
            self._connected = False
            if was_connected:
                self._connection_gap_start = datetime.now(timezone.utc)
                log.warning("Market hub disconnected")

    def _on_reconnect(self) -> None:
        with self._lock:
            self._reconnect_count += 1
            log.info(f"Market hub reconnect #{self._reconnect_count}")
        # Re-subscribe to all contracts (SignalR doesn't persist subs)
        with self._lock:
            for cid in list(self._subscribed_contracts):
                self._invoke_subscribe(cid)

    def _on_error(self, err) -> None:
        log.error(f"Market hub error: {err!r}")

    def _invoke_subscribe(self, contract_id: str) -> None:
        try:
            self._connection.send("SubscribeContractTrades", [contract_id])
            log.info(f"Subscribed to trades for {contract_id}")
        except Exception as e:
            log.error(f"SubscribeContractTrades({contract_id}) failed: {e!r}")

    # ─────────────────────────────────────────────────────────
    # Internal — market data handlers
    # ─────────────────────────────────────────────────────────
    def _on_gateway_trade(self, args) -> None:
        """GatewayTrade fires as (contractId, data). signalrcore delivers
        both as a single args list.
        """
        try:
            # Payloads observed:
            #   args = ["CON.F.US.MES.U26", {"symbolId": "...", "price": 7500.25,
            #                                "timestamp": "...", "type": 0, "volume": 2}]
            # Some SDKs pass a list of trades in `data` instead of one dict — handle both.
            if not args or len(args) < 2:
                return
            contract_id = args[0]
            payload = args[1]

            with self._lock:
                self._last_event_time = datetime.now(timezone.utc)

            # If payload is a list of trades, iterate
            trades = payload if isinstance(payload, list) else [payload]
            for trade in trades:
                if not isinstance(trade, dict):
                    continue
                price = trade.get("price")
                volume = trade.get("volume") or trade.get("size") or 0
                ts_str = trade.get("timestamp")
                if price is None or ts_str is None:
                    continue

                try:
                    ts = parse_gateway_timestamp(ts_str)
                except Exception:
                    ts = datetime.now(timezone.utc)

                closed_bar = None
                with self._lock:
                    agg = self._aggregators.get(contract_id)
                    if agg is None:
                        # Trade for a contract we didn't explicitly subscribe to.
                        # This can happen briefly during resubscribe. Ignore.
                        continue
                    closed_bar = agg.add_tick(float(price), int(volume), ts)
                    tick_cbs = list(self._tick_callbacks)

                if closed_bar is not None:
                    self._emit_bar_close(contract_id, closed_bar)

                # Fan out raw tick to any registered tick-callbacks (e.g. VWAP
                # aggregator). Isolated so one bad callback can't kill the loop.
                for cb in tick_cbs:
                    try:
                        cb(contract_id, float(price), int(volume), ts)
                    except Exception as e:
                        log.warning(f"tick callback error: {e!r}")
        except Exception as e:
            # Never let a callback exception kill the connection thread
            log.exception(f"Error in _on_gateway_trade: {e!r}")

    def _on_gateway_quote(self, args) -> None:
        """We only track for last-event-time freshness. Not used for bars."""
        with self._lock:
            self._last_event_time = datetime.now(timezone.utc)

    def _emit_bar_close(self, contract_id: str, bar: dict) -> None:
        """Fire the user's bar-close callback. Also updates last_bar_time."""
        with self._lock:
            self._last_bar_time[contract_id] = datetime.now(timezone.utc)
            cb = self._bar_callback
        if cb is not None:
            try:
                cb(contract_id, bar)
            except Exception as e:
                log.exception(f"User bar callback raised: {e!r}")

    # ─────────────────────────────────────────────────────────
    # Internal — sweep loop for stale buckets
    # ─────────────────────────────────────────────────────────
    def _sweep_loop(self) -> None:
        """Periodically checks if any bar bucket has aged past its close
        boundary and forces close. Only runs while connected.
        """
        while not self._sweep_stop.is_set():
            try:
                if self._connected:
                    now_utc = datetime.now(timezone.utc)
                    to_emit: list[tuple[str, dict]] = []
                    with self._lock:
                        for cid, agg in list(self._aggregators.items()):
                            closed = agg.force_close_if_stale(now_utc)
                            if closed is not None:
                                to_emit.append((cid, closed))
                    for cid, bar in to_emit:
                        self._emit_bar_close(cid, bar)
            except Exception as e:
                log.exception(f"Sweep loop error: {e!r}")
            self._sweep_stop.wait(SWEEP_INTERVAL_S)
