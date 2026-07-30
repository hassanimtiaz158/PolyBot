"""Report generation for backtest results.

Produces ``backtest_report.json``, ``backtest_report.csv``, and an
optional equity-curve plot (PNG).
"""

from __future__ import annotations

import csv
import json
import logging
import os
from typing import Any

from app.backtesting.models import BacktestResult, EquityPoint, FillRecord

logger = logging.getLogger(__name__)

_REPORT_DIR_DEFAULT = "backtest_reports"


class ReportGenerator:
    """Generate backtest reports in multiple formats.

    Parameters
    ----------
    output_dir : str
        Directory for output files (created if missing).
    """

    def __init__(self, output_dir: str = _REPORT_DIR_DEFAULT) -> None:
        self._output_dir = output_dir

    def generate(
        self,
        result: BacktestResult,
        label: str = "backtest",
    ) -> dict[str, str]:
        """Generate all report files.

        Parameters
        ----------
        result : BacktestResult
            Computed backtest result.
        label : str
            Label used in filenames.

        Returns
        -------
        dict[str, str]
            ``{"json": …, "csv": …, "equity_curve": …}``
        """
        os.makedirs(self._output_dir, exist_ok=True)

        json_path = os.path.join(self._output_dir, f"{label}_report.json")
        csv_path = os.path.join(self._output_dir, f"{label}_report.csv")
        equity_path = os.path.join(
            self._output_dir, f"{label}_equity_curve.png"
        )

        self._write_json(json_path, result, label)
        self._write_csv(csv_path, result)
        self._write_equity_curve(equity_path, result)

        paths = {
            "json": json_path,
            "csv": csv_path,
            "equity_curve": equity_path,
        }
        result.report_paths = paths
        logger.info("Backtest reports saved to %s", self._output_dir)
        return paths

    # ── JSON ─────────────────────────────────────────────────────────

    @staticmethod
    def _write_json(path: str, result: BacktestResult, label: str) -> None:
        data = {
            "label": label,
            "metrics": {
                "initial_equity": result.initial_equity,
                "final_equity": result.final_equity,
                "total_return": result.total_return,
                "total_return_pct": result.total_return * 100,
                "total_pnl": result.total_pnl,
                "max_drawdown": result.max_drawdown,
                "max_drawdown_pct": result.max_drawdown_pct,
                "win_rate": result.win_rate,
                "win_rate_pct": result.win_rate * 100,
                "loss_rate": result.loss_rate,
                "loss_rate_pct": result.loss_rate * 100,
                "profit_factor": result.profit_factor,
                "expectancy": result.expectancy,
                "sharpe_ratio": result.sharpe_ratio,
                "sortino_ratio": result.sortino_ratio,
                "turnover": result.turnover,
                "avg_holding_period": result.avg_holding_period,
                "avg_net_edge": result.avg_net_edge,
                "calibration_score": result.calibration_score,
                "slippage_impact": result.slippage_impact,
                "num_trades": result.num_trades,
                "num_wins": result.num_wins,
                "num_losses": result.num_losses,
                "total_fees": result.total_fees,
                "gross_profit": result.gross_profit,
                "gross_loss": result.gross_loss,
            },
            "fills": [
                {
                    "timestamp": f.timestamp,
                    "market_id": f.market_id,
                    "side": f.side,
                    "size": f.size,
                    "price": f.price,
                    "fee": f.fee,
                    "pnl_change": f.pnl_change,
                    "slippage": f.slippage,
                    "edge": f.edge,
                }
                for f in result.fills
            ],
            "equity_curve": [
                {
                    "timestamp": p.timestamp,
                    "equity": p.equity,
                    "total_exposure": p.total_exposure,
                    "unrealised_pnl": p.unrealised_pnl,
                    "realised_pnl": p.realised_pnl,
                    "num_positions": p.num_positions,
                }
                for p in result.equity_curve
            ],
        }

        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    # ── CSV ──────────────────────────────────────────────────────────

    @staticmethod
    def _write_csv(path: str, result: BacktestResult) -> None:
        metrics = {
            "metric": [
                "initial_equity",
                "final_equity",
                "total_return",
                "total_return_pct",
                "total_pnl",
                "max_drawdown",
                "max_drawdown_pct",
                "win_rate",
                "win_rate_pct",
                "loss_rate",
                "loss_rate_pct",
                "profit_factor",
                "expectancy",
                "sharpe_ratio",
                "sortino_ratio",
                "turnover",
                "avg_holding_period",
                "avg_net_edge",
                "calibration_score",
                "slippage_impact",
                "num_trades",
                "num_wins",
                "num_losses",
                "total_fees",
                "gross_profit",
                "gross_loss",
            ],
            "value": [
                result.initial_equity,
                result.final_equity,
                result.total_return,
                result.total_return * 100,
                result.total_pnl,
                result.max_drawdown,
                result.max_drawdown_pct,
                result.win_rate,
                result.win_rate * 100,
                result.loss_rate,
                result.loss_rate * 100,
                result.profit_factor,
                result.expectancy,
                result.sharpe_ratio,
                result.sortino_ratio,
                result.turnover,
                result.avg_holding_period,
                result.avg_net_edge,
                result.calibration_score,
                result.slippage_impact,
                result.num_trades,
                result.num_wins,
                result.num_losses,
                result.total_fees,
                result.gross_profit,
                result.gross_loss,
            ],
        }
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(metrics["metric"])
            writer.writerow(metrics["value"])

    # ── Equity curve (PNG) ───────────────────────────────────────────

    @staticmethod
    def _write_equity_curve(path: str, result: BacktestResult) -> None:
        """Write an equity-curve PNG.

        Falls back silently if ``matplotlib`` is not installed.
        """
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            logger.warning(
                "matplotlib not installed — skipping equity curve PNG"
            )
            # Write a simple text marker so the file exists
            with open(path, "w") as f:
                f.write("Equity curve PNG not generated (matplotlib missing)")
            return

        timestamps = [p.timestamp for p in result.equity_curve]
        equities = [p.equity for p in result.equity_curve]
        exposures = [p.total_exposure for p in result.equity_curve]

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

        ax1.plot(timestamps, equities, label="Equity", color="blue")
        ax1.axhline(y=result.initial_equity, color="gray", linestyle="--", alpha=0.5)
        ax1.fill_between(
            timestamps, result.initial_equity, equities,
            where=[e >= result.initial_equity for e in equities],
            interpolate=True, color="green", alpha=0.15,
        )
        ax1.fill_between(
            timestamps, result.initial_equity, equities,
            where=[e < result.initial_equity for e in equities],
            interpolate=True, color="red", alpha=0.15,
        )
        ax1.set_ylabel("Equity ($)")
        ax1.set_title(f"Equity Curve — P&L ${result.total_pnl:+.2f}")
        ax1.legend(loc="upper left")
        ax1.grid(True, alpha=0.3)

        ax2.plot(timestamps, exposures, label="Exposure", color="orange")
        ax2.set_xlabel("Timestamp (unix)")
        ax2.set_ylabel("Exposure (contracts)")
        ax2.legend(loc="upper left")
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
