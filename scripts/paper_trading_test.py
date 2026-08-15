"""Short paper trading test — runs 3 iterations with live Polymarket data.

Generates paper_trading_report.md and paper_trading_metrics.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.clob import ClobAdapter
from app.data.gamma import GammaAdapter
from app.data.normalizer import DataNormalizer
from app.data.validators import DataQuality, DataValidator
from app.ev.expected_value import ExpectedValueEngine
from app.execution.engine import ExecutionEngine
from app.execution.paper import PaperExecution
from app.features.liquidity import LiquidityFeatures
from app.features.orderbook import OrderBookFeatures
from app.modes.state import ModeState, OperatingMode
from app.orchestrator.pipeline import PipelineResult
from app.portfolio.tracker import PortfolioTracker
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.engine import RiskEngine
from app.risk.limits import RiskLimits
from app.risk.position_sizing import PositionSizer
from app.strategies.microstructure import MicrostructureStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("paper_test")

MAX_MARKETS = 15
SCAN_INTERVAL = 30
NUM_ITERATIONS = 3


# ── Metrics ────────────────────────────────────────────────────────


class Metrics:
    def __init__(self) -> None:
        self.start = datetime.now(UTC)
        self.signals = 0
        self.rejections: dict[str, int] = {}
        self.fills: list[dict] = []
        self.equity_curve: list[dict] = []
        self.edges: list[float] = []
        self.confidences: list[float] = []
        self.slippages: list[float] = []
        self.api_errors: list[str] = []
        self.market_count = 0
        self.snapshots: list[dict] = []
        self.risk_events: list[dict] = []

    def record_rejection(self, reason: str) -> None:
        key = reason.split(":")[0].strip()[:60]
        self.rejections[key] = self.rejections.get(key, 0) + 1

    def record_fill(self, fill: dict) -> None:
        self.fills.append(fill)
        if fill.get("slippage") is not None:
            self.slippages.append(fill["slippage"])

    def record_equity(self, eq: float) -> None:
        self.equity_curve.append({"equity": eq, "ts": datetime.now(UTC).isoformat()})

    def record_edge(self, e: float) -> None:
        self.edges.append(e)

    def record_confidence(self, c: float) -> None:
        self.confidences.append(c)


# ── Data Provider ──────────────────────────────────────────────────


async def make_provider(m: Metrics) -> Any:
    gamma = GammaAdapter()
    clob = ClobAdapter()
    norm = DataNormalizer()
    val = DataValidator(max_age_seconds=120)  # 2 minutes — CLOB data may lag
    obf = OrderBookFeatures()
    lf = LiquidityFeatures()

    async def provider() -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        try:
            markets = await gamma.list_markets(
                closed=False, limit=MAX_MARKETS, liquidity_num_min=1000,
            )
            logger.info("Gamma returned %d markets", len(markets))

            for raw in markets:
                mid = str(raw.get("id", ""))
                if not mid:
                    continue
                tids = norm.extract_token_ids(raw)
                if not tids:
                    continue
                try:
                    book = await clob.get_order_book(tids[0])
                except Exception as exc:
                    m.api_errors.append(f"{mid[:8]}: {exc}")
                    continue
                if not book:
                    continue

                snap = norm.normalize_snapshot(mid, book)
                qr = val.check_snapshot(snap)
                if qr.quality != DataQuality.HEALTHY:
                    logger.warning(
                        "Snapshot %s quality=%s reason=%s snap=%s",
                        mid[:8], qr.quality.value, qr.reason,
                        {k: v for k, v in snap.items() if k != 'volume'},
                    )
                    continue

                ob = obf.compute(snap, bids=book.get("bids"), asks=book.get("asks"))
                li = lf.compute(snap)

                features = {
                    "market_id": mid,
                    "question": raw.get("question", "")[:80],
                    "midpoint": ob.get("midpoint"),
                    "absolute_spread": ob.get("absolute_spread"),
                    "bid": snap.get("bid"),
                    "ask": snap.get("ask"),
                    "bid_depth": ob.get("bid_depth"),
                    "ask_depth": ob.get("ask_depth"),
                    "obi": ob.get("obi"),
                    "liquidity_score": li.get("liquidity_score"),
                    "total_depth": li.get("total_depth"),
                    "volume": li.get("volume"),
                    "timestamp": ob.get("timestamp"),
                    "data_fresh": ob.get("data_fresh", True),
                }
                out[mid] = features
                m.market_count += 1
                m.snapshots.append({
                    "market_id": mid,
                    "question": raw.get("question", "")[:60],
                    "midpoint": features.get("midpoint"),
                    "spread": features.get("absolute_spread"),
                    "obi": features.get("obi"),
                    "liq": features.get("liquidity_score"),
                })
        except Exception as exc:
            logger.exception("Provider error: %s", exc)
            m.api_errors.append(str(exc))
        return out

    return provider


# ── Pipeline ───────────────────────────────────────────────────────


async def run_pipeline(
    signal: Any, features: dict, ev_eng: Any, risk_eng: Any,
    exec_eng: Any, portfolio: Any, m: Metrics,
) -> PipelineResult:
    m.signals += 1
    if hasattr(signal, "confidence") and signal.confidence is not None:
        m.record_confidence(signal.confidence)

    # EV
    try:
        price = features.get("midpoint", signal.implied_probability or 0.5)
        spread = features.get("absolute_spread") or 0.0
        depth = features.get("total_depth") or 100.0
        ev = ev_eng.evaluate(
            model_probability=signal.model_probability or 0.5,
            implied_probability=signal.implied_probability or 0.5,
            spread=spread, price=price, size=100.0,
            depth=depth, confidence=signal.confidence or 0.0,
            fee_rate=0.05,
        )
        net_edge = ev.net_edge if ev.tradeable else None
        if net_edge is not None:
            m.record_edge(net_edge)
        if not ev.tradeable:
            reason = f"EV: net_edge={ev.net_edge:.4f}"
            m.record_rejection(reason)
            return PipelineResult(signal=signal, ev_result=ev, error=reason)
    except Exception as exc:
        m.record_rejection(f"EV error: {exc}")
        return PipelineResult(signal=signal, error=str(exc))

    # Risk
    try:
        rd = await risk_eng.evaluate(signal=signal, net_edge=net_edge)
    except Exception as exc:
        m.record_rejection(f"Risk error: {exc}")
        return PipelineResult(signal=signal, ev_result=ev, error=str(exc))
    if not rd.approved:
        m.record_rejection(rd.reason)
        return PipelineResult(signal=signal, ev_result=ev, risk_decision=rd, error=rd.reason)

    # Execute
    try:
        order = await exec_eng.execute(rd)
    except Exception as exc:
        m.record_rejection(f"Exec error: {exc}")
        return PipelineResult(signal=signal, ev_result=ev, risk_decision=rd, error=str(exc))

    if order.status in ("FILLED", "PARTIALLY_FILLED"):
        slippage = abs((order.average_fill or 0.5) - (signal.implied_probability or 0.5))
        fill = {
            "order_id": order.order_id, "market_id": order.market_id,
            "side": order.side, "size": order.filled_size,
            "price": order.average_fill, "slippage": round(slippage, 6),
            "ts": datetime.now(UTC).isoformat(),
        }
        m.record_fill(fill)
        portfolio.add_trade(
            market_id=order.market_id, side=order.side,
            size=order.filled_size, price=order.average_fill,
        )
    else:
        m.record_rejection(f"Order: {order.status} - {order.error}")

    m.record_equity(portfolio.equity)
    return PipelineResult(signal=signal, ev_result=ev, risk_decision=rd, order_result=order)


# ── Report ─────────────────────────────────────────────────────────


def write_reports(m: Metrics, portfolio: PortfolioTracker) -> None:
    end = datetime.now(UTC)
    dur = end - m.start
    eq = [e["equity"] for e in m.equity_curve]
    initial = 10000.0
    final = portfolio.equity
    pnl = final - initial
    pnl_pct = pnl / initial * 100

    peak = eq[0] if eq else initial
    max_dd = 0.0
    max_dd_pct = 0.0
    for v in eq:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak * 100 if peak else 0

    wins = sum(1 for f in m.fills if f.get("price", 0.5) < 0.5)
    hit_rate = wins / len(m.fills) * 100 if m.fills else 0
    avg_slip = sum(m.slippages) / len(m.slippages) if m.slippages else 0
    avg_edge = sum(m.edges) / len(m.edges) if m.edges else 0
    avg_conf = sum(m.confidences) / len(m.confidences) if m.confidences else 0

    total_signals = m.signals
    total_rej = sum(m.rejections.values())
    rej_pct = total_rej / total_signals * 100 if total_signals else 0

    # JSON
    data = {
        "evaluation_period": {
            "start": m.start.isoformat(), "end": end.isoformat(),
            "duration_hours": round(dur.total_seconds() / 3600, 2),
            "iterations": NUM_ITERATIONS,
        },
        "signals": {
            "total": total_signals, "rejected": total_rej,
            "rejected_pct": round(rej_pct, 2),
            "rejection_reasons": m.rejections,
        },
        "trades": {
            "filled": len(m.fills), "fills": m.fills,
        },
        "pnl": {
            "initial_equity": initial, "final_equity": round(final, 2),
            "net_pnl": round(pnl, 2), "net_pnl_pct": round(pnl_pct, 4),
        },
        "risk": {
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct, 4),
        },
        "performance": {
            "hit_rate": round(hit_rate, 2),
            "avg_net_edge": round(avg_edge, 6),
            "avg_confidence": round(avg_conf, 4),
        },
        "execution_quality": {
            "avg_slippage": round(avg_slip, 6),
        },
        "data_quality": {
            "markets_scanned": m.market_count,
            "api_errors": len(m.api_errors),
            "error_details": m.api_errors[:10],
        },
        "market_snapshots": m.snapshots,
        "equity_curve": m.equity_curve,
    }

    with open("paper_trading_metrics.json", "w") as f:
        json.dump(data, f, indent=2, default=str)

    # MD
    lines = [
        "# Paper Trading Report",
        "",
        f"**Generated:** {end.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"**Duration:** {dur.total_seconds()/60:.1f} minutes ({NUM_ITERATIONS} iterations)",
        f"**Scan Interval:** {SCAN_INTERVAL}s",
        f"**Markets Scanned:** {m.market_count} snapshots",
        "",
        "---",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Initial Equity | ${initial:,.2f} |",
        f"| Final Equity | ${final:,.2f} |",
        f"| Net P&L | ${pnl:,.2f} ({pnl_pct:+.2f}%) |",
        f"| Max Drawdown | ${max_dd:,.2f} ({max_dd_pct:.2f}%) |",
        f"| Total Signals | {total_signals} |",
        f"| Rejected | {total_rej} ({rej_pct:.1f}%) |",
        f"| Trades Filled | {len(m.fills)} |",
        f"| Hit Rate | {hit_rate:.1f}% |",
        f"| Avg Net Edge | {avg_edge:.4f} |",
        f"| Avg Confidence | {avg_conf:.4f} |",
        f"| Avg Slippage | {avg_slip:.4f} |",
        "",
        "---",
        "",
        "## Rejection Reasons",
        "",
    ]

    if m.rejections:
        lines.append("| Reason | Count |")
        lines.append("|--------|-------|")
        for r, c in sorted(m.rejections.items(), key=lambda x: -x[1]):
            lines.append(f"| {r} | {c} |")
    else:
        lines.append("No rejections.")

    lines.extend(["", "---", "", "## Trade Log", ""])
    if m.fills:
        lines.append("| Market | Side | Size | Price | Slippage |")
        lines.append("|--------|------|------|-------|----------|")
        for f in m.fills:
            lines.append(
                f"| {f['market_id'][:12]}... | {f['side']} "
                f"| {f['size']:.1f} | {f['price']:.4f} | {f['slippage']:.4f} |"
            )
    else:
        lines.append("No trades executed.")

    lines.extend(["", "---", "", "## Market Snapshots", ""])
    if m.snapshots:
        lines.append("| Market | Question | Midpoint | Spread | OBI | Liq |")
        lines.append("|--------|----------|----------|--------|-----|-----|")
        for s in m.snapshots[:20]:
            lines.append(
                f"| {s['market_id'][:10]}... | {s['question'][:40]}... "
                f"| {s.get('midpoint') or 'N/A'} | {s.get('spread') or 'N/A'} "
                f"| {s.get('obi') or 'N/A'} | {s.get('liq') or 'N/A'} |"
            )

    lines.extend(["", "---", "", "## Diagnosis", ""])

    if len(m.fills) == 0:
        lines.append(
            "### No trades executed\n\n"
            "The bot scanned live Polymarket markets but the strategy did not "
            "generate tradeable signals during this short window. This is normal "
            "for a brief test — the microstructure strategy requires significant "
            "order-book imbalance (OBI > 5%) and sufficient edge after costs.\n"
        )
    elif pnl < 0:
        lines.append(f"### Net loss of ${abs(pnl):.2f}\n")
    else:
        lines.append(f"### Net profit of ${pnl:.2f}\n")

    lines.extend([
        "",
        "---",
        "",
        "*Report generated by paper_trading.py — all trades are simulated.*",
    ])

    with open("paper_trading_report.md", "w") as f:
        f.write("\n".join(lines))

    logger.info("Reports written: paper_trading_report.md, paper_trading_metrics.json")


# ── Main ───────────────────────────────────────────────────────────


async def main() -> None:
    logger.info("=" * 60)
    logger.info("PAPER TRADING TEST — %d iterations with live data", NUM_ITERATIONS)
    logger.info("=" * 60)

    m = Metrics()
    portfolio = PortfolioTracker()
    breaker = CircuitBreaker(persist=False)
    limits = RiskLimits()
    sizer = PositionSizer()
    risk = RiskEngine(portfolio=portfolio, limits=limits, sizer=sizer, breaker=breaker)
    paper = PaperExecution(rejection_rate=0.01, latency_ms=200, fee_rate=0.05)
    exec_eng = ExecutionEngine(adapter=paper)
    ev_eng = ExpectedValueEngine(min_net_edge=0.01)

    strategy = MicrostructureStrategy(
        min_confidence=0.6, min_liquidity_score=0.3,
        min_obi_abs=0.05, min_edge_bps=10.0,
    )

    provider = await make_provider(m)

    for i in range(1, NUM_ITERATIONS + 1):
        logger.info("--- Iteration %d/%d ---", i, NUM_ITERATIONS)
        m.record_equity(portfolio.equity)

        features = await provider()
        logger.info("Got features for %d markets", len(features))

        for mid, feat in features.items():
            try:
                sig = strategy.generate_signal(feat)
            except Exception as exc:
                logger.warning("Strategy error on %s: %s", mid[:8], exc)
                continue

            if sig.decision.value != "CANDIDATE":
                m.record_rejection(f"micro: {sig.reason or 'NO_SIGNAL'}")
                continue

            result = await run_pipeline(
                sig, feat, ev_eng, risk, exec_eng, portfolio, m,
            )
            logger.info(
                "Signal %s/%s: filled=%s error=%s",
                mid[:8], sig.side,
                result.order_result is not None,
                result.error,
            )

        logger.info(
            "Iteration %d done: equity=$%.2f filled=%d rejected=%d",
            i, portfolio.equity, len(m.fills), sum(m.rejections.values()),
        )

        if i < NUM_ITERATIONS:
            logger.info("Sleeping %ds...", SCAN_INTERVAL)
            await asyncio.sleep(SCAN_INTERVAL)

    logger.info("=" * 60)
    logger.info("SESSION COMPLETE — generating reports")
    write_reports(m, portfolio)
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
