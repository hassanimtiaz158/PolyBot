"""Walk-forward validation for the Microstructure strategy.

Generates synthetic market data with per-market OBI cycles (same as the
full backtest), splits it into rolling train → validation windows, and
runs a fresh, deterministic MicrostructureStrategy on each unseen
period.  Produces JSON + per-window CSV reports and prints the
stability diagnostics (overfitting, parameter drift, regime
sensitivity, degradation, single-period luck).

Usage:
    python scripts/run_walk_forward.py [--windows N] [--seed S]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtesting.walk_forward import WalkForwardReporter, WalkForwardValidator
from app.strategies.microstructure import MicrostructureStrategy
from scripts.run_backtest_full import generate_synthetic_data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", type=int, default=10,
                        help="number of validation windows (default 10)")
    parser.add_argument("--seed", type=int, default=42,
                        help="random seed for synthetic data (default 42)")
    args = parser.parse_args()

    # Each validation window must span enough time-steps (one per market
    # per timestamp) for OBI cycles to open AND close positions.  The
    # full backtest yields ~3 trades per 100 universe time-steps, so a
    # 500-snapshot window (25 time-steps across 20 markets) produces
    # ~1 round-trip per window.
    val_size = 500
    train_size = 1500
    total_needed = train_size + args.windows * val_size
    num_markets = 20

    print("=" * 60)
    print("POLYBOT WALK-FORWARD VALIDATION")
    print("=" * 60)
    print("  Strategy:          MicrostructureStrategy")
    print("  Mode:              expanding")
    print(f"  Train size:        {train_size} snapshots")
    print(f"  Validation size:   {val_size} snapshots")
    print(f"  Windows:           {args.windows}")
    print(f"  Total snapshots:   {total_needed}")
    print()

    print("[1/3] Generating synthetic market data...")
    # generate_synthetic_data's num_snapshots is PER-MARKET; the
    # walk-forward windows consume the interleaved universe timeline.
    import math
    per_market = math.ceil(total_needed / num_markets)
    snapshots = generate_synthetic_data(
        num_markets=num_markets, num_snapshots=per_market, seed=args.seed
    )
    print(f"  Total snapshots:   {len(snapshots)}")
    print()

    def strategy_factory():
        return MicrostructureStrategy()

    print("[2/3] Running walk-forward validation...")
    validator = WalkForwardValidator(
        strategy_factory=strategy_factory,
        train_size=train_size,
        val_size=val_size,
        mode="expanding",
        initial_equity=10_000.0,
        fee_rate=0.05,
        evaluate_in_sample=True,
    )

    def progress(current: int, total: int) -> None:
        print(f"  Window {current}/{total}...")

    report = asyncio.run(validator.run(snapshots, progress_callback=progress))
    print()

    print("[3/3] Generating reports...")
    output_dir = r"D:\PolyBOT\backtest_reports"
    reporter = WalkForwardReporter(output_dir=output_dir)
    paths = reporter.generate(report, label="walk_forward")
    print(f"  JSON report:       {paths['json']}")
    print(f"  CSV report:        {paths['csv']}")
    print()

    d = report.diagnostics
    print("=" * 60)
    print("VERDICT: " + d.verdict)
    print("=" * 60)
    print("  Overfitting:          " + str(d.overfitting))
    print("  Unstable parameters:  " + str(d.unstable_parameters))
    print("  Regime sensitive:     " + str(d.regime_sensitive))
    print("  Degradation:          " + str(d.degradation))
    print("  Single-period luck:   " + str(d.single_period_luck))
    codes_str = ",".join(d.codes) if d.codes else "(none)"
    print(f"  Detector codes:       {codes_str}")
    for reason in d.reasons:
        print("    - " + reason)
    print()

    print("OUT-OF-SAMPLE METRICS (aggregate across windows)")
    print("-" * 60)
    print(f"  Total P&L:         ${report.total_pnl:>+10,.2f}")
    print(f"  Total Return:       {report.total_return:>10.2%}")
    print(f"  Profit Factor:      {report.profit_factor:>10.2f}")
    print(f"  Win Rate:           {report.win_rate:>10.2%}")
    print(f"  Number of Trades:   {report.num_trades:>10d}")
    print(f"  Max Drawdown:      ${report.max_drawdown:>10,.2f}  ({report.max_drawdown_pct:.2f}%)")
    print(f"  Expectancy:        ${report.expectancy:>+10,.2f}")
    print(f"  Calibration Score:  {report.calibration_score:>10.4f}")
    print(f"  Avg Net Edge:       {report.avg_net_edge:>10.4f}")
    print()
    print("PER-WINDOW P&L")
    print("-" * 60)
    print(f"  Mean:              ${report.mean_window_pnl:>+10,.2f}")
    print(f"  Median:            ${report.median_window_pnl:>+10,.2f}")
    print(f"  Std:               ${report.std_window_pnl:>+10,.2f}")
    print(f"  Best:              ${report.best_window_pnl:>+10,.2f}")
    print(f"  Worst:             ${report.worst_window_pnl:>+10,.2f}")
    print(f"  Profitable windows: {report.num_profitable_windows}/{len(report.windows)}")
    print(f"  Zero-trade windows: {report.num_zero_trade_windows}")
    print("=" * 60)


if __name__ == "__main__":
    main()
