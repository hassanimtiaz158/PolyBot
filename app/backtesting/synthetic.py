"""Synthetic market snapshot generation for backtesting and validation.

Used by the standalone backtest / walk-forward scripts and as a fallback
data source when the application has not yet ingested real market
history.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from app.backtesting.models import MarketSnapshot


@dataclass
class SyntheticSnapshot(MarketSnapshot):
    """MarketSnapshot extended with order-book imbalance and depth fields."""

    bid_depth: float = 500.0
    ask_depth: float = 500.0
    obi: float = 0.0
    velocity_60s: float = 0.0
    time_to_resolution: float = 3600.0


def generate_synthetic_data(
    num_markets: int = 20,
    num_snapshots: int = 100,
    seed: int = 42,
) -> list[SyntheticSnapshot]:
    """Generate synthetic market data with realistic microstructure features.

    Each market gets an independent OBI cycle (period 8..20) so signals
    alternate between YES and NO at different phases, creating round-trip
    trades (open + close).  Snapshots are interleaved so every timestamp
    is unique across all markets.
    """
    rng = random.Random(seed)
    base_time = time.time()

    market_phases: dict[str, int] = {}
    market_periods: dict[str, int] = {}
    for m in range(num_markets):
        mid = f"synth_{m + 1:03d}"
        market_periods[mid] = rng.randint(8, 20)
        market_phases[mid] = rng.randint(0, market_periods[mid] - 1)

    per_market: dict[str, list[SyntheticSnapshot]] = {
        f"synth_{m + 1:03d}": [] for m in range(num_markets)
    }

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

    all_snaps: list[SyntheticSnapshot] = []
    for m in range(num_markets):
        all_snaps.extend(per_market[f"synth_{m + 1:03d}"])

    all_snaps.sort(key=lambda s: s.timestamp)
    return all_snaps
