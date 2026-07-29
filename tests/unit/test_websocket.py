"""Tests for the WebSocket stream manager."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.data.websocket import ConnectionState, MarketEvent, WebSocketManager


@pytest.fixture
def manager() -> WebSocketManager:
    return WebSocketManager()


@pytest.fixture
def mock_ws() -> MagicMock:
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.recv = AsyncMock()
    ws.close = AsyncMock()
    ws.ping = AsyncMock()
    return ws


class TestInitialState:
    def test_starts_disconnected(self, manager: WebSocketManager) -> None:
        assert manager.state == ConnectionState.DISCONNECTED
        assert manager.is_connected is False
        assert manager.subscribed_markets == []


class TestConnect:
    async def test_successful_connect(self, manager: WebSocketManager, mock_ws: MagicMock) -> None:
        with (
            patch(
                "app.data.websocket.websockets.connect",
                AsyncMock(return_value=mock_ws),
            ) as mock_connect,
        ):
            await manager.connect(markets=["0xabc", "0xdef"])

        mock_connect.assert_called_once()
        url = mock_connect.call_args[0][0]
        assert url == "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        assert manager.state == ConnectionState.CONNECTED
        assert manager.is_connected is True
        assert manager.subscribed_markets == ["0xabc", "0xdef"]
        # Subscription should be sent
        expected_msg = json.dumps({"type": "market", "markets": ["0xabc", "0xdef"]})
        mock_ws.send.assert_called_once_with(expected_msg)

        # Cleanup: stop background tasks
        await manager.disconnect()

    async def test_connect_without_markets(
        self, manager: WebSocketManager, mock_ws: MagicMock
    ) -> None:
        with patch(
            "app.data.websocket.websockets.connect", AsyncMock(return_value=mock_ws)
        ):
            await manager.connect()

        mock_ws.send.assert_not_called()
        assert manager.subscribed_markets == []
        await manager.disconnect()

    async def test_connect_failure(self, manager: WebSocketManager) -> None:
        with (
            patch(
                "app.data.websocket.websockets.connect",
                AsyncMock(side_effect=ConnectionError("fail")),
            ),
            pytest.raises(ConnectionError),
        ):
            await manager.connect()

        assert manager.state == ConnectionState.DISCONNECTED

    async def test_connect_when_already_connected(
        self, manager: WebSocketManager, mock_ws: MagicMock
    ) -> None:
        with patch(
            "app.data.websocket.websockets.connect", AsyncMock(return_value=mock_ws)
        ):
            await manager.connect()
            await manager.connect()

        await manager.disconnect()

    async def test_normal_event_received(
        self, manager: WebSocketManager, mock_ws: MagicMock
    ) -> None:
        event_data: dict[str, Any] = {
            "type": "market",
            "market": "0xabc",
            "price": "0.45",
            "timestamp": str(int(__import__("time").time())),
        }
        mock_ws.recv = AsyncMock(return_value=json.dumps(event_data))

        received: list[MarketEvent] = []

        async def cb(event: MarketEvent) -> None:
            received.append(event)

        manager.add_callback(cb)

        with patch("websockets.connect", AsyncMock(return_value=mock_ws)):
            await manager.connect()
            # Let the listen loop process one message
            await _wait_for_listener(manager)

        assert len(received) >= 1
        assert received[0].channel == "market"
        assert received[0].market_id == "0xabc"

        await manager.disconnect()

    async def test_malformed_event_does_not_crash(
        self, manager: WebSocketManager, mock_ws: MagicMock
    ) -> None:
        """Malformed JSON should be silently dropped."""
        mock_ws.recv = AsyncMock(return_value="not valid json{{{")

        received: list[MarketEvent] = []

        async def cb(event: MarketEvent) -> None:
            received.append(event)

        manager.add_callback(cb)

        with patch("websockets.connect", AsyncMock(return_value=mock_ws)):
            await manager.connect()
            await _wait_for_listener(manager)

        # No events should be dispatched for malformed messages
        assert len(received) == 0

        await manager.disconnect()

    async def test_binary_message_decoded(
        self, manager: WebSocketManager, mock_ws: MagicMock
    ) -> None:
        """Binary messages should be decoded from UTF-8."""
        event_data = json.dumps({"type": "market", "market": "0xabc"})
        mock_ws.recv = AsyncMock(return_value=event_data.encode("utf-8"))

        received: list[MarketEvent] = []

        async def cb(event: MarketEvent) -> None:
            received.append(event)

        manager.add_callback(cb)

        with patch("websockets.connect", AsyncMock(return_value=mock_ws)):
            await manager.connect()
            await _wait_for_listener(manager)

        assert len(received) >= 1

        await manager.disconnect()

    async def test_duplicate_event(self, manager: WebSocketManager, mock_ws: MagicMock) -> None:
        """Duplicate events should be dispatched as separate events."""
        event_data = json.dumps({"type": "market", "market": "0xabc", "price": "0.45"})
        mock_ws.recv = AsyncMock(return_value=event_data)

        received: list[MarketEvent] = []

        async def cb(event: MarketEvent) -> None:
            received.append(event)

        manager.add_callback(cb)

        with patch("websockets.connect", AsyncMock(return_value=mock_ws)):
            await manager.connect()
            await _wait_for_listener(manager)
            await _wait_for_listener(manager)

        assert len(received) >= 2

        await manager.disconnect()

    async def test_callback_exception_does_not_crash(
        self, manager: WebSocketManager, mock_ws: MagicMock
    ) -> None:
        """A failing callback must not prevent other callbacks from receiving events."""
        event_data = json.dumps({"type": "market", "market": "0xabc"})
        mock_ws.recv = AsyncMock(return_value=event_data)

        received: list[MarketEvent] = []

        async def failing_cb(event: MarketEvent) -> None:
            raise RuntimeError("callback failed")

        async def good_cb(event: MarketEvent) -> None:
            received.append(event)

        manager.add_callback(failing_cb)
        manager.add_callback(good_cb)

        with patch("websockets.connect", AsyncMock(return_value=mock_ws)):
            await manager.connect()
            await _wait_for_listener(manager)

        assert len(received) >= 1

        await manager.disconnect()

    async def test_timeout_handling(self, manager: WebSocketManager, mock_ws: MagicMock) -> None:
        """A timeout on recv should trigger reconnection."""
                # Simulate timeout
        mock_ws.recv = AsyncMock(side_effect=TimeoutError)

        with (
            patch("websockets.connect", AsyncMock(return_value=mock_ws)),
            patch.object(manager, "_reconnect", AsyncMock()) as mock_reconnect,
        ):
            await manager.connect()
            await _wait_for_listener(manager)

            # Should have triggered reconnect
            mock_reconnect.assert_called_once()

        await manager.disconnect()

    async def test_disconnect_cleanup(self, manager: WebSocketManager, mock_ws: MagicMock) -> None:
        with patch("websockets.connect", AsyncMock(return_value=mock_ws)):
            await manager.connect()
            assert manager.is_connected is True

            await manager.disconnect()
            assert manager.state == ConnectionState.DISCONNECTED
            assert manager.is_connected is False
            mock_ws.close.assert_called_once()

    async def test_subscribe_additional_markets(
        self, manager: WebSocketManager, mock_ws: MagicMock
    ) -> None:
        with patch(
            "app.data.websocket.websockets.connect", AsyncMock(return_value=mock_ws)
        ):
            await manager.connect(markets=["0xabc"])
            mock_ws.send.assert_called_once()

            await manager.subscribe(["0xdef"])
            assert "0xdef" in manager.subscribed_markets
            assert mock_ws.send.call_count >= 2

            await manager.disconnect()

    async def test_reconnect_with_backoff(
        self, manager: WebSocketManager, mock_ws: MagicMock
    ) -> None:
        """reconnect_with_backoff should close existing socket and reconnect."""
        ws2 = MagicMock()
        ws2.send = AsyncMock()
        ws2.recv = AsyncMock(side_effect=StopAsyncIteration)  # Stop the listen loop gracefully
        ws2.close = AsyncMock()
        ws2.ping = AsyncMock()

        with (
            patch("websockets.connect", AsyncMock(side_effect=[mock_ws, ws2])),
        ):
            await manager.connect(markets=["0xabc"])
            assert manager.state == ConnectionState.CONNECTED

            await manager.reconnect_with_backoff()
            # Should have triggered reconnection
            assert manager.state in (ConnectionState.CONNECTED, ConnectionState.RECONNECTING)

            await manager.disconnect()

    async def test_invalid_market_metadata(
        self, manager: WebSocketManager, mock_ws: MagicMock
    ) -> None:
        """Message with missing market/asset_id should still produce an event."""
        event_data = json.dumps({"type": "price", "price": "0.50"})
        mock_ws.recv = AsyncMock(return_value=event_data)

        received: list[MarketEvent] = []

        async def cb(event: MarketEvent) -> None:
            received.append(event)

        manager.add_callback(cb)

        with patch("websockets.connect", AsyncMock(return_value=mock_ws)):
            await manager.connect()
            await _wait_for_listener(manager)

        assert len(received) >= 1
        assert received[0].market_id is None  # No market_id in payload
        assert received[0].channel == "price"

        await manager.disconnect()

    async def test_stale_data_reconnect(
        self, manager: WebSocketManager, mock_ws: MagicMock
    ) -> None:
        """Simulate stale timestamp detection by setting last_message_time."""
        import time

        with patch(
            "app.data.websocket.websockets.connect", AsyncMock(return_value=mock_ws)
        ):
            with patch.object(manager, "_reconnect", AsyncMock()):
                await manager.connect()
                manager._last_message_time = time.time() - 60
                await _wait_for_listener(manager)
                await manager.disconnect()


async def _wait_for_listener(manager: WebSocketManager) -> None:
    """Yield control briefly to let the listen loop's background task run."""
    import asyncio

    await asyncio.sleep(0.05)
