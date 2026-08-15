#!/usr/bin/env python3
"""CLI entry point for running backtests.

Usage:
    python scripts/backtest.py --data data/historical.csv --strategy momentum
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from app.backtesting.engine import BacktestEngine
from app.backtesting.models import MarketSnapshot
from app.backtesting.report import ReportGenerator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backtest.cli")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a backtest from historical market data.",
    )
    parser.add_argument(
        "--data", required=True,
        help="Path to historical data (CSV or JSON).",
    )
    parser.add_argument(
        "--strategy", default="momentum",
        choices=["momentum", "level", "custom"],
        help="Strategy to run (default: momentum).",
    )
    parser.add_argument(
        "--initial-equity", type=float, default=10_000.0,
        help="Starting portfolio equity (default: 10000).",
    )
    parser.add_argument(
        "--fee-rate", type=float, default=0.05,
        help="Taker fee coefficient (default: 0.05).",
    )
    parser.add_argument(
        "--output-dir", default="backtest_reports",
        help="Output directory for reports (default: backtest_reports).",
    )
    parser.add_argument(
        "--label", default="backtest",
        help="Report label used in filenames (default: backtest).",
    )
    return parser.parse_args(argv)


def load_snapshots(data_path: str) -> list[MarketSnapshot]:
    """Load market snapshots from a JSON or CSV file."""
    if data_path.endswith(".json"):
        with open(data_path) as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return [
                MarketSnapshot(**item) if isinstance(item, dict)
                else item
                for item in raw
            ]
        raise ValueError("JSON must contain an array of snapshots")
    # CSV
    import csv
    snapshots: list[MarketSnapshot] = []
    with open(data_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            snapshots.append(MarketSnapshot(
                timestamp=float(row["timestamp"]),
                market_id=row["market_id"],
                midpoint=float(row["midpoint"]),
                spread=float(row.get("spread", 0.02)),
                depth=float(row.get("depth", 1_000_000)),
                bid=(
                    float(row["bid"])
                    if row.get("bid") and row["bid"].strip()
                    else None
                ),
                ask=(
                    float(row["ask"])
                    if row.get("ask") and row["ask"].strip()
                    else None
                ),
                volume=float(row.get("volume", 0)),
            ))
    return snapshots


def _make_level_strategy():
    """Create a simple price-level strategy for the CLI."""
    from app.strategies.base import Strategy

    class LevelStrategy(Strategy):
        name = "level"

        def generate_signal(self, features, context=None):
            mid = features.get("midpoint", 0.50)
            mid_id = features.get("market_id", "unknown")
            if mid <= 0.48:
                return self._candidate(
                    mid_id, "YES", 0.55, mid, 0.75,
                    "price_low", features,
                )
            if mid >= 0.62:
                return self._candidate(
                    mid_id, "NO", 0.45, mid, 0.75,
                    "price_high", features,
                )
            return self._reject(mid_id, "no_opportunity")

    return LevelStrategy()


def _make_momentum_strategy():
    """Simple momentum strategy — buy on uptrend, sell on downtrend."""
    from app.strategies.base import Strategy

    class MomentumStrategy(Strategy):
        name = "momentum"
        _last_prices: dict[str, float] = {}

        def generate_signal(self, features, context=None):
            mid = features.get("midpoint", 0.50)
            mid_id = features.get("market_id", "unknown")
            prev = self._last_prices.get(mid_id)
            self._last_prices[mid_id] = mid

            if prev is None:
                return self._reject(mid_id, "no_history")

            change = (mid - prev) / max(prev, 1e-12)
            if change > 0.02:
                return self._candidate(
                    mid_id, "YES", mid + 0.05, mid, 0.7,
                    "uptrend", features,
                )
            if change < -0.02:
                return self._candidate(
                    mid_id, "NO", mid - 0.05, mid, 0.7,
                    "downtrend", features,
                )
            return self._reject(mid_id, "no_opportunity")

    return MomentumStrategy()


async def main() -> None:
    args = parse_args()

    logger.info("Loading data from %s", args.data)
    snapshots = load_snapshots(args.data)
    logger.info("Loaded %d snapshots", len(snapshots))

    engine = BacktestEngine(
        initial_equity=args.initial_equity,
        fee_rate=args.fee_rate,
    )

    if args.strategy == "level":
        engine.add_strategy(_make_level_strategy())
    elif args.strategy == "momentum":
        engine.add_strategy(_make_momentum_strategy())
    else:
        logger.error("Unknown strategy: %s", args.strategy)
        sys.exit(1)

    def progress(curr: int, total: int) -> None:
        if curr % max(1, total // 10) == 0 or curr == total:
            logger.info("Progress: %d / %d (%.0f%%)", curr, total, curr / total * 100)

    logger.info("Running backtest...")
    result = await engine.run(snapshots, progress_callback=progress)

    logger.info(
        "Backtest complete: %d trades, P&L=%.2f, return=%.2f%%",
        result.num_trades, result.total_pnl, result.total_return * 100,
    )

    gen = ReportGenerator(output_dir=args.output_dir)
    paths = gen.generate(result, label=args.label)

    logger.info("Reports written:")
    for fmt, path in paths.items():
        logger.info("  %s: %s", fmt, path)


if __name__ == "__main__":
    asyncio.run(main())
