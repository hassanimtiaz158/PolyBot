# Polymarket Quant Bot — Product Requirements Document (PRD)

**Version:** 1.0  
**Status:** Draft / Demo-ready  
**Date:** 2026-07-28

> **Important:** Profit is not guaranteed. The system must never represent a positive-P&L outcome as guaranteed. The product objective is risk-adjusted decision quality and controlled execution.

## 1. Product Vision

Build a Python-first Polymarket quantitative trading platform that continuously:
1. discovers and monitors eligible markets;
2. collects real-time market/order-book data;
3. computes microstructure and statistical features;
4. estimates fair outcome probabilities;
5. detects cross-market/logical pricing inconsistencies;
6. calculates net expected value after execution costs;
7. applies strict risk and data-quality gates;
8. paper-trades signals before live deployment;
9. executes only approved live orders;
10. records every signal, decision, order, fill, position, and P&L event;
11. exposes an operator dashboard and kill switches.

The bot should prefer **NO TRADE** over weak or uncertain trades.

## 2. Problem Statement

Prediction-market prices can contain temporary dislocations caused by liquidity, order-book imbalance, delayed information, heterogeneous participants, and market structure.

A naive automated trader can lose money through false confidence, overfitting, stale data, spread/slippage, poor liquidity, unexpected resolution, duplicate orders, API failures, excessive concentration, correlated positions, and operational failures.

Therefore risk management, execution quality, and observability are first-class requirements.

## 3. Goals

- **Research:** build reproducible market/order-book datasets.
- **Signal generation:** use order-book imbalance, spread, depth, momentum, volatility, time-to-resolution, liquidity, probability models, and cross-market relationships.
- **Positive-EV filtering:** only consider trades when estimated net edge exceeds a configurable threshold after costs and uncertainty.
- **Capital preservation:** enforce hard limits on position size, market exposure, total exposure, daily loss, consecutive losses, stale data, API errors, and model availability.
- **Validation:** require `backtest → walk-forward → paper trading → tiny live test → controlled scaling`.
- **Execution:** use supported Polymarket CLOB APIs/client for authenticated order management.
- **Monitoring:** expose equity/P&L, positions, signals, market quality, risk utilisation, model calibration, execution health, and kill switches.

## 4. Non-Goals

The MVP will NOT:
- guarantee profit;
- use Martingale;
- automatically increase stake after losses;
- trade every available market;
- use an LLM as the direct buy/sell authority;
- bypass geographic or platform restrictions;
- begin with high-risk/high-frequency assumptions;
- scale position size solely because a model reports high confidence.

## 5. Product Principles

1. Capital preservation first.
2. No signal is a trade until risk checks pass.
3. No data = no trade.
4. Stale data = no trade.
5. Uncertain model = no trade.
6. Positive gross edge is insufficient; use net edge.
7. Paper trading before live trading.
8. Every decision must be auditable.
9. The bot must fail closed.
10. Small, selective trading is preferred to forced activity.

## 6. Strategy Portfolio

### S1 — Market Microstructure
Use best bid/ask, spread, midpoint, top-N depth, order-book imbalance, recent trade direction, volume, short-horizon momentum, and realised volatility.

`OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth)`

Must be validated out-of-sample.

### S2 — Cross-Market / Logical Inconsistency Detector
Detect explicit relationships such as implication, mutual exclusivity, overlapping outcomes, complementary outcomes, and event constraints.

A candidate is tradable only if the relationship is explicit, prices are executable, and net expected value remains positive after costs.

### S3 — Fair Probability Model
Estimate `P(outcome | market + external features)` using candidates such as logistic regression, calibrated random forest, or LightGBM. Probability calibration is mandatory.

### S4 — Event / External Information Model
Optional later phase: `external data → structured extraction → probability model`. LLMs may extract structured facts but must not directly submit orders.

### S5 — Ensemble
Combine independently validated signals. Disagreement should reduce confidence or result in NO TRADE.

## 7. Market Eligibility

A market must pass configurable filters:
- sufficient liquidity;
- acceptable spread/depth;
- supported market structure;
- valid resolution metadata;
- fresh data;
- no operational warning;
- no excessive portfolio correlation.

Suggested market-quality score:
- Liquidity 25%;
- Spread 20%;
- Historical signal quality 20%;
- Model confidence 20%;
- Execution quality 15%.

Suggested states: `80–100 eligible`, `60–79 watchlist`, `<60 ignore`.

## 8. Decision Pipeline

```text
Market discovery
      ↓
Real-time data
      ↓
Data validation
      ↓
Feature engineering
      ↓
Strategy signals
      ↓
Probability calibration
      ↓
Expected-value calculation
      ↓
Uncertainty adjustment
      ↓
Execution-cost estimate
      ↓
Risk engine
      ↓
Position sizing
      ↓
NO TRADE / PAPER / LIVE
```

## 9. Expected Value

Use net rather than gross edge:

`Net Edge = Model Edge - Spread Cost - Slippage - Fees - Uncertainty Penalty`

The implementation must use the current applicable Polymarket execution/fee mechanics and executable order-book price.

A trade is eligible only when net edge, uncertainty, liquidity, position limits, data freshness, and execution health all pass.

## 10. Position Sizing and Risk

Hard limits:
- `MAX_POSITION_SIZE`;
- `MAX_MARKET_EXPOSURE`;
- `MAX_TOTAL_EXPOSURE`;
- `MAX_DAILY_LOSS`;
- `MAX_CONSECUTIVE_LOSSES`;
- `MAX_OPEN_POSITIONS`.

MVP uses conservative fixed-risk sizing. Fractional-Kelly can be a research mode. Martingale is prohibited.

Mandatory hard stops:
- daily drawdown limit;
- consecutive-loss circuit breaker;
- stale data;
- excessive API errors;
- model unavailable;
- database unavailable;
- abnormal order-book state;
- execution mismatch;
- unexpected account state.

When a safety dependency is unavailable: **NO NEW ORDERS**.

## 11. Operating Modes

- `RESEARCH` — no paper/live orders.
- `BACKTEST` — historical replay.
- `PAPER` — live data, simulated fills.
- `LIVE_GUARDED` — real orders with minimal configured risk.
- `HALTED` — no new orders; monitoring remains active.

Mode transitions require explicit operator action.

## 12. Dashboard

### Overview
Show mode, health, equity, realised/unrealised/today P&L, drawdown, open positions, active signals, eligible markets, and risk utilisation.

### Signals
Market, side, executable price, model probability, implied probability, gross edge, net edge, confidence, liquidity, spread, decision, reason.

### Positions
Market, side, entry, current, size, unrealised P&L, risk, time to resolution.

### Risk
Daily loss, total exposure, market exposure, consecutive losses, API health, data freshness.

### Audit
Every signal and order decision must be traceable.

## 13. Alerts

Optional free alert channel such as Telegram:
- trade accepted;
- order rejected;
- circuit breaker;
- daily-loss stop;
- stale data;
- API failure;
- unexpected position;
- restart.

Secrets must be environment variables.

## 14. Free/Low-Cost MVP Stack

- Python 3.11+
- asyncio
- Polymarket Gamma/CLOB APIs
- current official Polymarket Python client
- NumPy/Pandas
- scikit-learn/LightGBM
- SQLite
- FastAPI
- Streamlit
- Docker
- Git/GitHub
- `.env` / environment secrets

Local development is the default. Paid infrastructure is not required for research/demo.

## 15. Data Model

### markets
`market_id, question, condition_id, token_ids, status, resolution_time, liquidity, created_at, updated_at`

### market_snapshots
`timestamp, market_id, bid, ask, midpoint, spread, bid_depth, ask_depth, volume, time_to_resolution`

### signals
`signal_id, market_id, strategy, model_probability, implied_probability, gross_edge, estimated_cost, net_edge, confidence, decision, rejection_reason, timestamp`

### orders
`order_id, market_id, side, requested_price, requested_size, status, filled_size, average_fill, submitted_at, completed_at`

### positions
`position_id, market_id, side, size, average_entry, current_price, realised_pnl, unrealised_pnl`

### risk_events
`event_id, type, severity, details, timestamp`

## 16. MVP Acceptance Criteria

The MVP must:
1. discover eligible markets;
2. ingest live market data;
3. persist snapshots;
4. compute core features;
5. generate deterministic signals;
6. calculate net edge;
7. reject weak signals;
8. run reproducible backtests;
9. run walk-forward validation;
10. run paper trading;
11. enforce risk limits;
12. simulate order lifecycle;
13. display P&L/positions;
14. expose a kill switch;
15. produce an audit trail;
16. recover safely after restart;
17. keep credentials outside source control.

## 17. Success Metrics

### Strategy
Out-of-sample EV, profit factor, maximum drawdown, calibration error, Sharpe/Sortino where meaningful, turnover, hit rate, average net edge.

### Execution
Fill rate, slippage, rejection rate, latency, stale-data rate.

### Safety
Risk violations, circuit-breaker correctness, duplicate-order count, unexpected exposure count.

No single metric approves live trading.

## 18. Rollout

`Phase 0 Architecture/demo → Phase 1 Data → Phase 2 Features/signals → Phase 3 Backtesting/walk-forward → Phase 4 Paper → Phase 5 Guarded live → Phase 6 Evidence-based scaling`

## 19. Security & Compliance

- Never commit private keys/API credentials.
- Use `.env` locally and secret management in production.
- Restrict trading-wallet permissions.
- Maintain audit logs.
- Verify current platform rules and geographic eligibility before live trading.
- Never bypass restrictions.
- Provide manual emergency stop.

## 20. Demo Disclaimer

The dashboard demo uses synthetic data and green P&L only for UI demonstration. It does not represent real returns and must never submit real orders.
