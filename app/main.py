"""Application orchestrator.

Wires all components together, manages the event loop, and handles
startup/shutdown lifecycle for the Polymarket Quant Bot.
"""

import asyncio
import logging

from app.config.settings import settings
from app.monitoring.health import health_status, run_all_checks
from app.storage.db import db

logger = logging.getLogger(__name__)


class Application:
    """Top-level application orchestrator.

    Responsible for starting and stopping all subsystems in the correct
    order, managing the operating mode, and running the main event loop.
    """

    def __init__(self) -> None:
        self.mode: str = settings.mode
        self.running: bool = False

    async def startup(self) -> None:
        """Initialise all subsystems."""
        logger.info("Starting Polymarket Quant Bot", extra={"mode": self.mode})
        await db.init_schema()
        await run_all_checks()
        logger.info(
            "Startup complete",
            extra={"healthy": health_status.all_healthy()},
        )

    async def shutdown(self) -> None:
        """Gracefully shut down all subsystems."""
        logger.info("Shutting down")
        await db.close()
        logger.info("Shutdown complete")

    async def run(self) -> None:
        """Main entry point — runs the event loop until interrupted."""
        self.running = True
        await self.startup()
        try:
            while self.running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
        finally:
            await self.shutdown()


def main() -> None:
    """CLI entry point for the Polymarket Quant Bot."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = Application()
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
