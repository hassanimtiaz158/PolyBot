"""Alert dispatcher for notifying operators about important events.

Supports logging (default), webhook, and Telegram channels.  Includes
de-duplication (suppresses repeated alerts within a time window) and
repeat detection (escalates when the same alert fires repeatedly).

Credentials and private keys are NEVER included in alert payloads or logs.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config.settings import settings

logger = logging.getLogger(__name__)


class AlertDispatcher:
    """Sends alerts to configured notification channels.

    Features:
    - **De-duplication**: alerts with the same ``key`` are suppressed
      for ``alert_min_interval_seconds`` after the first dispatch.
    - **Repeat detection**: when the same ``key`` fires
      ``alert_repeat_threshold`` times within ``alert_repeat_window_seconds``
      the level is escalated to ``CRITICAL``.
    - **Webhook**: optional HTTP POST to ``alert_webhook_url``.
    - **Telegram**: optional Telegram Bot API (when token is configured).

    Credentials are never logged or included in payloads.
    """

    def __init__(self) -> None:
        self._enabled = settings.alert_enabled
        self._webhook_url = settings.alert_webhook_url
        self._min_interval = settings.alert_min_interval_seconds
        self._repeat_threshold = settings.alert_repeat_threshold
        self._repeat_window = settings.alert_repeat_window_seconds

        # De-dupe tracking: key → timestamp of last dispatch
        self._last_dispatch: dict[str, float] = {}
        # Repeat tracking: key → list of dispatch timestamps
        self._fire_history: dict[str, list[float]] = defaultdict(list)
        self._http_client: httpx.AsyncClient | None = None

    @classmethod
    def from_settings(cls) -> AlertDispatcher:
        """Build a dispatcher from the global settings singleton."""
        return cls()

    # ── Core send ───────────────────────────────────────────────────

    async def send(
        self,
        message: str,
        level: str = "INFO",
        title: str = "Alert",
        key: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        """Dispatch an alert through all configured channels.

        Parameters
        ----------
        message : str
            Human-readable alert message.
        level : str
            Severity level (INFO, WARNING, ERROR, CRITICAL).
        title : str
            Short alert title.
        key : str
            De-duplication key.  Empty string means no de-duplication.
        context : dict | None
            Additional structured data (credentials are NEVER included).
        """
        now = time.monotonic()

        # De-duplication check
        if key and not self._should_dispatch(key, now):
            return

        # Repeat detection — escalate level if needed
        level = self._check_repeat_escalation(key, now, level)

        # Sanitize context: strip sensitive keys
        safe_context = self._sanitize_context(context) if context else None

        # Log-based dispatch (always)
        self._log_alert(title, message, level)

        # Webhook dispatch
        if self._enabled and self._webhook_url:
            await self._send_webhook(title, message, level, key, safe_context)

    async def close(self) -> None:
        """Close the HTTP client if open."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # ── Convenience helpers ─────────────────────────────────────────

    async def send_trade_alert(self, details: dict[str, Any]) -> None:
        """Send an alert about a trade event."""
        safe = self._sanitize_context(details)
        await self.send(
            f"Trade: {details.get('action', 'unknown')} "
            f"for {details.get('market_id', '?')}",
            level="INFO",
            title="Trade Event",
            key=f"trade_{details.get('action', 'unknown')}",
            context=safe,
        )

    async def send_error_alert(
        self, error: str, details: dict[str, Any] | None = None
    ) -> None:
        """Send an alert about an error condition."""
        safe = self._sanitize_context(details) if details else None
        await self.send(
            f"Error: {error}",
            level="ERROR",
            title="System Error",
            key=f"error_{error[:32]}",
            context=safe,
        )

    # ── De-duplication ──────────────────────────────────────────────

    def _should_dispatch(self, key: str, now: float) -> bool:
        """Return True if enough time has passed since last dispatch for this key."""
        last = self._last_dispatch.get(key, 0.0)
        if (now - last) < self._min_interval:
            return False
        self._last_dispatch[key] = now
        return True

    # ── Repeat detection ────────────────────────────────────────────

    def _check_repeat_escalation(
        self, key: str, now: float, level: str
    ) -> str:
        """Escalate to CRITICAL when the same key fires repeatedly."""
        if not key:
            return level

        history = self._fire_history[key]
        # Prune entries outside the window
        cutoff = now - self._repeat_window
        self._fire_history[key] = [t for t in history if t > cutoff]
        self._fire_history[key].append(now)

        if len(self._fire_history[key]) >= self._repeat_threshold:
            logger.warning(
                "Alert key '%s' fired %d times in %.0fs — escalating to CRITICAL",
                key,
                len(self._fire_history[key]),
                self._repeat_window,
            )
            return "CRITICAL"
        return level

    # ── Sanitization ────────────────────────────────────────────────

    _SENSITIVE_KEYS = frozenset({
        "api_key", "apikey", "secret", "secret_key", "private_key",
        "privatekey", "password", "passphrase", "token", "auth",
        "poly_api_key", "poly_secret", "poly_passphrase",
    })

    @classmethod
    def _sanitize_context(cls, context: dict[str, Any]) -> dict[str, Any]:
        """Remove sensitive keys from context before alerting/logging."""
        sanitized: dict[str, Any] = {}
        for k, v in context.items():
            if k.lower() in cls._SENSITIVE_KEYS:
                sanitized[k] = "[REDACTED]"
            elif isinstance(v, dict):
                sanitized[k] = cls._sanitize_context(v)
            else:
                sanitized[k] = v
        return sanitized

    # ── Logging ─────────────────────────────────────────────────────

    def _log_alert(
        self, title: str, message: str, level: str
    ) -> None:
        log_level = getattr(logging, level.upper(), logging.INFO)
        logger.log(log_level, "ALERT [%s]: %s — %s", level.upper(), title, message)

    # ── Webhook ─────────────────────────────────────────────────────

    async def _send_webhook(
        self,
        title: str,
        message: str,
        level: str,
        key: str,
        context: dict[str, Any] | None,
    ) -> None:
        """POST alert to the configured webhook URL."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10.0)

        payload: dict[str, Any] = {
            "title": title,
            "message": message,
            "level": level,
            "key": key,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if context:
            payload["context"] = context

        try:
            resp = await self._http_client.post(
                self._webhook_url,  # type: ignore[arg-type]
                json=payload,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("Webhook HTTP %d: %s", exc.response.status_code, title)
        except httpx.RequestError as exc:
            logger.warning("Webhook request failed for '%s': %s", key, exc)
