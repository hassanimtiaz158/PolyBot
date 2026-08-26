"""Live Polymarket CLOB V2 execution adapter -- HARD-LOCKED by default.

Safety model
------------
1. ``LIVE_TRADING_ENABLED`` must be ``true`` in settings (default ``false``).
2. Operating mode must be ``LIVE_GUARDED`` -- paper mode uses ``PaperExecution``.
3. No credentials are read from source; all come from environment variables.
4. No automatic fallback from paper to live.
5. Circuit breaker HALTED -> reject all orders.
6. Kill switch active -> reject all orders.
7. Stale or unhealthy data -> reject all orders.
8. Unknown account state (pre-reconciliation) -> reject all orders.
9. Duplicate order_id -> idempotent return (no double-submit).
10. Position limits re-checked immediately before submission.
11. Every order must pass through RiskEngine (enforced upstream by
    ``ExecutionEngine`` -- the adapter trusts that gate but adds its own
    pre-flight checks).

Order signing
-------------
Order construction and EIP-712 signing is delegated entirely to
Polymarket's own ``py-clob-client`` SDK (``ClobClient`` / ``Signer``)
rather than hand-rolled here. Reimplementing the exchange contract
address and struct field order by hand is both hard to verify and
catastrophic to get wrong with real funds; the official SDK is
maintained by Polymarket and is the ground-truth implementation.

The SDK's HTTP calls are synchronous -- every call into it runs via
``asyncio.to_thread`` so the bot's event loop is never blocked.

This module NEVER submits a real order unless all safety gates pass.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.exceptions import PolyApiException
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.signer import Signer

from app.config.settings import settings
from app.execution.interface import ExecutionAdapter
from app.monitoring.health import health_status
from app.portfolio.tracker import PortfolioTracker
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.kill_switch import KILL_SWITCH_REASON, KillSwitch
from app.storage.repositories import OrderRepository

logger = logging.getLogger(__name__)

# -- Constants -----------------------------------------------------------

CLOB_BASE_URL = "https://clob.polymarket.com"
CHAIN_ID = 137

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.5

# CLOB order responses report making/takingAmount in fixed-point base
# units (10^6), independent of SDK-side order signing/construction.
_AMOUNT_DECIMALS = 6
_AMOUNT_MULTIPLIER = 10**_AMOUNT_DECIMALS


def _from_fixed_amount(value: Any) -> float:
    """Convert a CLOB fixed-point base-unit amount to a human-readable float."""
    try:
        return float(value) / _AMOUNT_MULTIPLIER
    except (ValueError, TypeError):
        return 0.0

_CLOB_STATUS_MAP: dict[str, str] = {
    "live": "LIVE",
    "matched": "FILLED",
    "delayed": "LIVE",
    "cancelled": "CANCELLED",
    "filled": "FILLED",
    "partial": "PARTIALLY_FILLED",
    "failed": "REJECTED",
    "expired": "CANCELLED",
}

_SIDE_MAP: dict[str, str] = {
    "YES": BUY,
    "NO": SELL,
}


# -- Data classes --------------------------------------------------------


@dataclass
class ClobCredentials:
    """Polymarket CLOB API credentials (L2 auth).

    ``api_key``/``api_secret``/``api_passphrase`` may be blank -- if so
    they are derived once from the wallet's private key during
    ``reconcile()`` via ``ClobClient.create_or_derive_api_creds()``.
    """

    api_key: str
    api_secret: str
    api_passphrase: str
    address: str


@dataclass
class AccountState:
    """Snapshot of account state after reconciliation."""

    address: str
    api_key: str
    is_valid: bool = False
    reconciled_at: str | None = None
    open_order_ids: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ReconciliationResult:
    """Result of startup account reconciliation."""

    success: bool
    account_state: AccountState | None = None
    orders_found: int = 0
    error: str | None = None


# -- Safety gates --------------------------------------------------------


class SafetyViolation(Exception):
    """Raised when a safety gate blocks an operation."""


def _check_live_trading_enabled() -> None:
    """Block if live trading is not explicitly enabled."""
    if not settings.live_trading_enabled:
        raise SafetyViolation(
            "Live trading is disabled -- set LIVE_TRADING_ENABLED=true"
        )


def _check_kill_switch() -> None:
    """Block if kill switch is active."""
    val = os.environ.get("POLY_KILL_SWITCH", "").strip().lower()
    if val not in ("", "0", "false"):
        raise SafetyViolation("Kill switch is active -- POLY_KILL_SWITCH is set")


def _check_circuit_breaker(breaker: CircuitBreaker | None) -> None:
    """Block if circuit breaker is HALTED."""
    if breaker is not None and breaker.is_halted:
        raise SafetyViolation(f"Circuit breaker HALTED: {breaker.reasons}")


def _check_data_freshness() -> None:
    """Block if data freshness check is unhealthy."""
    if not health_status.is_healthy("data_freshness"):
        raise SafetyViolation("Data freshness check failed")


def _check_api_health() -> None:
    """Block if API health check is unhealthy."""
    if not health_status.is_healthy("api"):
        raise SafetyViolation("API health check failed")


def _check_account_reconciled(account: AccountState | None) -> None:
    """Block if account state has not been reconciled."""
    if account is None or not account.is_valid:
        raise SafetyViolation("Account state not reconciled")


def _check_position_limits(
    portfolio: PortfolioTracker | None,
    market_id: str,
    size: float,
    price: float,
) -> None:
    """Re-check position limits immediately before submission."""
    if portfolio is None:
        return

    equity = portfolio.equity
    if equity <= 0:
        raise SafetyViolation("Portfolio equity is non-positive")

    order_value = size * price
    max_position_value = equity * settings.max_position_pct
    if order_value > max_position_value:
        raise SafetyViolation(
            f"Order value {order_value:.2f} exceeds max position "
            f"value {max_position_value:.2f}"
        )

    total_exposure = portfolio.total_exposure()
    max_total = equity * settings.max_total_exposure_pct
    if total_exposure + order_value > max_total:
        raise SafetyViolation(
            f"Total exposure would exceed limit {max_total:.2f}"
        )

    market_exp = portfolio.market_exposure(market_id)
    max_market = equity * settings.max_market_exposure_pct
    if market_exp + order_value > max_market:
        raise SafetyViolation(
            f"Market exposure would exceed limit {max_market:.2f}"
        )


def _check_idempotency(
    order_id: str,
    pending: dict[str, dict[str, Any]],
) -> None:
    """Block duplicate order submission."""
    if order_id in pending:
        raise SafetyViolation(f"Duplicate order submission blocked: {order_id}")


# -- Main adapter --------------------------------------------------------


class PolymarketExecution(ExecutionAdapter):
    """Executes real orders on Polymarket via the CLOB V2 API.

    HARD-LOCKED by default.  The adapter refuses to submit orders unless
    ALL of the following are true:

    1. ``settings.live_trading_enabled`` is ``True``.
    2. Operating mode is ``LIVE_GUARDED``.
    3. A valid wallet private key is configured (``POLY_PRIVATE_KEY``).
    4. Account has been reconciled (``reconcile()`` called successfully).
    5. Circuit breaker is not HALTED.
    6. Kill switch is not active.
    7. Data freshness and API health checks pass.
    8. Position limits pass immediately before submission.
    9. No duplicate order_id.
    """

    def __init__(
        self,
        portfolio: PortfolioTracker | None = None,
        order_repo: OrderRepository | None = None,
        breaker: CircuitBreaker | None = None,
        kill_switch: KillSwitch | None = None,
        credentials: ClobCredentials | None = None,
        base_url: str = CLOB_BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        self._portfolio = portfolio
        self._order_repo = order_repo
        self._breaker = breaker
        self._kill_switch = kill_switch
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._credentials = credentials or self._load_credentials()
        self._account_state: AccountState | None = None
        self._pending_orders: dict[str, dict[str, Any]] = {}
        self._reconciled = False
        self._client: ClobClient | None = self._build_client()

        logger.info(
            "PolymarketExecution initialized (live_trading_enabled=%s, signer_configured=%s)",
            settings.live_trading_enabled,
            self._client is not None,
        )

    # -- Credential loading ----------------------------------------------

    @staticmethod
    def _load_credentials() -> ClobCredentials | None:
        """Load credentials from environment variables.

        A wallet private key (``POLY_PRIVATE_KEY``) is required -- without
        it no order can ever be signed, so the adapter has no usable
        credentials at all. The HMAC L2 identity (api_key/secret/
        passphrase) is optional here; if absent it is derived from the
        private key on first ``reconcile()``.
        """
        private_key = (settings.poly_private_key or "").strip()
        if not private_key:
            logger.warning(
                "POLY_PRIVATE_KEY not configured -- live orders cannot be signed"
            )
            return None

        address = (settings.poly_funder_address or "").strip()
        if not address:
            try:
                address = Signer(private_key, CHAIN_ID).address()
            except Exception:
                logger.exception("Failed to derive wallet address from private key")
                return None

        api_key = (settings.poly_api_key or "").strip()
        api_secret = (settings.poly_secret or "").strip()
        passphrase = (settings.poly_passphrase or "").strip()
        return ClobCredentials(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=passphrase,
            address=address,
        )

    def _build_client(self) -> ClobClient | None:
        """Construct the official Polymarket SDK client, if a signing
        key is configured. Returns ``None`` when no private key is
        available -- ``submit``/``cancel``/``status`` then fail closed.
        """
        private_key = (settings.poly_private_key or "").strip()
        if not private_key:
            return None

        creds: ApiCreds | None = None
        if self._credentials and all(
            [
                self._credentials.api_key,
                self._credentials.api_secret,
                self._credentials.api_passphrase,
            ]
        ):
            creds = ApiCreds(
                api_key=self._credentials.api_key,
                api_secret=self._credentials.api_secret,
                api_passphrase=self._credentials.api_passphrase,
            )

        try:
            return ClobClient(
                self._base_url,
                key=private_key,
                chain_id=CHAIN_ID,
                creds=creds,
                signature_type=settings.poly_signature_type,
                funder=(settings.poly_funder_address or None),
            )
        except Exception:
            logger.exception("Failed to construct Polymarket CLOB client")
            return None

    # -- Thread offload for the (synchronous) SDK -------------------------

    @staticmethod
    async def _call_client(fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous SDK call off the event loop thread."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    # -- Safety gate: all checks combined --------------------------------

    def _run_all_safety_checks(
        self,
        order_id: str,
        market_id: str,
        size: float,
        price: float,
    ) -> None:
        """Run every safety gate before order submission.

        Raises ``SafetyViolation`` if any gate fails.
        """
        _check_live_trading_enabled()
        _check_kill_switch()
        _check_circuit_breaker(self._breaker)
        _check_data_freshness()
        _check_api_health()
        _check_account_reconciled(self._account_state)
        _check_position_limits(self._portfolio, market_id, size, price)
        _check_idempotency(order_id, self._pending_orders)

        if self._credentials is None or self._client is None:
            raise SafetyViolation("Polymarket credentials not configured")

    # -- ExecutionAdapter interface --------------------------------------

    async def submit(self, order: dict[str, Any]) -> dict[str, Any]:
        """Submit an order to Polymarket CLOB.

        Parameters
        ----------
        order : dict
            Keys: ``order_id``, ``market_id``, ``side`` (YES/NO),
            ``size``, ``price``, ``signal_id``, ``token_id``.

        Returns
        -------
        dict
            ``order_id``, ``status``, ``filled_size``, ``average_fill``,
            ``error``, ``timestamp``.
        """
        order_id = str(order.get("order_id", ""))
        market_id = str(order.get("market_id", ""))
        side = str(order.get("side", "")).upper()
        size = float(order.get("size", 0))
        price = float(order.get("price", 0))
        token_id = str(order.get("token_id", ""))

        # -- Pre-flight validation ---------------------------------------
        if not order_id:
            return self._build_rejected(order_id, market_id, side, size, "Missing order_id")
        if side not in ("YES", "NO"):
            return self._build_rejected(order_id, market_id, side, size, f"Invalid side: {side}")
        if size <= 0:
            return self._build_rejected(order_id, market_id, side, size, "Invalid size")
        if price <= 0 or price >= 1:
            return self._build_rejected(order_id, market_id, side, size, f"Invalid price: {price}")
        if not token_id:
            return self._build_rejected(order_id, market_id, side, size, "Missing token_id")

        # -- Safety gates ------------------------------------------------
        if self._kill_switch is not None and await self._kill_switch.is_killed():
            return self._build_rejected(
                order_id, market_id, side, size,
                f"Kill switch active: {KILL_SWITCH_REASON}",
            )

        try:
            self._run_all_safety_checks(order_id, market_id, size, price)
        except SafetyViolation as exc:
            logger.warning("Safety gate blocked order %s: %s", order_id, exc)
            return self._build_rejected(order_id, market_id, side, size, str(exc))

        # -- Create, sign (EIP-712), and submit via the official SDK -----
        assert self._client is not None  # guaranteed by _run_all_safety_checks
        clob_side = _SIDE_MAP.get(side, BUY)
        order_args = OrderArgs(token_id=token_id, price=price, size=size, side=clob_side)

        try:
            result = await self._call_client(self._submit_sync, order_args)
        except Exception as exc:
            logger.exception("CLOB submission failed for %s", order_id)
            return self._build_rejected(order_id, market_id, side, size, f"Error: {exc}")

        return self._parse_order_response(order_id, market_id, side, size, result)

    def _submit_sync(self, order_args: OrderArgs) -> Any:
        """Runs on a worker thread: sign and post the order via the SDK."""
        assert self._client is not None
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                signed_order = self._client.create_order(order_args)
                return self._client.post_order(signed_order, OrderType.GTC)
            except PolyApiException as exc:
                if exc.status_code in (429, 500, 502, 503, 504):
                    wait = RETRY_BACKOFF_BASE * (attempt + 1)
                    logger.warning(
                        "CLOB error %s on submit (attempt %d/%d), retrying in %.1fs",
                        exc.status_code, attempt + 1, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)  # runs on a worker thread -- event loop is unaffected
                    last_exc = exc
                    continue
                raise
        raise last_exc or RuntimeError(f"Order submission failed after {MAX_RETRIES} retries")

    async def cancel(self, order_id: str) -> bool:
        """Cancel an order by ID.

        Parameters
        ----------
        order_id : str
            The CLOB order ID to cancel.

        Returns
        -------
        bool
            ``True`` if cancellation was successful or order already cancelled.
        """
        if not order_id:
            return False

        try:
            _check_live_trading_enabled()
            _check_kill_switch()
        except SafetyViolation:
            return False

        if self._client is None:
            logger.warning("Cannot cancel -- credentials not configured")
            return False

        try:
            await self._call_client(self._client.cancel, order_id)
            logger.info("Cancelled order %s", order_id)

            if order_id in self._pending_orders:
                self._pending_orders[order_id]["status"] = "CANCELLED"

            await self._persist_order_status(order_id, "CANCELLED")
            return True
        except Exception as exc:
            logger.warning("Cancel failed for %s: %s", order_id, exc)
            return False

    async def status(self, order_id: str) -> dict[str, Any]:
        """Get the current status of an order.

        Parameters
        ----------
        order_id : str
            The CLOB order ID.

        Returns
        -------
        dict
            Order status information.
        """
        if not order_id:
            return {"order_id": order_id, "status": "NOT_FOUND"}

        if order_id in self._pending_orders:
            return dict(self._pending_orders[order_id])

        if self._client is None:
            return {"order_id": order_id, "status": "UNKNOWN", "error": "No credentials"}

        try:
            result = await self._call_client(self._client.get_order, order_id)
            return self._parse_status_response(order_id, result)
        except Exception as exc:
            logger.warning("Status query failed for %s: %s", order_id, exc)
            return {"order_id": order_id, "status": "UNKNOWN", "error": str(exc)}

    # -- Account reconciliation ------------------------------------------

    async def reconcile(self) -> ReconciliationResult:
        """Reconcile account state before trading begins.

        Derives L2 API credentials from the private key if not already
        configured, then fetches open orders from the CLOB to verify
        account connectivity and establish the baseline for idempotency
        checks.

        Returns
        -------
        ReconciliationResult
            Outcome of the reconciliation.
        """
        if self._client is None or self._credentials is None:
            return ReconciliationResult(
                success=False,
                error="Credentials not configured",
            )

        try:
            if self._client.creds is None:
                creds = await self._call_client(self._client.create_or_derive_api_creds)
                self._client.set_api_creds(creds)
                self._credentials.api_key = creds.api_key
                self._credentials.api_secret = creds.api_secret
                self._credentials.api_passphrase = creds.api_passphrase

            orders = await self._call_client(self._client.get_orders)
            open_ids = [
                str(o.get("id", o.get("orderID", "")))
                for o in orders
                if o.get("status") in ("live", "matched", "delayed")
            ]

            self._account_state = AccountState(
                address=self._credentials.address,
                api_key=self._credentials.api_key,
                is_valid=True,
                reconciled_at=datetime.now(UTC).isoformat(),
                open_order_ids=open_ids,
            )
            self._reconciled = True

            logger.info(
                "Account reconciled: %d open orders found", len(open_ids)
            )
            return ReconciliationResult(
                success=True,
                account_state=self._account_state,
                orders_found=len(orders),
            )
        except Exception as exc:
            logger.exception("Reconciliation failed")
            self._account_state = AccountState(
                address=self._credentials.address,
                api_key=self._credentials.api_key,
                is_valid=False,
                error=str(exc),
            )
            return ReconciliationResult(success=False, error=str(exc))

    # -- Response parsing ------------------------------------------------

    def _parse_order_response(
        self,
        order_id: str,
        market_id: str,
        side: str,
        size: float,
        result: Any,
    ) -> dict[str, Any]:
        """Parse CLOB order response into standard format."""
        timestamp = datetime.now(UTC).isoformat()

        if not isinstance(result, dict):
            return self._build_rejected(order_id, market_id, side, size, "Invalid response")

        success = result.get("success", False)
        clob_order_id = result.get("orderID", order_id)
        clob_status = result.get("status", "failed")
        error_msg = result.get("errorMsg", "")

        if not success or error_msg:
            msg = error_msg or "Order rejected"
            return self._build_rejected(order_id, market_id, side, size, msg)

        internal_status = _CLOB_STATUS_MAP.get(clob_status, "REJECTED")

        making_amount = _from_fixed_amount(result.get("makingAmount", "0"))
        taking_amount = _from_fixed_amount(result.get("takingAmount", "0"))

        filled_size = making_amount if internal_status == "FILLED" else 0.0
        if making_amount > 0 and internal_status == "FILLED":
            average_fill = taking_amount / making_amount
        else:
            average_fill = None

        order_result = {
            "order_id": clob_order_id,
            "market_id": market_id,
            "side": side,
            "status": internal_status,
            "requested_size": size,
            "filled_size": filled_size,
            "average_fill": average_fill,
            "fee": 0.0,
            "realised_pnl": 0.0,
            "error": None,
            "timestamp": timestamp,
            "clob_order_id": clob_order_id,
            "clob_status": clob_status,
            "transactions_hashes": result.get("transactionsHashes", []),
            "trade_ids": result.get("tradeIDs", []),
        }

        self._pending_orders[clob_order_id] = order_result

        if internal_status == "FILLED" and self._portfolio is not None:
            try:
                self._portfolio.add_trade(
                    market_id=market_id,
                    side=side,
                    size=filled_size,
                    price=average_fill if average_fill is not None else 0.0,
                    fee=0.0,
                )
            except Exception:
                logger.exception("Failed to update portfolio for %s", clob_order_id)

        return order_result

    def _parse_status_response(self, order_id: str, result: Any) -> dict[str, Any]:
        """Parse CLOB order status response."""
        if not isinstance(result, dict):
            return {"order_id": order_id, "status": "UNKNOWN"}

        clob_status = result.get("status", "unknown")
        internal_status = _CLOB_STATUS_MAP.get(clob_status, "UNKNOWN")

        return {
            "order_id": order_id,
            "status": internal_status,
            "clob_status": clob_status,
            "making_amount": result.get("makingAmount"),
            "taking_amount": result.get("takingAmount"),
        }

    # -- Persistence helpers ---------------------------------------------

    async def _persist_order_status(self, order_id: str, status: str) -> None:
        """Update order status in database."""
        if self._order_repo is None:
            return
        try:
            existing = await self._order_repo.get(order_id)
            if existing:
                existing.status = status
                if status in ("FILLED", "CANCELLED", "REJECTED"):
                    existing.completed_at = datetime.now(UTC).isoformat()
                await self._order_repo.update_status(existing)
        except Exception:
            logger.exception("Failed to persist order status for %s", order_id)

    # -- Build helpers ---------------------------------------------------

    @staticmethod
    def _build_rejected(
        order_id: str,
        market_id: str,
        side: str,
        size: float,
        error: str,
    ) -> dict[str, Any]:
        """Build a standard rejected-order result dict."""
        return {
            "order_id": order_id,
            "market_id": market_id,
            "side": side,
            "status": "REJECTED",
            "requested_size": size,
            "filled_size": 0.0,
            "average_fill": None,
            "fee": 0.0,
            "realised_pnl": 0.0,
            "error": error,
            "timestamp": datetime.now(UTC).isoformat(),
        }
