# Polymarket Quant Bot — Technical Design Document (TDD)

**Version:** 1.0  
**Status:** Draft / Implementation blueprint  
**Date:** 2026-07-28

## 1. Architecture

```text
                         ┌────────────────────┐
                         │ Polymarket APIs    │
                         │ Gamma + CLOB       │
                         └─────────┬──────────┘
                                   │
                          WebSocket/REST
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │ Market Data Collector    │
                    │ asyncio + validation     │
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │ Normalized Event Stream  │
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │ Feature / Strategy Layer │
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │ Probability / EV Engine  │
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │ Risk & Safety Engine     │
                    └────────────┬─────────────┘
                                 │
                         NO TRADE / TRADE
                                 │
                         ┌───────┴────────┐
                         ▼                ▼
                     Paper Exec       Live Exec
                         │                │
                         └───────┬────────┘
                                 ▼
                         Portfolio / P&L
                                 ▼
                         SQLite + Dashboard
```

## 2. Repository Layout

```text
polymarket-quant-bot/
├── app/
│   ├── config/settings.py
│   ├── data/{gamma.py,clob.py,websocket.py,normalizer.py,validators.py}
│   ├── features/{orderbook.py,momentum.py,volatility.py,market_quality.py}
│   ├── strategies/{base.py,microstructure.py,arbitrage.py,probability.py,ensemble.py}
│   ├── models/{calibration.py,probability_model.py}
│   ├── ev/{costs.py,expected_value.py}
│   ├── risk/{limits.py,circuit_breaker.py,position_sizing.py}
│   ├── execution/{interface.py,paper.py,polymarket.py}
│   ├── portfolio/tracker.py
│   ├── storage/{db.py,repositories.py}
│   ├── monitoring/{health.py,alerts.py}
│   └── main.py
├── tests/
├── notebooks/
├── dashboard/
├── scripts/
├── .env.example
├── docker-compose.yml
├── requirements.txt
├── PRD.md
└── TDD.md
```

## 3. Technology Decisions

| Layer | Technology |
|---|---|
| Runtime | Python 3.11+ |
| Concurrency | asyncio |
| Market API | Polymarket Gamma/CLOB |
| Trading client | Current official Polymarket Python client |
| Data | Pandas / NumPy |
| ML | scikit-learn / LightGBM |
| Storage | SQLite MVP |
| API | FastAPI |
| Dashboard | Streamlit MVP |
| Packaging | Docker |
| Secrets | environment variables |
| Alerts | Telegram or equivalent |

Keep API adapters isolated because platform APIs can change.

## 4. Core Interfaces

```python
class Strategy:
    name: str

    def generate_signal(self, snapshot, features, context):
        # Return side, model_probability, confidence, reason
        ...
```

```python
class RiskEngine:
    def evaluate(self, signal, portfolio, market, system_health):
        # Return approved, max_size, reasons
        ...
```

```python
class ExecutionAdapter:
    async def submit(self, order): ...
    async def cancel(self, order_id): ...
    async def status(self, order_id): ...
```

Paper and live execution implement the same interface.

## 5. Market Data

Collect:
- market metadata;
- outcome/token IDs;
- best bid/ask;
- order-book levels;
- recent trades;
- volume/liquidity;
- timestamps;
- resolution time.

Each event receives `received_at` and, when available, `source_timestamp`.

If `now - source_timestamp > DATA_MAX_AGE`, mark the market non-tradable.

## 6. Feature Engine

### Spread
`spread = ask - bid`

`relative_spread = (ask - bid) / midpoint`

### Order-book imbalance
`OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth)`

### Midpoint
`mid = (bid + ask) / 2`

### Momentum
Configurable returns such as 1s, 5s, 30s, and 5m. Prevent look-ahead leakage.

### Volatility
Rolling realised volatility on appropriately sampled prices.

### Liquidity
Use top-N depth, volume, spread, and fillability estimate.

### Time to resolution
`resolution_timestamp - current_timestamp`

## 7. Probability Model

Start with:
- logistic regression baseline;
- calibrated tree model.

Candidate inputs:
- market price;
- spread;
- OBI;
- depth;
- volume;
- momentum;
- volatility;
- time to resolution.

Calibration methods:
- Platt scaling;
- isotonic regression.

Metrics:
- Brier score;
- log loss;
- calibration curve;
- reliability diagram.

## 8. Arbitrage / Logical Inconsistency Engine

Represent relationships explicitly:

```python
Relationship(
    market_a="...",
    market_b="...",
    relation="IMPLIES",
)
```

Initial relationship types:
- IMPLIES;
- MUTUALLY_EXCLUSIVE;
- COMPLEMENT;
- SUM_CONSTRAINT.

Never infer a tradable relationship from vague semantic similarity alone.

Workflow:

```text
Relationship
    ↓
Executable prices
    ↓
Constraint check
    ↓
Cost estimate
    ↓
Net edge
    ↓
Risk check
    ↓
Candidate
```

## 9. Expected Value Engine

Inputs:
- model probability;
- executable price;
- estimated fill price;
- fees;
- spread;
- slippage;
- liquidity;
- model uncertainty.

Conceptual calculation:

```python
gross_edge = model_probability - executable_implied_probability
net_edge = gross_edge - estimated_execution_cost - uncertainty_penalty
```

The payoff model must be tested against actual settlement mechanics before live use.

## 10. Risk Engine

Example conservative research configuration:

```env
MAX_POSITION_PCT=0.01
MAX_MARKET_EXPOSURE_PCT=0.02
MAX_TOTAL_EXPOSURE_PCT=0.05
MAX_DAILY_LOSS_PCT=0.02
MAX_CONSECUTIVE_LOSSES=5
MAX_OPEN_POSITIONS=10
MAX_SPREAD=0.03
MIN_LIQUIDITY=...
MIN_NET_EDGE=0.05
MIN_CONFIDENCE=0.70
DATA_MAX_AGE_SECONDS=5
```

These are starting parameters for research/demo, not guaranteed-optimal settings.

Circuit breaker:

```python
if daily_pnl <= -max_daily_loss:
    halt_new_orders("DAILY_LOSS")

if consecutive_losses >= max_consecutive_losses:
    halt_new_orders("CONSECUTIVE_LOSSES")

if data_is_stale:
    halt_new_orders("STALE_DATA")

if api_health_bad:
    halt_new_orders("API_HEALTH")
```

Breaker state must persist across restarts.

## 11. Position Sizing

MVP:

`position_size = min(configured_risk_budget, risk_engine_limit, liquidity_limit)`

Research mode may support fractional Kelly with a deliberately small multiplier. Always cap with hard limits.

## 12. Paper Execution

Model:
- bid/ask;
- spread;
- partial fills;
- slippage;
- latency;
- cancellation;
- rejection;
- price movement between signal and fill.

Do not simulate perfect midpoint fills by default.

## 13. Live Execution

Live execution is a gated adapter requiring:
- explicit `LIVE_GUARDED` mode;
- authenticated client;
- pre-trade risk check;
- idempotency/client order ID;
- order status reconciliation;
- partial-fill handling;
- cancellation handling;
- unexpected-position detection;
- manual emergency stop.

Never store private keys in Git.

## 14. Order State Machine

```text
CREATED
  ↓
RISK_APPROVED
  ↓
SUBMITTED
  ├──→ REJECTED
  ├──→ CANCELLED
  ├──→ PARTIALLY_FILLED → FILLED
  └──→ FILLED
```

Unknown state must trigger reconciliation rather than blind resubmission.

## 15. Database Schema

```sql
CREATE TABLE markets (
    market_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    status TEXT,
    resolution_time TEXT,
    liquidity REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    bid REAL,
    ask REAL,
    midpoint REAL,
    spread REAL,
    bid_depth REAL,
    ask_depth REAL,
    volume REAL,
    time_to_resolution REAL
);

CREATE TABLE signals (
    signal_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    model_probability REAL,
    implied_probability REAL,
    gross_edge REAL,
    estimated_cost REAL,
    net_edge REAL,
    confidence REAL,
    decision TEXT NOT NULL,
    rejection_reason TEXT,
    timestamp TEXT NOT NULL
);

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    requested_price REAL,
    requested_size REAL,
    status TEXT NOT NULL,
    filled_size REAL,
    average_fill REAL,
    submitted_at TEXT,
    completed_at TEXT
);

CREATE TABLE positions (
    position_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    size REAL,
    average_entry REAL,
    current_price REAL,
    realised_pnl REAL,
    unrealised_pnl REAL
);

CREATE TABLE risk_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    details TEXT,
    timestamp TEXT NOT NULL
);
```

## 16. Backtesting

The backtester must:
- use timestamp-ordered data;
- prevent look-ahead leakage;
- model bid/ask;
- model configurable slippage/fees;
- support partial fills;
- enforce risk limits;
- reproduce strategy decisions deterministically.

Metrics:
- total P&L;
- net return;
- maximum drawdown;
- profit factor;
- expectancy;
- hit rate;
- Sharpe/Sortino;
- turnover;
- holding time;
- calibration;
- slippage impact.

## 17. Walk-Forward Validation

Example:

```text
Train: Jan–Mar → Validate: Apr
Train: Jan–Apr → Validate: May
Train: Jan–May → Validate: Jun
```

Validation periods must not influence training or threshold tuning.

## 18. Paper Trading

Paper execution consumes live data but simulates fills.

Promotion requires:
- minimum observation period;
- minimum signal count;
- stable risk behavior;
- no unresolved operational errors;
- positive net expectancy after simulated costs;
- acceptable drawdown.

Green paper P&L alone is not sufficient.

## 19. Dashboard

Pages:
1. Overview
2. Signals
3. Markets
4. Positions
5. Risk
6. Performance
7. Execution
8. Audit
9. Settings

Overview cards:
- Equity;
- Today P&L;
- Total P&L;
- Drawdown;
- Open exposure;
- Active signals;
- Eligible markets;
- System status.

Signal table:
`market | side | price | model P | implied P | gross edge | net edge | confidence | liquidity | spread | decision`

Risk widgets:
- daily loss;
- total exposure;
- market exposure;
- consecutive losses;
- data freshness;
- API health.

## 20. Observability

Structured events:

```json
{
  "timestamp": "...",
  "event": "SIGNAL_REJECTED",
  "market_id": "...",
  "reason": "STALE_DATA",
  "net_edge": 0.061
}
```

Required events:
`DATA_RECEIVED, DATA_STALE, SIGNAL_CREATED, SIGNAL_REJECTED, RISK_APPROVED, ORDER_SUBMITTED, ORDER_REJECTED, ORDER_FILLED, ORDER_CANCELLED, POSITION_UPDATED, CIRCUIT_BREAKER, SYSTEM_START, SYSTEM_STOP`

## 21. Testing

### Unit
Feature calculations, calibration, EV, risk limits, sizing, circuit breaker, order state machine.

### Integration
API adapters, database repositories, paper execution, dashboard data.

### Property
- no position exceeds hard limits;
- halted system cannot submit;
- stale data cannot approve an order;
- negative net edge cannot pass normal gate;
- unknown order state cannot trigger duplicate execution.

### Failure
Simulate API timeout, stale WebSocket, duplicate events, partial fills, database/process restart, corrupted model, missing metadata.

## 22. Security

- `.env` ignored by Git;
- secret rotation;
- least privilege;
- no credentials in logs;
- no private keys in dashboard;
- separate research/live credentials where possible;
- manual emergency stop.

## 23. Deployment

### Local MVP

```text
Docker Compose
├── bot
├── dashboard
└── sqlite volume
```

Later, only when needed:

`bot → PostgreSQL → Redis → FastAPI dashboard`

Do not add infrastructure until justified.

## 24. Recovery

On startup:
1. load persisted mode;
2. load risk state;
3. reconcile account;
4. reconcile open orders;
5. reconcile positions;
6. verify data freshness;
7. verify API health;
8. remain HALTED until health checks pass;
9. require explicit transition to trading mode.

## 25. Demo Data Policy

The included dashboard uses deterministic synthetic data. It must clearly display `DEMO / SIMULATION`, never connect to live credentials, never submit live orders, and use obviously synthetic market IDs.

## 26. Implementation Order

1. Config + SQLite.
2. Market adapter.
3. Snapshot recorder.
4. Feature engine.
5. Strategy interfaces.
6. EV engine.
7. Risk engine.
8. Paper execution.
9. Backtester.
10. Walk-forward runner.
11. Dashboard.
12. Live execution adapter.
13. Reconciliation.
14. Alerts.
15. Docker.

## 27. Definition of Done

Production-candidate status requires passing tests, reproducible backtests, documented walk-forward validation, stable paper trading, verified risk limits, restart-safe circuit breakers, working reconciliation, complete audit trail, protected secrets, explicit live-mode gating, and verification of current Polymarket rules/API behavior before deployment.
