/* DOM-stubbed Node harness for dashboard.js WebSocket behaviour.
 *
 * Loads dashboard.js against a minimal fake DOM, drives the fake
 * WebSocket through open/message/close cycles, and asserts:
 *   - the connection URL carries the apiKey query param
 *   - CONNECTED/connect and per-event partial rendering (each event
 *     updates only its own section)
 *   - reconnect backoff sequence 1s -> 2s -> 5s -> 10s -> 30s (capped)
 *   - LIVE CONNECTION LOST banner + DATA STALE on repeated failure
 *   - banner clears and full re-sync happens on reconnect
 *
 * Run:  node dashboard/dashboard.ws.test.js
 */

'use strict';

const assert = require('node:assert');

/* ---------- fake DOM ---------- */

class Node {
  constructor() {
    this.children = [];
    this.textContent = '';
    this.className = '';
    this.hidden = false;
    this.colSpan = 1;
    this.style = {};
    this.attrs = {};
    this.classes = new Set();
    this.listeners = {};
  }
  append(...kids) { kids.forEach((k) => this.children.push(k)); return this; }
  appendChild(k) { this.children.push(k); return k; }
  prepend(...kids) { this.children.unshift(...kids); return this; }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(n, v) { this.attrs[n] = String(v); if (n === 'hidden') this.hidden = true; if (n === 'class') this.className = String(v); }
  removeAttribute(n) { delete this.attrs[n]; if (n === 'hidden') this.hidden = false; }
  getAttribute(n) { return this.attrs[n] != null ? this.attrs[n] : null; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn); }
  get classList() {
    const el = this;
    return {
      add(...c) { c.forEach((x) => el.classes.add(x)); },
      remove(...c) { c.forEach((x) => el.classes.delete(x)); },
      toggle(c, on) {
        if (on === undefined) { if (el.classes.has(c)) { el.classes.delete(c); return false; } el.classes.add(c); return true; }
        if (on) el.classes.add(c); else el.classes.delete(c);
      },
      contains(c) { return el.classes.has(c); },
    };
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  getContext() { return { createLinearGradient: () => ({ addColorStop() {} }) }; }
  text() {
    const parts = [];
    if (this.textContent) parts.push(this.textContent);
    this.children.forEach((c) => parts.push(c instanceof Node ? c.text() : String(c)));
    return parts.join('');
  }
}

global.Node = Node;

function makeEl(tag) {
  const el = Object.assign(new Node(), { tagName: String(tag).toUpperCase() });
  return el;
}

const elements = {};
function elFor(sel) { if (!elements[sel]) elements[sel] = makeEl(sel); return elements[sel]; }

let domReady = null;
const document = {
  addEventListener(type, fn) { if (type === 'DOMContentLoaded') domReady = fn; },
  createElement: (tag) => makeEl(tag),
  createDocumentFragment: () => makeEl('#fragment'),
  querySelector: (sel) => elFor(sel),
  querySelectorAll: () => [],
  body: makeEl('body'),
};

/* ---------- global stubs ---------- */

global.window = {
  location: { search: '' },
  DASHBOARD_CONFIG: { apiBase: 'http://localhost:8000', apiKey: 'sekret' },
};
global.document = document;
global.getComputedStyle = () => ({ getPropertyValue: () => '' });
global.IntersectionObserver = class { constructor() {} observe() {} };

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    FakeWebSocket.instances.push(this);
  }
  close() { this.readyState = FakeWebSocket.CLOSED; if (this.onclose) this.onclose(); }
  trigger(type, event) { const h = this[`on${type}`]; if (h) h(event); }
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;
FakeWebSocket.instances = [];
global.WebSocket = FakeWebSocket;

/* Timers: capture so reconnect backoff can be inspected / driven
   without real delays. setInterval is captured and never fired. */
const timers = [];
const intervals = [];
global.setTimeout = (fn, delay) => { const t = { fn, delay, cleared: false }; timers.push(t); return t; };
global.clearTimeout = (t) => { if (t) t.cleared = true; };
global.setInterval = (fn, delay) => { const it = { fn, delay, cleared: false }; intervals.push(it); return it; };
global.clearInterval = (it) => { if (it) it.cleared = true; };

/* REST stub — mirrors the read-only dashboard endpoints. */
const API = {
  '/overview': {
    account_balance: 1000, available_balance: 900, today_pnl: 12.34, total_pnl: 50,
    realized_pnl: 40, unrealized_pnl: 10, max_drawdown: 0.03, total_exposure: 200,
    open_positions: 2, active_signals: 3, bot_mode: 'PAPER', circuit_breaker: null,
  },
  '/equity': { points: [{ timestamp: '2026-08-11T10:00:00Z', equity: 1000 }] },
  '/signals': { items: [], total: 0 },
  '/markets': { items: [], total: 0 },
  '/positions': { items: [], total: 0 },
  '/orders': { items: [], total: 0 },
  '/performance': {
    total_realised_pnl: 0, total_unrealised_pnl: 0, total_pnl: 0, open_positions: 0,
    total_markets: 0, total_signals: 0, total_orders: 0, filled_orders: 0, timestamp: '2026-08-11T10:00:00Z',
  },
  '/risk': {
    daily_loss: 0, daily_loss_limit: 100, exposure: 0, exposure_limit: 500,
    consecutive_losses: 0, consecutive_loss_limit: 3, spread_status: 'OK',
    liquidity_status: 'OK', data_freshness: 'FRESH', circuit_breaker: null,
  },
  '/health': { healthy: true, checks: { database: { healthy: true }, api: { healthy: true }, data_freshness: { healthy: true }, model_availability: { healthy: true } }, timestamp: '2026-08-11T10:00:00Z' },
  '/audit': { items: [], total: 0 },
};
global.fetch = async (url) => {
  const path = String(url);
  const key = Object.keys(API).find((k) => path.includes(k)) || '/overview';
  return { ok: true, status: 200, text: async () => JSON.stringify(API[key]) };
};

/* ---------- load the dashboard script ---------- */

require('./dashboard.js');

/* ---------- helpers ---------- */

const tick = () => new Promise((r) => setImmediate(r));
async function settle() { for (let i = 0; i < 8; i += 1) await tick(); }

function send(ws, type, data) {
  ws.trigger('message', { data: JSON.stringify({ type, data }) });
}
function fireTimer(predicate) {
  const t = timers.find((x) => !x.cleared && predicate(x));
  assert.ok(t, 'expected a pending timer');
  t.cleared = true;
  t.fn();
  return t;
}

const text = (sel) => elFor(sel).text();
const dataStatus = () => ({ text: elFor('#data-status').textContent, cls: elFor('#data-status').className });

async function main() {
  elFor('#conn-banner').hidden = true;
  assert.ok(typeof domReady === 'function', 'DOMContentLoaded handler registered');
  domReady();
  await settle();

  /* --- initial state --- */
  assert.equal(FakeWebSocket.instances.length, 1, 'first connect attempted');
  assert.equal(
    FakeWebSocket.instances[0].url,
    'ws://localhost:8000/ws/dashboard?apiKey=sekret',
    'WS URL carries apiKey query param',
  );
  assert.equal(elFor('#conn-banner').hidden, true, 'no banner before first failure');
  assert.equal(dataStatus().text, 'DATA LIVE', 'initial REST data is live');

  const sock = FakeWebSocket.instances[0];

  /* --- connected: banner stays hidden, re-sync happens --- */
  sock.trigger('open');
  assert.equal(dataStatus().text, 'DATA LIVE', 'connected -> DATA LIVE');
  await settle();

  /* --- per-event partial rendering --- */
  send(sock, 'MARKET_UPDATE', { items: [{ market_id: 'm1', question: 'Will Q1 happen?', status: 'active', liquidity: 100, updated_at: '2026-08-11T00:00:00Z' }], total: 1 });
  assert.ok(text('#markets-table').includes('Will Q1 happen?'), 'MARKET_UPDATE renders markets table');
  assert.ok(!text('#signals-table').includes('Will Q1 happen?'), 'MARKET_UPDATE does not touch signals');

  send(sock, 'SIGNAL_UPDATE', { items: [{ signal_id: 'sig_900', market_id: 'm1', strategy: 'micro', side: 'YES', decision: 'CANDIDATE', confidence: 0.9, net_edge: 0.05, timestamp: '2026-08-11T00:00:00Z' }], total: 1 });
  assert.ok(text('#signals-table').includes('sig_900'), 'SIGNAL_UPDATE renders signals table');
  assert.ok(text('#signals-table').includes('CANDIDATE'), 'SIGNAL_UPDATE badge text rendered');

  send(sock, 'POSITION_UPDATE', { items: [{ position_id: 'pos_1', market_id: 'm1', side: 'YES', size: 10, average_entry: 0.5, current_price: 0.55, unrealised_pnl: 0.5, realised_pnl: 1.5 }], total: 1 });
  assert.ok(text('#positions-table').includes('pos_1'), 'POSITION_UPDATE renders positions table');

  send(sock, 'ORDER_UPDATE', { items: [{ order_id: 'ord_1', market_id: 'm1', side: 'YES', average_fill: 0.45, requested_price: 0.5, requested_size: 10, filled_size: 10, status: 'FILLED', submitted_at: '2026-08-11T00:00:00Z' }], total: 1 });
  assert.ok(text('#orders-table').includes('ord_1'), 'ORDER_UPDATE renders orders table');

  send(sock, 'P&L_UPDATE', { account_balance: 2000, available_balance: 1500, today_pnl: 12.34, total_pnl: 60, realized_pnl: 45, unrealized_pnl: 15, max_drawdown: 0.02, total_exposure: 300, open_positions: 3, active_signals: 4, bot_mode: 'PAPER', circuit_breaker: null });
  assert.ok(text('#overview-cards').includes('+$12.34'), 'P&L_UPDATE renders overview cards');

  send(sock, 'RISK_UPDATE', { daily_loss: 10, daily_loss_limit: 100, exposure: 50, exposure_limit: 500, consecutive_losses: 1, consecutive_loss_limit: 3, spread_status: 'OK', liquidity_status: 'OK', data_freshness: 'FRESH', circuit_breaker: null });
  assert.ok(text('#risk-tiles').includes('Daily Loss'), 'RISK_UPDATE renders risk tiles');

  send(sock, 'HEALTH_UPDATE', { healthy: true, checks: { database: { healthy: true }, api: { healthy: false }, data_freshness: { healthy: true }, model_availability: { healthy: true } }, timestamp: '2026-08-11T00:00:00Z' });
  assert.ok(text('#system-tiles').includes('ERROR'), 'HEALTH_UPDATE renders system tiles (api unhealthy)');

  send(sock, 'CIRCUIT_BREAKER', { state: 'HALTED', reasons: ['DAILY_LOSS'], triggered_at: '2026-08-11T00:00:00Z' });
  assert.ok(text('#risk-tiles').includes('HALTED'), 'CIRCUIT_BREAKER reflects on risk tiles');
  assert.equal(elFor('#mode-label').textContent, 'SYSTEM HALTED', 'CIRCUIT_BREAKER halts mode badge');
  assert.ok(elFor('#mode-badge').classes.has('mode-halted'), 'mode badge carries halted class');

  /* --- heartbeats are ignored --- */
  send(sock, 'PING', null);
  send(sock, 'CONNECTED', null);

  /* --- disconnect -> LIVE CONNECTION LOST + DATA STALE + backoff --- */
  sock.trigger('close');
  assert.equal(dataStatus().text, 'DATA STALE', 'repeated failure -> DATA STALE');
  assert.ok(dataStatus().cls.includes('offline'), 'DATA STALE uses offline styling');
  assert.equal(elFor('#conn-banner').hidden, false, 'LIVE CONNECTION LOST banner visible');

  // Verify the backoff sequence 1s -> 2s -> 5s -> 10s -> 30s -> 30s.
  const expected = [1000, 2000, 5000, 10000, 30000, 30000];
  expected.forEach((delay, i) => {
    const n = FakeWebSocket.instances.length;
    fireTimer((x) => x.delay === delay); // fires connectWS -> new instance
    assert.equal(FakeWebSocket.instances.length, n + 1, `reconnect attempt ${i + 1} creates a socket`);
    FakeWebSocket.instances[FakeWebSocket.instances.length - 1].trigger('close');
  });
  assert.ok(
    timers.some((x) => !x.cleared && x.delay === 30000),
    'backoff caps at 30s',
  );

  // Banner persists while retrying.
  assert.equal(elFor('#conn-banner').hidden, false, 'banner stays while reconnecting');

  /* --- reconnect succeeds: banner clears, re-sync, live again --- */
  const attemptsBeforeReconnect = FakeWebSocket.instances.length;
  fireTimer((x) => x.delay === 30000); // fires connectWS -> new instance
  const newSock = FakeWebSocket.instances[attemptsBeforeReconnect];
  assert.ok(newSock, 'reconnect attempt creates a socket');
  assert.equal(elFor('#conn-banner').hidden, false, 'banner still visible during connecting');
  newSock.trigger('open');
  await settle();
  assert.equal(elFor('#conn-banner').hidden, true, 'banner clears after reconnect');
  assert.equal(dataStatus().text, 'DATA LIVE', 'DATA LIVE after reconnect');

  console.log('dashboard.js WebSocket harness: ALL ASSERTIONS PASSED');
}

main()
  .then(() => process.exit(0))
  .catch((err) => {
    console.error('HARNESS FAILURE:', err.message);
    process.exit(1);
  });
