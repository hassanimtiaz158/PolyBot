"""Full backtest with synthetic data and MicrostructureStrategy.

Generates synthetic market data, runs the backtest engine, produces
reports (JSON + CSV + equity curve), and prints key metrics to stdout.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtesting.engine import BacktestEngine
from app.backtesting.models import MarketSnapshot
from app.backtesting.report import ReportGenerator
from app.strategies.microstructure import MicrostructureStrategy
from app.storage.models import MarketSnapshot as StorageMarketSnapshot
from app.risk.engine import RiskEngine
from app.risk.limits import RiskLimits
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.position_sizing import PositionSizer
from app.portfolio.tracker import PortfolioTracker
from app.ev.expected_value import ExpectedValueEngine
from app.ev.costs import CostEstimator


# ---------------------------------------------------------------------------
# Extended snapshot with OBI/depth fields for synthetic data
# ---------------------------------------------------------------------------

@dataclass
class SyntheticSnapshot(MarketSnapshot):
    """MarketSnapshot extended with order-book imbalance and depth fields."""

    bid_depth: float = 500.0
    ask_depth: float = 500.0
    obi: float = 0.0
    velocity_60s: float = 0.0
    time_to_resolution: float = 3600.0


# ---------------------------------------------------------------------------
# BacktestEngine subclass with OBI-aware feature construction
# ---------------------------------------------------------------------------

class FullBacktestEngine(BacktestEngine):
    """BacktestEngine that includes OBI and velocity in feature dicts."""

    @staticmethod
    def _build_features(snap: MarketSnapshot) -> dict:
        ts_str = datetime.fromtimestamp(snap.timestamp, tz=UTC).isoformat()
        bid = snap.bid if snap.bid is not None else snap.midpoint - snap.spread / 2
        ask = snap.ask if snap.ask is not None else snap.midpoint + snap.spread / 2

        bid_depth = getattr(snap, "bid_depth", 500.0)
        ask_depth = getattr(snap, "ask_depth", 500.0)
        total_depth = bid_depth + ask_depth
        obi = getattr(snap, "obi", 0.0)
        velocity = getattr(snap, "velocity_60s", 0.0)

        return {
            "market_id": snap.market_id,
            "midpoint": snap.midpoint,
            "spread": snap.spread,
            "bid": bid,
            "ask": ask,
            "depth": total_depth,
            "volume": snap.volume,
            "liquidity_score": total_depth,
            "timestamp": ts_str,
            "obi": obi,
            "velocity_60s": velocity,
        }


# ---------------------------------------------------------------------------
# Synthetic data generation
# ---------------------------------------------------------------------------

def generate_synthetic_data(
    num_markets: int = 20,
    num_snapshots: int = 100,
    seed: int = 42,
) -> list[SyntheticSnapshot]:
    """Generate synthetic market data with realistic microstructure features.

    Each market gets an independent OBI cycle (period 12..25) so
    signals alternate between YES and NO at different phases, creating
    round-trip trades (open + close).  Snapshots are interleaved so
    every timestamp is unique across all markets.

    Parameters
    ----------
    num_markets : int
        Number of distinct markets to simulate.
    num_snapshots : int
        Snapshots per market.
    seed : int
        Random seed for reproducibility.

    Returns
    -------
    list[SyntheticSnapshot]
    """
    rng = random.Random(seed)
    base_time = time.time()

    # Each market gets a random OBI cycle period so they don't switch in lockstep.
    market_phases: dict[str, int] = {}
    market_periods: dict[str, int] = {}
    for m in range(num_markets):
        mid = f"synth_{m + 1:03d}"
        market_periods[mid] = rng.randint(8, 20)
        market_phases[mid] = rng.randint(0, market_periods[mid] - 1)

    # Build per-market snapshot lists first, then interleave.
    per_market: dict[str, list[SyntheticSnapshot]] = {f"synth_{m+1:03d}": [] for m in range(num_markets)}

    for m in range(num_markets):
        market_id = f"synth_{m + 1:03d}"
        period = market_periods[market_id]
        phase = market_phases[market_id]

        for s in range(num_snapshots):
            timestamp = base_time + s * 0.001
            midpoint = rng.uniform(0.3, 0.7)
            spread = rng.uniform(0.001, 0.015)
            bid = midpoint - spread / 2
            ask = midpoint + spread / 2

            # Per-market OBI cycle: first half bid-heavy (YES), second half ask-heavy (NO)
            cycle_pos = (s + phase) % period
            if cycle_pos < period // 2:
                bid_depth = rng.uniform(2000, 8000)
                ask_depth = rng.uniform(200, 800)
            else:
                bid_depth = rng.uniform(200, 800)
                ask_depth = rng.uniform(2000, 8000)

            volume = rng.uniform(5000, 50000)
            time_to_resolution = rng.uniform(3600, 86400)

            total_depth = bid_depth + ask_depth
            obi = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0
            velocity = obi * rng.uniform(0.02, 0.08)

            per_market[market_id].append(SyntheticSnapshot(
                timestamp=timestamp,
                market_id=market_id,
                midpoint=midpoint,
                spread=spread,
                bid=bid,
                ask=ask,
                depth=total_depth,
                volume=volume,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                obi=obi,
                velocity_60s=velocity,
                time_to_resolution=time_to_resolution,
            ))

    # Flatten and sort chronologically.
    # All markets share the same base timeline so the risk engine
    # sees interleaved snapshots from different markets.
    all_snaps: list[SyntheticSnapshot] = []
    for m in range(num_markets):
        all_snaps.extend(per_market[f"synth_{m+1:03d}"])

    all_snaps.sort(key=lambda s: s.timestamp)
    return all_snaps


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the full backtest pipeline."""
    print("=" * 60)
    print("POLYBOT FULL BACKTEST - SYNTHETIC DATA")
    print("=" * 60)

    # 1. Generate synthetic data
    print("\n[1/4] Generating synthetic market data...")
    snapshots = generate_synthetic_data(
        num_markets=20, num_snapshots=100, seed=42
    )
    print(f"  Markets: 20  |  Snapshots per market: 100")
    print(f"  Total snapshots: {len(snapshots)}")

    # 2. Initialize and run backtest engine
    print("\n[2/4] Running backtest engine...")
    strategy = MicrostructureStrategy()
    engine = FullBacktestEngine(initial_equity=10_000.0, fee_rate=0.05)
    engine.add_strategy(strategy)

    def progress(current: int, total: int) -> None:
        if current % 500 == 0 or current == total:
            print(f"  Processed {current}/{total} snapshots...")

    result = asyncio.run(engine.run(snapshots, progress_callback=progress))

    # 3. Generate reports
    print("\n[3/4] Generating reports...")
    output_dir = r"D:\PolyBOT\backtest_reports"
    reporter = ReportGenerator(output_dir=output_dir)
    paths = reporter.generate(result, label="full_synthetic")
    print(f"  JSON report:      {paths['json']}")
    print(f"  CSV report:       {paths['csv']}")
    print(f"  Equity curve:     {paths['equity_curve']}")

    # 4. Print key metrics
    print("\n[4/4] Key Metrics")
    print("=" * 60)
    print(f"  Total P&L:        ${result.total_pnl:>+10,.2f}")
    print(f"  Profit Factor:     {result.profit_factor:>10.2f}")
    print(f"  Max Drawdown:      ${result.max_drawdown:>10,.2f}  ({result.max_drawdown_pct:.2f}%)")
    print(f"  Win Rate:          {result.win_rate:>10.2%}")
    print(f"  Number of Trades:  {result.num_trades:>10d}")
    print(f"  Sharpe Ratio:      {result.sharpe_ratio:>10.2f}")
    print(f"  Expectancy:        ${result.expectancy:>+10,.2f}")
    print(f"  Calibration Score: {result.calibration_score:>10.4f}")
    print("=" * 60)
    print(f"  Initial Equity:    ${result.initial_equity:>10,.2f}")
    print(f"  Final Equity:      ${result.final_equity:>10,.2f}")
    print(f"  Total Return:      {result.total_return:>10.2%}")
    print(f"  Total Fees:        ${result.total_fees:>10,.2f}")
    print(f"  Sortino Ratio:     {result.sortino_ratio:>10.2f}")
    print(f"  Avg Holding:       {result.avg_holding_period:>10.1f}s")
    print(f"  Slippage Impact:   {result.slippage_impact:>10.4f}")
    print("=" * 60)


if __name__ == "__main__":
    main()
