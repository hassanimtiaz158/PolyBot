# Polymarket Quant Bot — Dashboard Architecture

**Author:** OpenCode review of `app/` (API, dashboard, storage, risk, orchestrator)
**Date:** 2026-08-11
**Status:** Design only — no trading logic is modified by this document.

---

## 1. Purpose and Governing Constraint

The dashboard is a **display and control surface**, never a trading system.

* **Display only** — the dashboard reads state through the read-only FastAPI
  backend (`app/api/`) or the deterministic `DemoProvider`. It never computes
  signals, never sizes positions, never submits orders, and never contains a
  copy of EV, risk, or execution logic.
* **Explicitly permitted control commands only** — the only write actions the
  dashboard may perform are safety/operator commands (halt, resume, clear
  breakers) that call the existing, already-safe modules
  (`app/modes/state.py`, `app/risk/circuit_breaker.py`). No other mutation is
  allowed from the UI.

This boundary is enforced at three layers:

1. **API layer**: the backend exposes only `GET` display endpoints plus the
   small, enumerated control set in §5.2. No endpoint accepts trade parameters.
2. **Client layer**: `app/dashboard/client.py` exposes fetch methods only;
   control commands are separate, explicit, and require confirmation.
3. **UI layer**: every page renders a mode banner and an explicit
   "read-only" caption; the execution page has no submit/cancel buttons.

---

## 2. Current State of the System (what the dashboard can show)

Verified from source:

| Subsystem | Files | What is available to display |
|---|---|---|
| FastAPI (read-only) | `app/api/app.py`, `app/api/routes/*.py` | health, system status, markets, signals, positions, orders, risk, performance, audit |
| Storage | `app/storage/db.py`, `models.py`, `repositories.py` | SQLite tables: `markets`, `market_snapshots`, `signals`, `orders`, `positions`, `risk_events`, `audit_events`, `circuit_breaker_state`, `_schema_version` |
| Risk | `app/risk/{engine,limits,circuit_breaker,correlation,position_sizing}.py` | hard limits (config), circuit-breaker state, exposure summary, rejection reasons, risk metrics |
| Monitoring | `app/monitoring/health.py` | `database`, `data_freshness`, `api`, `model_availability`, `execution`, `risk_engine` checks with last-updated timestamps |
| Modes | `app/modes/state.py` | `RESEARCH`, `BACKTEST`, `PAPER`, `LIVE_GUARDED`, `HALTED`; `trading_enabled`, `live_enabled` |
| Orchestrator | `app/orchestrator/{engine,pipeline,router}.py` | audit events for every signal, risk decision, order event; daily P&L and consecutive losses (in-memory only) |
| Dashboard | `app/dashboard/` | Streamlit app with 9 pages; mode banner; `ApiClient` + `DemoProvider`; demo/offline modes |

Existing API endpoints (all `GET`, all paginated):

| Endpoint | Route file | Returns |
|---|---|---|
| `/health` | `routes/health.py` | per-check booleans + `last_updated` |
| `/system/status` | `routes/status.py` | mode, trading/live enabled, DB connected, schema version, circuit breaker, uptime |
| `/markets` | `routes/markets.py` | markets list (filter by `status`) |
| `/signals` | `routes/signals.py` | signals (filter by `market_id`, `strategy`, `decision`) |
| `/positions` | `routes/positions.py` | positions (filter by `side`, `open_only`) |
| `/orders` | `routes/orders.py` | orders (filter by `market_id`, `status`) |
| `/risk` | `routes/risk.py` | exposure + configured limits + recent events |
| `/performance` | `routes/performance.py` | P&L totals + activity counts |
| `/audit` | `routes/audit.py` | audit trail (filter by `event_type`, `severity`) |

**Confirmed:** there are no `POST/PUT/DELETE` routes in `app/api/`
(grep verified). The API is read-only by construction.

### 2.1 Data gaps the dashboard cannot show today

These are displayable data that either exist but are not exposed, or exist
only in memory and are not persisted:

1. **Price/snapshot time series** — `market_snapshots` rows are written
   (`SnapshotRepository.insert`) but no endpoint reads them → no price charts.
2. **Equity curve** — not persisted anywhere. The Performance page calls a
   `equity_history` method that only exists on `DemoProvider`
   (`app/dashboard/client.py:493`); in live mode it shows
   "Equity history is not persisted by the bot."
3. **Today P&L, daily loss utilisation, consecutive losses, max drawdown** —
   tracked in `Orchestrator._daily_pnl` / `_consecutive_losses`
   (`app/orchestrator/engine.py:83-84`) but never persisted or exposed. The
   Overview/Risk pages render `"—"`.
4. **Market eligibility score** — computed by `MarketEligibility`
   (`app/discovery/eligibility.py`) but not persisted per market; the Markets
   page currently reads a non-existent `eligible` field.
5. **Risk-metric snapshots per decision** — `RiskDecision.risk_metrics` is
   detailed but only appears inside audit event `details` JSON.

Items 1–5 are **backend/persistence dependencies** for a richer dashboard.
They do not add trading logic; they only record or expose state the bot
already produces. They are phased in §10 as bot-side additions that the
dashboard consumes.

---

## 3. Architecture Overview

```
┌──────────────┐        ┌─────────────────────────────┐
│  Bot process │        │  FastAPI read-only backend  │
│  app.main    │        │  app/api  (GET + control)   │
│  orchestrator│───────►│                             │
│  risk, exec  │  writes│  /health /status /markets   │
│  storage     │   SQLite│  /signals /positions /orders│
└──────────────┘        │  /risk /performance /audit  │
                        │  /markets/{id}/snapshots    │  ← new read-only
                        │  /control/*   (halt/resume/ │  ← permitted control
                        └──────────────┬──────────────┘
                                       │  HTTPS (JSON)
                        ┌──────────────▼──────────────┐
                        │  Streamlit dashboard        │
                        │  app/dashboard/app.py       │
                        │  pages/*  components/*      │
                        │  ApiClient | DemoProvider   │
                        └─────────────────────────────┘
```

Rules:

* The dashboard is a **separate process** (already reflected in
  `docker-compose.yml` — `api`, `bot`, `dashboard` services). It never opens
  the database directly.
* All reads go through the API. All writes (control commands only) go through
  the API to the bot's existing safety modules.
* `DemoProvider` returns payloads with the exact same shape as the API so
  every page has one code path and the demo cannot diverge.
* Dashboard code must never import from `app/risk`, `app/ev`,
  `app/strategies`, or `app/execution`. (It already only imports
  `app/config/settings.py` and its own modules.)

---

## 4. Dashboard Pages

Nine pages exist (`app/dashboard/pages/`). The table below defines the
purpose, data source endpoint, and required states for each.

| # | Page | File | Purpose | Source endpoints |
|---|---|---|---|---|
| 1 | Overview | `pages/overview.py` | headline KPIs, health, status, recent audit + signals | `/health`, `/performance`, `/risk`, `/system/status`, `/signals`, `/markets`, `/audit` |
| 2 | Signals | `pages/signals.py` | strategy signals, edges, decisions | `/signals`, `/markets` (liquidity join) |
| 3 | Markets | `pages/markets.py` | tracked markets, liquidity, status, eligibility | `/markets`, `/markets/{id}/snapshots` |
| 4 | Positions | `pages/positions.py` | open positions, entry/current, P&L, risk | `/positions`, `/markets` (resolution join) |
| 5 | Risk | `pages/risk.py` | exposure, limits, circuit breaker, health, events | `/risk`, `/system/status`, `/health` |
| 6 | Performance | `pages/performance.py` | P&L totals + equity curve | `/performance`, `/performance/equity` (new) |
| 7 | Execution | `pages/execution.py` | order history, fills, statuses | `/orders` |
| 8 | Audit | `pages/audit.py` | event log with filters | `/audit` |
| 9 | Settings | `pages/settings.py` | read-only config view | `/risk` (limits), `/system/status` |

Proposed new pages (optional, future):

| # | Page | Purpose |
|---|---|---|
| 10 | Market detail | single-market price chart, spread, depth, signals, orders, positions |
| 11 | Controls (safety) | halt / resume / clear breakers — the only writable page (§5.2) |

### 4.1 Page-level content specification

**Overview**
- KPI row 1: API / Database / Data freshness / Model (healthy or not).
- KPI row 2: Equity (est.), Today P&L, Total P&L, Max drawdown.
- KPI row 3: Open exposure, Active signals, Eligible markets, Risk utilisation
  (`exposure / (equity × max_total_exposure_pct)`).
- System status table (mode, trading enabled, DB connected, schema, breaker,
  version, uptime).
- Recent audit events + active signals tables.

**Signals**
- KPIs: total signals, by decision (BUY/SELL/PASS → mapped from
  `decision` values, note the bot emits `CANDIDATE`/`NO_SIGNAL`).
- Table: market, side, price, model P, implied P, gross edge, net edge,
  confidence, liquidity, spread, decision, rejection reason.
- Caption: "Spread is not persisted by the bot" (accurate today — see §2.1).

**Markets**
- KPIs: tracked, active, eligible, total liquidity.
- Table: market id, question, status, YES/NO price, liquidity, eligible,
  updated.

**Positions**
- KPIs: open positions, unrealised P&L, notional exposure.
- Table: market, side, entry, current, size, unrealised P&L, risk (notional),
  time-to-resolution.

**Risk**
- KPIs: daily loss, total exposure, market exposure, consecutive losses,
  data freshness, API health, circuit breaker, open positions.
- Circuit-breaker panel (state, reasons, triggered_at) with success/error
  colouring.
- Configured limits table.
- Recent risk events table.

**Performance**
- KPIs: total/realised/unrealised P&L, open positions, markets, signals,
  orders, filled orders.
- Equity history chart (new endpoint; falls back to a clear "not persisted"
  empty state).

**Execution**
- KPIs: total/open/filled/rejected/cancelled orders.
- Order table with status filter; read-only caption (no buttons).

**Audit**
- Event-type text filter + severity select.
- Event table with pagination count.

**Settings**
- Dashboard connection info (URL, demo flag, timeout).
- Bot status table.
- Risk limits table.

---

## 5. API Endpoints Required

### 5.1 Read-only (display) endpoints — already implemented

Covered in §2. No changes needed for basic operation.

### 5.2 New read-only endpoints (display only, no logic)

| Endpoint | Response | Why |
|---|---|---|
| `GET /markets/{market_id}/snapshots?limit&offset` | recent `market_snapshots` rows (bid, ask, midpoint, spread, bid/ask depth, volume, ttr) | price charts on Market detail |
| `GET /performance/equity?since=` | equity curve points (derived from persisted order/position P&L; see §10 note) | Performance chart in non-demo mode |
| `GET /risk/utilisation` | daily-loss, exposure, consecutive-loss, drawdown utilisation vs limits | fills the `"—"` KPIs; requires the persistence dependency in §10 |

### 5.3 Control commands — the ONLY writable endpoints

These are **safety/operator commands**, not trading commands. They map 1:1 to
existing safe modules and never carry trade parameters.

| Endpoint | Method | Action (delegated to) | Guarantees |
|---|---|---|---|
| `/control/halt` | `POST` | `ModeState.transition(HALTED)` + emit `SYSTEM_STOP`-style audit event | idempotent; trading disabled; no order actions |
| `/control/resume` | `POST` body `{"mode": "..."}` | `ModeState.transition(target)` (validated against `_VALID_TRANSITIONS` in `app/modes/state.py:21`) | invalid target → 422; never auto-promotes |
| `/control/breakers/clear` | `POST` | `CircuitBreaker.clear_all()` | explicit operator action only |
| `/control/breakers/clear/{reason}` | `POST` | `CircuitBreaker.clear(reason)` | removes one trigger |

Rules for control endpoints:

1. They must **always** require an API key, even when `POLY_API_KEY` is unset
   (research mode) — otherwise a public endpoint could halt the bot. A
   separate `POLY_CONTROL_KEY` env var is recommended.
2. They never accept `side`, `price`, `size`, or any market selection.
3. They return the resulting state (`SystemStatusResponse`) so the UI can
   refresh the banner.
4. Every command is written to the audit trail (`EventBus.emit`) with the
   operator identity (from auth) recorded.
5. The dashboard renders these on a dedicated **Controls** page only, with a
   typed confirmation step; no control button appears on any display page.

### 5.4 Endpoint security summary

| Surface | Auth |
|---|---|
| `/health`, `/docs`, `/openapi.json`, `/redoc` | public (already in `_PUBLIC_PATHS`, `app/api/app.py:49`) |
| All other `GET` | `X-API-Key` required when `POLY_API_KEY` set (existing middleware) |
| `/control/*` | always `X-API-Key` (recommend dedicated `POLY_CONTROL_KEY`) |

---

## 6. Database Queries Required

All reads go through `app/storage/repositories.py`. The dashboard never
queries SQLite directly; these are the queries the API layer performs on the
dashboard's behalf.

### Already implemented (repositories)

| Query | Repository method |
|---|---|
| Paginated markets, filter by status | `MarketRepository.list_paginated` |
| Paginated signals, filter by market/strategy/decision | `SignalRepository.list_paginated` |
| Paginated positions, filter by side/open | `PositionRepository.list_paginated` |
| Paginated orders, filter by market/status | `OrderRepository.list_paginated` |
| Total exposure | `PositionRepository.total_exposure` |
| Open position count | `PositionRepository.count(open_only=True)` |
| P&L totals | `PositionRepository.pnl_summary` |
| Paginated risk/audit events | `RiskEventRepository.list_paginated` |
| Circuit-breaker state | direct `SELECT` in `routes/status.py:57` |

### Required (new, read-only)

| Query | Location | For |
|---|---|---|
| `SELECT ... FROM market_snapshots WHERE market_id = ? ORDER BY timestamp DESC LIMIT ?` | `SnapshotRepository` (method exists; expose via API) | price charts |
| `SELECT SUM(realised_pnl) ... FROM orders`-style equity bucketed by day | new repo method or view | equity curve |
| Per-day P&L and consecutive-loss aggregate | new repo method on orders/positions + `Orchestrator` persistence | `/risk/utilisation` |

Indexes already exist to support these:
`idx_snapshots_market_ts`, `idx_signals_market_ts`, `idx_orders_market_status`,
`idx_risk_events_ts`, `idx_positions_open` (`app/storage/db.py:122-133`).

---

## 7. Frontend Components

Existing (`app/dashboard/components/`):

| Component | File | Status |
|---|---|---|
| Mode banner / sidebar badge | `components/banner.py` | complete — the paper/live indicator (§12) |
| KPI cards | `components/cards.py` | complete — `kpi_row`, `money_card`, `pct_card` |
| Charts | `components/charts.py` | **empty stub** — needs a `price_chart` / `equity_chart` helper |
| Tables | `components/tables.py` | **empty stub** — needs a shared paginated dataframe helper |

Required additions:

1. **`components/state.py`** (new) — reusable renderers for the three UI
   states:
   - `render_loading(label)` → `st.spinner` + skeleton metrics.
   - `render_error(message)` → `st.error` with retry button.
   - `render_empty(title, hint)` → `st.info` with a consistent hint string.
2. **`components/charts.py`** — `price_series_chart(snapshots)`,
   `equity_curve_chart(points)`, `spread_chart(snapshots)`.
3. **`components/tables.py`** — `paginated_table(rows, page_size)` that wires
   `offset/limit` back to the API (Streamlit `st.dataframe` + pager buttons or
   `st.column_config`).
4. **`components/controls.py`** (new) — the confirmation-gated buttons that
   call control endpoints only (§5.3).
5. **`common.py`** — add typed fetch helpers with default-shapes so pages
   never touch raw dicts twice; add `session.fetch_with_meta()` returning
   `(data, error, stale)` so loading/error/empty states share one path.

---

## 8. Real-Time Update Strategy

Constraint: SQLite backend, no Redis. Chosen strategy for MVP is
**polling with auto-refresh**, with a documented upgrade path to SSE.

### 8.1 MVP: bounded polling

* Each page wraps its data section in a `st.fragment(run_every=…)`:
  * Overview / Risk / Controls: `5s`.
  * Signals / Markets / Positions / Execution / Audit: `15s`.
  * Performance: `60s`.
* `DashboardSession.fetch` records `last_success` and `last_failure` per page
  so a single failed call degrades gracefully to "stale" instead of dropping
  content (see §9).
* Polling interval is configurable via `DASHBOARD_REFRESH_SECONDS` in
  `app/dashboard/config.py`.

### 8.2 Upgrade path: server-sent events (SSE)

* Add `GET /stream` (SSE) on the FastAPI backend that forwards selected
  `EventBus` events (`CIRCUIT_BREAKER`, `RISK_REJECTED`, `ORDER_*`,
  `SYSTEM_*`) to connected dashboards.
* The dashboard subscribes and applies the event to refresh the banner +
  relevant tables immediately, while keeping polling as a fallback heartbeat.
* Not implemented now — do not add until the polled version is proven.

### 8.3 Backoff rules

* On repeated failures, double the page's refresh interval up to `60s`.
* When the API is unreachable, switch the whole dashboard to the existing
  `OFFLINE` mode banner (§12) and stop background polling (only the
  connection-check interval remains).

---

## 9. Error / Loading / Empty States

These three states are required on **every** data-driven widget. They are
rendered through the shared components in §7.1 so behaviour is consistent.

### Error states

| Condition | Detection | UI |
|---|---|---|
| API unreachable (connection refused) | `ApiError` from `ApiClient`; `session.offline` becomes `True` | whole-app `BOT OFFLINE` banner (§12); pages show `st.error("Bot API unreachable — no X available.")` |
| HTTP error (503 DB down, 500) | non-200 response | page-level `st.error` with `X-Request-ID` when present; retry button |
| 401 auth | API key rejected | `st.error` with guidance to set `POLY_API_KEY` / `DASHBOARD_API_URL` |
| 422 validation (e.g. bad filter) | response payload | `st.warning`; keep last good data |
| Per-endpoint failure while others work | per-fetch catch | keep last successful snapshot, show "stale — updated HH:MM:SS" caption, never blank the page |

### Loading states

* First load of a page: `st.spinner("Loading …")` and disabled metric
  skeletons so layout does not jump.
* In-page refresh (`fragment`): no spinner; existing data stays visible until
  replacement arrives (Streamlit fragment semantics preserve prior content).

### Empty states

| Page / widget | Empty-state copy (all use `st.info`) |
|---|---|
| Signals | "No signals recorded." |
| Markets | "No markets tracked." |
| Positions | "No open positions." |
| Orders | "No orders recorded." / "No orders match the status filter." |
| Audit | "No audit events match the filters." |
| Risk events | "No risk events recorded." |
| Equity chart | "Equity history is not persisted yet by the bot." (accuracy per §2.1) |
| Overview activity | "No active signals." / "No audit events recorded." |

---

## 10. Backend Dependencies (phased, no trading logic)

The following bot-side changes are *required by the dashboard* but do not
change trading behaviour — they only persist/expose state already produced.
They are listed here so they are implemented in the storage/API layer, never
in the dashboard.

| Phase | Change | Files | Enables |
|---|---|---|---|
| A | Expose snapshots via API | `app/api/routes/markets.py` (add `GET /markets/{id}/snapshots`) | price charts |
| B | Persist daily P&L / consecutive losses / equity snapshot on a timer | `app/orchestrator/engine.py` + new `equity_snapshots` table + repo | `/risk/utilisation`, equity curve |
| C | Persist per-market eligibility score | `app/discovery/scanner.py` + `markets.eligibility_score` column | accurate Eligible KPIs |
| D | Add control endpoints (§5.3) with `POLY_CONTROL_KEY` | `app/api/routes/control.py` + auth middleware | Controls page |

Each phase is additive; none alters signal generation, risk gating, or
execution.

---

## 11. Authentication Requirements

1. **API key middleware** already exists (`APIKeyAuthMiddleware`,
   `app/api/app.py:83`): all non-public paths require `X-API-Key` when
   `POLY_API_KEY` is set. Timing-safe comparison via `secrets.compare_digest`.
2. **Display reads**: `X-API-Key` = `POLY_API_KEY` (dashboard reads this from
   `.env`; `ApiClient` sends the header).
3. **Control commands**: require `POLY_CONTROL_KEY` (a separate, stronger
   secret). When unset, `/control/*` returns 503 (disabled) — never open.
4. **CORS**: restrict `allow_origins` to the dashboard origin
   (`http://localhost:8501`) in production instead of `*`
   (`app/api/app.py:166`).
5. **Dashboard process**: bind to `127.0.0.1` (already in
   `docker-compose.yml` ports) or put behind a reverse proxy with basic auth.
6. **Secrets policy** (unchanged): keys live in `.env`, ignored by Git; never
   serialized in any API response or audit event (`app/api/app.py` docstring).

---

## 12. Paper / Live Mode Indicators

Handled by `app/dashboard/components/banner.py` — already implemented and
tested (`tests/unit/test_dashboard_app.py`).

| Mode | Badge | Colour | Meaning |
|---|---|---|---|
| `DEMO` | DEMO — SYNTHETIC DATA | grey | fabricated data, bot not connected |
| `RESEARCH` | RESEARCH | green | no trading |
| `BACKTEST` | BACKTEST | green | historical replay only |
| `PAPER` | PAPER TRADING | blue | simulated fills, no real orders |
| `LIVE_GUARDED` | LIVE GUARDED — REAL ORDERS | red (alert) | real orders under reduced limits |
| `HALTED` | HALTED | dark red (alert) | no new orders |
| `OFFLINE` | BOT OFFLINE | slate (alert) | API unreachable |
| `UNKNOWN` | STATUS UNKNOWN | slate | no valid mode reported |

Rules:

* The banner is rendered at the top of **every** page and in the sidebar
  (`app/dashboard/app.py:27`).
* `LIVE_GUARDED` and `OFFLINE` are always rendered as alerts.
* The banner text is the authoritative paper/live indicator — derived from
  `/system/status.mode` (live source) or `DemoProvider.status()` (demo).
* A persistent caption under the sidebar banner states the read-only policy:
  "Read-only dashboard. No orders can be submitted from here."

---

## 13. Non-Goals and Guardrails

The dashboard must NEVER:

* contain order-book, feature, EV, risk, or position-sizing calculations;
* import `app/risk`, `app/ev`, `app/strategies`, `app/execution`,
  `app/portfolio`, `app/orchestrator` (verified against current imports);
* submit, cancel, or modify orders of any kind (paper or live);
* change risk limits or strategy enablement from the UI;
* expose credentials, private keys, or `POLY_SECRET` values;
* auto-promote the bot out of `HALTED` (resume is explicit and validated);
* connect to the database directly or to Polymarket directly.

The dashboard MAY, via §5.3 only: halt the bot, resume from a halt to a valid
mode, and clear circuit breakers — each logged, keyed, and confirmed.

---

## 14. Test Plan

Extend the existing dashboard tests (`tests/unit/test_dashboard_app.py`,
`tests/unit/test_dashboard_client.py`, `tests/integration/test_dashboard_data.py`):

| Area | Test |
|---|---|
| Client | control-command helpers send correct method/path/headers; 401 → typed error |
| Pages | each page renders in demo mode with the new components (loading/error/empty) |
| Offline | unreachable API → `BOT OFFLINE`, error states, no polling |
| Empty | empty DB → every empty-state string rendered |
| Mode banner | `PAPER`/`LIVE_GUARDED`/`HALTED`/`DEMO`/`OFFLINE` each map to the right badge |
| Read-only invariant | no page contains a button that submits an order; only Controls page has buttons |
| Control API | halt → mode HALTED + audit event; resume invalid target → 422; no control key → 503 |

---

## 15. Summary

The dashboard is a **display-first, safety-second** surface: nine read-only
pages served by an existing strictly-GET FastAPI backend, a deterministic demo
provider, explicit paper/live banners, and a small, audited, keyed control
surface for halt/resume/breaker-clear only. All trading logic remains in the
bot. The remaining work is additive storage/API exposure (snapshots, equity,
eligibility, control endpoints) plus frontend components for charts, tables,
and the three UI states — none of which touches how the bot trades.
