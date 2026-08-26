# PolyBOT

**A risk-first, fail-closed quantitative trading bot for Polymarket prediction markets.**

Market discovery, live order-book signals, strict risk gating, and a full audit trail — built to lose gracefully before it's ever trusted to trade for real.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Status](https://img.shields.io/badge/status-paper%20trading-yellow)
![Tests](https://img.shields.io/badge/tests-700%2B%20passing-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What this is

PolyBOT watches Polymarket markets in real time, scores them for eligibility, computes order-book microstructure features, and runs them through a strategy → expected-value → risk pipeline before any order is placed. Every stage is designed to **fail closed**: stale data, an unreachable API, a tripped circuit breaker, or a low-confidence signal all resolve to *no trade*, never a guess.

It ships with a read-only Streamlit dashboard for watching signals, positions, risk state, and the full audit log — and nothing in that dashboard can submit an order. The only way this bot trades is through its own risk-gated pipeline.

> **Status: paper trading.** Live execution exists in the code and is hard-gated behind explicit configuration, but it is off by default and not recommended until the strategy has real (not synthetic) walk-forward validation. See [Safety model](#safety-model) below.

## Why it's built this way

Most trading-bot side projects skip straight to "does it make money." This one is built backwards on purpose:

- **No data = no trade.** Every feature has a freshness check; stale data blocks the strategy that would otherwise use it.
- **Positive gross edge isn't enough.** Net edge (after modeled spread, slippage, and fees) is what gates a trade.
- **Capital preservation over upside.** Hard position, exposure, daily-loss, and consecutive-loss limits — enforced independently of whatever a strategy "believes."
- **A circuit breaker that doesn't trust itself.** Once tripped, it stays tripped across restarts until an operator explicitly clears it.
- **Everything is audited.** Every signal, rejection reason, and order event is logged with a machine-readable code, not just a log line.

## Architecture

```
Polymarket Gamma + CLOB APIs
            │
            ▼
   Market Discovery ──▶ eligibility scoring, persisted to SQLite
            │
            ▼
   Live Data Feed ────▶ order-book snapshots, validated & feature-engineered
            │
            ▼
   Strategy Engine ───▶ order-book microstructure signals
            │
            ▼
   Expected Value ────▶ gross edge → net edge (spread, slippage, fees)
            │
            ▼
   Risk Engine ────────▶ limits, circuit breaker, freshness, sizing
            │
      ┌─────┴─────┐
   REJECT       APPROVE
      │             │
      ▼             ▼
   audited      Paper / Live Execution
                     │
                     ▼
              Portfolio + Audit Trail
                     │
                     ▼
          FastAPI (read-only) + Streamlit Dashboard
```

## Features

| Area | What's there |
|---|---|
| **Market discovery** | Periodic Gamma API scan, weighted eligibility scoring (liquidity, spread, quality) |
| **Live data** | Concurrent CLOB order-book fetching, validation, and feature computation |
| **Strategies** | Order-book microstructure (OBI, spread, momentum); pluggable for more |
| **Risk engine** | Position/market/total exposure limits, daily loss, consecutive losses, spread/liquidity gates |
| **Circuit breaker** | 3-state (NORMAL/WARNING/HALTED), persisted, no silent auto-reset |
| **Kill switch** | Operator-controlled emergency stop, independent of the circuit breaker |
| **Execution** | Simulated paper fills with slippage; live Polymarket CLOB adapter (EIP-712 signed, hard-gated) |
| **Dashboard** | Read-only Streamlit UI — overview, signals, markets, positions, risk, performance, execution, audit |
| **Backtesting** | Timestamp-ordered replay + walk-forward validation with overfitting/regime-sensitivity detection |
| **Audit trail** | Structured, typed events for every decision — never just free-text logs |

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env            # fill in as needed — safe defaults out of the box
```

Run the bot (paper trading, no credentials required):

```bash
python -m app.main
```

Run the dashboard, in a separate terminal:

```bash
uvicorn app.api.app:app --port 8000        # read-only API backend
streamlit run app/dashboard/app.py         # dashboard UI
```

## Project structure

```
app/
├── config/         # Pydantic settings, .env loading
├── data/           # Gamma & CLOB API adapters, normalization, validation
├── discovery/      # Market eligibility scoring and scanning
├── orchestrator/   # Event loop, live data feed, signal routing
├── features/       # Order-book, momentum, volatility, liquidity features
├── strategies/     # Trading strategies
├── models/         # Probability models and calibration
├── ev/             # Expected value engine
├── risk/           # Limits, circuit breaker, position sizing, kill switch
├── execution/      # Paper + live (Polymarket CLOB) execution adapters
├── portfolio/      # Position tracking and P&L
├── storage/        # SQLite schema, repositories
├── monitoring/     # Health checks and alerts
├── audit/          # Structured event logging
├── modes/          # RESEARCH / BACKTEST / PAPER / LIVE_GUARDED / HALTED state machine
├── backtesting/    # Historical replay and walk-forward validation
├── reconciliation/ # Startup order/position reconciliation
├── api/            # Read-only FastAPI backend
├── dashboard/      # Streamlit dashboard (read-only)
└── main.py         # Application orchestrator — wires everything together

tests/              # Unit, integration, property (Hypothesis), and failure/chaos tests
scripts/            # CLI utilities (backtest, walk-forward, seed data)
```

## Safety model

- **Modes**: `RESEARCH → BACKTEST → PAPER → LIVE_GUARDED`, plus `HALTED`. Every transition is explicit; the system boots into `HALTED` until an operator moves it forward.
- **Live trading is double-gated**: requires both `MODE=LIVE_GUARDED` *and* `LIVE_TRADING_ENABLED=true` in `.env`. Neither is set by default.
- **No Martingale, ever.** Position sizing is fixed-risk and stateless — it never grows a bet because the last one lost. This is enforced as a hard invariant, not a config option.
- **Kill switch and circuit breaker persist across restarts.** A `HALTED` or `KILLED` state doesn't quietly clear itself just because the process restarted.

## Testing

```bash
pytest tests/unit tests/integration tests/failure tests/property
```

The suite covers unit, integration, Hypothesis-based property tests (safety invariants), and failure/chaos scenarios (API down, stale data, corrupt config, restart recovery).

## Documentation

- [PRD.md](PRD.md) — Product requirements
- [TDD.md](TDD.md) — Technical design
- [ARCHITECTURE.md](ARCHITECTURE.md) — Architecture review
- [SECURITY.md](SECURITY.md) — Security posture and audit findings
- [LIVE_TRADING_READINESS_REVIEW.md](LIVE_TRADING_READINESS_REVIEW.md) — Honest go/no-go assessment before any live capital

## License

[MIT](LICENSE)

## Principles

1. Capital preservation first.
2. No data = no trade. Stale data = no trade.
3. Positive gross edge is insufficient; use net edge.
4. Paper trading before live trading.
5. Every decision must be auditable.
6. The bot must fail closed.

---

*Not financial advice. Nothing here is a recommendation to trade. Use at your own risk, and only with capital you can afford to lose.*
