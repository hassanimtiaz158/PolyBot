"""WebSocket stream manager for real-time Polymarket market data.

Connects to ``wss://ws-subscriptions-clob.polymarket.com/ws/market`` for
public order-book, price, and market-lifecycle updates.

Handles reconnection with exponential backoff, malformed-event isolation,
and callback-based message routing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import websockets

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RECONNECT_BASE_DELAY = 1.0  # seconds
RECONNECT_MAX_DELAY = 60.0  # seconds
RECONNECT_JITTER = 0.1  # fraction of delay for randomisation
PING_INTERVAL = 25.0  # seconds — keepalive ping
MESSAGE_TIMEOUT = 30.0  # seconds without any message → assume disconnect


class ConnectionState(Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"


@dataclass
class MarketEvent:
    """A normalised market-data event delivered via WebSocket."""

    channel: str
    market_id: str | None
    timestamp: str
    raw: dict[str, Any] = field(repr=False)


Callback = Callable[[MarketEvent], Awaitable[None]]


class WebSocketManager:
    """Manages a WebSocket connection to Polymarket for real-time data.

    Usage::

        manager = WebSocketManager()

        async def on_event(event: MarketEvent) -> None:
            print(event.channel, event.market_id)

        manager.add_callback(on_event)
        await manager.connect(markets=["0xabc...", "0xdef..."])
        ...
        await manager.disconnect()
    """

    def __init__(self) -> None:
        self._callbacks: list[Callback] = []
        self._state = ConnectionState.DISCONNECTED
        self._ws: Any = None  # websockets.WebSocketClientProtocol
        self._listen_task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._subscribed_markets: list[str] = []
        self._last_message_time: float = 0.0
        self._reconnect_attempt = 0
        self._reconnect_task: asyncio.Task[None] | None = None

    # ── Public API ──────────────────────────────────────────────────

    @property
    def state(self) -> ConnectionState:
        """Current connection state."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """``True`` when the WebSocket is currently connected."""
        return self._state == ConnectionState.CONNECTED

    @property
    def subscribed_markets(self) -> list[str]:
        """List of market IDs (or token IDs) currently subscribed to."""
        return list(self._subscribed_markets)

    def add_callback(self, callback: Callback) -> None:
        """Register a callback to receive every incoming ``MarketEvent``.

        Callables should be async functions that accept a single
        ``MarketEvent`` argument.
        """
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def remove_callback(self, callback: Callback) -> None:
        """Unregister a previously added callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    async def connect(self, markets: list[str] | None = None) -> None:
        """Open a WebSocket connection and subscribe to market feeds.

        Parameters
        ----------
        markets:
            Optional list of market condition IDs or token IDs to subscribe
            to.  If ``None``, connects without subscriptions (call
            ``subscribe()`` later).
        """
        if self._state == ConnectionState.CONNECTED:
            logger.debug("WebSocket already connected")
            return

        self._subscribed_markets = markets or []
        self._state = ConnectionState.CONNECTING
        await self._connect_and_subscribe()

        # Start background listeners
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def disconnect(self) -> None:
        """Gracefully close the WebSocket connection."""
        self._state = ConnectionState.DISCONNECTED

        # Cancel and fully unwind background tasks so no new event
        # dispatches are queued before we drain the in-flight ones.
        for task in (self._listen_task, self._ping_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._listen_task = None
        self._ping_task = None

        # Drain in-flight dispatch tasks so no callback coroutine leaks
        if self._dispatch_tasks:
            await asyncio.gather(*self._dispatch_tasks, return_exceptions=True)
            self._dispatch_tasks.clear()

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        self._reconnect_attempt = 0
        logger.info("WebSocket disconnected")

    async def subscribe(self, markets: list[str]) -> None:
        """Subscribe to additional markets on an already-connected socket."""
        self._subscribed_markets = list(set(self._subscribed_markets + markets))
        if self._ws is not None and self._state == ConnectionState.CONNECTED:
            await self._send_subscription(markets)

    async def reconnect_with_backoff(self) -> None:
        """Explicitly trigger a reconnection with exponential backoff.

        This is called internally on disconnect but can also be called
        by the health-monitoring loop.
        """
        if self._state == ConnectionState.DISCONNECTED:
            return
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._state = ConnectionState.RECONNECTING
        await self._reconnect()

    # ── Internal: connection lifecycle ──────────────────────────────

    async def _connect_and_subscribe(self) -> None:
        """Establish the WS connection and send subscription message."""
        try:
            self._ws = await websockets.connect(
                WS_MARKET_URL,
                ping_interval=None,  # We handle pings ourselves
                close_timeout=5,
                max_size=2**20,  # 1 MB
            )
            self._state = ConnectionState.CONNECTED
            self._last_message_time = asyncio.get_running_loop().time()
            self._reconnect_attempt = 0

            logger.info("WebSocket connected to %s", WS_MARKET_URL)

            if self._subscribed_markets:
                await self._send_subscription(self._subscribed_markets)

        except Exception as exc:
            logger.error("WebSocket connection failed: %s", exc)
            self._state = ConnectionState.DISCONNECTED
            raise

    async def _send_subscription(self, markets: list[str]) -> None:
        """Send a subscription message for the given market IDs."""
        if self._ws is None:
            return
        msg = json.dumps({
            "type": "market",
            "markets": markets,
        })
        try:
            await self._ws.send(msg)
            logger.debug("Subscribed to %d markets", len(markets))
        except Exception as exc:
            logger.error("Failed to send subscription: %s", exc)

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff and jitter."""
        import random

        # Deduplicate: only one reconnect task at a time
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return

        delay = min(
            RECONNECT_BASE_DELAY * (2**self._reconnect_attempt),
            RECONNECT_MAX_DELAY,
        )
        jitter = delay * RECONNECT_JITTER * random.random()
        total_delay = delay + jitter

        logger.info(
            "Reconnecting in %.1fs (attempt %d)",
            total_delay,
            self._reconnect_attempt + 1,
        )
        await asyncio.sleep(total_delay)
        self._reconnect_attempt += 1

        try:
            await self._connect_and_subscribe()
        except Exception:
            # Schedule another reconnect (only if not disconnected)
            if self._state not in (ConnectionState.DISCONNECTED,):
                self._state = ConnectionState.RECONNECTING
                self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _listen_loop(self) -> None:
        """Continuously read messages from the WebSocket."""
        while self._state not in (ConnectionState.DISCONNECTED,):
            if self._ws is None:
                await asyncio.sleep(0.1)
                continue

            try:
                raw_msg = await asyncio.wait_for(
                    self._ws.recv(), timeout=MESSAGE_TIMEOUT
                )
                self._last_message_time = asyncio.get_running_loop().time()
                self._handle_raw_message(raw_msg)

            except TimeoutError:
                logger.warning("No WebSocket message for %.0fs", MESSAGE_TIMEOUT)
                if self._state == ConnectionState.CONNECTED:
                    self._state = ConnectionState.RECONNECTING
                    if self._reconnect_task is None or self._reconnect_task.done():
                        self._reconnect_task = asyncio.create_task(self._reconnect())
                break

            except Exception as exc:
                logger.warning("WebSocket receive error: %s", exc)
                if self._state == ConnectionState.CONNECTED:
                    self._state = ConnectionState.RECONNECTING
                    if self._reconnect_task is None or self._reconnect_task.done():
                        self._reconnect_task = asyncio.create_task(self._reconnect())
                break

    async def _ping_loop(self) -> None:
        """Send periodic keepalive pings."""
        while self._state not in (ConnectionState.DISCONNECTED,):
            await asyncio.sleep(PING_INTERVAL)
            if self._ws is not None and self._state == ConnectionState.CONNECTED:
                try:
                    await self._ws.ping()
                except Exception:
                    pass

    # ── Internal: message handling ──────────────────────────────────

    def _handle_raw_message(self, raw_msg: Any) -> None:
        """Parse a raw WebSocket message and dispatch to callbacks."""
        try:
            if isinstance(raw_msg, bytes):
                raw_msg = raw_msg.decode("utf-8")
            data = json.loads(raw_msg)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            logger.warning("Malformed WebSocket message (not JSON): %s", exc)
            return

        event = self._build_event(data)
        if event is not None:
            task = asyncio.create_task(self._dispatch(event))
            self._dispatch_tasks.add(task)
            task.add_done_callback(self._dispatch_tasks.discard)

    @staticmethod
    def _build_event(data: dict[str, Any]) -> MarketEvent | None:
        """Convert a raw JSON message dict into a ``MarketEvent``.

        Returns ``None`` for malformed or unparseable messages so they
        are silently dropped instead of crashing the listener.
        """
        try:
            channel = str(data.get("type", data.get("channel", "unknown")))
            market_id = data.get("market") or data.get("asset_id") or data.get("market_id")
            if isinstance(market_id, str):
                market_id = market_id
            else:
                market_id = None

            timestamp = _make_timestamp(data)
            return MarketEvent(
                channel=channel,
                market_id=market_id,
                timestamp=timestamp,
                raw=data,
            )
        except Exception as exc:
            logger.warning("Failed to build MarketEvent: %s", exc)
            return None

    async def _dispatch(self, event: MarketEvent) -> None:
        """Send the event to all registered callbacks.

        A failing callback never propagates the exception — it is logged
        and the other callbacks still receive the event.
        """
        for cb in self._callbacks:
            try:
                await cb(event)
            except Exception as exc:
                logger.error("Callback %s failed: %s", cb.__name__, exc)


def _make_timestamp(data: dict[str, Any]) -> str:
    """Extract or create an ISO-8601 timestamp from a WS message."""
    raw_ts = data.get("timestamp") or data.get("ts") or data.get("time")
    if raw_ts is not None:
        try:
            if isinstance(raw_ts, (int, float)):
                import datetime as dt_mod

                return dt_mod.datetime.fromtimestamp(raw_ts, tz=dt_mod.UTC).isoformat()
            if isinstance(raw_ts, str):
                if raw_ts.isdigit():
                    import datetime as dt_mod

                    return dt_mod.datetime.fromtimestamp(int(raw_ts), tz=dt_mod.UTC).isoformat()
                return raw_ts.replace("Z", "+00:00")
        except Exception:
            pass
    import datetime as dt_mod

    return dt_mod.datetime.now(dt_mod.UTC).isoformat()
