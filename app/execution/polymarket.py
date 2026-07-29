"""Live Polymarket execution adapter (gated by LIVE_GUARDED mode).

Not yet implemented — placeholder for Phase 8.
"""

import logging
from typing import Any

from app.execution.interface import ExecutionAdapter

logger = logging.getLogger(__name__)


class PolymarketExecution(ExecutionAdapter):
    """Executes real orders on Polymarket via the CLOB API.

    This adapter is gated by the LIVE_GUARDED operating mode and
    requires authenticated Polymarket credentials.
    """

    async def submit(self, order: dict[str, Any]) -> dict[str, Any]:
        logger.warning("PolymarketExecution.submit called but not implemented")
        raise NotImplementedError("Live execution not implemented in Phase 1")

    async def cancel(self, order_id: str) -> bool:
        logger.warning("PolymarketExecution.cancel called but not implemented")
        raise NotImplementedError("Live execution not implemented in Phase 1")

    async def status(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError("Live execution not implemented in Phase 1")
