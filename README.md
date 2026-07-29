# Polymarket Quant Bot

A risk-first, fail-closed quantitative trading platform for Polymarket prediction markets.

**Status:** Phase 1 — Config & Storage Layer

## Quick Start

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m app.main
```

## Project Structure

```
app/            # Application code
├── config/     # Pydantic settings, .env loading
├── data/       # Market data collection and normalization
├── discovery/  # Market eligibility and scanning
├── features/   # Feature engineering
├── strategies/ # Trading strategies
├── models/     # Probability models and calibration
├── ev/         # Expected value engine
├── risk/       # Risk limits, circuit breakers, sizing
├── execution/  # Order execution (paper + live)
├── portfolio/  # Position tracking and P&L
├── storage/    # SQLite database and repositories
├── monitoring/ # Health checks and alerts
├── audit/      # Structured audit logging
├── modes/      # Operating mode state machine
├── backtesting/# Historical replay and walk-forward
├── dashboard/  # Streamlit dashboard
└── main.py     # Application orchestrator
tests/          # Unit, integration, property, and failure tests
scripts/        # CLI utilities
```

## Documentation

- [PRD.md](PRD.md) — Product Requirements Document
- [TDD.md](TDD.md) — Technical Design Document
- [ARCHITECTURE.md](ARCHITECTURE.md) — Architecture Review

## Principles

1. Capital preservation first.
2. No data = no trade. Stale data = no trade.
3. Positive gross edge is insufficient; use net edge.
4. Paper trading before live trading.
5. Every decision must be auditable.
6. The bot must fail closed.
