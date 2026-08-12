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
function renderTable(tableEl, caption, columns, rows, emptyMessage, rowKey) {
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
      if (rowKey) {
        const key = rowKey(row);
        if (key != null) {
          tr.setAttribute('data-row-key', String(key));
          tr.classList.add('clickable-row');
        }
      }
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

const signalsFilter = {
  strategy: '',
  market: '',
  decision: '',
  minEdge: null,
  minConfidence: null,
  sortBy: null,
  sortDir: 'desc',
};

// Performance page analysis window (sent to the backend; the server
// computes every windowed value — the client never re-derives them).
const performanceFilter = {
  from: '',
  to: '',
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
    modelProbability: s.model_probability,
    impliedProbability: s.implied_probability,
    grossEdge: s.gross_edge,
    estimatedCost: s.estimated_cost,
    netEdge: s.net_edge,
    confidence: s.confidence,
    rejectionReason: s.rejection_reason,
    timestamp: s.timestamp,
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
    outcome: p.side === 'NO' ? 'NO' : 'YES',
    size: p.size,
    entry: p.average_entry,
    price: p.current_price,
    exposure: p.exposure,
    uPnL: p.unrealised_pnl,
    rPnL: p.realised_pnl,
    returnPct: p.return_pct,
    timeToResolution: p.time_to_resolution,
    riskStatus: p.risk_status,
  }));
}

function mapSnapshots(d) {
  return (d.items || []).map((s) => ({
    timestamp: s.timestamp,
    midpoint: s.midpoint,
    bid: s.bid,
    ask: s.ask,
    spread: s.spread,
    timeToResolution: s.time_to_resolution,
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

function mapAudit(d) {
  return (d.items || []).map((e) => ({
    id: e.event_id,
    eventType: e.event_type,
    severity: e.severity,
    details: e.details,
    time: fmtDateTime(e.timestamp),
  }));
}

function mapRisk(d) {
  return {
    accountBalance: d.account_balance,
    availableBalance: d.available_balance,
    exposure: d.exposure,
    exposurePct: d.exposure_pct,
    exposureLimit: d.exposure_limit,
    todayPnl: d.today_pnl,
    dailyLoss: d.daily_loss,
    dailyLossLimit: d.daily_loss_limit,
    consecutiveLosses: d.consecutive_losses,
    consecutiveLossLimit: d.consecutive_loss_limit,
    openPositions: d.open_positions,
    maxOpenPositions: d.max_open_positions,
    largestPosition: d.largest_position,
    largestPositionMarket: d.largest_position_market,
    largestMarketExposure: d.largest_market_exposure,
    averageSpread: d.average_spread,
    minimumLiquidity: d.minimum_liquidity,
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
  const d = await fetchJSON('/api/dashboard/signals?limit=100');
  state.groups.signals.data = mapSignals(d);
}

async function loadMarkets() {
  const d = await fetchJSON('/api/dashboard/markets?limit=100');
  state.groups.markets.data = mapMarkets(d);
}

async function loadPositions() {
  const d = await fetchJSON('/api/dashboard/positions?limit=50');
  state.groups.positions.data = mapPositions(d);
  state.groups.positions.total = d.pagination ? d.pagination.total : null;
}

async function loadOrders() {
  const d = await fetchJSON('/api/dashboard/orders?limit=50');
  state.groups.orders.data = mapOrders(d);
}

function performanceQuery() {
  const params = new URLSearchParams();
  if (performanceFilter.from) params.set('from_date', performanceFilter.from);
  if (performanceFilter.to) params.set('to_date', performanceFilter.to);
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

function mapPerformance(d) {
  const points = (list) =>
    (list || []).map((p) => ({ label: fmtDay(p.timestamp), timestamp: p.timestamp, value: p.value == null ? 0 : p.value }));
  const breakdown = (list) =>
    (list || []).map((p) => ({ label: p.label || '—', value: p.pnl == null ? 0 : p.pnl }));
  const charts = d.charts || {};
  return {
    mode: d.mode || 'PAPER',
    totalRealisedPnl: d.total_realised_pnl,
    totalUnrealisedPnl: d.total_unrealised_pnl,
    totalPnl: d.total_pnl,
    openPositions: d.open_positions,
    totalMarkets: d.total_markets,
    totalSignals: d.total_signals,
    totalOrders: d.total_orders,
    filledOrders: d.filled_orders,
    todayPnl: d.today_pnl,
    weekPnl: d.week_pnl,
    monthPnl: d.month_pnl,
    returnPct: d.return_pct,
    maxDrawdown: d.max_drawdown,
    winRate: d.win_rate,
    lossRate: d.loss_rate,
    profitFactor: d.profit_factor,
    expectancy: d.expectancy,
    averageTrade: d.average_trade,
    averageWin: d.average_win,
    averageLoss: d.average_loss,
    numberOfTrades: d.number_of_trades,
    averageHoldingTime: d.average_holding_time,
    averageNetEdge: d.average_net_edge,
    slippage: d.slippage,
    charts: {
      equity: points(charts.equity),
      dailyPnl: points(charts.daily_pnl),
      cumulativePnl: points(charts.cumulative_pnl),
      drawdown: points(charts.drawdown),
      byStrategy: breakdown(charts.by_strategy),
      byCategory: breakdown(charts.by_category),
    },
    timestamp: fmtDateTime(d.timestamp),
  };
}

async function loadPerformance() {
  const d = await fetchJSON(`/api/dashboard/performance${performanceQuery()}`);
  state.groups.performance.data = mapPerformance(d);
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
  state.groups.audit.data = mapAudit(d);
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

function applyWSGroup(groupName, mappedData, total) {
  const g = state.groups[groupName];
  if (!g) return;
  g.data = mappedData;
  if (total != null) g.total = total;
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
    applyWSGroup('positions', mapPositions(data), data.total);
    renderPositionsTable();
    renderPositionsFullTable();
    renderPositionsSummary();
    renderOverviewCards();
    updateCharts();
    refreshPositionDrawer();
    updateDataStatus();
  },

  ORDER_UPDATE(data) {
    applyWSGroup('orders', mapOrders(data));
    renderOrdersTable();
    renderOrdersFullTable();
    renderPerformance();
    updateCharts();
    // Win/loss, performance totals and the equity curve derive from
    // order history, so pull those groups on demand (they have no
    // dedicated event type).
    refreshGroup('performance', [renderPerformance]);
    refreshGroup('equity', [updateCharts]);
    updateDataStatus();
  },

  'P&L_UPDATE'(data) {
    applyWSGroup('overview', mapOverview(data));
    renderOverviewCards();
    renderPositionsSummary();
    renderPerformance();
    updateCharts();
    refreshGroup('equity', [updateCharts]);
    refreshGroup('performance', [renderPerformance]);
    updateDataStatus();
  },

  RISK_UPDATE(data) {
    applyWSGroup('risk', mapRisk(data));
    renderPositionsSummary();
    renderRisk();
    renderRiskPage();
    renderSystem();
    updateDataStatus();
  },

  HEALTH_UPDATE(data) {
    applyWSGroup('health', data);
    renderSystem();
    renderRisk();
    renderRiskPage();
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
    renderRiskPage();
    renderSystem();
    updateModeBadge();
    updateDataStatus();
  },
};

function initPerformanceFilters() {
  const from = $('#perf-from');
  const to = $('#perf-to');
  const applyBtn = $('#perf-apply');
  const resetBtn = $('#perf-reset');
  if (!from || !to || !applyBtn || !resetBtn) return;

  const syncFromState = () => {
    if (from.value !== performanceFilter.from) from.value = performanceFilter.from;
    if (to.value !== performanceFilter.to) to.value = performanceFilter.to;
  };
  const applyWindow = () => {
    performanceFilter.from = from.value || '';
    performanceFilter.to = to.value || '';
    const g = state.groups.performance;
    g.data = null;
    g.status = 'idle';
    g.error = null;
    renderPerformance();
    loadGroup('performance');
  };

  applyBtn.addEventListener('click', applyWindow);
  resetBtn.addEventListener('click', () => {
    from.value = '';
    to.value = '';
    applyWindow();
  });
  [from, to].forEach((input) => {
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') applyWindow();
    });
  });
  syncFromState();
}

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

const DECISION_LABELS = {
  TRADE: 'TRADE',
  CANDIDATE: 'TRADE',
  SKIP: 'SKIP',
  NO_SIGNAL: 'SKIP',
  REJECTED: 'REJECTED',
  WAIT: 'WAIT',
};

const DECISION_LEVEL = {
  TRADE: 'HEALTHY',
  CANDIDATE: 'HEALTHY',
  SKIP: 'WARNING',
  NO_SIGNAL: 'WARNING',
  REJECTED: 'ERROR',
  WAIT: 'WARNING',
};

const REJECTION_LABELS = {
  EDGE_TOO_LOW: 'EDGE TOO LOW',
  LOW_CONFIDENCE: 'LOW CONFIDENCE',
  STALE_DATA: 'STALE DATA',
  HIGH_SPREAD: 'HIGH SPREAD',
  LOW_LIQUIDITY: 'LOW LIQUIDITY',
  RISK_LIMIT: 'RISK LIMIT',
  MODEL_UNCERTAIN: 'MODEL UNCERTAIN',
  NET_EDGE_TOO_LOW: 'EDGE TOO LOW',
  NET_EDGE_BELOW_THRESHOLD: 'EDGE TOO LOW',
  CONFIDENCE_BELOW_THRESHOLD: 'LOW CONFIDENCE',
  CONFIDENCE_TOO_LOW: 'LOW CONFIDENCE',
  SPREAD_TOO_HIGH: 'HIGH SPREAD',
  LIQUIDITY_TOO_LOW: 'LOW LIQUIDITY',
  SYSTEM_HALTED: 'SYSTEM HALTED',
  DAILY_LOSS_LIMIT_REACHED: 'DAILY LOSS LIMIT',
  CONSECUTIVE_LOSS_LIMIT_REACHED: 'CONSECUTIVE LOSSES',
  MAX_OPEN_POSITIONS_REACHED: 'MAX POSITIONS',
  TOTAL_EXPOSURE_TOO_HIGH: 'EXPOSURE LIMIT',
  POSITION_SIZE_EXCEEDS_MAX: 'POSITION SIZE',
  MARKET_EXPOSURE_TOO_HIGH: 'MARKET EXPOSURE',
  INVALID_DATA: 'INVALID DATA',
};

function humanizeRejection(reason) {
  if (!reason) return '';
  return REJECTION_LABELS[reason] || reason.replace(/_/g, ' ');
}

function resolveDecision(signal) {
  if (signal.decision === 'CANDIDATE' || signal.decision === 'TRADE') return 'TRADE';
  if (signal.decision === 'NO_SIGNAL' || signal.decision === 'SKIP') {
    return signal.rejectionReason ? 'REJECTED' : 'SKIP';
  }
  return signal.decision || 'WAIT';
}

function decisionBadge(decision) {
  const label = DECISION_LABELS[decision] || decision || 'UNKNOWN';
  const level = DECISION_LEVEL[decision] || 'WARNING';
  return statusBadge(level, label);
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
const riskStatusCell = (status) => {
  const level = status === 'CRITICAL' ? 'ERROR' : status === 'WARNING' ? 'WARNING' : 'HEALTHY';
  return statusBadge(level, status || '—');
};
const fmtDuration = (seconds) => {
  if (seconds == null) return '—';
  const s = Math.max(0, Math.round(seconds));
  if (s >= 86400) return `${num(Math.floor(s / 86400))}d ${Math.floor((s % 86400) / 3600)}h`;
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  if (s >= 60) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${s}s`;
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

const overviewSignalColumns = [
  { key: 'market', label: 'Market', render: (r) => shortMarket(marketQuestion(r.market)) },
  { key: 'strategy', label: 'Strategy' },
  { key: 'side', label: 'Side', align: 'center' },
  { key: 'decision', label: 'Decision', render: (r) => { const d = resolveDecision(r); return decisionBadge(d); } },
  { key: 'confidence', label: 'Conf.', align: 'right', render: (r) => (r.confidence == null ? '—' : pct1(r.confidence * 100)) },
  { key: 'netEdge', label: 'Net Edge', align: 'right', render: (r) => (r.netEdge == null ? '—' : r.netEdge > 0 ? `+${pct2(r.netEdge)}` : pct2(r.netEdge)) },
  { key: 'time', label: 'Time' },
];

const signalColumns = [
  { key: 'market', label: 'Market', render: (r) => shortMarket(marketQuestion(r.market)) },
  { key: 'outcome', label: 'Outcome', render: (r) => r.side || '—' },
  { key: 'strategy', label: 'Strategy' },
  { key: 'currentPrice', label: 'Current Price', align: 'right', render: (r) => (r.modelProbability == null ? '—' : r.modelProbability.toFixed(3)) },
  { key: 'modelProbability', label: 'Model Probability', align: 'right', render: (r) => (r.modelProbability == null ? '—' : pct1(r.modelProbability * 100)) },
  { key: 'impliedProbability', label: 'Implied Probability', align: 'right', render: (r) => (r.impliedProbability == null ? '—' : pct1(r.impliedProbability * 100)) },
  { key: 'grossEdge', label: 'Gross Edge', align: 'right', render: (r) => (r.grossEdge == null ? '—' : r.grossEdge > 0 ? `+${pct2(r.grossEdge)}` : pct2(r.grossEdge)) },
  { key: 'estimatedCost', label: 'Est. Costs', align: 'right', render: (r) => (r.estimatedCost == null ? '—' : money(r.estimatedCost)) },
  { key: 'netEdge', label: 'Net Edge', align: 'right', render: (r) => (r.netEdge == null ? '—' : r.netEdge > 0 ? `+${pct2(r.netEdge)}` : pct2(r.netEdge)) },
  { key: 'confidence', label: 'Confidence', align: 'right', render: (r) => (r.confidence == null ? '—' : pct1(r.confidence * 100)) },
  { key: 'liquidity', label: 'Liquidity', align: 'right', render: (r) => '—' },
  { key: 'spread', label: 'Spread', align: 'right', render: (r) => '—' },
  { key: 'timeToResolution', label: 'Time to Resolution', render: (r) => '—' },
  { key: 'time', label: 'Signal Time' },
  { key: 'decision', label: 'Decision', render: (r) => { const d = resolveDecision(r); return decisionBadge(d); } },
  { key: 'rejectionReason', label: 'Rejection Reason', render: (r) => {
    const reason = r.rejectionReason;
    if (!reason) return '—';
    return statusBadge('ERROR', humanizeRejection(reason));
  }},
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

const positionsFullColumns = [
  { key: 'market', label: 'Market', render: (r) => shortMarket(marketQuestion(r.market)) },
  {
    key: 'outcome',
    label: 'Outcome',
    align: 'center',
    render: (r) => statusBadge(r.outcome === 'NO' ? 'ERROR' : 'HEALTHY', r.outcome),
  },
  { key: 'side', label: 'Side', align: 'center', render: (r) => (r.side === 'NO' ? 'SHORT' : 'LONG') },
  { key: 'entry', label: 'Entry Price', align: 'right', render: (r) => (r.entry == null ? '—' : r.entry.toFixed(3)) },
  { key: 'price', label: 'Current Price', align: 'right', render: (r) => (r.price == null ? '—' : r.price.toFixed(3)) },
  { key: 'size', label: 'Position Size', align: 'right', render: (r) => optNum(r.size) },
  { key: 'exposure', label: 'Exposure', align: 'right', render: (r) => (r.exposure == null ? '—' : money(r.exposure)) },
  { key: 'uPnL', label: 'Unrealized P&L', align: 'right', render: (r) => (r.uPnL == null ? '—' : signedMoney(r.uPnL)) },
  { key: 'rPnL', label: 'Realized P&L', align: 'right', render: (r) => (r.rPnL == null ? '—' : signedMoney(r.rPnL)) },
  {
    key: 'returnPct',
    label: 'Return %',
    align: 'right',
    render: (r) => (r.returnPct == null ? '—' : pct1(r.returnPct * 100)),
  },
  { key: 'timeToResolution', label: 'Time To Resolution', align: 'right', render: (r) => fmtDuration(r.timeToResolution) },
  { key: 'riskStatus', label: 'Risk Status', align: 'center', render: (r) => riskStatusCell(r.riskStatus) },
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
  renderTable(table, 'Active strategy signals', overviewSignalColumns, candidates, 'No active signals');
}

function renderSignalsFullTable() {
  const g = state.groups.signals;
  const table = $('#signals-full-table');
  if (g.status === 'error') return renderTableMessage(table, 'Unable to load live data', 'error');
  if (g.status === 'loading' || g.status === 'idle') return renderTableMessage(table, 'Loading live data…');
  renderSignalsPage(table);
}

/* ================================================================
   Signals page — filtering + sorting
   ================================================================ */

function getFilteredSignals() {
  const g = state.groups.signals;
  if (!g.data) return [];
  let rows = g.data;

  if (signalsFilter.strategy) {
    rows = rows.filter((r) => r.strategy === signalsFilter.strategy);
  }
  if (signalsFilter.market) {
    rows = rows.filter((r) => r.market === signalsFilter.market);
  }
  if (signalsFilter.decision) {
    rows = rows.filter((r) => resolveDecision(r) === signalsFilter.decision);
  }
  if (signalsFilter.minEdge != null) {
    rows = rows.filter((r) => r.netEdge != null && r.netEdge >= signalsFilter.minEdge);
  }
  if (signalsFilter.minConfidence != null) {
    rows = rows.filter((r) => r.confidence != null && r.confidence >= signalsFilter.minConfidence);
  }

  if (signalsFilter.sortBy) {
    const key = signalsFilter.sortBy;
    const dir = signalsFilter.sortDir === 'asc' ? 1 : -1;
    rows = [...rows].sort((a, b) => {
      const av = a[key];
      const bv = b[key];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === 'string') return av.localeCompare(bv) * dir;
      return (av - bv) * dir;
    });
  }

  return rows;
}

function populateFilterDropdowns() {
  const g = state.groups.signals;
  if (!g.data) return;
  const stratSelect = $('#filter-strategy');
  const mktSelect = $('#filter-market');
  if (!stratSelect || !mktSelect) return;

  const strategySet = new Set();
  const marketSet = new Set();
  g.data.forEach((s) => {
    if (s.strategy) strategySet.add(s.strategy);
    if (s.market) marketSet.add(s.market);
  });

  const currentStrat = stratSelect.value;
  stratSelect.innerHTML = '<option value="">All Strategies</option>';
  [...strategySet].sort().forEach((s) => {
    const opt = document.createElement('option');
    opt.value = s;
    opt.textContent = s;
    stratSelect.appendChild(opt);
  });
  stratSelect.value = currentStrat;

  const currentMkt = mktSelect.value;
  mktSelect.innerHTML = '<option value="">All Markets</option>';
  [...marketSet].sort().forEach((m) => {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = shortMarket(marketQuestion(m));
    mktSelect.appendChild(opt);
  });
  mktSelect.value = currentMkt;
}

function renderSignalsSummary(total, filtered) {
  const el = $('#signals-summary');
  if (!el) return;
  if (total === filtered) {
    el.textContent = `${filtered} signal${filtered !== 1 ? 's' : ''}`;
  } else {
    el.textContent = `Showing ${filtered} of ${total} signals`;
  }
}

function renderSignalsPage(tableEl) {
  const g = state.groups.signals;
  const table = tableEl || $('#signals-full-table');
  if (!table) return;
  if (g.status === 'error') return renderTableMessage(table, 'Unable to load live data', 'error');
  if (g.status === 'loading' || g.status === 'idle') return renderTableMessage(table, 'Loading live data…');

  populateFilterDropdowns();
  const filtered = getFilteredSignals();
  renderSignalsSummary(g.data.length, filtered.length);
  renderTable(table, 'Signal analysis', signalColumns, filtered, 'No signals match filters');
}

function initSignalsFilters() {
  const bind = (sel, key, transform) => {
    const el = $(sel);
    if (!el) return;
    el.addEventListener('input', () => {
      signalsFilter[key] = transform ? transform(el.value) : el.value;
      renderSignalsPage();
    });
  };

  bind('#filter-strategy', 'strategy');
  bind('#filter-market', 'market');
  bind('#filter-decision', 'decision');
  bind('#filter-min-edge', 'minEdge', (v) => v === '' ? null : parseFloat(v) || null);
  bind('#filter-min-confidence', 'minConfidence', (v) => v === '' ? null : parseFloat(v) || null);

  const resetBtn = $('#filter-reset');
  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      signalsFilter.strategy = '';
      signalsFilter.market = '';
      signalsFilter.decision = '';
      signalsFilter.minEdge = null;
      signalsFilter.minConfidence = null;
      signalsFilter.sortBy = null;
      signalsFilter.sortDir = 'desc';
      $('#filter-strategy').value = '';
      $('#filter-market').value = '';
      $('#filter-decision').value = '';
      $('#filter-min-edge').value = '';
      $('#filter-min-confidence').value = '';
      renderSignalsPage();
    });
  }
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
  renderTable(table, 'All positions', positionsFullColumns, g.data, 'No positions yet', (r) => r.id);
}

function renderPositionsSummary() {
  const g = state.groups.positions;
  const risk = state.groups.risk;
  const ov = state.groups.overview;
  const container = $('#positions-summary-cards');
  if (!container) return;
  if (g.status === 'error' && risk.status === 'error') {
    container.replaceChildren(errorCard('Portfolio Summary', g.error, true));
    return;
  }
  const count = g.status === 'ok' ? (g.total != null ? g.total : (g.data ? g.data.length : null)) : null;
  const r = risk.status === 'ok' ? risk.data : null;
  const o = ov.status === 'ok' ? ov.data : null;
  const largestMarket = r ? r.largestPositionMarket : null;
  const cards = [
    metricCard({
      label: 'Total Exposure',
      value: r && r.exposure != null ? moneyInt(r.exposure) : '—',
      sub: r ? 'notional (size × price)' : null,
    }),
    metricCard({ label: 'Number of Positions', value: count == null ? '—' : num(count), sub: 'open positions' }),
    metricCard({ label: 'Largest Position', value: r ? money(r.largestPosition) : '—', sub: largestMarket ? shortMarket(marketQuestion(largestMarket)) : null }),
    metricCard({ label: 'Largest Market', value: largestMarket ? shortMarket(marketQuestion(largestMarket)) : '—', sub: largestMarket ? largestMarket : null }),
    metricCard({ label: 'Portfolio P&L', value: o ? signedMoney(o.totalPnl) : '—', tone: o ? pnlTone(o.totalPnl) : 'default', sub: 'realised + unrealised (backend)' }),
    metricCard({ label: 'Portfolio Drawdown', value: o ? `-${pct1(o.maxDrawdown * 100)}` : '—', tone: o && o.maxDrawdown > 0.08 ? 'neg' : 'default', sub: 'peak to trough' }),
  ];
  container.replaceChildren(...cards);
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

function renderPerformance() {
  renderPerformanceMode();
  renderPerformanceWindowNote();
  renderPerformanceCards();
  renderPerformanceCharts();
}

function renderPerformanceMode() {
  const label = $('#perf-mode-label');
  if (!label) return;
  const g = state.groups.performance;
  const mode = g.status === 'ok' && g.data ? String(g.data.mode || 'PAPER').toUpperCase() : 'PAPER';
  label.textContent = mode === 'LIVE' ? 'LIVE PERFORMANCE' : 'PAPER PERFORMANCE';
  const badge = $('#perf-mode-badge');
  if (badge) badge.classList.toggle('mode-halted', mode !== 'LIVE' && mode !== 'PAPER');
}

function renderPerformanceWindowNote() {
  const note = $('#perf-window-note');
  if (!note) return;
  const from = performanceFilter.from;
  const to = performanceFilter.to;
  if (from && to) note.textContent = `Analysis window ${from} → ${to} UTC`;
  else if (from) note.textContent = `Analysis window from ${from} UTC`;
  else if (to) note.textContent = `Analysis window up to ${to} UTC`;
  else note.textContent = 'Showing all-time performance';
}

function renderPerformanceCards() {
  const g = state.groups.performance;
  const container = $('#performance-cards');
  if (g.status !== 'ok' || !g.data) {
    container.replaceChildren(errorCard('Performance', g.error, true));
    return;
  }
  const p = g.data;
  const tradesSub = `window ${num(p.numberOfTrades == null ? 0 : p.numberOfTrades)} filled order${p.numberOfTrades === 1 ? '' : 's'}`;
  const cards = [
    metricCard({ label: 'Total P&L (window)', value: signedMoney(p.totalPnl), tone: pnlTone(p.totalPnl), sub: tradesSub, highlight: true }),
    metricCard({ label: "Today's P&L", value: signedMoney(p.todayPnl), tone: pnlTone(p.todayPnl), sub: 'since midnight UTC' }),
    metricCard({ label: 'Week P&L', value: signedMoney(p.weekPnl), tone: pnlTone(p.weekPnl), sub: 'since Monday UTC' }),
    metricCard({ label: 'Month P&L', value: signedMoney(p.monthPnl), tone: pnlTone(p.monthPnl), sub: 'since 1st of month UTC' }),
    metricCard({
      label: 'Return %',
      value: p.returnPct == null ? '—' : pct2(p.returnPct),
      tone: p.returnPct == null ? 'default' : pnlTone(p.returnPct),
      sub: 'window P&L ÷ initial equity',
    }),
    metricCard({
      label: 'Max Drawdown',
      value: p.maxDrawdown == null ? '—' : `-${pct2(p.maxDrawdown)}`,
      tone: p.maxDrawdown != null && p.maxDrawdown > 0.08 ? 'neg' : 'default',
      sub: 'peak to trough (window)',
    }),
    metricCard({
      label: 'Win Rate',
      value: p.winRate == null ? '—' : pct1(p.winRate * 100),
      tone: p.winRate == null ? 'default' : p.winRate >= 0.5 ? 'pos' : 'neg',
      sub: 'winning trades ÷ decided trades',
    }),
    metricCard({
      label: 'Loss Rate',
      value: p.lossRate == null ? '—' : pct1(p.lossRate * 100),
      tone: p.lossRate == null ? 'default' : p.lossRate > 0.5 ? 'neg' : 'default',
      sub: 'losing trades ÷ decided trades',
    }),
    metricCard({ label: 'Profit Factor', value: p.profitFactor == null ? '—' : p.profitFactor.toFixed(2), sub: 'gross profit ÷ gross loss' }),
    metricCard({ label: 'Expectancy', value: signedMoney(p.expectancy), tone: pnlTone(p.expectancy), sub: 'mean P&L per trade' }),
    metricCard({ label: 'Average Trade', value: signedMoney(p.averageTrade), tone: pnlTone(p.averageTrade), sub: 'mean absolute P&L' }),
    metricCard({ label: 'Average Win', value: p.averageWin == null ? '—' : signedMoney(p.averageWin), tone: p.averageWin != null ? 'pos' : 'default' }),
    metricCard({ label: 'Average Loss', value: p.averageLoss == null ? '—' : signedMoney(p.averageLoss), tone: p.averageLoss != null ? 'neg' : 'default' }),
    metricCard({ label: 'Number of Trades', value: optNum(p.numberOfTrades), sub: tradesSub }),
    metricCard({ label: 'Avg Holding Time', value: fmtDuration(p.averageHoldingTime), sub: 'filled order duration' }),
    metricCard({ label: 'Avg Net Edge', value: p.averageNetEdge == null ? '—' : pct2(p.averageNetEdge), sub: 'signal edge at entry' }),
    metricCard({ label: 'Slippage', value: p.slippage == null ? '—' : money(p.slippage), tone: p.slippage != null && p.slippage > 0 ? 'neg' : 'default', sub: 'fill slippage (window)' }),
    metricCard({ label: 'Realised P&L (all-time)', value: signedMoney(p.totalRealisedPnl), tone: pnlTone(p.totalRealisedPnl), sub: 'closed-position P&L, all-time' }),
  ];
  container.replaceChildren(...cards);
}

function renderPerformanceCharts() {
  if (!charts.perfEquity) return;

  const g = state.groups.performance;
  const empty = g.status !== 'ok' || !g.data;
  const c = empty ? { equity: [], dailyPnl: [], cumulativePnl: [], drawdown: [], byStrategy: [], byCategory: [] } : g.data.charts;

  const equityLabels = c.equity.map((p) => fmtDateTime(p.timestamp));
  charts.perfEquity.data.labels = equityLabels;
  charts.perfEquity.data.datasets[0].data = c.equity.map((p) => p.value);
  charts.perfEquity.update('none');

  const note = $('#perf-equity-note');
  if (note) {
    if (empty) note.textContent = 'Unable to load live data — performance API unavailable.';
    else if (c.equity.length === 0) note.textContent = 'No equity data in the selected window.';
    else note.textContent = `${num(c.equity.length)} observations · last ${money(c.equity[c.equity.length - 1].value)}`;
  }

  const dailyLabels = c.dailyPnl.map((p) => p.label);
  charts.perfDaily.data.labels = dailyLabels;
  charts.perfDaily.data.datasets[0].data = c.dailyPnl.map((p) => p.value);
  charts.perfDaily.data.datasets[0].backgroundColor = c.dailyPnl.map((p) =>
    p.value >= 0 ? 'rgba(34,197,94,0.75)' : 'rgba(239,68,68,0.75)',
  );
  charts.perfDaily.update('none');

  charts.perfCumulative.data.labels = c.cumulativePnl.map((p) => p.label);
  charts.perfCumulative.data.datasets[0].data = c.cumulativePnl.map((p) => p.value);
  charts.perfCumulative.update('none');

  charts.perfDrawdown.data.labels = c.drawdown.map((p) => fmtDateTime(p.timestamp));
  charts.perfDrawdown.data.datasets[0].data = c.drawdown.map((p) => p.value);
  charts.perfDrawdown.update('none');

  const strategy = c.byStrategy;
  charts.perfStrategy.data.labels = strategy.map((p) => p.label);
  charts.perfStrategy.data.datasets[0].data = strategy.map((p) => p.value);
  charts.perfStrategy.data.datasets[0].backgroundColor = strategy.map((p) =>
    p.value >= 0 ? 'rgba(34,197,94,0.75)' : 'rgba(239,68,68,0.75)',
  );
  charts.perfStrategy.update('none');

  const category = c.byCategory;
  charts.perfCategory.data.labels = category.map((p) => p.label);
  charts.perfCategory.data.datasets[0].data = category.map((p) => p.value);
  charts.perfCategory.data.datasets[0].backgroundColor = category.map((p) =>
    p.value >= 0 ? 'rgba(0,200,255,0.8)' : 'rgba(239,68,68,0.8)',
  );
  charts.perfCategory.update('none');
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
  const detail = $('#risk-tiles-view');
  if (detail) renderTiles(detail, tiles);
}

/* ================================================================
   Dedicated Risk page (view-risk)
   Renders only backend-computed values — no client-side derivation
   of risk controls.  The circuit breaker state and its reasons come
   straight from the read-only API; nothing here can re-enable
   trading or change limits.
   ================================================================ */

const REASON_LABELS = {
  DAILY_LOSS: 'DAILY LOSS LIMIT REACHED',
  DAILY_LOSS_LIMIT: 'DAILY LOSS LIMIT REACHED',
  STALE_DATA: 'STALE MARKET DATA',
  STALE_MARKET_DATA: 'STALE MARKET DATA',
  HIGH_SPREAD: 'HIGH SPREAD',
  LOW_LIQUIDITY: 'LOW LIQUIDITY',
  API_HEALTH: 'API DISCONNECTED',
  API_DISCONNECTED: 'API DISCONNECTED',
  MODEL_UNAVAILABLE: 'MODEL UNAVAILABLE',
  CONSECUTIVE_LOSSES: 'CONSECUTIVE LOSSES LIMIT',
};

function humanizeReason(reason) {
  if (REASON_LABELS[reason]) return REASON_LABELS[reason];
  return String(reason || 'UNKNOWN').replace(/_/g, ' ');
}

function riskCard({ label, value, status, sub, tone, pct, highlight = false }) {
  const card = el('article', 'risk-card' + (highlight ? ' highlight' : ''));
  card.append(el('span', 'risk-card-label', label));
  const row = el('div', 'risk-card-row');
  const valueEl = el('span', `risk-card-value ${tone || ''}`, value);
  row.appendChild(valueEl);
  if (status) row.appendChild(statusBadge(status));
  card.appendChild(row);
  if (sub) card.append(el('span', 'risk-card-sub', sub));
  if (pct != null) {
    const bar = el('div', 'risk-card-bar');
    const fill = el('i', `tile-bar-fill bar-${String(status || 'HEALTHY').toLowerCase()}`);
    fill.style.width = `${clamp(pct, 0, 100)}%`;
    bar.appendChild(fill);
    card.appendChild(bar);
  }
  return card;
}

function renderRiskSection(sel, cards) {
  const container = $(sel);
  if (!container) return;
  container.replaceChildren(...cards);
}

function riskSectionMessage(sel, message, level) {
  const container = $(sel);
  if (container) container.replaceChildren(el('p', `risk-empty ${level || 'info'}`, message));
}

function buildAccountRiskCards(r) {
  if (!r) return [riskCard({ label: 'Balance', value: '—', status: 'WARNING' })];
  const exposureStatus = ratioLevel(r.exposure, r.exposureLimit);
  return [
    riskCard({
      label: 'Balance',
      value: money(r.accountBalance),
      sub: 'initial equity + realised/unrealised P&L',
      highlight: true,
    }),
    riskCard({ label: 'Available Balance', value: money(r.availableBalance), sub: 'balance minus notional exposure' }),
    riskCard({
      label: 'Total Exposure',
      value: moneyInt(r.exposure),
      status: exposureStatus,
      pct: ratioPct(r.exposure, r.exposureLimit),
      sub: `notional (size × price) · limit ${moneyInt(r.exposureLimit)}`,
    }),
    riskCard({
      label: 'Exposure %',
      value: pct1(r.exposurePct),
      status: exposureStatus,
      sub: 'of maximum exposure',
    }),
    riskCard({ label: 'Maximum Exposure', value: moneyInt(r.exposureLimit), sub: 'config max_total_exposure_pct' }),
  ];
}

function buildLossControlCards(r) {
  if (!r) return [riskCard({ label: "Today's P&L", value: '—', status: 'WARNING' })];
  return [
    riskCard({ label: "Today's P&L", value: signedMoney(r.todayPnl), tone: pnlTone(r.todayPnl), sub: 'filled orders since midnight UTC' }),
    riskCard({
      label: 'Daily Loss',
      value: money(r.dailyLoss),
      status: ratioLevel(r.dailyLoss, r.dailyLossLimit),
      pct: ratioPct(r.dailyLoss, r.dailyLossLimit),
    }),
    riskCard({ label: 'Daily Loss Limit', value: money(r.dailyLossLimit), sub: 'config max_daily_loss_pct' }),
    riskCard({
      label: 'Consecutive Losses',
      value: `${r.consecutiveLosses} / ${r.consecutiveLossLimit}`,
      status: ratioLevel(r.consecutiveLosses, r.consecutiveLossLimit),
      pct: ratioPct(r.consecutiveLosses, r.consecutiveLossLimit),
    }),
    riskCard({ label: 'Maximum Consecutive Losses', value: num(r.consecutiveLossLimit), sub: 'config max_consecutive_losses' }),
  ];
}

function buildMarketRiskCards(r) {
  if (!r) return [riskCard({ label: 'Open Positions', value: '—', status: 'WARNING' })];
  const largestMarket = r.largestPositionMarket
    ? shortMarket(marketQuestion(r.largestPositionMarket))
    : '—';
  const spreadStatus =
    r.spreadStatus === 'HIGH' ? 'ERROR' : r.spreadStatus === 'OK' ? 'HEALTHY' : 'WARNING';
  const liquidityStatus =
    r.liquidityStatus === 'LOW' ? 'ERROR' : r.liquidityStatus === 'OK' ? 'HEALTHY' : 'WARNING';
  return [
    riskCard({
      label: 'Open Positions',
      value: num(r.openPositions),
      status: ratioLevel(r.openPositions, r.maxOpenPositions),
      pct: ratioPct(r.openPositions, r.maxOpenPositions),
      sub: `of max ${num(r.maxOpenPositions)}`,
    }),
    riskCard({ label: 'Largest Position', value: money(r.largestPosition), sub: largestMarket }),
    riskCard({ label: 'Largest Market Exposure', value: money(r.largestMarketExposure), sub: 'single-market notional' }),
    riskCard({ label: 'Average Spread', value: r.averageSpread == null ? '—' : pct2(r.averageSpread), status: spreadStatus }),
    riskCard({ label: 'Minimum Liquidity', value: r.minimumLiquidity == null ? '—' : money(r.minimumLiquidity), status: liquidityStatus }),
  ];
}

function buildSystemRiskCards(r, health) {
  const level = (name) =>
    health && health.checks && health.checks[name]
      ? checkHealthy(health.checks[name].healthy)
      : 'UNAVAILABLE';
  const breaker = r ? r.circuitBreaker : null;
  const breakerHalted = breaker && (breaker.state === 'HALTED' || breaker.state === 'TRIPPED');
  const breakerWarning = breaker && breaker.state === 'WARNING';

  let riskEngine;
  if (state.killArmed || breakerHalted) riskEngine = 'ERROR';
  else if (breakerWarning || (r && (r.spreadStatus === 'HIGH' || r.liquidityStatus === 'LOW'))) {
    riskEngine = 'WARNING';
  } else riskEngine = 'HEALTHY';

  const freshness =
    !r ? 'UNAVAILABLE'
      : r.dataFreshness === 'FRESH' ? 'HEALTHY'
        : r.dataFreshness === 'STALE' ? 'ERROR'
          : 'WARNING';

  return [
    riskCard({ label: 'Data Freshness', value: r ? r.dataFreshness : '—', status: freshness }),
    riskCard({ label: 'API Health', value: level('api'), status: level('api') }),
    riskCard({ label: 'Model Health', value: level('model_availability'), status: level('model_availability') }),
    riskCard({ label: 'Database Health', value: level('database'), status: level('database') }),
    riskCard({ label: 'Risk Engine Health', value: riskEngine, status: riskEngine }),
  ];
}

function renderBreaker(r) {
  const panel = $('#breaker-panel');
  if (!panel) return;
  const breaker = r ? r.circuitBreaker : null;
  const stateStr = breaker && breaker.state ? String(breaker.state).toUpperCase() : 'NORMAL';
  const halted = stateStr === 'HALTED' || stateStr === 'TRIPPED';
  const warning = stateStr === 'WARNING';
  const level = halted ? 'error' : warning ? 'warning' : 'healthy';

  panel.classList.remove('breaker-normal', 'breaker-warning', 'breaker-halted');
  panel.classList.add(halted ? 'breaker-halted' : warning ? 'breaker-warning' : 'breaker-normal');

  const indicator = $('#breaker-indicator');
  if (indicator) {
    indicator.textContent = stateStr;
    indicator.className = `breaker-indicator ${level}`;
  }
  const statusEl = $('#breaker-status');
  if (statusEl) {
    statusEl.textContent = halted
      ? 'TRADING DISABLED'
      : warning
        ? 'TRADING PERMITTED — CAUTION'
        : 'TRADING ENABLED';
    statusEl.className = `breaker-status ${level}`;
  }

  const reasons = $('#breaker-reasons');
  if (reasons) {
    reasons.replaceChildren();
    const reasonList = breaker && breaker.reasons ? breaker.reasons : [];
    if (reasonList.length) {
      reasons.append(el('p', 'breaker-reasons-title', 'REASONS'));
      reasonList.forEach((reason) => {
        reasons.appendChild(el('span', 'breaker-reason', humanizeReason(reason)));
      });
    } else if (halted || warning) {
      reasons.append(el('p', 'breaker-reasons-title', 'No reasons recorded by backend'));
    }
  }

  const banner = $('#risk-halt-banner');
  if (banner) {
    banner.hidden = !(halted || state.killArmed);
    const list = $('#risk-halt-reasons');
    if (list) {
      list.replaceChildren();
      const reasonList = breaker && breaker.reasons ? breaker.reasons : [];
      if (reasonList.length) {
        reasonList.forEach((reason) => {
          list.appendChild(el('li', null, humanizeReason(reason)));
        });
      } else {
        list.appendChild(el('li', null, 'Halt triggered by the backend risk engine.'));
      }
    }
  }
}

function renderRiskPage() {
  const g = state.groups.risk;
  const health = state.groups.health;
  const sections = ['#risk-account-cards', '#risk-loss-cards', '#risk-market-cards', '#risk-system-cards'];
  if (!$('#risk-account-cards')) return;

  if (g.status === 'error') {
    const message = `Unable to load live risk data — ${describeError(g.error)}`;
    sections.forEach((sel) => riskSectionMessage(sel, message, 'error'));
    renderBreaker(null);
    return;
  }
  if (g.status === 'loading' || g.status === 'idle') {
    sections.forEach((sel) => riskSectionMessage(sel, 'Loading live risk data…'));
    renderBreaker(null);
    return;
  }
  const r = g.data;
  renderRiskSection('#risk-account-cards', buildAccountRiskCards(r));
  renderRiskSection('#risk-loss-cards', buildLossControlCards(r));
  renderRiskSection('#risk-market-cards', buildMarketRiskCards(r));
  renderRiskSection('#risk-system-cards', buildSystemRiskCards(r, health.status === 'ok' ? health.data : null));
  renderBreaker(r);
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
  renderPositionsSummary();
  renderOrdersTable();
  renderOrdersFullTable();
  renderAuditTable();
  renderPerformance();
  renderRisk();
  renderRiskPage();
  renderSystem();
  updateModeBadge();
  updateCharts();
  updateDataStatus();
  renderFooter();
}

/* ================================================================
   Position detail drawer
   ================================================================ */

const positionDrawer = {
  open: false,
  positionId: null,
  marketId: null,
  chart: null,
};

function initPositionDrawer() {
  const closeBtn = $('#position-drawer-close');
  const backdrop = $('#position-drawer-backdrop');
  if (closeBtn) closeBtn.addEventListener('click', closePositionDrawer);
  if (backdrop) {
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closePositionDrawer();
    });
  }
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closePositionDrawer();
  });
  const table = $('#positions-full-table');
  if (table) {
    table.addEventListener('click', (event) => {
      const row = event.target && event.target.closest ? event.target.closest('tr[data-row-key]') : null;
      if (!row) return;
      const id = row.dataset.rowKey;
      const pos = (state.groups.positions.data || []).find((p) => String(p.id) === String(id));
      if (pos) openPositionDrawer(pos);
    });
  }
}

function openPositionDrawer(pos) {
  positionDrawer.open = true;
  positionDrawer.positionId = pos ? pos.id : null;
  positionDrawer.marketId = pos ? pos.market : null;
  $('#position-drawer-backdrop').hidden = false;
  document.body.style.overflow = 'hidden';

  renderPositionDrawerHeader(pos);
  renderPositionDrawerSummary(pos);
  $('#position-price-chart').replaceChildren(el('p', 'drawer-empty', 'Loading price history…'));
  const tables = ['#position-history-table', '#position-signals-table', '#position-orders-table', '#position-risk-table'];
  tables.forEach((sel) => renderTableMessage($(sel), 'Loading detail…'));
  loadPositionDrawerDetails();
}

function closePositionDrawer() {
  if (!positionDrawer.open) return;
  positionDrawer.open = false;
  $('#position-drawer-backdrop').hidden = true;
  document.body.style.overflow = '';
  if (positionDrawer.chart) {
    positionDrawer.chart.destroy();
    positionDrawer.chart = null;
  }
}

function refreshPositionDrawer() {
  if (!positionDrawer.open) return;
  const pos = (state.groups.positions.data || []).find(
    (p) => String(p.id) === String(positionDrawer.positionId),
  );
  renderPositionDrawerSummary(pos);
  loadPositionDrawerDetails();
}

async function loadPositionDrawerDetails() {
  const marketId = positionDrawer.marketId;
  if (!marketId) return;
  const q = encodeURIComponent(marketId);
  const urls = {
    snapshots: `/api/dashboard/markets/${q}/snapshots?limit=120`,
    history: `/api/dashboard/positions?open_only=false&market_id=${q}&limit=50`,
    signals: `/api/dashboard/signals?market_id=${q}&limit=50`,
    orders: `/api/dashboard/orders?market_id=${q}&limit=50`,
    risk: `/api/dashboard/audit?market_id=${q}&limit=50`,
  };
  const entries = await Promise.allSettled(
    Object.entries(urls).map(async ([key, path]) => [key, await fetchJSON(path)]),
  );
  const results = {};
  entries.forEach((entry) => {
    if (entry.status === 'fulfilled') results[entry.value[0]] = entry.value[1];
    else results[entry.reason && entry.reason.code] = null;
  });
  renderPositionPriceChart(results.snapshots ? mapSnapshots(results.snapshots) : null);
  renderPositionHistoryTable(results.history ? mapPositions(results.history) : null);
  renderPositionSignalsTable(results.signals ? mapSignals(results.signals) : null);
  renderPositionOrdersTable(results.orders ? mapOrders(results.orders) : null);
  renderPositionRiskTable(results.audit ? mapAudit(results.audit) : null);
}

function renderPositionDrawerHeader(pos) {
  const kicker = $('#position-drawer-kicker');
  const title = $('#position-drawer-title');
  if (!pos) {
    kicker.textContent = positionDrawer.marketId || '—';
    title.textContent = 'Position';
    return;
  }
  kicker.textContent = `${pos.market} · ${pos.id}`;
  title.textContent = marketQuestion(pos.market);
}

function renderPositionDrawerSummary(pos) {
  const container = $('#position-drawer-summary');
  if (!pos) {
    container.replaceChildren(el('p', 'drawer-empty', 'No live data for this position.'));
    return;
  }
  const cells = [
    ['Side', pos.side === 'NO' ? 'SHORT (NO)' : 'LONG (YES)'],
    ['Position Size', optNum(pos.size)],
    ['Entry Price', pos.entry == null ? '—' : pos.entry.toFixed(3)],
    ['Current Price', pos.price == null ? '—' : pos.price.toFixed(3)],
    ['Exposure', pos.exposure == null ? '—' : money(pos.exposure)],
    ['Unrealized P&L', pos.uPnL == null ? '—' : signedMoney(pos.uPnL)],
    ['Realized P&L', pos.rPnL == null ? '—' : signedMoney(pos.rPnL)],
    ['Return %', pos.returnPct == null ? '—' : pct1(pos.returnPct * 100)],
    ['Time To Resolution', fmtDuration(pos.timeToResolution)],
  ];
  const dl = el('dl', 'drawer-facts');
  cells.forEach(([label, value]) => {
    const item = el('div', 'drawer-fact');
    item.append(el('dt', 'drawer-fact-label', label), el('dd', 'drawer-fact-value', value));
    dl.appendChild(item);
  });
  const statusWrap = el('div', 'drawer-fact');
  const statusValue = el('dd', 'drawer-fact-value');
  statusValue.appendChild(riskStatusCell(pos.riskStatus));
  statusWrap.append(el('dt', 'drawer-fact-label', 'Risk Status'), statusValue);
  dl.appendChild(statusWrap);
  container.replaceChildren(dl);
}

function renderPositionPriceChart(snapshots) {
  const wrap = $('#position-price-chart');
  if (positionDrawer.chart) {
    positionDrawer.chart.destroy();
    positionDrawer.chart = null;
  }
  if (!snapshots || !snapshots.length) {
    wrap.replaceChildren(el('p', 'drawer-empty', 'No price history available.'));
    return;
  }
  if (typeof Chart === 'undefined') {
    wrap.replaceChildren(el('p', 'drawer-empty', 'Chart.js could not be loaded.'));
    return;
  }
  const canvas = document.createElement('canvas');
  wrap.replaceChildren(canvas);
  const bodyStyle = getComputedStyle(document.body);
  const faint = bodyStyle.getPropertyValue('--text-faint').trim();
  const gridColor = 'rgba(148,163,184,0.08)';
  positionDrawer.chart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: snapshots.map((s) => fmtDateTime(s.timestamp)),
      datasets: [
        {
          label: 'Midpoint',
          data: snapshots.map((s) => s.midpoint),
          borderColor: '#00c8ff',
          backgroundColor: 'rgba(0,200,255,0.12)',
          fill: true,
          tension: 0.35,
          borderWidth: 2,
          pointRadius: 0,
          pointHitRadius: 10,
        },
      ],
    },
    options: {
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
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(3)}`,
          },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 8, color: faint } },
        y: { grid: { color: gridColor }, ticks: { color: faint } },
      },
    },
  });
}

const positionHistoryColumns = [
  { key: 'market', label: 'Market', render: (r) => shortMarket(marketQuestion(r.market)) },
  { key: 'side', label: 'Side', align: 'center', render: (r) => (r.side === 'NO' ? 'SHORT' : 'LONG') },
  { key: 'size', label: 'Size', align: 'right', render: (r) => optNum(r.size) },
  { key: 'entry', label: 'Entry', align: 'right', render: (r) => (r.entry == null ? '—' : r.entry.toFixed(3)) },
  { key: 'price', label: 'Current', align: 'right', render: (r) => (r.price == null ? '—' : r.price.toFixed(3)) },
  { key: 'uPnL', label: 'Unrealized P&L', align: 'right', render: (r) => (r.uPnL == null ? '—' : signedMoney(r.uPnL)) },
  { key: 'rPnL', label: 'Realized P&L', align: 'right', render: (r) => (r.rPnL == null ? '—' : signedMoney(r.rPnL)) },
  { key: 'riskStatus', label: 'Risk Status', align: 'center', render: (r) => riskStatusCell(r.riskStatus) },
];

function renderPositionHistoryTable(rows) {
  const table = $('#position-history-table');
  if (rows == null) return renderTableMessage(table, 'Unable to load live data', 'error');
  renderTable(table, 'Position history', positionHistoryColumns, rows, 'No position history for this market.');
}

function renderPositionSignalsTable(rows) {
  const table = $('#position-signals-table');
  if (rows == null) return renderTableMessage(table, 'Unable to load live data', 'error');
  renderTable(table, 'Related signals', overviewSignalColumns, rows, 'No signals for this market.');
}

function renderPositionOrdersTable(rows) {
  const table = $('#position-orders-table');
  if (rows == null) return renderTableMessage(table, 'Unable to load live data', 'error');
  renderTable(table, 'Order history', orderColumns, rows, 'No orders for this market.');
}

function renderPositionRiskTable(rows) {
  const table = $('#position-risk-table');
  if (rows == null) return renderTableMessage(table, 'Unable to load live data', 'error');
  renderTable(table, 'Risk decisions', auditColumns, rows, 'No risk events for this market.');
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

  /* ---- Performance page charts (windowed, backend-computed) ---- */
  const perfLine = (label, color) => ({
    type: 'line',
    data: { labels: [], datasets: [{ label, data: [], borderColor: color, backgroundColor: 'rgba(0,200,255,0) ', fill: false, tension: 0.35, borderWidth: 2, pointRadius: 0, pointHitRadius: 10 }] },
    options: { ...baseOptions, scales: { x: baseOptions.scales.x, y: { ...baseOptions.scales.y, ticks: { ...baseOptions.scales.y.ticks, callback: (v) => moneyInt(v) } } } },
  });

  const perfEquityCtx = $('#perf-equity-chart').getContext('2d');
  const perfGrad = perfEquityCtx.createLinearGradient(0, 0, 0, 260);
  perfGrad.addColorStop(0, 'rgba(0,200,255,0.28)');
  perfGrad.addColorStop(1, 'rgba(0,200,255,0)');
  charts.perfEquity = new Chart(perfEquityCtx, {
    type: 'line',
    data: { labels: [], datasets: [{ label: 'Equity', data: [], borderColor: '#00c8ff', backgroundColor: perfGrad, fill: true, tension: 0.35, borderWidth: 2, pointRadius: 0, pointHitRadius: 10 }] },
    options: {
      ...baseOptions,
      scales: {
        x: baseOptions.scales.x,
        y: { ...baseOptions.scales.y, ticks: { ...baseOptions.scales.y.ticks, callback: (v) => moneyInt(v) } },
      },
    },
  });

  charts.perfDaily = new Chart($('#perf-daily-chart').getContext('2d'), {
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

  charts.perfCumulative = new Chart($('#perf-cumulative-chart').getContext('2d'), {
    ...perfLine('Cumulative P&L', '#34d399'),
  });

  charts.perfDrawdown = new Chart($('#perf-drawdown-chart').getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [{ label: 'Drawdown', data: [], borderColor: '#f87171', backgroundColor: 'rgba(248,113,113,0.12)', fill: true, tension: 0.35, borderWidth: 2, pointRadius: 0, pointHitRadius: 10 }] },
    options: {
      ...baseOptions,
      scales: {
        x: baseOptions.scales.x,
        y: { ...baseOptions.scales.y, ticks: { ...baseOptions.scales.y.ticks, callback: (v) => pct2(v) } },
      },
    },
  });

  charts.perfStrategy = new Chart($('#perf-strategy-chart').getContext('2d'), {
    type: 'bar',
    data: { labels: [], datasets: [{ label: 'P&L', data: [], borderRadius: 3 }] },
    options: {
      ...baseOptions,
      indexAxis: 'y',
      scales: {
        x: { ...baseOptions.scales.y, ticks: { ...baseOptions.scales.y.ticks, callback: (v) => moneyInt(v) } },
        y: { ...baseOptions.scales.x, ticks: { ...baseOptions.scales.x.ticks, maxTicksLimit: 8 } },
      },
    },
  });

  charts.perfCategory = new Chart($('#perf-category-chart').getContext('2d'), {
    type: 'bar',
    data: { labels: [], datasets: [{ label: 'P&L', data: [], borderRadius: 3 }] },
    options: {
      ...baseOptions,
      indexAxis: 'y',
      scales: {
        x: { ...baseOptions.scales.y, ticks: { ...baseOptions.scales.y.ticks, callback: (v) => moneyInt(v) } },
        y: { ...baseOptions.scales.x, ticks: { ...baseOptions.scales.x.ticks, maxTicksLimit: 8 } },
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

  const wl = getWinLoss();
  charts.winloss.data.datasets[0].data = wl ? [wl.wins, wl.losses] : [0, 0];
  charts.winloss.update('none');

  // Performance page charts derive from the windowed performance payload.
  renderPerformanceCharts();
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
  initSignalsFilters();
  initPerformanceFilters();
  initPositionDrawer();
  startScheduler();
  connectWS();
}

document.addEventListener('DOMContentLoaded', init);
