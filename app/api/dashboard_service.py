"""Read-only aggregation helpers for the dashboard API.

All functions derive values from the persisted database and public
configuration.  No trading logic lives here: nothing in this module
submits orders, evaluates signals, or mutates state.  No secret is
ever read or returned.

Derived values
--------------
* account / available balance   — ``initial_equity + P&L`` (and minus
  notional exposure).
* today's P&L and streaks      — reconstructed from filled orders using
  the same per-order P&L convention as the orchestrator.
* equity curve / drawdown      — reconstructed from filled orders.
* spread / liquidity / freshness — derived from the latest snapshots and
  the markets table.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

from app.api.models import (
    CircuitBreakerInfo,
    DashboardOverviewResponse,
    DashboardRiskResponse,
    EquityPoint,
    HealthCheckDetail,
    HealthResponse,
    PerformanceBreakdownPoint,
    PerformanceChartPoint,
    PerformanceCharts,
    PerformanceResponse,
    PositionResponse,
)
from app.config.settings import settings
from app.modes.state import OperatingMode
from app.monitoring.health import checks as health_checks
from app.monitoring.health import health_status
from app.storage.db import Database
from app.storage.models import Market, Order, Position
from app.storage.repositories import (
    MarketRepository,
    OrderRepository,
    PositionRepository,
    SignalRepository,
    SnapshotRepository,
)

# ── Small helpers ───────────────────────────────────────────────────


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _operating_mode(raw: str) -> str:
    try:
        return OperatingMode(raw).value
    except ValueError:
        return OperatingMode.HALTED.value


async def _breaker_info(db: Database) -> CircuitBreakerInfo | None:
    """Read the persisted circuit breaker state, if any."""
    try:
        cursor = await db.conn.execute(
            "SELECT value FROM circuit_breaker_state WHERE key = ?",
            ("circuit_breaker",),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        data = json.loads(row["value"])
        return CircuitBreakerInfo(
            state=str(data.get("state", "NORMAL")),
            reasons=list(data.get("reasons", [])),
            triggered_at=data.get("triggered_at"),
        )
    except Exception:
        return None


async def circuit_breaker(db: Database) -> CircuitBreakerInfo | None:
    """Public wrapper around the persisted circuit breaker state."""
    return await _breaker_info(db)


async def build_health(db: Database) -> HealthResponse:
    """Run the dashboard health checks and assemble a ``HealthResponse``."""
    results: dict[str, bool] = {"database": await db.health()}
    for name in ("data_freshness", "api", "model_availability"):
        try:
            results[name] = await health_checks[name].check()
        except Exception:
            results[name] = False
    return HealthResponse(
        healthy=all(results.values()),
        checks={
            name: HealthCheckDetail(
                healthy=ok,
                last_updated=health_status.last_updated.get(name),
            )
            for name, ok in results.items()
        },
        timestamp=datetime.now(UTC).isoformat(),
    )


def _order_pnl(order: Order) -> float:
    """P&L contribution of a single filled order.

    Mirrors the orchestrator's per-order tracking convention
    (``app/orchestrator/engine.py``): a YES position pays 0.50 and a
    NO position pays 0.50, expressed as ``size × (0.50 - entry)``.
    """
    if order.filled_size is None or order.average_fill is None:
        return 0.0
    if order.filled_size <= 0:
        return 0.0
    entry = order.average_fill
    if order.side == "YES":
        return order.filled_size * (0.50 - entry)
    return order.filled_size * (entry - 0.50)


async def _notional_exposure(db: Database) -> float:
    """Sum of ``size × current_price`` across open positions (USDC)."""
    positions = await PositionRepository(db).list_open()
    return round(
        sum(
            float(p.size or 0.0) * float(p.current_price or 0.0)
            for p in positions
        ),
        6,
    )


def _start_of_today() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# ── Public aggregation functions ────────────────────────────────────


async def equity_curve(db: Database) -> list[EquityPoint]:
    """Reconstruct an equity curve from persisted order fills."""
    orders = await OrderRepository(db).list_filled()
    if not orders:
        return []
    ordered = sorted(
        orders,
        key=lambda o: o.submitted_at or o.completed_at or "",
    )
    equity = settings.initial_equity
    points: list[EquityPoint] = [
        EquityPoint(timestamp=datetime.now(UTC).isoformat(), equity=round(equity, 6))
    ]
    for order in ordered:
        equity += _order_pnl(order)
        ts = order.completed_at or order.submitted_at or datetime.now(UTC).isoformat()
        points.append(EquityPoint(timestamp=ts, equity=round(equity, 6)))
    return points


def max_drawdown(points: list[EquityPoint]) -> float:
    """Maximum peak-to-trough drawdown as a fraction of peak equity."""
    peak = -math.inf
    max_dd = 0.0
    for point in points:
        peak = max(peak, point.equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - point.equity) / peak)
    return round(max_dd, 6)


async def today_pnl(db: Database) -> float:
    """P&L contribution of orders filled since the start of today."""
    orders = await OrderRepository(db).list_filled()
    start = _start_of_today()
    total = 0.0
    for order in orders:
        ts = _parse_ts(order.submitted_at or order.completed_at)
        if ts is not None and ts >= start:
            total += _order_pnl(order)
    return round(total, 6)


async def consecutive_losses(db: Database) -> int:
    """Current losing streak across the full filled-order history."""
    orders = sorted(
        await OrderRepository(db).list_filled(),
        key=lambda o: o.submitted_at or o.completed_at or "",
    )
    streak = 0
    for order in orders:
        if _order_pnl(order) < 0:
            streak += 1
        else:
            streak = 0
    return streak


async def spread_status(db: Database) -> str:
    """``OK`` when every latest spread is within limit, else ``HIGH``."""
    rows = await SnapshotRepository(db).latest_spreads()
    if not rows:
        return "UNKNOWN"
    spreads: list[float] = []
    for row in rows:
        spread = row["spread"]
        if isinstance(spread, (int, float)):
            spreads.append(float(spread))
    if not spreads:
        return "UNKNOWN"
    if max(spreads) > settings.max_spread:
        return "HIGH"
    return "OK"


async def liquidity_status(db: Database) -> str:
    """``OK`` when every market clears minimum liquidity, else ``LOW``."""
    markets = await MarketRepository(db).list_all()
    if not markets:
        return "UNKNOWN"
    liquidities = [float(m.liquidity) for m in markets if m.liquidity is not None]
    if not liquidities:
        return "UNKNOWN"
    if min(liquidities) < settings.min_liquidity:
        return "LOW"
    return "OK"


async def data_freshness_status(db: Database) -> str:
    """``FRESH`` when the latest snapshot is within the max-age window."""
    latest = await SnapshotRepository(db).latest_timestamp()
    if latest is None:
        return "UNKNOWN"
    ts = _parse_ts(latest)
    if ts is None:
        return "UNKNOWN"
    age = (datetime.now(UTC) - ts).total_seconds()
    if age <= settings.data_max_age_seconds:
        return "FRESH"
    return "STALE"


# ── Position detail derivations (display only) ──────────────────────


def _position_risk_status(position: Position) -> str:
    """Classify a position's risk from its unrealised loss relative to
    the entry cost basis.

    ``NORMAL`` / ``WARNING`` / ``CRITICAL`` thresholds are display
    conventions only — they never gate or modify trading.
    """
    cost = float(position.size or 0.0) * float(position.average_entry or 0.0)
    unrealised = position.unrealised_pnl
    if unrealised is None or cost <= 0:
        return "NORMAL"
    loss_ratio = -float(unrealised) / cost
    if loss_ratio >= 0.5:
        return "CRITICAL"
    if loss_ratio >= 0.1:
        return "WARNING"
    return "NORMAL"


def _enrich_position(
    position: Position,
    market: Market | None,
    time_to_resolution: float | None,
) -> PositionResponse:
    """Project a persisted position into the enriched dashboard shape.

    Every derived number is computed here — the dashboard displays them
    verbatim and never re-derives P&L or sizing client-side.
    """
    size = float(position.size or 0.0)
    entry = float(position.average_entry or 0.0)
    price = float(position.current_price or 0.0)

    exposure = round(size * price, 6) if price > 0 else 0.0

    return_pct: float | None = None
    cost_basis = size * entry
    if cost_basis > 0 and position.unrealised_pnl is not None:
        return_pct = round(float(position.unrealised_pnl) / cost_basis, 6)

    return PositionResponse(
        position_id=position.position_id,
        market_id=position.market_id,
        side=position.side,
        size=position.size,
        average_entry=position.average_entry,
        current_price=position.current_price,
        realised_pnl=position.realised_pnl,
        unrealised_pnl=position.unrealised_pnl,
        exposure=exposure,
        return_pct=return_pct,
        time_to_resolution=time_to_resolution,
        risk_status=_position_risk_status(position),
    )


async def _time_to_resolution_for(
    db: Database, market: Market | None, market_id: str
) -> float | None:
    """Remaining seconds until the market resolves.

    Prefers the market's persisted ``resolution_time``; falls back to
    the latest snapshot's ``time_to_resolution`` when the market has no
    resolution timestamp.
    """
    if market is not None and market.resolution_time:
        resolved_at = _parse_ts(market.resolution_time)
        if resolved_at is not None:
            seconds = (resolved_at - datetime.now(UTC)).total_seconds()
            return round(seconds, 6)
    rows = await SnapshotRepository(db).list_by_market(market_id, limit=1)
    if rows and rows[0].time_to_resolution is not None:
        return round(float(rows[0].time_to_resolution), 6)
    return None


async def build_positions(
    db: Database,
    *,
    limit: int = 50,
    offset: int = 0,
    open_only: bool = True,
    market_id: str | None = None,
) -> tuple[list[PositionResponse], int]:
    """Return an enriched, paginated page of positions.

    ``market_id`` restricts to a single market (used by the position
    detail view to show full position history for one market).
    """
    repo = PositionRepository(db)
    items, total = await repo.list_paginated(
        limit=limit, offset=offset, open_only=open_only, market_id=market_id
    )
    if not items:
        return [], total

    markets = {
        m.market_id: m for m in await MarketRepository(db).list_all()
    }
    responses: list[PositionResponse] = []
    for position in items:
        market = markets.get(position.market_id)
        ttr = await _time_to_resolution_for(db, market, position.market_id)
        responses.append(_enrich_position(position, market, ttr))
    return responses, total


async def build_overview(db: Database) -> DashboardOverviewResponse:
    """Assemble the dashboard Overview payload."""
    position_repo = PositionRepository(db)
    pnl = await position_repo.pnl_summary()
    realized = pnl["total_realised_pnl"]
    unrealized = pnl["total_unrealised_pnl"]
    total_pnl_value = realized + unrealized
    account_balance = settings.initial_equity + total_pnl_value
    exposure = await _notional_exposure(db)
    points = await equity_curve(db)

    return DashboardOverviewResponse(
        account_balance=round(account_balance, 6),
        available_balance=round(account_balance - exposure, 6),
        today_pnl=await today_pnl(db),
        total_pnl=round(total_pnl_value, 6),
        realized_pnl=round(realized, 6),
        unrealized_pnl=round(unrealized, 6),
        max_drawdown=max_drawdown(points),
        total_exposure=exposure,
        open_positions=await position_repo.count(open_only=True),
        active_signals=await SignalRepository(db).count_candidates(),
        bot_mode=_operating_mode(settings.mode),
        circuit_breaker=await _breaker_info(db),
    )


async def build_risk(db: Database) -> DashboardRiskResponse:
    """Assemble the dashboard Risk payload.

    All values are read-only aggregates of persisted state and public
    configuration.  Nothing here gates or mutates trading — the
    circuit-breaker state is read, never written.
    """
    position_repo = PositionRepository(db)
    pnl = await position_repo.pnl_summary()
    account_balance = (
        settings.initial_equity + pnl["total_realised_pnl"] + pnl["total_unrealised_pnl"]
    )
    today = await today_pnl(db)
    exposure = await _notional_exposure(db)
    exposure_limit = round(account_balance * settings.max_total_exposure_pct, 6)
    open_positions = await position_repo.list_open()

    # Largest single position by notional (size × current price).
    largest_position = 0.0
    largest_position_market: str | None = None
    for p in open_positions:
        notional = float(p.size or 0.0) * float(p.current_price or 0.0)
        if notional > largest_position:
            largest_position = notional
            largest_position_market = p.market_id

    # Largest per-market exposure (notional, summed across sides).
    per_market: dict[str, float] = {}
    for p in open_positions:
        per_market[p.market_id] = per_market.get(p.market_id, 0.0) + (
            float(p.size or 0.0) * float(p.current_price or 0.0)
        )
    largest_market_exposure = max(per_market.values(), default=0.0)

    # Average spread across each market's latest snapshot.
    spreads: list[float] = []
    for row in await SnapshotRepository(db).latest_spreads():
        spread = row["spread"]
        if isinstance(spread, (int, float)):
            spreads.append(float(spread))
    average_spread = round(sum(spreads) / len(spreads), 6) if spreads else None

    # Minimum observed liquidity across known markets.
    markets = await MarketRepository(db).list_all()
    liquidities = [float(m.liquidity) for m in markets if m.liquidity is not None]
    minimum_liquidity = min(liquidities) if liquidities else None

    return DashboardRiskResponse(
        account_balance=round(account_balance, 6),
        available_balance=round(account_balance - exposure, 6),
        exposure=round(exposure, 6),
        exposure_pct=round(exposure / exposure_limit * 100, 4) if exposure_limit > 0 else 0.0,
        exposure_limit=exposure_limit,
        today_pnl=today,
        daily_loss=round(max(0.0, -today), 6),
        daily_loss_limit=round(account_balance * settings.max_daily_loss_pct, 6),
        consecutive_losses=await consecutive_losses(db),
        consecutive_loss_limit=settings.max_consecutive_losses,
        open_positions=len(open_positions),
        max_open_positions=settings.max_open_positions,
        largest_position=round(largest_position, 6),
        largest_position_market=largest_position_market,
        largest_market_exposure=round(largest_market_exposure, 6),
        average_spread=average_spread,
        minimum_liquidity=minimum_liquidity,
        spread_status=await spread_status(db),
        liquidity_status=await liquidity_status(db),
        data_freshness=await data_freshness_status(db),
        circuit_breaker=await _breaker_info(db),
    )


# ── Performance statistics (backend-computed, display only) ─────────


def _start_of_week(now: datetime) -> datetime:
    """Start of the current week (Monday 00:00 UTC)."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=now.weekday()
    )


def _start_of_month(now: datetime) -> datetime:
    """Start of the current calendar month (00:00 UTC)."""
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _parse_date_only(value: str | None, *, end_of_day: bool) -> datetime | None:
    """Parse a ``YYYY-MM-DD`` bound as a full UTC datetime."""
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    if end_of_day:
        return day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_window(
    from_date: str | None, to_date: str | None
) -> tuple[datetime | None, datetime | None]:
    """Normalise date-window bounds to inclusive UTC datetimes.

    Full ISO timestamps are kept as-is; bare ``YYYY-MM-DD`` values
    expand to the start of the day (``from``) or end of the day (``to``).
    """
    from_dt = _parse_ts(from_date) if from_date else None
    if from_date and (from_dt is None or from_dt.tzinfo is None):
        from_dt = _parse_date_only(from_date, end_of_day=False)
    to_dt = _parse_ts(to_date) if to_date else None
    if to_date and (to_dt is None or to_dt.tzinfo is None):
        to_dt = _parse_date_only(to_date, end_of_day=True)
    if from_dt is not None and to_dt is not None and to_dt < from_dt:
        from_dt, to_dt = to_dt, from_dt
    return from_dt, to_dt


def _order_time(order: Order) -> datetime | None:
    """Best-effort completion timestamp for an order."""
    return _parse_ts(order.completed_at or order.submitted_at)


def _within_window(order: Order, from_dt: datetime | None, to_dt: datetime | None) -> bool:
    """Whether an order falls inside an inclusive [from, to] window."""
    ts = _order_time(order)
    if ts is None:
        return from_dt is None and to_dt is None
    if from_dt is not None and ts < from_dt:
        return False
    if to_dt is not None and ts > to_dt:
        return False
    return True


_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Sports": (
        "sport", "match", "game", "champion", "tournament", "season", "score",
        "nba", "nfl", "mlb", "football", "basketball", "baseball", "hockey",
        "soccer", "tennis", "olympic", "race", "fight", "super bowl", "world cup",
    ),
    "Politics": (
        "president", "election", "congress", "senate", "house", "vote",
        "government", "senator", "primary", "debate", "campaign", "policy",
    ),
    "Economics": (
        "economy", "inflation", "gdp", "fed", "interest rate", "unemployment",
        "recession", "rate cut", "cpi", "stock market", "tariff",
    ),
    "Crypto": (
        "bitcoin", "btc", "ethereum", "eth", "crypto", "coin", "token",
        "solana", "defi", "blockchain", "etf",
    ),
    "Tech": (
        "ai", "tech", "apple", "google", "microsoft", "tesla", "nvidia",
        "software", "chip", "launch", "iphone",
    ),
    "Weather": (
        "weather", "temperature", "hurricane", "storm", "snow", "rain",
        "heat", "climate", "flood",
    ),
    "Entertainment": (
        "movie", "oscar", "album", "song", "award", "box office", "netflix",
        "grossing", "film",
    ),
}


def _market_category(question: str) -> str:
    """Classify a market question into a coarse display category.

    Keyword matching is deterministic and display-only; markets that
    match nothing are labelled ``Other``.
    """
    text = (question or "").lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category
    return "Other"


def _order_slippage(order: Order) -> float:
    """Slippage cost in USDC for a filled order.

    Adverse price movement is ``|average_fill - requested_price|``;
    multiplied by the filled size it gives the dollar cost of slippage.
    """
    if (
        order.average_fill is None
        or order.requested_price is None
        or order.filled_size is None
    ):
        return 0.0
    return abs(order.average_fill - order.requested_price) * float(order.filled_size)


def _holding_time(order: Order) -> float | None:
    """Seconds between submission and completion, if both timestamps exist."""
    submitted = _parse_ts(order.submitted_at)
    completed = _parse_ts(order.completed_at)
    if submitted is None or completed is None:
        return None
    seconds = (completed - submitted).total_seconds()
    return max(0.0, seconds)


async def _signal_index(db: Database) -> dict[str, list[tuple[str, str, float | None]]]:
    """Map each market to its signals: ``[(timestamp, strategy, net_edge)]``.

    Used to attribute each order's P&L to the strategy (and net edge)
    of the most recent signal for that market, matching the pipeline's
    signal-driven execution model.
    """
    signals = await SignalRepository(db).list_recent(limit=1000)
    index: dict[str, list[tuple[str, str, float | None]]] = {}
    for signal in signals:
        ts = signal.timestamp or ""
        index.setdefault(signal.market_id, []).append(
            (ts, signal.strategy, signal.net_edge)
        )
    for entries in index.values():
        entries.sort(key=lambda e: e[0])
    return index


def _attribute_signal(
    order: Order, index: dict[str, list[tuple[str, str, float | None]]]
) -> tuple[str, float | None]:
    """Attribute an order to its most recent prior signal for the market."""
    entries = index.get(order.market_id)
    if not entries:
        return "UNKNOWN", None
    order_ts = _order_time(order)
    order_key = order_ts.isoformat() if order_ts is not None else "\uffff"
    strategy: str = "UNKNOWN"
    edge: float | None = None
    for ts, candidate_strategy, candidate_edge in entries:
        if ts > order_key:
            break
        strategy = candidate_strategy
        edge = candidate_edge
    return strategy, edge


def _round_series(
    values: list[PerformanceChartPoint],
) -> list[PerformanceChartPoint]:
    return [
        PerformanceChartPoint(timestamp=p.timestamp, value=round(p.value, 6))
        for p in values
    ]


def _max_drawdown_from_points(points: list[PerformanceChartPoint]) -> float:
    """Maximum peak-to-trough drawdown as a fraction of peak equity."""
    peak = -math.inf
    max_dd = 0.0
    for point in points:
        peak = max(peak, point.value)
        if peak > 0:
            max_dd = max(max_dd, (peak - point.value) / peak)
    return round(max_dd, 6)


def _period_pnl(orders: list[Order], start: datetime) -> float:
    """P&L of orders whose completion time is on/after ``start``."""
    return round(
        sum(
            _order_pnl(order)
            for order in orders
            if (ts := _order_time(order)) is not None and ts >= start
        ),
        6,
    )


async def build_performance(
    db: Database,
    from_date: str | None = None,
    to_date: str | None = None,
) -> PerformanceResponse:
    """Assemble the Performance page payload.

    Every statistic and chart series is derived here from persisted
    order fills — the dashboard displays these numbers verbatim and
    never recomputes authoritative P&L client-side.
    """
    from_dt, to_dt = _parse_window(from_date, to_date)
    all_orders = sorted(
        await OrderRepository(db).list_filled(),
        key=lambda o: o.completed_at or o.submitted_at or "",
    )
    windowed = [o for o in all_orders if _within_window(o, from_dt, to_dt)]

    # ── Equity curve over the window (baseline = P&L before the window) ──
    baseline = settings.initial_equity
    if from_dt is not None:
        baseline += sum(
            _order_pnl(order)
            for order in all_orders
            if (ts := _order_time(order)) is not None and ts < from_dt
        )
    equity = baseline
    equity_points: list[PerformanceChartPoint] = []
    if windowed:
        first_ts = _order_time(windowed[0])
        equity_points.append(
            PerformanceChartPoint(
                timestamp=(from_dt or first_ts or datetime.now(UTC)).isoformat(),
                value=round(equity, 6),
            )
        )
        for order in windowed:
            equity += _order_pnl(order)
            equity_points.append(
                PerformanceChartPoint(
                    timestamp=(_order_time(order) or datetime.now(UTC)).isoformat(),
                    value=round(equity, 6),
                )
            )
    else:
        equity_points.append(
            PerformanceChartPoint(
                timestamp=(from_dt or datetime.now(UTC)).isoformat(),
                value=round(equity, 6),
            )
        )

    # ── Daily P&L, cumulative P&L, drawdown ─────────────────────────────
    daily_buckets: dict[str, float] = {}
    for order in windowed:
        ts = _order_time(order)
        if ts is None:
            continue
        day = ts.date().isoformat()
        daily_buckets[day] = daily_buckets.get(day, 0.0) + _order_pnl(order)
    daily_pnl = [
        PerformanceChartPoint(timestamp=day, value=round(value, 6))
        for day, value in sorted(daily_buckets.items())
    ]
    cumulative: list[PerformanceChartPoint] = []
    running = 0.0
    for point in daily_pnl:
        running += point.value
        cumulative.append(
            PerformanceChartPoint(timestamp=point.timestamp, value=round(running, 6))
        )

    peak = -math.inf
    drawdown: list[PerformanceChartPoint] = []
    for point in equity_points:
        peak = max(peak, point.value)
        if peak > 0:
            drawdown.append(
                PerformanceChartPoint(
                    timestamp=point.timestamp,
                    value=round((peak - point.value) / peak, 6),
                )
            )

    # ── Trade statistics ────────────────────────────────────────────────
    pnls = [_order_pnl(order) for order in windowed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    num_trades = len(pnls)
    win_loss_count = len(wins) + len(losses)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total_pnl = round(sum(pnls), 6)

    signal_index = await _signal_index(db)
    holdings = [h for h in (_holding_time(order) for order in windowed) if h is not None]
    edges: list[float] = []
    strategy_pnl: dict[str, float] = {}
    category_pnl: dict[str, float] = {}
    markets = {m.market_id: m for m in await MarketRepository(db).list_all()}
    for order in windowed:
        strategy, edge = _attribute_signal(order, signal_index)
        strategy_pnl[strategy] = strategy_pnl.get(strategy, 0.0) + _order_pnl(order)
        market = markets.get(order.market_id)
        category = _market_category(market.question) if market else "Other"
        category_pnl[category] = category_pnl.get(category, 0.0) + _order_pnl(order)
        if edge is not None:
            edges.append(float(edge))

    total_slippage = sum(_order_slippage(order) for order in windowed)

    def _mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 6) if values else None

    now = datetime.now(UTC)
    pnl_summary = await PositionRepository(db).pnl_summary()

    return PerformanceResponse(
        total_realised_pnl=pnl_summary["total_realised_pnl"],
        total_unrealised_pnl=pnl_summary["total_unrealised_pnl"],
        total_pnl=total_pnl,
        open_positions=await PositionRepository(db).count(open_only=True),
        total_markets=await MarketRepository(db).count(),
        total_signals=await SignalRepository(db).count(),
        total_orders=await OrderRepository(db).count(),
        filled_orders=await OrderRepository(db).count_filled(),
        timestamp=now.isoformat(),
        mode=_operating_mode(settings.mode),
        today_pnl=_period_pnl(windowed, _start_of_today()),
        week_pnl=_period_pnl(windowed, _start_of_week(now)),
        month_pnl=_period_pnl(windowed, _start_of_month(now)),
        return_pct=(
            round(total_pnl / settings.initial_equity, 6)
            if settings.initial_equity > 0
            else None
        ),
        max_drawdown=_max_drawdown_from_points(equity_points),
        win_rate=round(len(wins) / win_loss_count, 6) if win_loss_count > 0 else None,
        loss_rate=round(len(losses) / win_loss_count, 6)
        if win_loss_count > 0
        else None,
        profit_factor=round(gross_profit / gross_loss, 6) if gross_loss > 0 else None,
        expectancy=_mean(pnls),
        average_trade=_mean([abs(p) for p in pnls]),
        average_win=_mean(wins),
        average_loss=_mean(losses),
        number_of_trades=num_trades,
        average_holding_time=_mean(holdings),
        average_net_edge=_mean(edges),
        slippage=round(total_slippage, 6) if windowed else None,
        charts=PerformanceCharts(
            equity=_round_series(equity_points),
            daily_pnl=daily_pnl,
            cumulative_pnl=cumulative,
            drawdown=drawdown,
            by_strategy=[
                PerformanceBreakdownPoint(label=k, pnl=round(v, 6))
                for k, v in sorted(
                    strategy_pnl.items(), key=lambda item: item[1], reverse=True
                )
            ],
            by_category=[
                PerformanceBreakdownPoint(label=k, pnl=round(v, 6))
                for k, v in sorted(
                    category_pnl.items(), key=lambda item: item[1], reverse=True
                )
            ],
        ),
    )
