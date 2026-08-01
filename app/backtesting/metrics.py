"""Backtest metrics computation.

All metrics are computed *after* the backtest run from the recorded
fills, trades, and equity curve.  No look-ahead possible.
"""

from __future__ import annotations

import math

from app.backtesting.models import BacktestResult, EquityPoint, FillRecord, TradeRecord

_EPS = 1e-12


class MetricsCalculator:
    """Compute all backtest metrics from the recording artefacts.

    Parameters
    ----------
    initial_equity : float
    equity_curve : list[EquityPoint]
        Portfolio snapshots recorded after *every* processed snapshot.
    fills : list[FillRecord]
        Every fill event in chronological order.
    """

    def __init__(
        self,
        initial_equity: float,
        equity_curve: list[EquityPoint],
        fills: list[FillRecord],
    ) -> None:
        self._initial_equity = max(initial_equity, _EPS)
        self._curve = equity_curve
        self._fills = fills

    # ── Public entry point ──────────────────────────────────────────

    def compute(self) -> BacktestResult:
        """Compute all metrics and return a ``BacktestResult``."""
        trades = self._build_trades()
        (
            num_wins, num_losses, gross_profit, gross_loss,
        ) = self._win_loss_stats(trades)
        num_trades = len(trades)
        win_rate = num_wins / num_trades if num_trades > 0 else 0.0
        loss_rate = num_losses / num_trades if num_trades > 0 else 0.0
        profit_factor = (
            gross_profit / abs(gross_loss)
            if abs(gross_loss) > _EPS
            else float("inf") if gross_profit > 0 else 0.0
        )
        expectancy = (
            (gross_profit + gross_loss) / num_trades if num_trades > 0 else 0.0
        )

        final_equity = (
            self._curve[-1].equity if self._curve else self._initial_equity
        )
        total_pnl = final_equity - self._initial_equity
        total_return = total_pnl / self._initial_equity

        max_dd_val, max_dd_pct = self._max_drawdown()
        sharpe = self._sharpe_ratio()
        sortino = self._sortino_ratio()
        turnover = self._turnover()
        avg_hold = self._avg_holding_period(trades)
        avg_edge = self._avg_net_edge()
        calib = self._calibration_score()
        slippage_impact = self._slippage_impact()
        total_fees = sum(f.fee for f in self._fills)

        return BacktestResult(
            initial_equity=self._initial_equity,
            final_equity=final_equity,
            total_return=total_return,
            total_pnl=total_pnl,
            max_drawdown=max_dd_val,
            max_drawdown_pct=max_dd_pct,
            win_rate=win_rate,
            loss_rate=loss_rate,
            profit_factor=profit_factor,
            expectancy=expectancy,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            turnover=turnover,
            avg_holding_period=avg_hold,
            avg_net_edge=avg_edge,
            calibration_score=calib,
            slippage_impact=slippage_impact,
            num_trades=num_trades,
            num_wins=num_wins,
            num_losses=num_losses,
            total_fees=total_fees,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            equity_curve=list(self._curve),
            fills=list(self._fills),
            trades=trades,
        )

    # ── Trade building ──────────────────────────────────────────────

    def _build_trades(self) -> list[TradeRecord]:
        """Pair fills into open-close trades using side alternation.

        Tracks open positions per market.  A fill with the **same**
        side as the oldest open trade is treated as an addition to
        the position.  A fill with the **opposite** side closes/
        reduces the oldest open trade (FIFO).
        """
        trades: list[TradeRecord] = []
        open_trades: dict[str, list[TradeRecord]] = {}

        for fill in self._fills:
            key = fill.market_id
            existing = open_trades.get(key, [])

            if not existing:
                # No open position → opening fill
                open_trades[key] = [TradeRecord(
                    market_id=fill.market_id,
                    side=fill.side,
                    entry_time=fill.timestamp,
                    exit_time=fill.timestamp,
                    entry_price=fill.price,
                    exit_price=fill.price,
                    size=fill.size,
                    pnl=0.0,
                    fees=fill.fee,
                    holding_period=0.0,
                    edge=fill.edge,
                )]
            elif fill.side == existing[0].side:
                # Same side → adding to position
                existing.append(TradeRecord(
                    market_id=fill.market_id,
                    side=fill.side,
                    entry_time=fill.timestamp,
                    exit_time=fill.timestamp,
                    entry_price=fill.price,
                    exit_price=fill.price,
                    size=fill.size,
                    pnl=0.0,
                    fees=fill.fee,
                    holding_period=0.0,
                    edge=fill.edge,
                ))
            else:
                # Opposite side → closing/reducing the oldest trade
                opened = existing.pop(0)
                holding_period = fill.timestamp - opened.entry_time
                opened.exit_time = fill.timestamp
                opened.exit_price = fill.price
                opened.pnl = fill.pnl_change
                opened.fees += fill.fee
                opened.holding_period = holding_period
                trades.append(opened)
                if not existing:
                    del open_trades[key]

        return trades

    # ── Individual metrics ───────────────────────────────────────────

    @staticmethod
    def _win_loss_stats(
        trades: list[TradeRecord],
    ) -> tuple[int, int, float, float]:
        num_wins = 0
        num_losses = 0
        gross_profit = 0.0
        gross_loss = 0.0
        for t in trades:
            if t.pnl >= 0:
                num_wins += 1
                gross_profit += t.pnl
            else:
                num_losses += 1
                gross_loss += t.pnl
        return num_wins, num_losses, gross_profit, gross_loss

    def _max_drawdown(self) -> tuple[float, float]:
        """Maximum drawdown in value and percent."""
        if len(self._curve) < 2:
            return 0.0, 0.0
        peak = self._curve[0].equity
        max_dd_val = 0.0
        for pt in self._curve:
            if pt.equity > peak:
                peak = pt.equity
            dd = peak - pt.equity
            if dd > max_dd_val:
                max_dd_val = dd
        max_dd_pct = (
            max_dd_val / peak * 100.0 if peak > _EPS else 0.0
        )
        return max_dd_val, max_dd_pct

    def _sharpe_ratio(self) -> float:
        """Annualised Sharpe (daily returns, risk-free ≈ 0)."""
        returns = self._period_returns()
        if len(returns) < 2:
            return 0.0
        mean_r = sum(returns) / len(returns)
        std_r = math.sqrt(
            sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        )
        if std_r < _EPS:
            return 0.0
        # Annualise: periods are per-snapshot, ~365*24*60/interval
        # Use sqrt of number of periods as a rough scalar
        return (mean_r / std_r) * math.sqrt(len(returns))

    def _sortino_ratio(self) -> float:
        """Annualised Sortino (downside deviation only)."""
        returns = self._period_returns()
        if len(returns) < 2:
            return 0.0
        mean_r = sum(returns) / len(returns)
        downside = [r for r in returns if r < 0]
        if len(downside) < 1:
            return 0.0
        dd_std = math.sqrt(
            sum(r * r for r in downside) / len(downside)
        )
        if dd_std < _EPS:
            return 0.0
        return (mean_r / dd_std) * math.sqrt(len(returns))

    def _turnover(self) -> float:
        """Total traded volume / average equity."""
        if not self._curve:
            return 0.0
        total_volume = sum(f.size * f.price for f in self._fills)
        avg_equity = sum(p.equity for p in self._curve) / len(self._curve)
        if avg_equity < _EPS:
            return 0.0
        return total_volume / avg_equity

    @staticmethod
    def _avg_holding_period(trades: list[TradeRecord]) -> float:
        if not trades:
            return 0.0
        return sum(t.holding_period for t in trades) / len(trades)

    def _avg_net_edge(self) -> float:
        edges = [f.edge for f in self._fills if f.edge is not None]
        if not edges:
            return 0.0
        return sum(edges) / len(edges)

    def _calibration_score(self) -> float:
        """Brier score for edge vs outcome (0 = perfect).

        Uses a simplified outcome model: trades with positive P&L
        are "correct" (predicted direction was right), negative are
        "wrong".  Compares |edge| vs |pnl| magnitude as a proxy for
        calibration.
        """
        trades = self._build_trades()
        if len(trades) < 2:
            return 0.0
        scores = []
        for t in trades:
            predicted = 0.5 + (t.edge / 2.0) if t.edge is not None else 0.5
            predicted = max(0.01, min(0.99, predicted))
            outcome = 1.0 if t.pnl > 0 else 0.0
            scores.append((predicted - outcome) ** 2)
        return sum(scores) / len(scores)

    def _slippage_impact(self) -> float:
        """Total slippage cost as fraction of gross P&L."""
        total_slippage = sum(f.slippage * f.size for f in self._fills)
        total_pnl = (
            self._curve[-1].equity - self._initial_equity
            if self._curve
            else 0.0
        )
        if abs(total_pnl) < _EPS:
            return 0.0
        return total_slippage / abs(total_pnl)

    def _period_returns(self) -> list[float]:
        """Compute per-snapshot return as fraction."""
        if len(self._curve) < 2:
            return []
        returns = []
        for i in range(1, len(self._curve)):
            prev = self._curve[i - 1].equity
            curr = self._curve[i].equity
            if prev > _EPS:
                returns.append((curr - prev) / prev)
        return returns
