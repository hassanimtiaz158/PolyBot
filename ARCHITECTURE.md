# Polymarket Quant Bot — Architecture Review & Implementation Plan

**Author:** Lead Software Architect  
**Date:** 2026-07-28  
**Status:** Phase 0 — Architecture Review (no implementation code)

---

## 1. Architecture Review

### 1.1 High-Level Data Flow

The system follows a strictly sequential pipeline with safety gates at every stage:

```
Polymarket APIs (Gamma + CLOB)
       │
       ▼
┌──────────────────┐
│ Market Discovery │  ← discover & filter eligible markets
└────────┬─────────┘
         ▼
┌──────────────────┐
│ Data Collection  │  ← order-book, trades, metadata, WebSocket stream
└────────┬─────────┘
         ▼
┌──────────────────┐
│ Data Validation  │  ← freshness, completeness, structural checks
└────────┬─────────┘
   FAIL  │  PASS
    ──►  │
    HALT │  ▼
         ┌──────────────────┐
         │ Feature Engine   │  ← OBI, spread, depth, momentum, vol, liquidity, TTR
         └────────┬─────────┘
                  ▼
         ┌──────────────────┐
         │ Strategy Engine  │  ← Microstructure, Cross-Market, Probability, Ensemble
         └────────┬─────────┘
                  ▼
         ┌──────────────────┐
         │ Probability Model│  ← calibration (Platt/isotonic), Brier, reliability
         └────────┬─────────┘
                  ▼
         ┌──────────────────┐
         │ EV Engine        │  ← gross edge, cost estimate, uncertainty penalty → net edge
         └────────┬─────────┘
                  ▼
         ┌──────────────────┐
         │ Risk Engine      │  ← limits, circuit breaker, data freshness, sizing
         └────────┬─────────┘
   REJECT │  APPROVE
    ──►   │
    HALT  │  ▼
         ┌──────────────────┐
         │ Paper Execution  │  ← simulated fills, slippage, partial fills
         │   OR             │
         │ Live Execution   │  ← gated by LIVE_GUARDED mode only
         └────────┬─────────┘
                  ▼
         ┌──────────────────┐
         │ Portfolio / P&L  │  ← position tracking, P&L calculation
         └────────┬─────────┘
                  ▼
         ┌──────────────────┐
         │ SQLite + Dash    │  ← persistence, audit trail, Streamlit UI
         └──────────────────┘
```

### 1.2 Strengths of the Existing Design

- **Fail-closed by construction**: stale data, API errors, and model failures all map to NO TRADE / HALT.
- **Strict separation of concerns**: API adapters are isolated behind interfaces; strategies never touch Polymarket directly.
- **Mode gating**: `LIVE_GUARDED` is an explicit, operator-required transition, preventing accidental real orders.
- **Audit trail**: every signal, risk decision, and order event is recorded with structured data.
- **Conservative defaults**: hard limits on position size, exposure, daily loss, consecutive losses, and open positions.
- **No Martingale clause**: explicitly prohibited in both PRD and TDD; non-negotiable.
- **Adapter pattern for Polymarket APIs**: changes to the platform API affect only the adapter layer, not strategies or risk.

### 1.3 Potential Weaknesses / Risks

| Risk | Mitigation in Design |
|---|---|
| Overfitting false signals | Out-of-sample validation, walk-forward, paper trading gate |
| API credential exposure | `.env` + secret management, no credentials in Git |
| Order state ambiguity | State machine with reconciliation; unknown state → no blind resubmit |
| Model calibration drift | Required recalibration monitoring; model unavailability halts trading |
| Correlation concentration | MAX_OPEN_POSITIONS + MAX_MARKET_EXPOSURE + portfolio correlation filter |
| Data latency | `DATA_MAX_AGE_SECONDS = 5`; stale data blocks all trades |

---

## 2. Dependency Graph

```
                         ┌─────────────┐
                         │ config/     │
                         │ settings.py │
                         └──────┬──────┘
                                │
         ┌──────────────────────┼──────────────────────┐
         ▼                      ▼                      ▼
  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
  │ data/        │      │ storage/     │      │ monitoring/  │
  │ gamma.py     │      │ db.py        │      │ health.py    │
  │ clob.py      │      │ repositories │      │ alerts.py    │
  │ websocket.py │      └──────┬───────┘      └──────┬───────┘
  │ normalizer   │             │                      │
  │ validators   │             │                      │
  └──────┬───────┘             │                      │
         │                     │                      │
         ▼                     │                      │
  ┌──────────────┐             │                      │
  │ features/    │             │                      │
  │ orderbook    │             │                      │
  │ momentum     │             │                      │
  │ volatility   │             │                      │
  │ market_qual  │             │                      │
  └──────┬───────┘             │                      │
         │                     │                      │
         ▼                     │                      │
  ┌──────────────────┐         │                      │
  │ strategies/      │         │                      │
  │ base.py          │         │                      │
  │ microstructure  │◄──────── │ ─── reads positions  │
  │ arbitrage       │         │      and signals      │
  │ probability     │         │                      │
  │ ensemble        │         │                      │
  └──────┬───────────┘         │                      │
         │                     │                      │
         ▼                     │                      │
  ┌──────────────────┐         │                      │
  │ models/          │         │                      │
  │ calibration      │         │                      │
  │ probability_model│         │                      │
  └──────┬───────────┘         │                      │
         │                     │                      │
         ▼                     │                      │
  ┌──────────────────┐         │                      │
  │ ev/              │         │                      │
  │ costs.py         │         │                      │
  │ expected_value   │         │                      │
  └──────┬───────────┘         │                      │
         │                     │                      │
         ▼                     │                      │
  ┌──────────────────┐         │                      │
  │ risk/            │◄────────┼──────────────────────┘
  │ limits.py        │         │   (reads system health
  │ circuit_breaker  │         │    for stale/API checks)
  │ position_sizing  │         │
  └──────┬───────────┘         │
         │                     │
         ▼                     │
  ┌──────────────────┐         │
  │ execution/       │         │
  │ interface.py     │         │
  │ paper.py         │         │
  │ polymarket.py    │         │
  └──────┬───────────┘         │
         │                     │
         ▼                     │
  ┌──────────────────┐         │
  │ portfolio/       │─────────┘
  │ tracker.py       │   (writes positions, P&L)
  └──────────────────┘
         │
         ▼
  ┌──────────────────┐
  │ main.py          │  ← orchestrator; wires all components
  │ (orchestrator)   │     manages mode state, event loop, startup/shutdown
  └──────────────────┘
```

### Key Dependency Rules

1. **Data layer** depends only on config + external API clients.
2. **Feature engine** depends only on normalised data.
3. **Strategies** depend only on features + config.
4. **Probability model** depends only on features + config.
5. **EV engine** depends on strategies + probability model.
6. **Risk engine** depends on EV + portfolio + system health.
7. **Execution** depends on risk engine output.
8. **Portfolio** depends on execution callbacks.
9. **Database** is consumed by dashboard + recovery logic; written by most components.
10. **No circular dependencies** are permitted between any modules.

---

## 3. Recommended Repository Structure

```
polymarket-quant-bot/
├── app/
│   ├── __init__.py
│   ├── main.py                          # Orchestrator: event loop, mode mgmt, wiring
│   │
│   ├── config/
│   │   ├── __init__.py
│   │   └── settings.py                  # Pydantic-settings, .env loading, defaults
│   │
│   ├── data/
│   │   ├── __init__.py
│   │   ├── gamma.py                     # Polymarket Gamma API adapter
│   │   ├── clob.py                      # Polymarket CLOB API adapter
│   │   ├── websocket.py                 # WebSocket stream manager
│   │   ├── normalizer.py                # Normalise external data → internal schema
│   │   └── validators.py                # Freshness, structure, completeness checks
│   │
│   ├── discovery/
│   │   ├── __init__.py
│   │   ├── eligibility.py               # Market eligibility scoring (liquidity, spread, etc.)
│   │   └── scanner.py                   # Periodic market discovery sweep
│   │
│   ├── features/
│   │   ├── __init__.py
│   │   ├── orderbook.py                 # OBI, spread, depth, midpoint
│   │   ├── momentum.py                  # Returns over configurable windows
│   │   ├── volatility.py                # Rolling realised volatility
│   │   ├── liquidity.py                 # Depth-based liquidity estimate
│   │   └── market_quality.py            # Composite quality score
│   │
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── base.py                      # Abstract Strategy interface
│   │   ├── microstructure.py            # S1: order-book signal strategy
│   │   ├── arbitrage.py                 # S2: cross-market / logical inconsistency
│   │   ├── probability.py               # S3: fair probability model strategy
│   │   └── ensemble.py                  # S5: signal combination / disagreement
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   ├── calibration.py               # Platt scaling, isotonic regression
│   │   └── probability_model.py         # Logistic regression, tree-based models
│   │
│   ├── ev/
│   │   ├── __init__.py
│   │   ├── costs.py                     # Execution cost estimates (spread, slippage, fees)
│   │   └── expected_value.py            # Gross → net edge calculation
│   │
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── limits.py                    # Hard limit enforcement
│   │   ├── circuit_breaker.py           # Breaker state machine + persistence
│   │   └── position_sizing.py           # Fixed-risk / fractional-Kelly sizing
│   │
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── interface.py                 # Abstract ExecutionAdapter
│   │   ├── paper.py                     # Paper: simulated fills with slippage
│   │   ├── polymarket.py                # Live: real Polymarket orders
│   │   └── state_machine.py             # Order state machine (CREATED→RISK_APPROVED→SUBMITTED→...)
│   │
│   ├── portfolio/
│   │   ├── __init__.py
│   │   └── tracker.py                   # Position tracking, P&L calculation
│   │
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── db.py                        # SQLite connection manager, schema init
│   │   └── repositories.py             # CRUD repositories for each entity
│   │
│   ├── monitoring/
│   │   ├── __init__.py
│   │   ├── health.py                    # System health checks (API, DB, model, data)
│   │   └── alerts.py                    # Telegram/notification dispatcher
│   │
│   ├── backtesting/
│   │   ├── __init__.py
│   │   ├── backtester.py                # Timestamp-ordered replay engine
│   │   └── walk_forward.py              # Walk-forward validation runner
│   │
│   ├── reconciliation/
│   │   ├── __init__.py
│   │   └── reconciler.py                # Order/position reconciliation on startup
│   │
│   ├── audit/
│   │   ├── __init__.py
│   │   └── logger.py                    # Structured event logging
│   │
│   └── modes/
│       ├── __init__.py
│       └── state.py                     # Mode state machine (RESEARCH/BACKTEST/PAPER/LIVE_GUARDED/HALTED)
│
├── dashboard/                           # Streamlit dashboard (separate from static demo)
│   ├── app.py                           # Streamlit entry point
│   ├── pages/
│   │   ├── overview.py
│   │   ├── signals.py
│   │   ├── markets.py
│   │   ├── positions.py
│   │   ├── risk.py
│   │   ├── performance.py
│   │   ├── execution.py
│   │   ├── audit.py
│   │   └── settings.py
│   └── components/                      # Reusable Streamlit widgets
│       ├── cards.py
│       ├── tables.py
│       └── charts.py
│
├── tests/
│   ├── __init__.py
│   ├── unit/
│   │   ├── test_features.py
│   │   ├── test_ev.py
│   │   ├── test_risk.py
│   │   ├── test_strategies.py
│   │   ├── test_calibration.py
│   │   ├── test_position_sizing.py
│   │   ├── test_circuit_breaker.py
│   │   └── test_state_machine.py
│   ├── integration/
│   │   ├── test_data_pipeline.py
│   │   ├── test_db_repositories.py
│   │   ├── test_paper_execution.py
│   │   └── test_dashboard_data.py
│   ├── property/
│   │   └── test_safety_properties.py    # Hypothesis-based property tests
│   ├── failure/
│   │   └── test_failure_scenarios.py    # API timeout, stale data, restart
│   ├── conftest.py                      # Shared fixtures, synthetic data generators
│   └── factories.py                     # Test data factories
│
├── notebooks/
│   ├── 01-data-exploration.ipynb
│   ├── 02-feature-analysis.ipynb
│   └── 03-model-calibration.ipynb
│
├── scripts/
│   ├── seed_data.py                     # Generate synthetic data for demo/testing
│   ├── run_backtest.py                  # CLI backtest runner
│   ├── run_walk_forward.py              # CLI walk-forward runner
│   └── reset_db.py                      # Reset SQLite to clean state
│
├── dashboard/                           # DEPRECATED after Streamlit migration
│   ├── index.html                       # Static demo (retained for reference)
│   └── README.md
│
├── .env.example                         # Template for secrets (no real values)
├── .gitignore                           # .env, __pycache__, *.db, etc.
├── docker-compose.yml                   # bot + dashboard + sqlite volume
├── Dockerfile                           # Python 3.11+ container
├── requirements.txt                     # Python dependencies
├── pyproject.toml                       # Project metadata, tool configs
├── PRD.md                               # Existing (unchanged)
├── TDD.md                               # Existing (unchanged)
└── ARCHITECTURE.md                      # This file
```

### Differences from TDD Layout

| TDD Layout | Recommended Layout | Rationale |
|---|---|---|
| No `discovery/` | `discovery/` added | Market eligibility scoring and discovery are distinct from raw data collection. Having a separate module prevents the data layer from bloating with business logic. |
| No `backtesting/` | `backtesting/` added | Backtesting is a cross-cutting validation concern that reuses strategies, risk, and EV. It deserves its own module rather than being embedded elsewhere. |
| No `reconciliation/` | `reconciliation/` added | Startup reconciliation (orders, positions, account state) is a distinct responsibility critical for safety. |
| No `audit/` | `audit/` added | Structured audit logging is used by all modules. Centralising it ensures consistent formatting and prevents ad-hoc log lines. |
| No `modes/` | `modes/` added | Operating mode state machine is a top-level safety concern. It must be explicit, testable, and persistable. |
| `monitoring/` inside `app/` | `monitoring/` inside `app/` | Kept as-is; it fits well. |
| Dashboard as `dashboard/` at root | Dashboard moved into `app/dashboard/` as Streamlit | The static HTML is a demo placeholder. The real dashboard lives alongside the app code. The original `dashboard/` is marked deprecated. |
| No `pyproject.toml` | `pyproject.toml` added | Modern Python packaging, tool configuration (pytest, ruff, mypy). |

---

## 4. Missing Components

| Missing Component | Criticality | Notes |
|---|---|---|
| **Market Discovery / Eligibility Scanner** | HIGH | PRD section 7 defines eligibility scoring (liquidity 25%, spread 20%, etc.). No module exists for this. |
| **Configuration Management** | HIGH | `app/config/settings.py` with Pydantic-settings + `.env` is the foundation all modules depend on. |
| **Database Layer** | HIGH | `app/storage/db.py` + `repositories.py`. Schema is defined in TDD but not implemented. |
| **Monitoring / Health Checks** | HIGH | `app/monitoring/health.py` — needed by risk engine for stale-data and API-health gates. |
| **Audit Logger** | HIGH | `app/audit/logger.py` — structured event logging required by PRD section 20. |
| **Mode State Machine** | HIGH | `app/modes/state.py` — RESEARCH/BACKTEST/PAPER/LIVE_GUARDED/HALTED transitions must be explicit and persisted. |
| **Order State Machine** | HIGH | `app/execution/state_machine.py` — CREATED→RISK_APPROVED→SUBMITTED→... with reconciliation. |
| **Backtester** | HIGH | TDD section 16 — timestamp-ordered replay with look-ahead prevention. |
| **Walk-Forward Validator** | HIGH | TDD section 17 — required before paper trading. |
| **Reconciliation Module** | HIGH | `app/reconciliation/reconciler.py` — startup order/position reconciliation. |
| **Alerts** | MEDIUM | Telegram notification dispatcher. Optional for MVP but adds safety. |
| **Streamlit Dashboard** | MEDIUM | The static HTML demo is sufficient for Phase 0; the real Streamlit dashboard is built in Phase 5. |
| **Docker Setup** | LOW | `Dockerfile`, `docker-compose.yml` needed for reproducible deployment but not for local research. |
| **Seed Data Script** | LOW | `scripts/seed_data.py` — generates synthetic data for development and testing. |

---

## 5. Contradictions & Ambiguities

### 5.1 Pipeline Ordering Differences

| PRD (Sec 8) Stage | TDD (Sec 1) Stage | Issue |
|---|---|---|
| Uncertainty adjustment (separate step) | Included within EV Engine | **Ambiguity**: PRD has 13 stages; TDD diagram has ~9. The PRD's explicit "Uncertainty adjustment" is a separate stage after EV calculation, whereas the TDD merges it into the EV engine. **Recommendation**: Follow the TDD — incorporate uncertainty penalty inside `ev/expected_value.py` as a configurable parameter rather than a separate pipeline stage. This reduces pipeline complexity without losing the concept. |
| Execution-cost estimate (separate step) | Included within EV Engine | Same as above. Merge into EV engine. |
| Position sizing (separate step after risk) | Inside Risk Engine | **Ambiguity**: PRD lists position sizing after risk and before the trade decision. TDD puts sizing inside the risk engine. **Recommendation**: Follow the TDD — sizing is a logical output of the risk engine. The risk engine evaluates the trade, determines max acceptable size, and returns the decision. |

### 5.2 Market Quality Score vs No Explicit Module

PRD section 7 defines a precise market-quality scoring formula (Liquidity 25%, Spread 20%, etc.) with eligibility bands (80–100 eligible, 60–79 watchlist, <60 ignore). However, no module in the TDD repository layout implements this scoring. The TDD mentions `market_quality.py` inside `features/`, but it is listed as a feature rather than a gating/eligibility component.

**Recommendation**: Create `app/discovery/eligibility.py` that implements the market-quality score and market filtering logic. The feature-level `market_quality.py` computes raw inputs; the eligibility module applies the weighted scoring and threshold logic.

### 5.3 Dashboard Technology

TDD specifies Streamlit. The existing `dashboard/index.html` is a static HTML demo. This is acceptable for Phase 0 but must be replaced with Streamlit before production use.

**Recommendation**: Retain `dashboard/index.html` as a demo artifact. Build the real dashboard as `app/dashboard/` with Streamlit. Mark the static HTML directory as deprecated in comments.

### 5.4 Data Discovery vs Data Collection

PRD begins with "Market discovery" but TDD has no dedicated discovery module. The data layer (`data/`) is focused on collecting data for known markets. There is no scanner that periodically discovers new eligible markets.

**Recommendation**: Add `app/discovery/scanner.py` that periodically queries the Polymarket API for open markets, filters by eligibility, and registers new ones in the database.

### 5.5 Model Retraining Lifecycle

Neither PRD nor TDD explicitly specify:
- How often models are retrained.
- What triggers retraining (e.g., after N new samples, after calibration score degrades).
- How model versions are tracked.
- How a model rollback works.

**Recommendation**: Add a `models/versioning.py` module and document the retraining policy. For MVP, use manual retraining triggered by the operator. Models should be persisted with version metadata.

### 5.6 Portfolio Correlation Filter

PRD section 7 mentions "no excessive portfolio correlation" as an eligibility filter, but no specific correlation metric or threshold is defined. The TDD does not implement this.

**Recommendation**: Add a placeholder in `risk/limits.py` that computes pairwise position correlation and rejects new positions that would push portfolio correlation above `MAX_PORTFOLIO_CORRELATION`. Default: disabled for MVP.

### 5.7 Backtesting Data Source

TDD specifies backtesting uses "timestamp-ordered data" but does not specify whether this comes from the SQLite `market_snapshots` table or from an external parquet/CSV file. For long historical periods, SQLite may be too large.

**Recommendation**: Backtesting reads from the same `market_snapshots` table for convenience. For large-scale historical testing, support loading from CSV/Parquet files in `scripts/`.

---

## 6. Phased Implementation Plan

### Phase 0 — Architecture & Foundation (current)
- [x] PRD review
- [x] TDD review
- [x] Existing dashboard inspection
- [x] ARCHITECTURE.md created
- [ ] Repository scaffold: directories, `__init__.py` files
- [ ] `.env.example` with all documented env vars
- [ ] `.gitignore` (`.env`, `*.db`, `__pycache__`, `.mypy_cache`)
- [ ] `requirements.txt` with pinned dependencies
- [ ] `pyproject.toml` (project metadata, pytest config, ruff, mypy)

### Phase 1 — Config & Storage Layer
**Files to create:**
- `app/config/__init__.py`
- `app/config/settings.py` — Pydantic Settings model with env loading
- `app/storage/__init__.py`
- `app/storage/db.py` — SQLite connection, schema init (TDD section 15)
- `app/storage/repositories.py` — CRUD for markets, snapshots, signals, orders, positions, risk_events

**Testing:** Verify schema creation, insert/query round-trip, repository methods.

### Phase 2 — Data Layer
**Files to create:**
- `app/data/__init__.py`
- `app/data/gamma.py` — Polymarket Gamma API adapter (read-only market metadata)
- `app/data/clob.py` — Polymarket CLOB API adapter (order-book, trades)
- `app/data/websocket.py` — WebSocket subscription manager
- `app/data/normalizer.py` — External data → internal schema
- `app/data/validators.py` — Freshness, completeness checks
- `app/discovery/__init__.py`
- `app/discovery/eligibility.py` — Market eligibility scoring
- `app/discovery/scanner.py` — Periodic market discovery

**Testing:** Unit tests with mocked API responses. Integration test against sandbox if available.

### Phase 3 — Features & Signals
**Files to create:**
- `app/features/__init__.py`
- `app/features/orderbook.py`
- `app/features/momentum.py`
- `app/features/volatility.py`
- `app/features/liquidity.py`
- `app/features/market_quality.py`
- `app/strategies/__init__.py`
- `app/strategies/base.py` — Abstract base
- `app/strategies/microstructure.py`
- `app/strategies/arbitrage.py`
- `app/strategies/probability.py`
- `app/strategies/ensemble.py`

**Testing:** Deterministic feature outputs for known inputs. Strategy signal shape and bounds.

### Phase 4 — Models, EV & Risk
**Files to create:**
- `app/models/__init__.py`
- `app/models/calibration.py`
- `app/models/probability_model.py`
- `app/ev/__init__.py`
- `app/ev/costs.py`
- `app/ev/expected_value.py`
- `app/risk/__init__.py`
- `app/risk/limits.py`
- `app/risk/circuit_breaker.py`
- `app/risk/position_sizing.py`

**Testing:** Property-based tests — no position exceeds limits, no trade on stale data, no trade with negative net edge.

### Phase 5 — Backtesting & Walk-Forward
**Files to create:**
- `app/backtesting/__init__.py`
- `app/backtesting/backtester.py`
- `app/backtesting/walk_forward.py`
- `scripts/run_backtest.py`
- `scripts/run_walk_forward.py`

**Testing:** Reproduce known strategy decisions deterministically. Verify look-ahead prevention.

### Phase 6 — Paper Execution
**Files to create:**
- `app/execution/__init__.py`
- `app/execution/interface.py`
- `app/execution/paper.py`
- `app/execution/state_machine.py`
- `app/portfolio/__init__.py`
- `app/portfolio/tracker.py`

**Testing:** Simulated fills with slippage, partial fills, cancellation scenarios. Verify no perfect-midpoint assumption.

### Phase 7 — Dashboard & Monitoring
**Files to create:**
- `app/dashboard/app.py` — Streamlit entry
- `app/dashboard/pages/*.py`
- `app/dashboard/components/*.py`
- `app/monitoring/__init__.py`
- `app/monitoring/health.py`
- `app/monitoring/alerts.py`
- `app/audit/__init__.py`
- `app/audit/logger.py`
- `app/modes/__init__.py`
- `app/modes/state.py`

**Testing:** Dashboard renders without errors. Alerts fire on circuit breaker. Mode transitions are logged.

### Phase 8 — Live Execution (Guarded)
**Files to create:**
- `app/execution/polymarket.py`
- `app/reconciliation/__init__.py`
- `app/reconciliation/reconciler.py`

**Testing:** Integration tests with dry-run mode against Polymarket sandbox. Verify LIVE_GUARDED gating. Verify startup reconciliation.

### Phase 9 — Docker & Hardening
**Files to create:**
- `Dockerfile`
- `docker-compose.yml`
- `scripts/seed_data.py`
- `scripts/reset_db.py`

**Testing:** Docker Compose build and run end-to-end. Verify clean restart recovery.

---

## 7. Testing Strategy

### 7.1 Test Pyramid

```
        ┌──────┐
        │ E2E  │  ← Docker Compose smoke tests (Phase 9)
       ┌┴──────┴┐
       │Integration│  ← data pipeline, DB, paper exec, dashboard data
      ┌┴──────────┴┐
      │  Property  │  ← safety invariants via Hypothesis
     ┌┴────────────┴┐
     │    Unit      │  ← features, EV, risk, sizing, CB, state machines
```

### 7.2 Unit Tests (target: >80% coverage)

| Module | Key Test Cases |
|---|---|
| `features/` | OBI calculation, spread, relative spread, momentum windows, rolling vol, liquidity estimate |
| `ev/` | Gross edge, net edge with costs, uncertainty penalty, fee modelling |
| `risk/` | Hard limits, market exposure, total exposure, daily loss, consecutive loss counter |
| `risk/circuit_breaker.py` | Every trigger condition, persistence, reset, restart |
| `risk/position_sizing.py` | Fixed-risk size, fractional Kelly cap, hard limit override |
| `strategies/` | Signal shape (0–1 probability), confidence bounds, rejection for missing data |
| `models/calibration.py` | Platt scaling, isotonic regression, Brier score, reliability diagram |
| `execution/state_machine.py` | All state transitions, illegal transitions, unknown-state handling |

### 7.3 Property-Based Tests (Hypothesis)

| Invariant | Description |
|---|---|
| `position_never_exceeds_max` | For any sequence of signals and fills, position size ≤ MAX_POSITION_SIZE |
| `halted_system_cannot_submit` | When mode is HALTED, `submit()` raises or returns no-op |
| `stale_data_blocks_orders` | If `now - source_timestamp > DATA_MAX_AGE`, risk engine returns REJECT |
| `negative_net_edge_rejected` | If net_edge < MIN_NET_EDGE, pipeline produces NO TRADE |
| `unknown_order_state_no_duplicate` | Unknown order status triggers reconciliation, never blind resubmit |
| `no_martingale` | After a losing trade, position size never increases |

### 7.4 Integration Tests

| Scenario | Description |
|---|---|
| Data pipeline | Mock Polymarket API → normalizer → snapshot DB write → read back |
| Paper execution | Signal → risk → paper submit → order lifecycle → portfolio update |
| Dashboard data | DB write → Streamlit data query → correct rendering |
| Circuit breaker persistence | CB triggered → restart → CB state loaded → system stays HALTED |

### 7.5 Failure Tests

| Scenario | Expected Behaviour |
|---|---|
| API timeout | Data validation fails → stale data → NO TRADE |
| WebSocket disconnect | Reconnect with backoff; stale flag → HALT |
| DB unavailable | Risk engine halts; mode transitions to HALTED |
| Process restart | Recovery sequence: load mode → reconcile → health check → stay HALTED |
| Corrupted model file | Model loading fails → model unavailable → HALT |

### 7.6 CI / Automation

- Run unit + property tests on every PR/commit.
- Run integration tests nightly or on demand.
- Run failure tests before mode promotion (PAPER → LIVE_GUARDED).
- Linting: `ruff` + `mypy` (strict mode).

---

## 8. Risk-Control Strategy

### 8.1 Principle: Fail Closed

Every safety dependency is a single point of failure that defaults to **NO TRADE / HALTED**:

| Dependency | Failure Mode | System Response |
|---|---|---|
| Data source (API) | Timeout, disconnect | Stale data flag → NO TRADE |
| WebSocket | Disconnect | Stale data flag → HALT after grace period |
| Database | Unavailable | HALT — no persistence, no risk state |
| Probability model | Corrupted, missing | HALT — model unavailable |
| Risk engine | Exception | HALT — no risk approval possible |
| Circuit breaker | State corruption | Load default HALTED; operator must clear |

### 8.2 Mode State Machine

```
                   ┌────────────┐
                   │  RESEARCH  │
                   └──────┬─────┘
                          │ operator action
                          ▼
                   ┌────────────┐
           ┌──────►│  BACKTEST  │◄──────┐
           │       └──────┬─────┘       │
           │              │ pass criteria│
           │              ▼              │
           │       ┌────────────┐        │
           │       │   PAPER    │────────┘
           │       └──────┬─────┘  (fail criteria)
           │              │ pass criteria
           │              ▼
           │       ┌──────────────┐
           │       │LIVE_GUARDED  │
           │       └──────┬───────┘
           │              │ operator or CB
           │              ▼
           │       ┌────────────┐
           └───────┤  HALTED    │
                   └────────────┘
```

- All transitions require explicit operator action (except HALTED, which can be triggered automatically).
- On startup, system always loads into HALTED mode until health checks pass.
- `LIVE_GUARDED` enforces lower risk limits than PAPER.

### 8.3 Circuit Breaker Persistence

The circuit breaker state is persisted to SQLite:
```json
{
  "breakers": ["DAILY_LOSS", "STALE_DATA"],
  "triggered_at": "2026-07-28T14:22:00Z",
  "mode_before": "PAPER"
}
```

On restart, breakers are loaded and system starts HALTED. Operator must explicitly clear breakers and transition out of HALTED.

### 8.4 Stale Data Handling

```
DATA_MAX_AGE_SECONDS = 5  (configurable)

per-market timer:
  if now - last_valid_timestamp > DATA_MAX_AGE_SECONDS:
      market_flags[market_id] = STALE
      market_eligible[market_id] = False

global timer:
  if any market is STALE:
      circuit_breaker.trigger_if_enabled("STALE_DATA")
```

### 8.5 Execution Safety

| Guard | Implementation |
|---|---|
| Duplicate orders | Client-order-ID generated per signal; idempotency key on Polymarket |
| Unexpected positions | Startup reconciliation compares DB positions to on-chain positions |
| Partial fills | State machine tracks filled vs requested; no auto-resubmission |
| Rejected orders | Logged, audit trail updated, no blind retry |
| Emergency stop | Kill-switch in dashboard sets mode to HALTED and cancels open orders |

### 8.6 No Martingale Enforcement

This is a **hard-coded invariant**, not a configuration option:
- After a losing trade, position size for the next trade of the same strategy is computed independently (no carry-over).
- The risk engine has no memory of prior position sizes.
- Position sizing is always based on current equity × fixed percentage.
- Test: `property/test_safety_properties.py::test_no_martingale` verifies this.

---

## Summary

### Architecture Summary

The Polymarket Quant Bot is a **risk-first, fail-closed** quantitative trading platform that follows a strict sequential pipeline: Market Discovery → Data Collection → Validation → Feature Engineering → Strategy Engine → Probability Model → EV Engine → Risk Engine → Paper/Live Execution → Portfolio → Database → Dashboard. Capital preservation is the primary objective; every trade must pass data freshness, risk limit, and circuit breaker checks. The system enforces explicit mode gating (`RESEARCH → BACKTEST → PAPER → LIVE_GUARDED`), requires operator-initiated transitions, and maintains a complete audit trail of every signal, order, and risk event. Polymarket API adapters are fully isolated from the strategy and risk layers.

### Files That Should Be Created

- `app/config/settings.py`
- `app/data/gamma.py`, `clob.py`, `websocket.py`, `normalizer.py`, `validators.py`
- `app/discovery/eligibility.py`, `scanner.py`
- `app/features/orderbook.py`, `momentum.py`, `volatility.py`, `liquidity.py`, `market_quality.py`
- `app/strategies/base.py`, `microstructure.py`, `arbitrage.py`, `probability.py`, `ensemble.py`
- `app/models/calibration.py`, `probability_model.py`
- `app/ev/costs.py`, `expected_value.py`
- `app/risk/limits.py`, `circuit_breaker.py`, `position_sizing.py`
- `app/execution/interface.py`, `paper.py`, `polymarket.py`, `state_machine.py`
- `app/portfolio/tracker.py`
- `app/storage/db.py`, `repositories.py`
- `app/monitoring/health.py`, `alerts.py`
- `app/audit/logger.py`
- `app/modes/state.py`
- `app/backtesting/backtester.py`, `walk_forward.py`
- `app/reconciliation/reconciler.py`
- `app/dashboard/app.py` + pages + components
- `app/main.py` (orchestrator)
- `.env.example`, `.gitignore`, `requirements.txt`, `pyproject.toml`
- `Dockerfile`, `docker-compose.yml`
- All `tests/` files
- All `scripts/` files

### Files That Should Be Modified

- `dashboard/index.html` — add a comment at top: `<!-- DEPRECATED: replaced by Streamlit dashboard in app/dashboard/ -->`
- `dashboard/README.md` — add deprecation notice pointing to `app/dashboard/`

### Recommended Implementation Order

```
Phase 0: ARCHITECTURE.md + repository scaffold + .env + .gitignore + requirements.txt
Phase 1: config/settings.py + storage/db.py + storage/repositories.py
Phase 2: data/* + discovery/*
Phase 3: features/* + strategies/*
Phase 4: models/* + ev/* + risk/*
Phase 5: backtesting/*
Phase 6: execution/interface.py + execution/paper.py + execution/state_machine.py + portfolio/*
Phase 7: dashboard/* + monitoring/* + audit/* + modes/*
Phase 8: execution/polymarket.py + reconciliation/*
Phase 9: Docker + scripts/* + hardening
```

### Unresolved Questions

1. **Model retraining lifecycle**: What triggers retraining? How are model versions tracked? What is the rollback procedure? (Needs operator decision.)
2. **Polymarket sandbox availability**: Does Polymarket offer a testnet/sandbox for API integration testing? If not, paper execution with mock data is the only pre-live validation path.
3. **Fee modelling**: What are the exact current Polymarket fee mechanics (maker/taker, flat fee, percentage)? The TDD says "must be tested against actual settlement mechanics before live use."
4. **Historical data source**: Is there access to historical Polymarket order-book data, or will data collection begin from scratch? Backtesting quality depends on this.
5. **Portfolio correlation threshold**: What is the acceptable `MAX_PORTFOLIO_CORRELATION` value? Disabled for MVP, but must be defined before live trading.
6. **Multi-outcome markets**: Some Polymarket markets have more than two outcomes (e.g., "Which party wins?" with multiple choices). The current schema assumes binary (YES/NO) markets. How should multi-outcome markets be handled?
7. **Dashboard deployment**: Should the Streamlit dashboard be a separate Docker container (as suggested by docker-compose.yml) or embedded in the bot process? Separate container is recommended for isolation.
8. **Concurrent strategy conflicts**: If S1 and S2 generate opposing signals for the same market, how is the conflict resolved? Ensemble strategy handles this, but the tie-breaking rule is unspecified.
9. **Data retention policy**: How long are market snapshots retained? SQLite may grow unbounded in live mode. A retention/archival policy is needed before live deployment.
