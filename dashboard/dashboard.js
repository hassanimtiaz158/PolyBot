/* ================================================================
   POLYBOT — Quant Trading Terminal
   dashboard.js  (vanilla JS + Chart.js via CDN)

   Reads all data from the read-only FastAPI backend
   (/api/dashboard/*). Contains NO trading algorithms and makes NO
   trading calls — it only fetches and displays what the backend
   exposes. If the backend is unavailable, the UI shows
   "Unable to load live data" and never falls back to synthetic data.

   Real-time updates arrive over the /ws/dashboard WebSocket; the
   server pushes a typed event only when a section's data changes.
   The REST endpoints remain the initial source of truth and act as a
   fallback while the socket is unavailable.

   Configuration (optional):
     window.DASHBOARD_CONFIG = { apiBase, apiKey, intervals }
     URL query:  ?apiBase=...&apiKey=...
   ================================================================ */

'use strict';

/* ---------- configuration ---------- */

const REQUEST_TIMEOUT_MS = 7000;
const STALE_FACTOR = 2.5;

const DEFAULT_CONFIG = {
  apiBase: 'http://localhost:8000',
  apiKey: '',
  intervals: {
    overview: 5000,
    equity: 10000,
    signals: 3000,
    markets: 15000,
    positions: 3000,
    orders: 10000,
    performance: 10000,
    risk: 2000,
    health: 5000,
    audit: 15000,
  },
};

function resolveConfig() {
  const cfg = Object.assign({}, DEFAULT_CONFIG, window.DASHBOARD_CONFIG || {});
  const params = new URLSearchParams(window.location.search);
  if (params.get('apiBase')) cfg.apiBase = params.get('apiBase');
  if (params.get('apiKey')) cfg.apiKey = params.get('apiKey');
  cfg.apiBase = String(cfg.apiBase).replace(/\/+$/, '');
  return cfg;
}

const CONFIG = resolveConfig();

/* ---------- formatting helpers ---------- */

const USD = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
});

const USD_INT = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
});

const NUM = new Intl.NumberFormat('en-US');

const PERCENT1 = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

const money = (v) => USD.format(v);
const moneyInt = (v) => USD_INT.format(v);
const num = (v) => NUM.format(v);
const pct1 = (v) => `${PERCENT1.format(v)}%`;
const pct2 = (v) => `${(v * 100).toFixed(2)}%`;
const round2 = (v) => Math.round(v * 100) / 100;
const pnlTone = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : 'zero');
const signedMoney = (v) => (v > 0 ? '+' : '') + money(v);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const shortMarket = (q) => (q.length > 44 ? q.slice(0, 42) + '…' : q);
const fmtDay = (ts) => (ts ? String(ts).slice(5, 10) : '');
const fmtDateTime = (ts) => (ts ? String(ts).slice(0, 19).replace('T', ' ') : '');
const optNum = (v) => (v == null ? '—' : num(v));
const optPct1 = (v) => (v == null ? '—' : pct1(v * 100));
const optPct2 = (v) => (v == null ? '—' : pct2(v));
const optMoney = (v) => (v == null ? '—' : money(v));

/* ---------- DOM helpers ---------- */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function statusBadge(level, label) {
  const norm = String(level || 'HEALTHY').toUpperCase();
  const badge = el('span', `badge badge-${norm.toLowerCase()}`, label || norm);
  badge.prepend(el('span', 'badge-dot'));
  badge.setAttribute('role', 'status');
  return badge;
}

function metricCard({ label, value, tone = 'default', sub, highlight = false, wide = false }) {
  const card = el('article', 'card' + (highlight ? ' highlight' : '') + (wide ? ' card-wide' : ''));
  card.append(el('p', 'card-label', label));
  const valueEl = el('p', `card-value ${tone}`, value);
  card.appendChild(valueEl);
  if (sub) card.append(el('p', 'card-sub', sub));
  return card;
}

function errorCard(label, err, wide) {
  return metricCard({
    label,
    value: 'Unable to load live data',
    tone: 'neg',
    sub: describeError(err),
    wide,
  });
}

/**
 * Build a table from a column descriptor and rows.
 * col = { key, label, align, render(row) -> Node | string }
 * When rows is empty and emptyMessage is set, a message row is shown.
 */
function renderTable(tableEl, caption, columns, rows, emptyMessage) {
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  columns.forEach((col) => {
    const th = document.createElement('th');
    th.scope = 'col';
    th.textContent = col.label;
    if (col.align) th.classList.add(`align-${col.align}`);
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);

  const tbody = document.createElement('tbody');
  if (rows.length) {
    rows.forEach((row) => {
      const tr = document.createElement('tr');
      columns.forEach((col) => {
        const td = document.createElement('td');
        const value = col.render ? col.render(row) : row[col.key];
        if (value instanceof Node) td.appendChild(value);
        else td.textContent = value == null || value === '' ? '—' : String(value);
        if (col.align) td.classList.add(`align-${col.align}`);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
  } else if (emptyMessage) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = columns.length;
    td.className = 'table-message';
    td.textContent = emptyMessage;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  tableEl.replaceChildren();
  const cap = document.createElement('caption');
  cap.className = 'visually-hidden';
  cap.textContent = caption;
  tableEl.append(cap, thead, tbody);
}

/** Show a single-row status message in a table (loading / error). */
function renderTableMessage(tableEl, message, level = 'info') {
  tableEl.replaceChildren();
  const tbody = document.createElement('tbody');
  const tr = document.createElement('tr');
  const td = document.createElement('td');
  td.colSpan = 8;
  td.className = `table-message ${level}`;
  td.textContent = message;
  tr.appendChild(td);
  tbody.appendChild(tr);
  tableEl.appendChild(tbody);
}

/** Render a grid of status/value tiles. */
function renderTiles(container, items) {
  const frag = document.createDocumentFragment();
  items.forEach((item) => {
    const tile = el('div', 'tile');
    tile.append(el('span', 'tile-label', item.label));

    const valueEl = el('span', 'tile-value', item.value);
    if (item.tone) valueEl.classList.add(item.tone);
    tile.appendChild(valueEl);

    const row = el('div', 'tile-row');
    if (item.status) row.appendChild(statusBadge(item.status));
    tile.appendChild(row);

    if (item.pct != null) {
      const bar = el('div', 'tile-bar');
      const fill = el('i', `tile-bar-fill bar-${String(item.status || 'HEALTHY').toLowerCase()}`);
      fill.style.width = `${clamp(item.pct, 0, 100)}%`;
      bar.appendChild(fill);
      tile.appendChild(bar);
    }

    frag.appendChild(tile);
  });
  container.replaceChildren(frag);
}

/* ================================================================
   API transport
   ================================================================ */

function buildHeaders() {
  const headers = { Accept: 'application/json' };
  if (CONFIG.apiKey) headers['X-API-Key'] = CONFIG.apiKey;
  return headers;
}

/** GET + JSON parse with timeout. Throws { code, status } on failure. */
async function fetchJSON(path) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(CONFIG.apiBase + path, {
      headers: buildHeaders(),
      signal: controller.signal,
      cache: 'no-store',
    });
  } catch (err) {
    if (err && err.name === 'AbortError') throw { code: 'timeout' };
    throw { code: 'network' };
  } finally {
    clearTimeout(timer);
  }

  if (response.status === 401 || response.status === 403) {
    throw { code: 'auth', status: response.status };
  }
  if (!response.ok) throw { code: 'server', status: response.status };

  let text;
  try {
    text = await response.text();
  } catch {
    throw { code: 'network' };
  }
  try {
    return JSON.parse(text);
  } catch {
    throw { code: 'bad_response' };
  }
}

function classifyError(err) {
  if (err && err.code) return { code: err.code, status: err.status || null };
  return { code: 'network', status: null };
}

function describeError(err) {
  const code = (err && err.code) || 'network';
  switch (code) {
    case 'auth': return 'Authentication failed. Check the API key.';
    case 'timeout': return 'Request timed out.';
    case 'network': return 'API unavailable.';
    case 'server': return `Server error (HTTP ${err.status || '?'}).`;
    case 'bad_response': return 'Malformed response from API.';
    default: return 'Unknown error.';
  }
}

/* ================================================================
   State — one group per endpoint.
   status: 'idle' | 'loading' | 'ok' | 'error'
   ================================================================ */

function newGroup() {
  return { status: 'idle', error: null, data: null, lastUpdate: null, lastAttempt: null, inFlight: false };
}

const state = {
  groups: {
    overview: newGroup(),
    equity: newGroup(),
    signals: newGroup(),
    markets: newGroup(),
    positions: newGroup(),
    orders: newGroup(),
    performance: newGroup(),
    risk: newGroup(),
    health: newGroup(),
    audit: newGroup(),
  },
  killArmed: false,
  // True while the real-time WebSocket is connected; REST polling for
  // event-covered groups is paused then (the socket is the freshness
  // source). REST resumes as fallback when the socket drops.
  wsActive: false,
};

/* ================================================================
   Loaders — map API payloads into render-friendly shapes.
   The map* functions are shared with the WebSocket event handlers so
   REST and WS updates always produce the same render shape.
   ================================================================ */

function mapOverview(d) {
  return {
    accountBalance: d.account_balance,
    availableBalance: d.available_balance,
    todayPnl: d.today_pnl,
    totalPnl: d.total_pnl,
    realizedPnl: d.realized_pnl,
    unrealizedPnl: d.unrealized_pnl,
    maxDrawdown: d.max_drawdown,
    totalExposure: d.total_exposure,
    openPositions: d.open_positions,
    activeSignals: d.active_signals,
    botMode: d.bot_mode,
    circuitBreaker: d.circuit_breaker,
  };
}

function mapSignals(d) {
  return (d.items || []).map((s) => ({
    id: s.signal_id,
    market: s.market_id,
    strategy: s.strategy,
    side: s.side,
    decision: s.decision,
    confidence: s.confidence,
    edge: s.net_edge,
    time: fmtDateTime(s.timestamp),
  }));
}

function mapMarkets(d) {
  return (d.items || []).map((m) => ({
    id: m.market_id,
    question: m.question,
    status: m.status,
    liquidity: m.liquidity,
    updated: fmtDay(m.updated_at) || fmtDateTime(m.updated_at),
  }));
}

function mapPositions(d) {
  return (d.items || []).map((p) => ({
    id: p.position_id,
    market: p.market_id,
    side: p.side,
    size: p.size,
    entry: p.average_entry,
    price: p.current_price,
    uPnL: p.unrealised_pnl,
    rPnL: p.realised_pnl,
  }));
}

function mapOrders(d) {
  return (d.items || []).map((o) => ({
    id: o.order_id,
    market: o.market_id,
    side: o.side,
    price: o.average_fill ?? o.requested_price,
    size: o.requested_size,
    filledSize: o.filled_size,
    averageFill: o.average_fill,
    status: o.status,
    time: fmtDateTime(o.submitted_at),
  }));
}

function mapRisk(d) {
  return {
    dailyLoss: d.daily_loss,
    dailyLossLimit: d.daily_loss_limit,
    exposure: d.exposure,
    exposureLimit: d.exposure_limit,
    consecutiveLosses: d.consecutive_losses,
    consecutiveLossLimit: d.consecutive_loss_limit,
    spreadStatus: d.spread_status,
    liquidityStatus: d.liquidity_status,
    dataFreshness: d.data_freshness,
    circuitBreaker: d.circuit_breaker,
  };
}

async function loadOverview() {
  const d = await fetchJSON('/api/dashboard/overview');
  state.groups.overview.data = mapOverview(d);
}

async function loadEquity() {
  const d = await fetchJSON('/api/dashboard/equity');
  state.groups.equity.data = (d.points || []).map((p) => ({
    label: fmtDay(p.timestamp),
    value: typeof p.equity === 'number' ? p.equity : 0,
  }));
}

async function loadSignals() {
  const d = await fetchJSON('/api/dashboard/signals?limit=50');
  state.groups.signals.data = mapSignals(d);
}

async function loadMarkets() {
  const d = await fetchJSON('/api/dashboard/markets?limit=100');
  state.groups.markets.data = mapMarkets(d);
}

async function loadPositions() {
  const d = await fetchJSON('/api/dashboard/positions?limit=50');
  state.groups.positions.data = mapPositions(d);
}

async function loadOrders() {
  const d = await fetchJSON('/api/dashboard/orders?limit=50');
  state.groups.orders.data = mapOrders(d);
}

async function loadPerformance() {
  const d = await fetchJSON('/api/dashboard/performance');
  state.groups.performance.data = {
    totalRealisedPnl: d.total_realised_pnl,
    totalUnrealisedPnl: d.total_unrealised_pnl,
    totalPnl: d.total_pnl,
    openPositions: d.open_positions,
    totalMarkets: d.total_markets,
    totalSignals: d.total_signals,
    totalOrders: d.total_orders,
    filledOrders: d.filled_orders,
    timestamp: fmtDateTime(d.timestamp),
  };
}

async function loadRisk() {
  const d = await fetchJSON('/api/dashboard/risk');
  state.groups.risk.data = mapRisk(d);
}

async function loadHealth() {
  const d = await fetchJSON('/api/dashboard/health');
  state.groups.health.data = d; // { healthy, checks: {name: {healthy, last_updated}}, timestamp }
}

async function loadAudit() {
  const d = await fetchJSON('/api/dashboard/audit?limit=50');
  state.groups.audit.data = (d.items || []).map((e) => ({
    id: e.event_id,
    eventType: e.event_type,
    severity: e.severity,
    details: e.details,
    time: fmtDateTime(e.timestamp),
  }));
}

const LOADERS = {
  overview: loadOverview,
  equity: loadEquity,
  signals: loadSignals,
  markets: loadMarkets,
  positions: loadPositions,
  orders: loadOrders,
  performance: loadPerformance,
  risk: loadRisk,
  health: loadHealth,
  audit: loadAudit,
};

/* ================================================================
   Load coordinator — never overlaps requests for the same group.
   ================================================================ */

async function loadGroup(name) {
  const g = state.groups[name];
  if (g.inFlight) return;
  g.inFlight = true;
  g.lastAttempt = Date.now();
  if (!g.data) g.status = 'loading';
  try {
    await LOADERS[name]();
    g.status = 'ok';
    g.error = null;
    g.lastUpdate = Date.now();
  } catch (err) {
    g.error = classifyError(err);
    g.status = 'error';
  } finally {
    g.inFlight = false;
    renderAll();
  }
}

function loadAllGroups() {
  Object.keys(LOADERS).forEach((name) => loadGroup(name));
}

function startScheduler() {
  loadAllGroups();

  setInterval(() => {
    const now = Date.now();
    // While the WebSocket is live, the server pushes changes for these
    // groups, so per-endpoint REST polling for them is paused. Groups
    // without a WS event type keep polling.
    const paused = state.wsActive ? WS_EVENT_GROUPS : new Set();
    Object.keys(CONFIG.intervals).forEach((name) => {
      if (paused.has(name)) return;
      const g = state.groups[name];
      const interval = CONFIG.intervals[name];
      if (!g.inFlight && (g.lastAttempt == null || now - g.lastAttempt >= interval)) {
        loadGroup(name);
      }
    });
    updateDataStatus();
  }, 1000);
}

/* ================================================================
   Real-time WebSocket updates
   The backend pushes typed events only when a section's data
   changes. Each event re-renders just the affected components —
   never the whole dashboard. Reconnects back off 1s/2s/5s/10s/30s
   and, on repeated failure, the UI shows "LIVE CONNECTION LOST"
   and "DATA STALE" while REST polling takes over.
   ================================================================ */

const WS_BACKOFF_MS = [1000, 2000, 5000, 10000, 30000];

// Groups fully refreshed by a WS event type.
const WS_EVENT_GROUPS = new Set([
  'overview',
  'signals',
  'markets',
  'positions',
  'orders',
  'risk',
  'health',
]);

// Groups with no dedicated WS event type; REST always polls these.
const REST_ALWAYS_GROUPS = ['equity', 'performance', 'audit'];

// Maps event type -> group name whose payload fully refreshes it.
const WS_EVENT_TO_GROUP = {
  MARKET_UPDATE: 'markets',
  SIGNAL_UPDATE: 'signals',
  POSITION_UPDATE: 'positions',
  ORDER_UPDATE: 'orders',
  'P&L_UPDATE': 'overview',
  RISK_UPDATE: 'risk',
  HEALTH_UPDATE: 'health',
};

// wsState: 'connecting' | 'connected' | 'offline'
let wsState = 'connecting';
let wsFailed = false;
let wsAttempt = 0;
let wsSocket = null;
let wsRetryTimer = null;

function wsUrl() {
  const url = new URL(CONFIG.apiBase);
  const scheme = url.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = url.host;
  const path = url.pathname.replace(/\/+$/, '');
  const query = new URLSearchParams();
  if (CONFIG.apiKey) query.set('apiKey', CONFIG.apiKey);
  const qs = query.toString();
  return `${scheme}//${host}${path}/ws/dashboard${qs ? `?${qs}` : ''}`;
}

function showConnBanner(show) {
  const banner = $('#conn-banner');
  if (banner) banner.hidden = !show;
}

function updateConnectionUI() {
  state.wsActive = wsState === 'connected';
  showConnBanner(wsFailed && wsState !== 'connected');
  updateDataStatus();
}

function scheduleWSReconnect() {
  clearTimeout(wsRetryTimer);
  const delay = WS_BACKOFF_MS[Math.min(wsAttempt, WS_BACKOFF_MS.length - 1)];
  wsAttempt += 1;
  wsRetryTimer = setTimeout(connectWS, delay);
}

function onWSFailure() {
  wsFailed = true;
  wsState = 'offline';
  wsSocket = null;
  updateConnectionUI();
  scheduleWSReconnect();
}

function connectWS() {
  if (typeof WebSocket === 'undefined') {
    // No socket support in this runtime — REST fallback only.
    wsFailed = true;
    wsState = 'offline';
    updateConnectionUI();
    return;
  }
  clearTimeout(wsRetryTimer);
  wsState = 'connecting';
  updateConnectionUI();

  let sock;
  try {
    sock = new WebSocket(wsUrl());
  } catch {
    onWSFailure();
    return;
  }
  wsSocket = sock;

  sock.onopen = () => {
    if (wsSocket !== sock) return;
    wsState = 'connected';
    wsFailed = false;
    wsAttempt = 0;
    updateConnectionUI();
    // Re-sync so incremental events start from a consistent state.
    loadAllGroups();
  };

  sock.onmessage = (event) => {
    if (wsSocket !== sock) return;
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    handleWSEvent(msg);
  };

  sock.onclose = () => {
    if (wsSocket !== sock) return;
    onWSFailure();
  };

  sock.onerror = () => {
    // onclose always follows; handled there.
  };
}

function applyWSGroup(groupName, mappedData) {
  const g = state.groups[groupName];
  if (!g) return;
  g.data = mappedData;
  g.status = 'ok';
  g.error = null;
  g.lastUpdate = Date.now();
}

/** Fetch one group over REST and run only its renderers (no full re-render). */
async function refreshGroup(name, renderers) {
  const g = state.groups[name];
  if (!g || g.inFlight) return;
  g.inFlight = true;
  try {
    await LOADERS[name]();
    g.status = 'ok';
    g.error = null;
    g.lastUpdate = Date.now();
  } catch (err) {
    g.error = classifyError(err);
    g.status = 'error';
  } finally {
    g.inFlight = false;
    renderers.forEach((fn) => fn());
    updateDataStatus();
  }
}

function handleWSEvent(msg) {
  if (!msg || typeof msg.type !== 'string') return;
  if (msg.type === 'CONNECTED' || msg.type === 'PING') return;
  const handler = WS_HANDLERS[msg.type];
  if (handler) handler(msg.data || {});
}

const WS_HANDLERS = {
  MARKET_UPDATE(data) {
    applyWSGroup('markets', mapMarkets(data));
    renderMarketsTable();
    renderOverviewCards();
    updateDataStatus();
  },

  SIGNAL_UPDATE(data) {
    applyWSGroup('signals', mapSignals(data));
    renderSignalsTable();
    renderSignalsFullTable();
    updateDataStatus();
  },

  POSITION_UPDATE(data) {
    applyWSGroup('positions', mapPositions(data));
    renderPositionsTable();
    renderPositionsFullTable();
    renderOverviewCards();
    updateCharts();
    updateDataStatus();
  },

  ORDER_UPDATE(data) {
    applyWSGroup('orders', mapOrders(data));
    renderOrdersTable();
    renderOrdersFullTable();
    renderPerformanceCards();
    updateCharts();
    // Win/loss, performance totals and the equity curve derive from
    // order history, so pull those groups on demand (they have no
    // dedicated event type).
    refreshGroup('performance', [renderPerformanceCards]);
    refreshGroup('equity', [updateCharts]);
    updateDataStatus();
  },

  'P&L_UPDATE'(data) {
    applyWSGroup('overview', mapOverview(data));
    renderOverviewCards();
    renderPerformanceCards();
    updateCharts();
    refreshGroup('equity', [updateCharts]);
    refreshGroup('performance', [renderPerformanceCards]);
    updateDataStatus();
  },

  RISK_UPDATE(data) {
    applyWSGroup('risk', mapRisk(data));
    renderRisk();
    renderSystem();
    updateDataStatus();
  },

  HEALTH_UPDATE(data) {
    applyWSGroup('health', data);
    renderSystem();
    renderRisk();
    updateDataStatus();
  },

  CIRCUIT_BREAKER(data) {
    const breaker = {
      state: data.state ?? null,
      reasons: data.reasons || [],
      triggered_at: data.triggered_at,
    };
    const risk = state.groups.risk.data;
    if (risk) risk.circuitBreaker = breaker;
    const overview = state.groups.overview.data;
    if (overview) overview.circuitBreaker = breaker;
    renderRisk();
    renderSystem();
    updateModeBadge();
    updateDataStatus();
  },
};

/* ================================================================
   Staleness — DATA STALE indicator
   ================================================================ */

function isGroupStale(name) {
  const g = state.groups[name];
  const interval = CONFIG.intervals[name];
  if (!interval) return false;
  if (g.lastUpdate == null) return true; // never succeeded
  return Date.now() - g.lastUpdate > interval * STALE_FACTOR;
}

function updateDataStatus() {
  const el = $('#data-status');
  if (!el) return;
  const anyData = Object.values(state.groups).some((g) => g.status === 'ok' && g.data);
  if (!anyData) {
    el.textContent = 'DATA STALE';
    el.className = 'data-status offline';
  } else if (state.wsActive) {
    // The socket is the freshness source while connected.
    el.textContent = 'DATA LIVE';
    el.className = 'data-status live';
  } else if (wsState === 'offline') {
    // Repeated connection failure: the real-time feed is down.
    el.textContent = 'DATA STALE';
    el.className = 'data-status offline';
  } else if (Object.keys(CONFIG.intervals).some(isGroupStale)) {
    el.textContent = 'DATA STALE';
    el.className = 'data-status stale';
  } else {
    el.textContent = 'DATA LIVE';
    el.className = 'data-status live';
  }
}

/* ================================================================
   Derived metrics (display only — computed from live API data)
   ================================================================ */

function orderPnl(o) {
  const size = o.filledSize;
  const fill = o.averageFill;
  if (size == null || fill == null || size <= 0) return 0;
  return o.side === 'YES' ? size * (0.5 - fill) : size * (fill - 0.5);
}

function getWinLoss() {
  const g = state.groups.orders;
  if (g.status !== 'ok' || !g.data) return null;
  let wins = 0;
  let losses = 0;
  g.data.forEach((o) => {
    const pnl = orderPnl(o);
    if (pnl > 0) wins += 1;
    else if (pnl < 0) losses += 1;
  });
  if (wins + losses === 0) return null;
  return { wins, losses };
}

function marketQuestion(marketId) {
  const markets = state.groups.markets.data;
  if (!markets) return marketId;
  const found = markets.find((m) => m.id === marketId);
  return found ? found.question : marketId;
}

/* ================================================================
   Status / badge helpers
   ================================================================ */

function decisionBadge(decision) {
  if (decision === 'CANDIDATE') return statusBadge('HEALTHY', 'CANDIDATE');
  if (decision === 'NO_SIGNAL') return statusBadge('WARNING', 'NO_SIGNAL');
  return statusBadge('ERROR', decision || 'UNKNOWN');
}

const ORDER_STATUS_LEVEL = {
  FILLED: 'HEALTHY',
  PARTIALLY_FILLED: 'WARNING',
  CREATED: 'WARNING',
  PENDING: 'WARNING',
  REPLACED: 'WARNING',
  CANCELLED: 'ERROR',
  REJECTED: 'ERROR',
};

const orderStatusCell = (row) => statusBadge(ORDER_STATUS_LEVEL[row.status] || 'WARNING', row.status || '—');
const marketStatusCell = (row) =>
  statusBadge(row.status === 'active' ? 'HEALTHY' : 'WARNING', (row.status || 'UNKNOWN').toUpperCase());
const severityCell = (row) => {
  const level = row.severity === 'HIGH' ? 'ERROR' : row.severity === 'MEDIUM' ? 'WARNING' : 'HEALTHY';
  return statusBadge(level, row.severity || '—');
};
const checkHealthy = (healthy) => (healthy ? 'HEALTHY' : 'ERROR');
const ratioLevel = (v, limit) => {
  const ratio = limit > 0 ? v / limit : 0;
  if (ratio >= 0.8) return 'ERROR';
  if (ratio >= 0.5) return 'WARNING';
  return 'HEALTHY';
};
const ratioPct = (v, limit) => (limit > 0 ? (v / limit) * 100 : 0);
const groupLevel = (g) => (g.status === 'ok' ? 'HEALTHY' : g.status === 'error' ? 'ERROR' : 'WARNING');

/* ================================================================
   Table column descriptors
   ================================================================ */

const signalColumns = [
  { key: 'id', label: 'Signal' },
  { key: 'market', label: 'Market', render: (r) => shortMarket(marketQuestion(r.market)) },
  { key: 'strategy', label: 'Strategy' },
  { key: 'side', label: 'Side', align: 'center' },
  { key: 'decision', label: 'Decision', render: (r) => decisionBadge(r.decision) },
  { key: 'confidence', label: 'Conf.', align: 'right', render: (r) => optPct1(r.confidence) },
  { key: 'edge', label: 'Net Edge', align: 'right', render: (r) => (r.edge == null ? '—' : r.edge > 0 ? `+${optPct2(r.edge)}` : optPct2(r.edge)) },
  { key: 'time', label: 'Time' },
];

const positionColumns = [
  { key: 'id', label: 'Position' },
  { key: 'market', label: 'Market', render: (r) => shortMarket(marketQuestion(r.market)) },
  { key: 'side', label: 'Side', align: 'center' },
  { key: 'size', label: 'Size', align: 'right', render: (r) => optNum(r.size) },
  { key: 'entry', label: 'Entry', align: 'right', render: (r) => (r.entry == null ? '—' : r.entry.toFixed(3)) },
  { key: 'price', label: 'Current', align: 'right', render: (r) => (r.price == null ? '—' : r.price.toFixed(3)) },
  { key: 'uPnL', label: 'Unreal. P&L', align: 'right', render: (r) => (r.uPnL == null ? '—' : signedMoney(r.uPnL)) },
  { key: 'rPnL', label: 'Realised P&L', align: 'right', render: (r) => (r.rPnL == null ? '—' : signedMoney(r.rPnL)) },
];

const orderColumns = [
  { key: 'id', label: 'Order' },
  { key: 'market', label: 'Market', render: (r) => shortMarket(marketQuestion(r.market)) },
  { key: 'side', label: 'Side', align: 'center' },
  { key: 'price', label: 'Price', align: 'right', render: (r) => (r.price == null ? '—' : r.price.toFixed(3)) },
  { key: 'size', label: 'Size', align: 'right', render: (r) => optNum(r.size) },
  { key: 'status', label: 'Status', render: orderStatusCell },
  { key: 'time', label: 'Time' },
];

const marketColumns = [
  { key: 'id', label: 'Market ID' },
  { key: 'question', label: 'Question' },
  { key: 'status', label: 'Status', render: marketStatusCell },
  { key: 'liquidity', label: 'Liquidity', align: 'right', render: (r) => optMoney(r.liquidity) },
  { key: 'updated', label: 'Updated' },
];

const auditColumns = [
  { key: 'id', label: 'Event' },
  { key: 'eventType', label: 'Type' },
  { key: 'severity', label: 'Severity', render: severityCell },
  { key: 'details', label: 'Details' },
  { key: 'time', label: 'Timestamp' },
];

/* ================================================================
   Renderers
   ================================================================ */

function renderOverviewCards() {
  const g = state.groups.overview;
  const container = $('#overview-cards');
  if (g.status !== 'ok' || !g.data) {
    container.replaceChildren(errorCard('Overview', g.error, true));
    return;
  }
  const o = g.data;
  const wl = getWinLoss();
  const winRate = wl ? (wl.wins / (wl.wins + wl.losses)) * 100 : null;
  const cards = [
    metricCard({ label: 'Account Balance', value: money(o.accountBalance), sub: `available ${money(o.availableBalance)}`, highlight: true }),
    metricCard({ label: "Today's P&L", value: signedMoney(o.todayPnl), tone: pnlTone(o.todayPnl), sub: 'live' }),
    metricCard({ label: 'Total P&L', value: signedMoney(o.totalPnl), tone: pnlTone(o.totalPnl), sub: `realised ${signedMoney(o.realizedPnl)}` }),
    metricCard({ label: 'Maximum Drawdown', value: `-${pct1(o.maxDrawdown * 100)}`, tone: o.maxDrawdown > 0.08 ? 'neg' : 'default', sub: 'peak to trough' }),
    metricCard({ label: 'Total Exposure', value: moneyInt(o.totalExposure), sub: `${o.openPositions} open positions` }),
    metricCard({
      label: 'Win Rate',
      value: winRate == null ? '—' : pct1(winRate),
      tone: winRate == null ? 'default' : winRate >= 50 ? 'pos' : 'neg',
      sub: wl ? `${wl.wins}W / ${wl.losses}L (filled orders)` : 'not provided by API',
    }),
  ];
  container.replaceChildren(...cards);
}

function renderSignalsTable() {
  const g = state.groups.signals;
  const table = $('#signals-table');
  if (g.status === 'error') return renderTableMessage(table, 'Unable to load live data', 'error');
  if (g.status === 'loading' || g.status === 'idle') return renderTableMessage(table, 'Loading live data…');
  const candidates = g.data.filter((s) => s.decision === 'CANDIDATE');
  renderTable(table, 'Active strategy signals', signalColumns, candidates, 'No active signals');
}

function renderSignalsFullTable() {
  const g = state.groups.signals;
  const table = $('#signals-full-table');
  if (g.status === 'error') return renderTableMessage(table, 'Unable to load live data', 'error');
  if (g.status === 'loading' || g.status === 'idle') return renderTableMessage(table, 'Loading live data…');
  renderTable(table, 'All strategy signals', signalColumns, g.data, 'No signals yet');
}

function renderMarketsTable() {
  const g = state.groups.markets;
  const table = $('#markets-table');
  if (g.status === 'error') return renderTableMessage(table, 'Unable to load live data', 'error');
  if (g.status === 'loading' || g.status === 'idle') return renderTableMessage(table, 'Loading live data…');
  renderTable(table, 'Market list', marketColumns, g.data, 'No markets yet');
}

function renderPositionsTable() {
  const g = state.groups.positions;
  const table = $('#positions-table');
  if (g.status === 'error') return renderTableMessage(table, 'Unable to load live data', 'error');
  if (g.status === 'loading' || g.status === 'idle') return renderTableMessage(table, 'Loading live data…');
  renderTable(table, 'Open positions', positionColumns, g.data, 'No open positions');
}

function renderPositionsFullTable() {
  const g = state.groups.positions;
  const table = $('#positions-full-table');
  if (g.status === 'error') return renderTableMessage(table, 'Unable to load live data', 'error');
  if (g.status === 'loading' || g.status === 'idle') return renderTableMessage(table, 'Loading live data…');
  renderTable(table, 'All positions', positionColumns, g.data, 'No positions yet');
}

function renderOrdersTable() {
  const g = state.groups.orders;
  const table = $('#orders-table');
  if (g.status === 'error') return renderTableMessage(table, 'Unable to load live data', 'error');
  if (g.status === 'loading' || g.status === 'idle') return renderTableMessage(table, 'Loading live data…');
  renderTable(table, 'Recent orders', orderColumns, g.data.slice(0, 5), 'No recent orders');
}

function renderOrdersFullTable() {
  const g = state.groups.orders;
  const table = $('#orders-full-table');
  if (g.status === 'error') return renderTableMessage(table, 'Unable to load live data', 'error');
  if (g.status === 'loading' || g.status === 'idle') return renderTableMessage(table, 'Loading live data…');
  renderTable(table, 'All orders', orderColumns, g.data, 'No orders yet');
}

function renderAuditTable() {
  const g = state.groups.audit;
  const table = $('#audit-table');
  if (g.status === 'error') return renderTableMessage(table, 'Unable to load live data', 'error');
  if (g.status === 'loading' || g.status === 'idle') return renderTableMessage(table, 'Loading live data…');
  renderTable(table, 'Audit trail', auditColumns, g.data, 'No audit events');
}

function renderPerformanceCards() {
  const g = state.groups.performance;
  const container = $('#performance-cards');
  if (g.status !== 'ok' || !g.data) {
    container.replaceChildren(errorCard('Performance', g.error, true));
    return;
  }
  const p = g.data;
  const cards = [
    metricCard({ label: 'Total P&L', value: signedMoney(p.totalPnl), tone: pnlTone(p.totalPnl) }),
    metricCard({ label: 'Realised P&L', value: signedMoney(p.totalRealisedPnl), tone: pnlTone(p.totalRealisedPnl) }),
    metricCard({ label: 'Unrealised P&L', value: signedMoney(p.totalUnrealisedPnl), tone: pnlTone(p.totalUnrealisedPnl) }),
    metricCard({ label: 'Open Positions', value: num(p.openPositions) }),
    metricCard({ label: 'Markets', value: num(p.totalMarkets) }),
    metricCard({ label: 'Signals', value: num(p.totalSignals) }),
    metricCard({ label: 'Orders', value: `${num(p.totalOrders)} (${num(p.filledOrders)} filled)` }),
    metricCard({ label: 'Last Updated', value: p.timestamp, sub: 'server time' }),
  ];
  container.replaceChildren(...cards);
}

function buildRiskTiles() {
  const g = state.groups.risk;
  const h = state.groups.health;
  const ov = state.groups.overview;

  if (g.status !== 'ok' || !g.data) {
    return [{ label: 'Risk Data', value: 'UNAVAILABLE', status: 'ERROR' }];
  }
  const r = g.data;

  const apiHealth =
    h.status === 'ok' && h.data && h.data.checks
      ? checkHealthy(h.data.checks.api && h.data.checks.api.healthy)
      : 'UNAVAILABLE';
  const breaker = r.circuitBreaker;
  const breakerState = breaker ? breaker.state : 'N/A';
  const breakerStatus = state.killArmed
    ? 'ERROR'
    : breaker == null
      ? 'WARNING'
      : breaker.state === 'NORMAL'
        ? 'HEALTHY'
        : breaker.state === 'HALTED' || breaker.state === 'TRIPPED'
          ? 'ERROR'
          : 'WARNING';

  const drawdown = ov.status === 'ok' && ov.data ? ov.data.maxDrawdown : null;

  return [
    {
      label: 'Daily Loss',
      value: `${money(r.dailyLoss)} / ${money(r.dailyLossLimit)}`,
      pct: ratioPct(r.dailyLoss, r.dailyLossLimit),
      status: ratioLevel(r.dailyLoss, r.dailyLossLimit),
    },
    {
      label: 'Exposure',
      value: `${moneyInt(r.exposure)} / ${moneyInt(r.exposureLimit)}`,
      pct: ratioPct(r.exposure, r.exposureLimit),
      status: ratioLevel(r.exposure, r.exposureLimit),
    },
    {
      label: 'Drawdown',
      value: drawdown == null ? '—' : `-${pct1(drawdown * 100)}`,
      status: drawdown == null ? 'WARNING' : drawdown > 0.1 ? 'ERROR' : drawdown > 0.08 ? 'WARNING' : 'HEALTHY',
    },
    {
      label: 'Consecutive Losses',
      value: `${r.consecutiveLosses} / ${r.consecutiveLossLimit}`,
      pct: ratioPct(r.consecutiveLosses, r.consecutiveLossLimit),
      status: ratioLevel(r.consecutiveLosses, r.consecutiveLossLimit),
    },
    {
      label: 'Data Freshness',
      value: r.dataFreshness || '—',
      status: r.dataFreshness === 'FRESH' ? 'HEALTHY' : r.dataFreshness === 'STALE' ? 'WARNING' : 'WARNING',
    },
    { label: 'API Health', value: apiHealth, status: apiHealth },
    { label: 'Circuit Breaker', value: breakerState, status: breakerStatus },
  ];
}

function renderRisk() {
  const tiles = buildRiskTiles();
  renderTiles($('#risk-tiles'), tiles);
  renderTiles($('#risk-tiles-view'), tiles);
}

function buildSystemTiles() {
  const h = state.groups.health;
  const signals = state.groups.signals;
  const orders = state.groups.orders;
  const risk = state.groups.risk;

  const level = (name) => {
    if (h.status !== 'ok' || !h.data || !h.data.checks) return 'UNAVAILABLE';
    const check = h.data.checks[name];
    return check ? checkHealthy(check.healthy) : 'UNAVAILABLE';
  };
  const riskEngine = state.killArmed
    ? 'ERROR'
    : risk.status !== 'ok' || !risk.data
      ? 'ERROR'
      : risk.data.spreadStatus === 'HIGH' || risk.data.liquidityStatus === 'LOW'
        ? 'WARNING'
        : 'HEALTHY';
  const execution = state.killArmed ? 'ERROR' : groupLevel(orders);

  return [
    { label: 'Market Data', value: level('data_freshness'), status: level('data_freshness') },
    { label: 'Database', value: level('database'), status: level('database') },
    { label: 'Strategy Engine', value: groupLevel(signals), status: groupLevel(signals) },
    { label: 'Model', value: level('model_availability'), status: level('model_availability') },
    { label: 'Risk Engine', value: riskEngine, status: riskEngine },
    { label: 'Execution', value: execution, status: execution },
    { label: 'API', value: level('api'), status: level('api') },
  ];
}

function renderSystem() {
  const tiles = buildSystemTiles();
  renderTiles($('#system-tiles'), tiles);
  renderTiles($('#system-tiles-view'), tiles);
}

function updateModeBadge() {
  const ov = state.groups.overview;
  const badge = $('#mode-badge');
  const label = $('#mode-label');
  const breaker = ov && ov.data ? ov.data.circuitBreaker : null;
  const breakerHalted =
    breaker && (breaker.state === 'HALTED' || breaker.state === 'TRIPPED');
  if (state.killArmed || breakerHalted) {
    badge.classList.add('mode-halted');
    label.textContent = 'SYSTEM HALTED';
    return;
  }
  if (ov.status === 'ok' && ov.data && ov.data.botMode) {
    const mode = String(ov.data.botMode).toUpperCase();
    label.textContent = /MODE$/.test(mode) ? mode : `${mode} MODE`;
    badge.classList.toggle('mode-halted', mode === 'HALTED');
  } else {
    label.textContent = 'PAPER MODE';
    badge.classList.remove('mode-halted');
  }
}

function renderFooter() {
  $('#last-updated').textContent = `API ${CONFIG.apiBase} · last update ${new Date().toLocaleTimeString()}`;
}

function renderAll() {
  renderOverviewCards();
  renderSignalsTable();
  renderSignalsFullTable();
  renderMarketsTable();
  renderPositionsTable();
  renderPositionsFullTable();
  renderOrdersTable();
  renderOrdersFullTable();
  renderAuditTable();
  renderPerformanceCards();
  renderRisk();
  renderSystem();
  updateModeBadge();
  updateCharts();
  updateDataStatus();
  renderFooter();
}

/* ================================================================
   Chart.js setup
   ================================================================ */

const charts = {};

function createCharts() {
  if (typeof Chart === 'undefined') {
    $$('.chart-wrap').forEach((wrap) => {
      wrap.replaceChildren(el('p', 'chart-note', 'Chart.js could not be loaded.'));
    });
    return;
  }

  const bodyStyle = getComputedStyle(document.body);
  const mono = bodyStyle.getPropertyValue('--mono').trim();
  const faint = bodyStyle.getPropertyValue('--text-faint').trim();
  const gridColor = 'rgba(148,163,184,0.08)';

  Chart.defaults.font.family = mono;
  Chart.defaults.font.size = 11;
  Chart.defaults.color = faint;

  const baseOptions = {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 350 },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#121b2a',
        borderColor: '#2a3a52',
        borderWidth: 1,
        padding: 10,
        titleColor: '#dbe4f0',
        bodyColor: '#94a3b8',
      },
    },
    scales: {
      x: { grid: { display: false }, ticks: { maxTicksLimit: 8, color: faint } },
      y: { grid: { color: gridColor }, ticks: { color: faint } },
    },
  };

  const equityCtx = $('#equity-chart').getContext('2d');
  const grad = equityCtx.createLinearGradient(0, 0, 0, 260);
  grad.addColorStop(0, 'rgba(0,200,255,0.28)');
  grad.addColorStop(1, 'rgba(0,200,255,0)');
  charts.equity = new Chart(equityCtx, {
    type: 'line',
    data: { labels: [], datasets: [{ label: 'Equity', data: [], borderColor: '#00c8ff', backgroundColor: grad, fill: true, tension: 0.35, borderWidth: 2, pointRadius: 0, pointHitRadius: 10 }] },
    options: {
      ...baseOptions,
      scales: {
        x: baseOptions.scales.x,
        y: { ...baseOptions.scales.y, ticks: { ...baseOptions.scales.y.ticks, callback: (v) => moneyInt(v) } },
      },
    },
  });

  charts.daily = new Chart($('#daily-pnl-chart').getContext('2d'), {
    type: 'bar',
    data: { labels: [], datasets: [{ label: 'Daily P&L', data: [], borderRadius: 3 }] },
    options: {
      ...baseOptions,
      scales: {
        x: baseOptions.scales.x,
        y: { ...baseOptions.scales.y, ticks: { ...baseOptions.scales.y.ticks, callback: (v) => moneyInt(v) } },
      },
    },
  });

  charts.winloss = new Chart($('#winloss-chart').getContext('2d'), {
    type: 'doughnut',
    data: {
      labels: ['Wins', 'Losses'],
      datasets: [{ data: [0, 0], backgroundColor: ['rgba(34,197,94,0.85)', 'rgba(239,68,68,0.85)'], borderColor: '#0d1420', borderWidth: 3 }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '68%',
      plugins: {
        legend: { display: true, position: 'bottom', labels: { color: faint, padding: 14, usePointStyle: true } },
        tooltip: { backgroundColor: '#121b2a', borderColor: '#2a3a52', borderWidth: 1, padding: 10 },
      },
    },
    plugins: [doughnutCenter],
  });

  charts.dailyPerf = new Chart($('#monthly-chart').getContext('2d'), {
    type: 'bar',
    data: { labels: [], datasets: [{ label: 'Daily P&L', data: [], borderRadius: 3 }] },
    options: {
      ...baseOptions,
      scales: {
        x: baseOptions.scales.x,
        y: { ...baseOptions.scales.y, ticks: { ...baseOptions.scales.y.ticks, callback: (v) => moneyInt(v) } },
      },
    },
  });
}

const doughnutCenter = {
  id: 'doughnutCenter',
  afterDraw(chart) {
    const meta = chart.getDatasetMeta(0);
    if (!meta.data.length) return;
    const { ctx } = chart;
    const { x, y } = meta.data[0];
    const [wins, losses] = chart.data.datasets[0].data;
    const total = wins + losses;
    const bodyStyle = getComputedStyle(document.body);
    const mono = bodyStyle.getPropertyValue('--mono').trim();
    ctx.save();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = bodyStyle.getPropertyValue('--text-faint').trim();
    ctx.font = `11px ${mono}`;
    ctx.fillText('WIN RATE', x, y - 14);
    ctx.fillStyle = bodyStyle.getPropertyValue('--text').trim();
    ctx.font = `600 24px ${mono}`;
    ctx.fillText(total > 0 ? `${Math.round((wins / total) * 100)}%` : 'NO DATA', x, y + 10);
    ctx.restore();
  },
};

function updateCharts() {
  if (!charts.equity) return;

  const eq = state.groups.equity;
  const points = eq.status === 'ok' && eq.data ? eq.data : [];
  charts.equity.data.labels = points.map((p) => p.label);
  charts.equity.data.datasets[0].data = points.map((p) => p.value);
  charts.equity.update('none');

  const note = $('#equity-note');
  if (eq.status === 'error') note.textContent = `Unable to load live data — ${describeError(eq.error)}`;
  else if (points.length === 0) note.textContent = 'No equity data available yet.';
  else note.textContent = `${num(points.length)} observations · last ${money(points[points.length - 1].value)}`;

  // Daily P&L is derived from the equity curve (back-end provides the series).
  const daily = points.length > 1 ? points.slice(1).map((p, i) => ({ label: p.label, value: round2(p.value - points[i].value) })) : [];
  const applyDaily = (chart) => {
    chart.data.labels = daily.map((d) => d.label);
    chart.data.datasets[0].data = daily.map((d) => d.value);
    chart.data.datasets[0].backgroundColor = daily.map((d) => (d.value >= 0 ? 'rgba(34,197,94,0.75)' : 'rgba(239,68,68,0.75)'));
    chart.update('none');
  };
  applyDaily(charts.daily);
  applyDaily(charts.dailyPerf);

  const wl = getWinLoss();
  charts.winloss.data.datasets[0].data = wl ? [wl.wins, wl.losses] : [0, 0];
  charts.winloss.update('none');
}

/* ================================================================
   Kill switch (UI-only simulation — no trading action)
   ================================================================ */

function initKillSwitch() {
  $('#kill-switch').addEventListener('click', () => {
    state.killArmed = !state.killArmed;
    const btn = $('#kill-switch');
    btn.classList.toggle('is-armed', state.killArmed);
    btn.setAttribute('aria-pressed', String(state.killArmed));
    btn.textContent = state.killArmed ? 'Resume' : 'Kill Switch';

    const banner = $('#halt-banner');
    if (state.killArmed) {
      if (!banner) {
        const b = el('div', 'halt-banner', '⚠ SYSTEM HALTED — kill switch armed. Trading disabled.');
        b.id = 'halt-banner';
        b.setAttribute('role', 'alert');
        $('.content').prepend(b);
      }
    } else if (banner) {
      banner.remove();
    }
    renderAll();
  });
}

/* ================================================================
   Nav active-state tracking
   ================================================================ */

function initNav() {
  const links = $$('.nav-link');
  const sections = $$('.view');

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const id = entry.target.id;
        links.forEach((link) => {
          const active = link.getAttribute('href') === `#${id}`;
          link.classList.toggle('is-active', active);
          if (active) link.setAttribute('aria-current', 'page');
          else link.removeAttribute('aria-current');
        });
      });
    },
    { rootMargin: '-40% 0px -55% 0px', threshold: 0 },
  );

  sections.forEach((section) => observer.observe(section));
}

/* ================================================================
   Init
   ================================================================ */

function init() {
  createCharts();
  renderAll();
  initNav();
  initKillSwitch();
  startScheduler();
  connectWS();
}

document.addEventListener('DOMContentLoaded', init);
