"""Debug script to trace backtest execution."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.strategies.base import StrategyDecision
from app.strategies.microstructure import MicrostructureStrategy
from scripts.run_backtest_full import FullBacktestEngine, generate_synthetic_data

snapshots = generate_synthetic_data(num_markets=2, num_snapshots=10)
engine = FullBacktestEngine(initial_equity=10000)
strategy = MicrostructureStrategy()
engine.add_strategy(strategy)

async def test():
    fills = 0
    for snap in snapshots[:20]:
        features = engine._build_features(snap)
        signal = strategy.generate_signal(features)
        if signal.decision == StrategyDecision.CANDIDATE:
            decision = await engine._risk_engine.evaluate(
                signal=signal,
                net_edge=signal.gross_edge,
                daily_pnl=engine._daily_pnl,
                consecutive_losses=engine._consecutive_losses,
            )
            print(
                f"market={snap.market_id} side={signal.side} "
                f"approved={decision.approved} size={decision.size:.2f} "
                f"reason={decision.reason}"
            )
            if decision.approved and decision.size > 0:
                result = engine._execution.execute(
                    market_id=snap.market_id,
                    side=signal.side,
                    size=decision.size,
                    snapshot=snap,
                    edge=signal.gross_edge,
                    signal_id=signal.signal_id,
                )
                fills += 1
                print(
                    f"  FILL: price={result['fill_price']:.4f} "
                    f"fee={result['fee']:.4f} pnl={result['pnl_change']:.4f}"
                )
    print(f"\nTotal fills: {fills}")

asyncio.run(test())
