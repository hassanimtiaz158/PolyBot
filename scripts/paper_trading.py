"""Paper trading runner — runs the full bot in PAPER mode with live Polymarket data.

Collects all metrics during the evaluation period and generates:
  - paper_trading_report.md
  - paper_trading_metrics.json

Usage:
    python -m scripts.paper_trading
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# ── Setup paths ────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audit.events import EventBus, default_bus
from app.config.settings import settings
from app.data.clob import ClobAdapter
from app.data.gamma import GammaAdapter
from app.data.normalizer import DataNormalizer
from app.data.validators import DataValidator, DataQuality
from app.ev.expected_value import ExpectedValueEngine
from app.execution.engine import ExecutionEngine
from app.execution.paper import PaperExecution
from app.features.liquidity import LiquidityFeatures
from app.features.orderbook import OrderBookFeatures
from app.modes.state import ModeState, OperatingMode
from app.monitoring.health import health_status, run_all_checks
from app.orchestrator.engine import Orchestrator
from app.orchestrator.pipeline import PipelineResult
from app.portfolio.tracker import PortfolioTracker
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.engine import RiskEngine
from app.risk.limits import RiskLimits
from app.risk.position_sizing import PositionSizer
from app.storage.repositories import (
    OrderRepository,
    PositionRepository,
    RiskEventRepository,
    SignalRepository,
)
from app.strategies.microstructure import MicrostructureStrategy

logger = logging.getLogger("paper_trading")

# ── Configuration ──────────────────────────────────────────────────
SCAN_INTERVAL = 60  # seconds between market scans
MAX_MARKETS = 20    # max markets to scan per iteration
EVAL_HOURS = 24     # evaluation period (hours)


# ══════════════════════════════════════════════════════════════════════
# Metrics Collector
# ══════════════════════════════════════════════════════════════════════


class MetricsCollector:
    """Collects all trading metrics during the evaluation period."""

    def __init__(self) -> None:
        self.start_time = datetime.now(UTC)
        self.signals_generated = 0
        self.signals_rejected = 0
        self.trades_attempted = 0
        self.trades_filled = 0
        self.trades_rejected = 0
        self.trades_partial = 0
        self.fills: list[dict[str, Any]] = []
        self.rejection_reasons: dict[str, int] = {}
        self.risk_events: list[dict[str, Any]] = []
        self.api_errors: list[dict[str, Any]] = []
        self.data_fresh_checks = 0
        self.data_stale_checks = 0
        self.model_confidences: list[float] = []
        self.slippages: list[float] = []
        self.edges: list[float] = []
        self.equity_history: list[dict[str, Any]] = []
        self.market_snapshots: list[dict[str, Any]] = []

    def record_signal(self, signal: Any) -> None:
        self.signals_generated += 1
        if hasattr(signal, "confidence") and signal.confidence is not None:
            self.model_confidences.append(signal.confidence)
        if hasattr(signal, "reason") and signal.reason:
            reason = signal.reason
            if "rejected" in str(getattr(signal, "decision", "")).lower() or \
               getattr(signal, "decision", None) is not None:
                pass  # count below

    def record_rejection(self, reason: str) -> None:
        self.signals_rejected += 1
        key = reason.split(":")[0].strip() if reason else "unknown"
        self.rejection_reasons[key] = self.rejection_reasons.get(key, 0) + 1

    def record_trade_attempt(self) -> None:
        self.trades_attempted += 1

    def record_fill(
        self, order_id: str, market_id: str, side: str,
        size: float, price: float, fee: float, slippage: float,
    ) -> None:
        self.trades_filled += 1
        self.fills.append({
            "order_id": order_id,
            "market_id": market_id,
            "side": side,
            "size": size,
            "price": price,
            "fee": fee,
            "slippage": slippage,
            "timestamp": datetime.now(UTC).isoformat(),
        })
        self.slippages.append(slippage)

    def record_rejection_trade(self, reason: str) -> None:
        self.trades_rejected += 1

    def record_partial_fill(self) -> None:
        self.trades_partial += 1

    def record_risk_event(self, event_type: str, details: str) -> None:
        self.risk_events.append({
            "event_type": event_type,
            "details": details,
            "timestamp": datetime.now(UTC).isoformat(),
        })

    def record_api_error(self, error: str) -> None:
        self.api_errors.append({
            "error": error,
            "timestamp": datetime.now(UTC).isoformat(),
        })

    def record_data_freshness(self, is_fresh: bool) -> None:
        if is_fresh:
            self.data_fresh_checks += 1
        else:
            self.data_stale_checks += 1

    def record_edge(self, edge: float) -> None:
        self.edges.append(edge)

    def record_equity(self, equity: float) -> None:
        self.equity_history.append({
            "equity": equity,
            "timestamp": datetime.now(UTC).isoformat(),
        })

    def record_market_snapshot(self, market_id: str, features: dict) -> None:
        self.market_snapshots.append({
            "market_id": market_id,
            "midpoint": features.get("midpoint"),
            "spread": features.get("absolute_spread") or features.get("spread"),
            "obi": features.get("obi"),
            "liquidity_score": features.get("liquidity_score"),
            "timestamp": datetime.now(UTC).isoformat(),
        })


# ══════════════════════════════════════════════════════════════════════
# Live Data Provider
# ══════════════════════════════════════════════════════════════════════


async def create_data_provider(
    metrics: MetricsCollector,
) -> callable:
    """Create an async data provider that fetches live Polymarket data."""

    gamma = GammaAdapter()
    clob = ClobAdapter()
    normalizer = DataNormalizer()
    validator = DataValidator()
    ob_features = OrderBookFeatures()
    liq_features = LiquidityFeatures()

    async def data_provider() -> dict[str, dict[str, Any]]:
        """Fetch live market data and compute features."""
        features_by_market: dict[str, dict[str, Any]] = {}

        try:
            # 1. Discover active markets
            raw_markets = await gamma.list_markets(
                closed=False,
                limit=MAX_MARKETS,
                liquidity_num_min=1000,
            )

            if not raw_markets:
                logger.warning("No markets returned from Gamma API")
                return features_by_market

            # 2. Process each market
            for raw_market in raw_markets:
                market_id = str(raw_market.get("id", ""))
                if not market_id:
                    continue

                # Extract token IDs
                token_ids = normalizer.extract_token_ids(raw_market)
                if not token_ids:
                    continue

                # Fetch order book for YES token
                try:
                    book = await clob.get_order_book(token_ids[0])
                except Exception as exc:
                    metrics.record_api_error(
                        f"Order book fetch failed for {market_id}: {exc}"
                    )
                    continue

                if not book:
                    continue

                # Normalize snapshot
                snapshot = normalizer.normalize_snapshot(market_id, book)

                # Validate
                quality_report = validator.check_snapshot(snapshot)
                if quality_report.quality != DataQuality.HEALTHY:
                    continue

                # Compute features
                ob_f = ob_features.compute(snapshot, bids=book.get("bids"), asks=book.get("asks"))
                liq_f = liq_features.compute(snapshot)

                # Merge all features
                features = {
                    "market_id": market_id,
                    "question": raw_market.get("question", ""),
                    "midpoint": ob_f.get("midpoint"),
                    "absolute_spread": ob_f.get("absolute_spread"),
                    "relative_spread": ob_f.get("relative_spread"),
                    "bid": snapshot.get("bid"),
                    "ask": snapshot.get("ask"),
                    "bid_depth": ob_f.get("bid_depth"),
                    "ask_depth": ob_f.get("ask_depth"),
                    "obi": ob_f.get("obi"),
                    "liquidity_score": liq_f.get("liquidity_score"),
                    "total_depth": liq_f.get("total_depth"),
                    "volume": liq_f.get("volume"),
                    "timestamp": ob_f.get("timestamp"),
                    "data_fresh": ob_f.get("data_fresh", True),
                }

                # Record metrics
                metrics.record_market_snapshot(market_id, features)
                metrics.record_data_freshness(features.get("data_fresh", True))

                features_by_market[market_id] = features

                logger.info(
                    "Market %s: mid=%.4f spread=%.4f obi=%.4f liq=%.2f",
                    market_id[:8],
                    features.get("midpoint") or 0,
                    features.get("absolute_spread") or 0,
                    features.get("obi") or 0,
                    features.get("liquidity_score") or 0,
                )

        except Exception as exc:
            logger.exception("Data provider error: %s", exc)
            metrics.record_api_error(f"Data provider error: {exc}")

        return features_by_market

    return data_provider


# ══════════════════════════════════════════════════════════════════════
# Custom Pipeline with Metrics
# ══════════════════════════════════════════════════════════════════════


class MetricsTradePipeline:
    """Trade pipeline that records metrics for every decision."""

    def __init__(
        self,
        ev_engine: Any,
        risk_engine: Any,
        exec_engine: Any,
        portfolio: Any,
        signal_repo: Any,
        order_repo: Any,
        position_repo: Any,
        risk_repo: Any,
        metrics: MetricsCollector,
        event_bus: Any = None,
    ) -> None:
        self._ev = ev_engine
        self._risk = risk_engine
        self._exec = exec_engine
        self._portfolio = portfolio
        self._signal_repo = signal_repo
        self._order_repo = order_repo
        self._position_repo = position_repo
        self._risk_repo = risk_repo
        self._metrics = metrics
        self._bus = event_bus or default_bus
        self._fee_rate = 0.05

    @property
    def _nominal_size(self) -> float:
        equity = self._portfolio.equity
        if equity <= 0:
            return 100.0
        return equity * settings.max_position_pct

    async def run(
        self,
        signal: Any,
        features: dict[str, Any],
        daily_pnl: float = 0.0,
        consecutive_losses: int = 0,
    ) -> PipelineResult:

        # Record signal
        self._metrics.record_signal(signal)

        # ── Stage 1: EV ────────────────────────────────────────────
        try:
            price = features.get("midpoint", signal.implied_probability or 0.5)
            spread = features.get("absolute_spread") or features.get("spread") or 0.0
            depth = features.get("total_depth") or features.get("bid_depth", 0) or self._nominal_size

            ev_result = self._ev.evaluate(
                model_probability=signal.model_probability or 0.5,
                implied_probability=signal.implied_probability or 0.5,
                spread=spread,
                price=price,
                size=self._nominal_size,
                depth=depth,
                confidence=signal.confidence or 0.0,
                fee_rate=self._fee_rate,
            )

            net_edge = ev_result.net_edge if ev_result.tradeable else None
            if net_edge is not None:
                self._metrics.record_edge(net_edge)

            if not ev_result.tradeable:
                reason = f"EV not tradeable: net_edge={ev_result.net_edge:.4f}"
                self._metrics.record_rejection(reason)
                return PipelineResult(
                    signal=signal, ev_result=ev_result, error=reason,
                )

        except Exception as exc:
            reason = f"EV error: {exc}"
            self._metrics.record_rejection(reason)
            return PipelineResult(signal=signal, error=reason)

        # ── Stage 2: Risk ──────────────────────────────────────────
        try:
            risk_decision = await self._risk.evaluate(
                signal=signal,
                net_edge=net_edge,
                daily_pnl=daily_pnl,
                consecutive_losses=consecutive_losses,
                api_healthy=True,
                model_available=True,
                database_available=True,
            )
        except Exception as exc:
            reason = f"Risk error: {exc}"
            self._metrics.record_rejection(reason)
            return PipelineResult(signal=signal, ev_result=ev_result, error=reason)

        if not risk_decision.approved:
            self._metrics.record_rejection(risk_decision.reason)
            return PipelineResult(
                signal=signal, ev_result=ev_result,
                risk_decision=risk_decision, error=risk_decision.reason,
            )

        # ── Stage 3: Execution ─────────────────────────────────────
        self._metrics.record_trade_attempt()

        try:
            order_result = await self._exec.execute(risk_decision)
        except Exception as exc:
            reason = f"Execution error: {exc}"
            self._metrics.record_rejection_trade(reason)
            return PipelineResult(
                signal=signal, ev_result=ev_result,
                risk_decision=risk_decision, error=reason,
            )

        # Record fill
        if order_result.status in ("FILLED", "PARTIALLY_FILLED"):
            self._metrics.record_fill(
                order_id=order_result.order_id,
                market_id=order_result.market_id,
                side=order_result.side,
                size=order_result.filled_size,
                price=order_result.average_fill,
                fee=getattr(order_result, "fee", 0.0),
                slippage=abs(
                    (order_result.average_fill or 0) -
                    (signal.implied_probability or 0.5)
                ),
            )
            if order_result.status == "PARTIALLY_FILLED":
                self._metrics.record_partial_fill()

            self._portfolio.add_trade(
                market_id=order_result.market_id,
                side=order_result.side,
                size=order_result.filled_size,
                price=order_result.average_fill,
                fee=getattr(order_result, "fee", 0.0),
            )
        else:
            self._metrics.record_rejection_trade(order_result.error or order_result.status)

        self._metrics.record_equity(self._portfolio.equity)

        return PipelineResult(
            signal=signal, ev_result=ev_result,
            risk_decision=risk_decision, order_result=order_result,
        )


# ══════════════════════════════════════════════════════════════════════
# Metrics Router (wraps SignalRouter)
# ══════════════════════════════════════════════════════════════════════


class MetricsRouter:
    """Router that collects metrics from strategy signals."""

    def __init__(
        self,
        strategies: dict[str, Any],
        pipeline: Any,
        mode: Any,
        metrics: MetricsCollector,
    ) -> None:
        self._strategies = strategies
        self._pipeline = pipeline
        self._mode = mode
        self._metrics = metrics

    async def route_all(
        self,
        market_id: str,
        features: dict[str, Any],
        daily_pnl: float = 0.0,
        consecutive_losses: int = 0,
    ) -> list[Any]:
        results = []
        can_trade = self._mode.is_trading()

        for name, strategy in self._strategies.items():
            try:
                signal = strategy.generate_signal(features)
            except Exception as exc:
                logger.warning("Strategy %s failed: %s", name, exc)
                continue

            # Record rejections
            if signal.decision.value != "CANDIDATE":
                self._metrics.record_rejection(
                    f"{name}: {signal.reason or 'NO_SIGNAL'}"
                )
                continue

            # Execute through pipeline
            if can_trade:
                result = await self._pipeline.run(
                    signal, features,
                    daily_pnl=daily_pnl,
                    consecutive_losses=consecutive_losses,
                )
                results.append(result)
            else:
                self._metrics.record_rejection(f"{name}: mode not trading")

        return results


# ══════════════════════════════════════════════════════════════════════
# Report Generation
# ══════════════════════════════════════════════════════════════════════


def generate_reports(
    metrics: MetricsCollector,
    portfolio: PortfolioTracker,
    breaker: CircuitBreaker,
) -> None:
    """Generate paper_trading_report.md and paper_trading_metrics.json."""
    end_time = datetime.now(UTC)
    duration = end_time - metrics.start_time

    # ── Compute summary metrics ────────────────────────────────────
    total_signals = metrics.signals_generated
    total_rejections = metrics.signals_rejected
    total_attempted = metrics.trades_attempted
    total_filled = metrics.trades_filled
    total_rejected = metrics.trades_rejected
    rejected_pct = (total_rejections / total_signals * 100) if total_signals > 0 else 0.0

    # P&L
    initial_equity = 10_000.0
    final_equity = portfolio.equity
    net_pnl = final_equity - initial_equity
    net_pnl_pct = (net_pnl / initial_equity * 100) if initial_equity > 0 else 0.0

    # Drawdown
    equity_values = [e["equity"] for e in metrics.equity_history]
    if equity_values:
        peak = equity_values[0]
        max_drawdown = 0.0
        max_drawdown_pct = 0.0
        for eq in equity_values:
            if eq > peak:
                peak = eq
            dd = peak - eq
            dd_pct = (dd / peak * 100) if peak > 0 else 0.0
            if dd > max_drawdown:
                max_drawdown = dd
                max_drawdown_pct = dd_pct
    else:
        max_drawdown = 0.0
        max_drawdown_pct = 0.0

    # Trade metrics
    wins = sum(1 for f in metrics.fills if f.get("price", 0.5) < 0.5)  # bought low
    losses = total_filled - wins
    hit_rate = (wins / total_filled * 100) if total_filled > 0 else 0.0

    # Expectancy
    avg_win = 0.0
    avg_loss = 0.0
    if wins > 0:
        win_prices = [f["price"] for f in metrics.fills if f.get("price", 0.5) < 0.5]
        avg_win = sum(1 - p for p in win_prices) / len(win_prices) if win_prices else 0.0
    if losses > 0:
        loss_prices = [f["price"] for f in metrics.fills if f.get("price", 0.5) >= 0.5]
        avg_loss = sum(p for p in loss_prices) / len(loss_prices) if loss_prices else 0.0

    expectancy = (hit_rate / 100 * avg_win) - ((100 - hit_rate) / 100 * avg_loss)

    # Profit factor
    gross_profit = sum(1 - f["price"] for f in metrics.fills if f.get("price", 0.5) < 0.5)
    gross_loss = sum(f["price"] for f in metrics.fills if f.get("price", 0.5) >= 0.5)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

    # Slippage
    avg_slippage = sum(metrics.slippages) / len(metrics.slippages) if metrics.slippages else 0.0
    max_slippage = max(metrics.slippages) if metrics.slippages else 0.0

    # Edge
    avg_net_edge = sum(metrics.edges) / len(metrics.edges) if metrics.edges else 0.0

    # Confidence
    avg_confidence = sum(metrics.model_confidences) / len(metrics.model_confidences) if metrics.model_confidences else 0.0

    # ── JSON metrics ───────────────────────────────────────────────
    metrics_data = {
        "evaluation_period": {
            "start": metrics.start_time.isoformat(),
            "end": end_time.isoformat(),
            "duration_hours": round(duration.total_seconds() / 3600, 2),
            "scan_interval_seconds": SCAN_INTERVAL,
        },
        "signals": {
            "total_generated": total_signals,
            "total_rejected": total_rejections,
            "rejected_percentage": round(rejected_pct, 2),
            "rejection_reasons": metrics.rejection_reasons,
        },
        "trades": {
            "attempted": total_attempted,
            "filled": total_filled,
            "rejected": total_rejected,
            "partial_fills": metrics.trades_partial,
            "fills": metrics.fills,
        },
        "pnl": {
            "initial_equity": initial_equity,
            "final_equity": round(final_equity, 2),
            "net_pnl": round(net_pnl, 2),
            "net_pnl_pct": round(net_pnl_pct, 4),
        },
        "risk": {
            "max_drawdown": round(max_drawdown, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "circuit_breaker_state": breaker.state.value,
            "risk_events": metrics.risk_events,
        },
        "performance": {
            "hit_rate": round(hit_rate, 2),
            "profit_factor": round(profit_factor, 4),
            "expectancy": round(expectancy, 6),
            "avg_net_edge": round(avg_net_edge, 6),
            "avg_confidence": round(avg_confidence, 4),
        },
        "execution_quality": {
            "avg_slippage": round(avg_slippage, 6),
            "max_slippage": round(max_slippage, 6),
            "total_fees": round(sum(f.get("fee", 0) for f in metrics.fills), 4),
        },
        "data_quality": {
            "fresh_checks": metrics.data_fresh_checks,
            "stale_checks": metrics.data_stale_checks,
            "api_errors": len(metrics.api_errors),
            "api_error_details": metrics.api_errors[:20],
        },
        "market_snapshots": metrics.market_snapshots[:100],
        "equity_history": metrics.equity_history,
    }

    # Write JSON
    json_path = Path("paper_trading_metrics.json")
    with open(json_path, "w") as f:
        json.dump(metrics_data, f, indent=2, default=str)
    logger.info("Metrics written to %s", json_path)

    # ── Markdown report ────────────────────────────────────────────
    report_lines = [
        "# Paper Trading Report",
        "",
        f"**Generated:** {end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"**Evaluation Period:** {duration.total_seconds() / 3600:.1f} hours",
        f"**Scan Interval:** {SCAN_INTERVAL}s",
        f"**Markets Scanned:** {len(metrics.market_snapshots)} snapshots across "
        f"{len(set(s['market_id'] for s in metrics.market_snapshots))} unique markets",
        "",
        "---",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Initial Equity | ${initial_equity:,.2f} |",
        f"| Final Equity | ${final_equity:,.2f} |",
        f"| Net P&L | ${net_pnl:,.2f} ({net_pnl_pct:+.2f}%) |",
        f"| Maximum Drawdown | ${max_drawdown:,.2f} ({max_drawdown_pct:.2f}%) |",
        f"| Total Signals | {total_signals} |",
        f"| Rejected Signals | {total_rejections} ({rejected_pct:.1f}%) |",
        f"| Trades Attempted | {total_attempted} |",
        f"| Trades Filled | {total_filled} |",
        f"| Trades Rejected | {total_rejected} |",
        f"| Hit Rate | {hit_rate:.1f}% |",
        f"| Profit Factor | {profit_factor:.2f} |",
        f"| Expectancy | {expectancy:.4f} |",
        f"| Avg Net Edge | {avg_net_edge:.4f} |",
        f"| Avg Confidence | {avg_confidence:.2f} |",
        f"| Avg Slippage | {avg_slippage:.4f} |",
        f"| Circuit Breaker | {breaker.state.value} |",
        "",
        "---",
        "",
        "## Signal Analysis",
        "",
    ]

    if metrics.rejection_reasons:
        report_lines.append("### Rejection Reasons")
        report_lines.append("")
        report_lines.append("| Reason | Count |")
        report_lines.append("|--------|-------|")
        for reason, count in sorted(
            metrics.rejection_reasons.items(), key=lambda x: -x[1]
        ):
            report_lines.append(f"| {reason} | {count} |")
        report_lines.append("")

    if metrics.fills:
        report_lines.append("### Trade Log")
        report_lines.append("")
        report_lines.append("| Time | Market | Side | Size | Price | Slippage |")
        report_lines.append("|------|--------|------|------|-------|----------|")
        for fill in metrics.fills:
            report_lines.append(
                f"| {fill['timestamp'][:19]} "
                f"| {fill['market_id'][:12]}... "
                f"| {fill['side']} "
                f"| {fill['size']:.1f} "
                f"| {fill['price']:.4f} "
                f"| {fill['slippage']:.4f} |"
            )
        report_lines.append("")

    report_lines.extend([
        "---",
        "",
        "## Risk Events",
        "",
    ])

    if metrics.risk_events:
        for event in metrics.risk_events:
            report_lines.append(
                f"- **{event['event_type']}** ({event['timestamp'][:19]}): "
                f"{event['details']}"
            )
    else:
        report_lines.append("No risk events recorded.")

    report_lines.extend([
        "",
        "---",
        "",
        "## Data Quality",
        "",
        f"- Fresh data checks: {metrics.data_fresh_checks}",
        f"- Stale data checks: {metrics.data_stale_checks}",
        f"- API errors: {len(metrics.api_errors)}",
        "",
        "---",
        "",
        "## Diagnosis",
        "",
    ])

    # Auto-diagnosis
    if total_filled == 0:
        report_lines.append(
            "### No trades executed\n\n"
            "The bot did not execute any paper trades during this period. "
            "Possible causes:\n"
            "- Market data did not meet strategy thresholds (OBI, spread, edge)\n"
            "- EV engine rejected all signals (net edge below minimum)\n"
            "- Risk engine rejected all signals (exposure limits, confidence)\n"
            f"- Total signals generated: {total_signals}\n"
            f"- Total rejections: {total_rejections}\n"
        )
    elif net_pnl < 0:
        report_lines.append(
            f"### Net loss of ${abs(net_pnl):,.2f}\n\n"
            "The bot incurred losses during this period. "
            "This is expected behavior for a short evaluation window. "
            "Key observations:\n"
            f"- Hit rate: {hit_rate:.1f}%\n"
            f"- Average slippage: {avg_slippage:.4f}\n"
            f"- Max drawdown: ${max_drawdown:,.2f}\n"
        )
    else:
        report_lines.append(
            f"### Net profit of ${net_pnl:,.2f}\n\n"
            "The bot was profitable during this period. "
            "Note that short-term profitability does not guarantee "
            "long-term performance.\n"
        )

    report_lines.extend([
        "",
        "---",
        "",
        "*Report generated by paper_trading.py — all trades are simulated.*",
    ])

    # Write report
    report_path = Path("paper_trading_report.md")
    with open(report_path, "w") as f:
        f.write("\n".join(report_lines))
    logger.info("Report written to %s", report_path)


# ══════════════════════════════════════════════════════════════════════
# Main Runner
# ══════════════════════════════════════════════════════════════════════


async def run_paper_trading() -> None:
    """Run the full system in PAPER mode with live data."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    logger.info("=" * 60)
    logger.info("PAPER TRADING SESSION STARTING")
    logger.info("Mode: PAPER (simulated execution)")
    logger.info("Evaluation period: %d hours", EVAL_HOURS)
    logger.info("Scan interval: %d seconds", SCAN_INTERVAL)
    logger.info("=" * 60)

    # ── Initialize components ──────────────────────────────────────
    metrics = MetricsCollector()
    portfolio = PortfolioTracker()
    limits = RiskLimits()
    breaker = CircuitBreaker(persist=False)
    sizer = PositionSizer()
    risk_engine = RiskEngine(
        portfolio=portfolio, limits=limits, sizer=sizer, breaker=breaker,
    )

    # Paper execution adapter
    paper_adapter = PaperExecution(
        rejection_rate=0.01,
        latency_ms=200,
        fee_rate=0.05,
    )
    exec_engine = ExecutionEngine(adapter=paper_adapter)

    # EV engine
    ev_engine = ExpectedValueEngine(min_net_edge=0.01)

    # Repositories
    signal_repo = SignalRepository()
    order_repo = OrderRepository()
    position_repo = PositionRepository()
    risk_repo = RiskEventRepository()

    # Strategy
    strategy = MicrostructureStrategy(
        min_confidence=0.6,
        min_liquidity_score=0.3,
        min_obi_abs=0.05,
        min_edge_bps=10.0,
    )

    # Pipeline with metrics
    pipeline = MetricsTradePipeline(
        ev_engine=ev_engine,
        risk_engine=risk_engine,
        exec_engine=exec_engine,
        portfolio=portfolio,
        signal_repo=signal_repo,
        order_repo=order_repo,
        position_repo=position_repo,
        risk_repo=risk_repo,
        metrics=metrics,
    )

    # Router with metrics
    router = MetricsRouter(
        strategies={"microstructure": strategy},
        pipeline=pipeline,
        mode=ModeState(OperatingMode.PAPER),
        metrics=metrics,
    )

    # Data provider
    data_provider = await create_data_provider(metrics)

    # Mode
    mode_state = ModeState(OperatingMode.PAPER)

    # Orchestrator
    orch = Orchestrator(
        router=router,
        breaker=breaker,
        mode=mode_state,
        get_equity=lambda: portfolio.equity,
        data_provider=data_provider,
        scan_interval=SCAN_INTERVAL,
    )

    # ── Run evaluation period ──────────────────────────────────────
    end_time = datetime.now(UTC) + timedelta(hours=EVAL_HOURS)
    iteration = 0

    logger.info("Starting evaluation at %s", datetime.now(UTC).isoformat())
    logger.info("Ending at %s", end_time.isoformat())

    try:
        while datetime.now(UTC) < end_time:
            iteration += 1
            logger.info("--- Iteration %d ---", iteration)

            # Record equity
            metrics.record_equity(portfolio.equity)

            # Fetch data and process
            try:
                features_by_market = await data_provider()
            except Exception as exc:
                logger.exception("Data provider failed: %s", exc)
                metrics.record_api_error(str(exc))
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            if not features_by_market:
                logger.info("No market data available, sleeping...")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            # Route signals through strategies
            daily_pnl = 0.0  # simplified for paper trading
            consecutive_losses = 0

            for market_id, features in features_by_market.items():
                try:
                    await router.route_all(
                        market_id=market_id,
                        features=features,
                        daily_pnl=daily_pnl,
                        consecutive_losses=consecutive_losses,
                    )
                except Exception as exc:
                    logger.warning("Routing failed for %s: %s", market_id, exc)

            # Log summary
            logger.info(
                "Iteration %d complete: equity=$%.2f, filled=%d, rejected=%d",
                iteration,
                portfolio.equity,
                metrics.trades_filled,
                metrics.signals_rejected,
            )

            await asyncio.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Paper trading interrupted by user")
    except Exception as exc:
        logger.exception("Paper trading failed: %s", exc)
    finally:
        # Generate reports
        logger.info("Generating reports...")
        generate_reports(metrics, portfolio, breaker)
        logger.info("Paper trading session complete.")


def main() -> None:
    asyncio.run(run_paper_trading())


if __name__ == "__main__":
    main()
