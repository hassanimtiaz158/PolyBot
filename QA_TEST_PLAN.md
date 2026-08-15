# Polymarket Quant Bot — QA Test Plan

**Version:** 1.0  
**Date:** 2026-08-15  
**Status:** Active

---

## 1. Scope

This test plan covers end-to-end verification of the Polymarket Quant Bot in **PAPER MODE ONLY**. No real-money trades, no live wallet connections, no LIVE mode enablement.

**System Under Test:**  
- Data ingestion (Gamma, CLOB, WebSocket)
- Feature engineering (orderbook, momentum, volatility, liquidity, market quality)
- Strategies (Microstructure, Arbitrage, Probability stub, Ensemble stub)
- Probability model (logistic regression, calibration)
- Expected Value engine (gross edge, costs, net edge)
- Risk Engine (all limits, circuit breaker, kill switch)
- Paper execution (simulated fills, slippage, partial fills)
- Portfolio tracking (positions, P&L, equity, exposure)
- FastAPI backend (health, dashboard endpoints)
- WebSocket broadcast (real-time updates)
- Streamlit dashboard (9 pages, mode indicators, data consistency)
- Database (SQLite schema, migrations, repositories)

---

## 2. Test Environment

| Component | Version/Config |
|-----------|----------------|
| Python | 3.11+ |
| Database | SQLite (data/polymarket.db) |
| Mode | PAPER (default RESEARCH for tests) |
| API Keys | None (research mode) |
| Dependencies | requirements.txt (pinned) |

---

## 3. Test Phases

### Phase 1: Static Code Quality ✓
- [ ] Formatter (ruff format)
- [ ] Linter (ruff check)
- [ ] Type checker (mypy)
- [ ] Import checker
- [ ] Security scan (secrets, unsafe patterns)

### Phase 2: Environment & Startup ✓
- [ ] Python version check
- [ ] Dependencies install
- [ ] Database initialization
- [ ] Application startup (app.main)
- [ ] Health checks pass

### Phase 3: Database Testing
- [ ] Schema creation (all tables)
- [ ] CRUD operations (all repositories)
- [ ] Duplicate handling
- [ ] Transaction rollback
- [ ] Index verification
- [ ] Migration handling
- [ ] Persistence after restart

### Phase 4: Data Ingestion & Validation
- [ ] Gamma adapter (mocked)
- [ ] CLOB adapter (mocked)
- [ ] WebSocket manager (connect, reconnect, malformed)
- [ ] Normalizer (Gamma → Market, CLOB → Snapshot)
- [ ] Validators (HEALTHY, STALE, INVALID, DISCONNECTED)
- [ ] Stale data → NO TRADE
- [ ] Invalid data → NO TRADE

### Phase 5: Feature Engineering
- [ ] Orderbook features (midpoint, spread, OBI, depth)
- [ ] Momentum (returns, velocity)
- [ ] Volatility (rolling realised vol)
- [ ] Liquidity (depth, volume, spread score)
- [ ] Market quality (composite score)
- [ ] Edge cases: NaN, Inf, zero, missing, extreme
- [ ] No look-ahead bias

### Phase 6: Strategy Testing
- [ ] MicrostructureStrategy (OBI, edge, confidence gates)
- [ ] ArbitrageStrategy (IMPLIES, MUTUALLY_EXCLUSIVE, COMPLEMENT, SUM_CONSTRAINT)
- [ ] ProbabilityStrategy (stub → NO_SIGNAL)
- [ ] EnsembleStrategy (stub → NO_SIGNAL)
- [ ] All strategies return NO_SIGNAL appropriately

### Phase 7: Probability Model
- [ ] Model training (dummy data)
- [ ] Inference (probability in [0,1])
- [ ] Calibration (Platt, isotonic)
- [ ] Metrics (Brier, log loss, ECE)
- [ ] Missing features → zero fill + warning
- [ ] Unavailable model → NO_TRADE

### Phase 8: Expected Value Engine
- [ ] Gross edge calculation
- [ ] Cost estimation (spread, slippage, fees, partial fill, uncertainty)
- [ ] Net edge = gross - total_cost
- [ ] Tradeable when net_edge ≥ min_net_edge
- [ ] Scenarios: positive/negative gross, high spread, low liquidity

### Phase 9: Risk Engine (HIGHEST PRIORITY)
- [ ] Data freshness check
- [ ] Data validity check
- [ ] Spread limit
- [ ] Liquidity limit
- [ ] Net edge threshold
- [ ] Confidence threshold
- [ ] Position size limit
- [ ] Market exposure limit
- [ ] Total exposure limit
- [ ] Daily loss limit
- [ ] Consecutive losses limit
- [ ] Max open positions
- [ ] Circuit breaker (NORMAL→WARNING→HALTED)
- [ ] Kill switch (ACTIVE→KILLED)
- [ ] Correlation limits (event, strategy, directional, resolution)
- [ ] REJECTED → ExecutionEngine NEVER executes
- [ ] Multiple violations → most restrictive wins

### Phase 10: Circuit Breaker & Kill Switch
- [ ] NORMAL → WARNING (soft trigger)
- [ ] WARNING → HALTED (escalation)
- [ ] NORMAL → HALTED (hard trigger)
- [ ] HALTED persists across restart
- [ ] HALTED → NORMAL only via explicit clear
- [ ] Kill switch engaged → no new orders
- [ ] Kill switch resume requires confirmation
- [ ] Kill switch persists across restart

### Phase 11: Paper Execution
- [ ] Order creation & validation
- [ ] Risk approval gate
- [ ] Submission → fill simulation
- [ ] Partial fill handling
- [ ] Full fill handling
- [ ] Cancellation
- [ ] Rejection
- [ ] Slippage & latency simulation
- [ ] Duplicate order protection
- [ ] State machine (CREATED→RISK_APPROVED→SUBMITTED→FILLED/PARTIAL/REJECTED)

### Phase 12: Portfolio Tracking
- [ ] Position creation (additive)
- [ ] Position update (weighted avg entry)
- [ ] Position closing (opposite side)
- [ ] Realised P&L
- [ ] Unrealised P&L (mark-to-market)
- [ ] Total equity
- [ ] Drawdown
- [ ] Multiple positions/markets
- [ ] Correlated exposure queries

### Phase 13: FastAPI Backend
- [ ] GET /health
- [ ] GET /api/dashboard/overview
- [ ] GET /api/dashboard/equity
- [ ] GET /api/dashboard/signals
- [ ] GET /api/dashboard/markets
- [ ] GET /api/dashboard/positions
- [ ] GET /api/dashboard/orders
- [ ] GET /api/dashboard/performance
- [ ] GET /api/dashboard/risk
- [ ] GET /api/dashboard/health
- [ ] GET /api/dashboard/audit
- [ ] 200 responses, empty responses, invalid params
- [ ] Auth middleware (when POLY_API_KEY set)
- [ ] Response schema validation

### Phase 14: WebSocket Broadcast
- [ ] Connection / rejection
- [ ] Initial state
- [ ] Market/signal/position/order/P&L/risk/health updates
- [ ] Disconnect / reconnect
- [ ] Multiple clients
- [ ] Malformed/duplicate events
- [ ] Server restart handling

### Phase 15: Dashboard Functional
- [ ] Overview page (KPIs, health, recent activity)
- [ ] Signals page (table, filters, decisions)
- [ ] Markets page (list, eligibility, snapshots)
- [ ] Positions page (open, P&L, exposure)
- [ ] Risk page (utilisation, circuit breaker, limits)
- [ ] Performance page (P&L, equity curve)
- [ ] Execution page (orders, fills, statuses)
- [ ] Audit page (event log, filters)
- [ ] Settings page (config view)
- [ ] Navigation, loading, empty, error states
- [ ] Paper/Live mode indicator
- [ ] No fake data when backend unavailable

### Phase 16: Dashboard Data Consistency
- [ ] Balance matches backend
- [ ] P&L matches backend
- [ ] Exposure matches backend
- [ ] Positions match backend
- [ ] Orders match backend
- [ ] Signals match backend
- [ ] Risk metrics match backend
- [ ] Drawdown matches backend
- [ ] No frontend-only calculations contradicting backend

### Phase 17: End-to-End Paper Scenarios
- [ ] **Scenario A:** Valid profitable signal → approved paper trade
- [ ] **Scenario B:** Low edge → rejected
- [ ] **Scenario C:** Stale data → rejected
- [ ] **Scenario D:** Daily loss limit → rejected
- [ ] **Scenario E:** Kill switch → rejected
- [ ] **Scenario F:** Partial fill → position updated correctly
- [ ] **Scenario G:** Application restart → state recovered correctly

### Phase 18: Failure/Chaos Testing
- [ ] Database unavailable → HALTED
- [ ] Market data timeout → STALE → NO TRADE
- [ ] WebSocket disconnect → reconnect → stale flag
- [ ] Model corrupted → MODEL_UNAVAILABLE → NO TRADE
- [ ] API failure → API_HEALTH → HALTED
- [ ] Network partition → safe state
- [ ] Invalid config → startup failure

### Phase 19: Security Testing
- [ ] No hardcoded credentials in repo
- [ ] No API keys in source
- [ ] No private keys in source
- [ ] SQL injection prevention (parameterized queries)
- [ ] XSS prevention (Streamlit)
- [ ] CORS configuration
- [ ] Auth on control endpoints
- [ ] Secrets never in logs
- [ ] Frontend never receives secrets

### Phase 20: Performance Testing
- [ ] API response latency (< 500ms typical)
- [ ] Dashboard load time
- [ ] WebSocket latency
- [ ] Database query latency
- [ ] Feature calculation time
- [ ] Strategy calculation time
- [ ] RiskEngine latency
- [ ] Memory leaks check
- [ ] Excessive CPU check

### Phase 21: Regression Testing
- [ ] Run full existing test suite
- [ ] No previously passing tests fail
- [ ] Fix root causes, not test modifications

---

## 4. Test Data Strategy

| Data Type | Source |
|-----------|--------|
| Market metadata | Synthetic (scripts/seed_data.py or inline) |
| Order book snapshots | Synthetic with known values |
| Strategy signals | Deterministic from fixed features |
| Model predictions | Fixed probability outputs |
| Orders/fills | Paper execution simulator |
| Portfolio state | In-memory tracker + DB persistence |

---

## 5. Pass/Fail Criteria

| Severity | Definition | Action |
|----------|------------|--------|
| CRITICAL | Safety violation, data loss, money at risk | Block release |
| HIGH | Core functionality broken, risk limit bypassed | Block release |
| MEDIUM | Feature incomplete, incorrect calculation | Fix before paper |
| LOW | Cosmetic, minor UX, non-blocking | Track for next sprint |
| INFO | Observation, documentation gap | Document |

**Final Status Determination:**
- **NOT READY** — Any CRITICAL/HIGH open
- **READY FOR PAPER TRADING** — All CRITICAL/HIGH fixed, MEDIUM documented
- **READY FOR MANUAL LIVE REVIEW** — Paper trading stable, all tests pass, explicit operator sign-off

---

## 6. Test Execution Log

| Phase | Test | Status | Notes |
|-------|------|--------|-------|
| 1 | Ruff format |  |  |
| 1 | Ruff check |  |  |
| 1 | MyPy |  |  |
| 2 | Startup |  |  |
| 3 | Database init |  |  |
| 3 | CRUD |  |  |
| 4 | Data validators |  |  |
| 5 | Feature calcs |  |  |
| 6 | Strategies |  |  |
| 7 | Probability model |  |  |
| 8 | EV engine |  |  |
| 9 | Risk engine |  |  |
| 10 | Circuit breaker |  |  |
| 11 | Paper execution |  |  |
| 12 | Portfolio |  |  |
| 13 | API endpoints |  |  |
| 14 | WebSocket |  |  |
| 15 | Dashboard pages |  |  |
| 16 | Data consistency |  |  |
| 17 | E2E scenarios |  |  |
| 18 | Chaos tests |  |  |
| 19 | Security |  |  |
| 20 | Performance |  |  |
| 21 | Regression |  |  |

---

## 7. Bug Tracking Template

For each failed test:
- **Test Name:**
- **Expected Behavior:**
- **Actual Behavior:**
- **Reproduction Steps:**
- **Root Cause:**
- **Fix Applied:**
- **Regression Test Added:**
- **Severity:** CRITICAL/HIGH/MEDIUM/LOW/INFO

---

## 8. Final Report

Will generate `QA_REPORT.md` with:
- Executive Summary
- Environment
- Tests Executed/Passed/Failed/Blocked/Skipped
- Bugs Found/Fixed/Remaining
- Security/Performance/Risk/Dashboard/API/Database Findings
- End-to-End Results
- Production Readiness Status