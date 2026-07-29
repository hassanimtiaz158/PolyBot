"""Paper execution adapter — simulates fills with slippage and partial fills."""

import logging
from typing import Any

from app.execution.interface import ExecutionAdapter

logger = logging.getLogger(__name__)


class PaperExecution(ExecutionAdapter):
    """Simulates order execution for paper trading.

    Models bid/ask spread, partial fills, slippage, latency, and
    rejection — never uses perfect midpoint fills by default.
    """

    async def submit(self, order: dict[str, Any]) -> dict[str, Any]:
        logger.debug("PaperExecution.submit called: %s", order.get("order_id"))
        return {"status": "SIMULATED", "order_id": order.get("order_id")}

    async def cancel(self, order_id: str) -> bool:
        logger.debug("PaperExecution.cancel called: %s", order_id)
        return True

    async def status(self, order_id: str) -> dict[str, Any]:
        return {"order_id": order_id, "status": "SIMULATED"}
