"""
Interactive Brokers broker through the TWS API (paper or live).

The bot talks to a running TWS or IB Gateway session over a local socket,
using ib_async (the maintained Python client for the TWS API). Paper vs live
is decided by which account is logged in to TWS / the Gateway; IBKR_PAPER=true
makes the bot refuse to start on a live account.

Markets
-------
IBKR_MARKET=spot     -> PAXOS crypto (BTC/USD, ETH/USD, ...). Long only:
                        IBKR does not allow short crypto positions.
IBKR_MARKET=futures  -> CME micro futures (MBT = 0.1 BTC, MET = 0.1 ETH),
                        long and short. Quantities are rounded down to whole
                        contracts, entries go to the nearest contract that
                        expires more than IBKR_ROLL_DAYS away, and positions
                        are closed IBKR_ROLL_DAYS before their contract
                        expires. Signals use the continuous-future bars.

Protective exits
----------------
Entries go in as plain orders whose orderRef carries the stop and take-profit
("acx-e-<stop>-<target>-<id>", as on Alpaca). ensure_protection() then:
* futures: places an exchange-held STP (plus a take-profit LMT in the same
  OCA group) sized to the position, and re-sizes it as fills change;
* spot: IBKR crypto accepts only MKT and LMT orders, so the bot watches the
  quote and closes at market once the stop or target is reached. The stop
  is only as good as the bot's uptime and its check interval.

Setup
-----
1. Enable API access in TWS / IB Gateway (Configure > API > Settings), and
   add this machine's IP to Trusted IPs when the Gateway runs elsewhere.
2. Spot: request Cryptocurrency trading permission. Futures: request futures
   permission and a CME market-data subscription (or IBKR_MARKET_DATA_TYPE=3
   for delayed data, which is fine for testing but not for live stops).
   A paper account shares the live account's permissions and data.
3. Set BROKER=ibkr and the IBKR_* values in .env.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import queue
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
from ib_async import IB, ContFuture, Contract, Crypto, Future, LimitOrder, MarketOrder, StopOrder, Trade
from ib_async import Order as IBOrder
from ib_async.util import UNSET_DOUBLE
from loguru import logger

import config
from brokers.base import (
    BLOCKING_QUOTE,
    AccountInfo,
    BarCloseCallback,
    Broker,
    Order,
    OrderRequest,
    Position,
    StreamRunner,
    TradeUpdateCallback,
    spread_pct,
)
from brokers.order_tags import ENTRY_TAG, EXIT_TAG, entry_client_id, parse_entry_client_id
from brokers.polling_stream import PollingStreamRunner

_T = TypeVar("_T")

DEFAULT_FUTURES_PRODUCTS: dict[str, str] = {"BTC/USD": "MBT", "ETH/USD": "MET"}
_SPOT_EXCHANGE = "PAXOS"
_FUTURES_EXCHANGE = "CME"
CLOSE_TAG = "acx-c-"            # market orders placed by close_position()

_REQUEST_TIMEOUT = 30           # seconds for any TWS request
_HISTORY_TIMEOUT = 120          # historical data can be slow to arrive
_ORDER_ACK_SECONDS = 10         # wait this long for TWS to acknowledge an order
_QUOTE_WAIT_SECONDS = 5         # first quote after subscribing
_CLOSE_COOLDOWN_SECONDS = 30    # don't send a second close while the first settles
_QTY_TOLERANCE = 0.001          # re-place exits if position size drifts > 0.1 %
_ENTRIES_FILE = Path("logs") / "ibkr_entries.json"

# bot timeframe -> (IB bar size, seconds per bar, max days per request)
_BAR_SIZES: dict[str, tuple[str, int, int]] = {
    "1Min":  ("1 min", 60, 1),
    "5Min":  ("5 mins", 300, 7),
    "15Min": ("15 mins", 900, 14),
    "1Hour": ("1 hour", 3600, 30),
    "1Day":  ("1 day", 86400, 365),
}

_STATUSES: dict[str, str] = {
    "PendingSubmit": "pending_new",
    "ApiPending": "pending_new",
    "PreSubmitted": "new",
    "Submitted": "new",
    "PendingCancel": "pending_cancel",
    "Cancelled": "canceled",
    "ApiCancelled": "canceled",
    "Filled": "filled",
    "Inactive": "rejected",
}
_ORDER_TYPES = {"MKT": "market", "LMT": "limit", "STP": "stop", "STP LMT": "stop_limit"}

# TWS status messages that are not problems.
_INFO_CODES = {2100, 2104, 2106, 2107, 2108, 2119, 2150, 2158, 10167}
# Order log codes that do not mean the order was rejected.
_BENIGN_ORDER_CODES = {0, 202, 399}


def _num(value: Any) -> float:
    """IB marks unset numbers with UNSET_DOUBLE; treat those (and NaN/None) as 0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(number) or number >= UNSET_DOUBLE else number


def _parse_ib_time(text: str) -> datetime | None:
    """'20260929 14:03:11 America/New_York' (completed-order time) -> UTC datetime."""
    parts = (text or "").replace("-", " ").split()
    if len(parts) < 2:
        return None
    try:
        stamp = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y%m%d %H:%M:%S")
        tz = ZoneInfo(parts[2]) if len(parts) > 2 else timezone.utc
    except (ValueError, KeyError):
        return None
    return stamp.replace(tzinfo=tz).astimezone(timezone.utc)


def _expiry(contract: Contract) -> date | None:
    raw = (contract.lastTradeDateOrContractMonth or "")[:8]
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


def _duration(days: float) -> str:
    """TWS durationStr; anything over a year has to be given in years."""
    if days > 365:
        return f"{math.ceil(days / 365)} Y"
    return f"{max(1, math.ceil(days))} D"


def _parse_product_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        symbol, _, product = item.partition("=")
        if product:
            mapping[symbol.strip().upper()] = product.strip().upper()
    return mapping


@dataclass
class _Instrument:
    contract: Contract
    min_tick: float
    min_size: float             # in order units (coins, or contracts)
    size_increment: float
    multiplier: float           # coins per order unit (1 for spot)


class _LoopThread:
    """Runs ib_async's asyncio loop on its own thread so any bot thread can call in."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="ibkr-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro: Coroutine[Any, Any, _T], timeout: float) -> _T:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise TimeoutError(f"TWS did not answer within {timeout:.0f}s") from None


class IBKRBroker(Broker):
    name = "ibkr"

    def __init__(self) -> None:
        self.market = config.IBKR_MARKET
        if self.market not in ("spot", "futures"):
            raise ValueError("IBKR_MARKET must be 'spot' or 'futures'")
        self.futures_products = {
            **DEFAULT_FUTURES_PRODUCTS,
            **_parse_product_map(config.IBKR_FUTURES_PRODUCTS),
        }
        # ib_async logs every API error itself; we log them once through loguru.
        logging.getLogger("ib_async").setLevel(logging.CRITICAL)

        self._loop = _LoopThread()
        self._ib: IB = self._loop.run(self._connect(), _REQUEST_TIMEOUT)
        accounts = self._ib.managedAccounts()
        if config.IBKR_ACCOUNT and config.IBKR_ACCOUNT not in accounts:
            raise ValueError(f"IBKR_ACCOUNT {config.IBKR_ACCOUNT} is not one of {accounts}")
        self.account = config.IBKR_ACCOUNT or accounts[0]
        if config.IBKR_PAPER and not self.is_paper:
            self._ib.disconnect()
            raise ValueError(
                f"IBKR_PAPER=true but {self.account} is a live account. Log TWS / IB Gateway "
                "in to your paper account, or set IBKR_PAPER=false to trade live."
            )

        self._instruments: dict[str, _Instrument] = {}     # spot symbol or futures conId key
        self._fronts: dict[str, tuple[date, _Instrument]] = {}
        self._by_con_id: dict[int, Contract] = {}
        self._tickers: dict[int, Any] = {}
        self._fill_prices: dict[int, tuple[float, datetime]] = {}
        self._closing: dict[str, float] = {}
        self._protect_lock = threading.Lock()
        logger.info(
            f"IBKR broker initialised ({'PAPER' if self.is_paper else 'LIVE'} account {self.account}, "
            f"{self.market}) via {config.IBKR_HOST}:{config.IBKR_PORT}"
        )

    # ---- Connection -------------------------------------------------------------
    async def _connect(self) -> IB:
        ib = IB()
        ib.errorEvent += self._on_error
        ib.disconnectedEvent += lambda: logger.error("Disconnected from TWS / IB Gateway")
        await ib.connectAsync(
            config.IBKR_HOST,
            config.IBKR_PORT,
            clientId=config.IBKR_CLIENT_ID,
            timeout=15,
            account=config.IBKR_ACCOUNT,
        )
        ib.reqMarketDataType(config.IBKR_MARKET_DATA_TYPE)
        return ib

    @staticmethod
    def _on_error(req_id: int, code: int, message: str, contract: Contract | None) -> None:
        text = f"TWS {code}: {message}" + (f" ({contract.localSymbol or contract.symbol})" if contract else "")
        if code in _INFO_CODES:
            logger.debug(text)
        elif code == 1100:
            logger.error(text)
        elif code in (1101, 1102, 202):
            logger.info(text)
        else:
            logger.warning(text)

    @property
    def connected(self) -> bool:
        return self._ib.isConnected()

    def _call(self, fn: Callable[..., Any], *args: Any, timeout: float = _REQUEST_TIMEOUT) -> Any:
        """
        Run an ib_async method (or one of ours) on the loop thread and wait for it.

        Many ib_async "...Async" methods send the request as soon as they are
        called, and none of its state is thread-safe, so the call itself has
        to happen on the loop thread, not just the await.
        """
        if not self.connected:
            raise ConnectionError("Not connected to TWS / IB Gateway")

        async def call() -> Any:
            result = fn(*args)
            # Only await requests: eventkit Events are awaitable too (they wait for the next emit).
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                return await result
            return result

        return self._loop.run(call(), timeout)

    # ---- Capabilities -------------------------------------------------------
    @property
    def is_paper(self) -> bool:
        return self.account.startswith("D")     # paper accounts are DU... / DF...

    @property
    def supports_short(self) -> bool:
        return self.market == "futures"

    # ---- Instruments ------------------------------------------------------------
    def _spot(self, symbol: str) -> _Instrument:
        if symbol not in self._instruments:
            base, _, quote = symbol.partition("/")
            details = self._call(self._ib.reqContractDetailsAsync, Crypto(base, _SPOT_EXCHANGE, quote or "USD"))
            if not details:
                raise ValueError(f"{symbol} is not tradable on IBKR ({_SPOT_EXCHANGE})")
            d = details[0]
            self._instruments[symbol] = _Instrument(
                contract=d.contract,
                min_tick=_num(d.minTick) or 1e-8,
                min_size=_num(d.minSize),
                size_increment=_num(d.sizeIncrement) or 1e-8,
                multiplier=1.0,
            )
        return self._instruments[symbol]

    def _front(self, symbol: str) -> _Instrument:
        """Nearest futures contract that expires more than IBKR_ROLL_DAYS from today."""
        today = date.today()
        cached = self._fronts.get(symbol)
        if cached and cached[0] == today:
            return cached[1]
        product = self.futures_products.get(symbol)
        if not product:
            raise ValueError(f"No IBKR futures product configured for {symbol}")
        details = self._call(
            self._ib.reqContractDetailsAsync, Future(product, exchange=_FUTURES_EXCHANGE, currency="USD")
        )
        cutoff = today + timedelta(days=config.IBKR_ROLL_DAYS)
        live = [d for d in details if (_expiry(d.contract) or today) > cutoff]
        if not live:
            raise ValueError(f"{symbol}: no {product} futures contract found on {_FUTURES_EXCHANGE}")
        d = min(live, key=lambda d: _expiry(d.contract))
        instrument = _Instrument(
            contract=d.contract,
            min_tick=_num(d.minTick) or 0.01,
            min_size=1.0,
            size_increment=1.0,
            multiplier=float(d.contract.multiplier or 1),
        )
        self._fronts[symbol] = (today, instrument)
        return instrument

    def _trade_instrument(self, symbol: str) -> _Instrument:
        return self._front(symbol) if self.market == "futures" else self._spot(symbol)

    def _qualified(self, contract: Contract) -> Contract:
        """Positions arrive without an exchange; look the contract up by conId to route orders."""
        if contract.conId not in self._by_con_id:
            found = self._call(self._ib.qualifyContractsAsync, Contract(conId=contract.conId))
            if not found or found[0] is None:
                raise ValueError(f"Cannot resolve IBKR contract {contract.conId}")
            self._by_con_id[contract.conId] = found[0]
        return self._by_con_id[contract.conId]

    def _is_ours(self, contract: Contract) -> bool:
        """Only crypto (spot) or the configured micro futures; ignore stocks etc. in the account."""
        if self.market == "futures":
            return contract.secType == "FUT" and contract.symbol in self.futures_products.values()
        return contract.secType == "CRYPTO"

    def _bot_symbol(self, contract: Contract) -> str:
        if contract.secType == "FUT":
            for symbol, product in self.futures_products.items():
                if product == contract.symbol:
                    return symbol
            return contract.symbol
        return f"{contract.symbol}/{contract.currency or 'USD'}"

    @staticmethod
    def _multiplier(contract: Contract) -> float:
        return float(contract.multiplier or 1) if contract.secType == "FUT" else 1.0

    def _units(self, instrument: _Instrument, symbol: str, qty: float) -> float:
        """Bot quantity (coins) -> order units, rounded down to the allowed increment."""
        raw = Decimal(str(qty)) / Decimal(str(instrument.multiplier))
        step = Decimal(str(instrument.size_increment))
        units = float((raw / step).to_integral_value(rounding=ROUND_DOWN) * step)
        if units <= 0 or units < instrument.min_size:
            if self.market == "futures":
                raise ValueError(
                    f"{symbol}: {qty} is less than one {instrument.contract.symbol} contract "
                    f"({instrument.multiplier:g} coins); the account is too small for this size"
                )
            raise ValueError(f"{symbol}: qty {qty} is below IBKR's minimum order size {instrument.min_size}")
        return units

    @staticmethod
    def _price(instrument: _Instrument, price: float) -> float:
        tick = Decimal(str(instrument.min_tick))
        return float((Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick)

    def list_symbols(self) -> list[str]:
        if self.market == "futures":
            candidates = list(self.futures_products)
            resolve: Callable[[str], Any] = self._front
        else:
            candidates = [f"{coin}/USD" for coin in config.IBKR_CRYPTO_SYMBOLS]
            resolve = self._spot
        symbols = []
        for symbol in candidates:
            try:
                resolve(symbol)
                symbols.append(symbol)
            except Exception as exc:
                logger.debug(f"{symbol}: not available on IBKR ({exc})")
        return sorted(symbols)

    # ---- Account --------------------------------------------------------------
    def get_account(self) -> AccountInfo:
        values = self._call(self._ib.accountSummaryAsync, self.account)
        tags = {v.tag: _num(v.value) for v in values if v.currency in ("USD", "BASE", "")}
        cash = tags.get("TotalCashValue", 0.0)
        available = tags.get("AvailableFunds", cash)
        return AccountInfo(
            cash=cash,
            portfolio_value=tags.get("NetLiquidation", 0.0),
            # Crypto can't be bought on margin; futures margin is not used for sizing.
            buying_power=min(cash, available) if self.market == "spot" else available,
            status="active" if self.connected else "disconnected",
        )

    def _held(self) -> dict[str, list[tuple[Contract, float, float]]]:
        """symbol -> [(contract, signed order units, avg cost)] for our open positions."""
        held: dict[str, list[tuple[Contract, float, float]]] = {}
        for pos in self._call(self._ib.positions, self.account):
            if pos.position and self._is_ours(pos.contract):
                held.setdefault(self._bot_symbol(pos.contract), []).append(
                    (pos.contract, float(pos.position), float(pos.avgCost))
                )
        return held

    def get_positions(self) -> dict[str, Position]:
        portfolio = {item.contract.conId: item for item in self._call(self._ib.portfolio, self.account)}
        positions: dict[str, Position] = {}
        for symbol, entries in self._held().items():
            if len(entries) > 1:
                logger.warning(f"{symbol}: {len(entries)} contracts held at once; managing the first")
            contract, units, avg_cost = entries[0]
            mult = self._multiplier(contract)
            item = portfolio.get(contract.conId)
            positions[symbol] = Position(
                symbol=symbol,
                qty=abs(units) * mult,
                side="short" if units < 0 else "long",
                avg_entry=avg_cost / mult,       # futures avgCost includes the multiplier
                market_value=float(item.marketValue) if item else 0.0,
                unrealized_pl=float(item.unrealizedPNL) if item else 0.0,
            )
        return positions

    # ---- Orders -----------------------------------------------------------------
    def _order(self, trade: Trade) -> Order:
        o, status, contract = trade.order, trade.orderStatus, trade.contract
        mult = self._multiplier(contract)
        filled = _num(status.filled) or _num(o.filledQuantity)
        avg_price = _num(status.avgFillPrice) or None
        first_fill = None
        if o.permId in self._fill_prices:
            avg_price = avg_price or self._fill_prices[o.permId][0]
            first_fill = self._fill_prices[o.permId][1]
        submitted = trade.log[0].time if trade.log else None
        if not trade.isActive():
            # Orders loaded from history carry only their completion time.
            submitted = first_fill or _parse_ib_time(o.completedTime) or submitted
        return Order(
            id=str(o.permId),
            symbol=self._bot_symbol(contract),
            side="sell" if o.action.upper() == "SELL" else "buy",
            status=_STATUSES.get(status.status, status.status.lower()),
            qty=_num(o.totalQuantity) * mult,
            filled_qty=filled * mult,
            filled_avg_price=avg_price,
            order_type=_ORDER_TYPES.get(o.orderType, o.orderType.lower()),
            client_order_id=o.orderRef or "",
            submitted_at=submitted,
            raw=trade,
        )

    def _refresh_fill_prices(self) -> None:
        """Average fill price and first fill time per order; completed orders omit them."""
        totals: dict[int, list[Any]] = {}
        for fill in self._call(self._ib.reqExecutionsAsync):
            ex = fill.execution
            row = totals.setdefault(ex.permId, [0.0, 0.0, fill.time])
            row[0] += _num(ex.shares)
            row[1] += _num(ex.shares) * _num(ex.price)
            row[2] = min(row[2], fill.time)
        for perm_id, (qty, notional, first) in totals.items():
            if qty > 0:
                self._fill_prices[perm_id] = (notional / qty, first)

    def _open_trades(self) -> list[Trade]:
        trades = self._call(self._ib.reqOpenOrdersAsync)
        return [t for t in trades if t.order.permId and t.isActive() and self._is_ours(t.contract)]

    def get_open_orders(self, symbol: str | None = None, nested: bool = False) -> list[Order]:
        """nested=False: entry orders only. nested=True: also the managed exits."""
        orders = [self._order(t) for t in self._open_trades()]
        if symbol:
            orders = [o for o in orders if o.symbol == symbol]
        if not nested:
            orders = [o for o in orders if not o.client_order_id.startswith(EXIT_TAG)]
        return orders

    def get_closed_orders(self, limit: int = 500) -> list[Order]:
        self._refresh_fill_prices()
        trades = self._call(self._ib.reqCompletedOrdersAsync, False)
        orders = [self._order(t) for t in trades if t.order.permId and self._is_ours(t.contract)]
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        orders.sort(key=lambda o: o.submitted_at or epoch, reverse=True)
        return orders[:limit]

    async def _place_async(self, contract: Contract, order: IBOrder) -> Trade:
        trade = self._ib.placeOrder(contract, order)
        deadline = time.monotonic() + _ORDER_ACK_SECONDS
        while not trade.order.permId and not trade.isDone() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return trade

    def _place(self, contract: Contract, order: IBOrder) -> Order:
        order.account = self.account
        trade = self._call(self._place_async, contract, order, timeout=_ORDER_ACK_SECONDS + _REQUEST_TIMEOUT)
        problems = [e.message for e in trade.log if e.errorCode not in _BENIGN_ORDER_CODES and e.message]
        if trade.isDone() and not _num(trade.orderStatus.filled) and (
            problems or trade.orderStatus.status == "Inactive"
        ):
            raise RuntimeError(f"IBKR rejected the order: {'; '.join(problems) or trade.orderStatus.status}")
        if not trade.order.permId:
            raise RuntimeError(
                f"TWS did not acknowledge the order within {_ORDER_ACK_SECONDS}s; "
                "it may still be pending, check TWS"
            )
        return self._order(trade)

    def submit_order(self, request: OrderRequest) -> Order:
        """Submit a plain entry; ensure_protection() adds the exits once it fills."""
        instrument = self._trade_instrument(request.symbol)
        units = self._units(instrument, request.symbol, request.qty)
        action = "SELL" if request.side == "sell" else "BUY"
        ref = request.client_order_id or entry_client_id(request.stop_loss, request.take_profit)

        if request.order_type == "limit":
            if request.limit_price is None:
                raise ValueError("limit order requires limit_price")
            order = LimitOrder(action, units, self._price(instrument, request.limit_price), tif="GTC")
        elif self.market == "spot":
            # IBKR crypto market buys must be sized in USD (cashQty); a marketable
            # IOC limit keeps the coin quantity and caps slippage instead.
            quote = self.get_latest_quote(request.symbol)
            if quote["spread_pct"] >= BLOCKING_QUOTE["spread_pct"]:
                raise ValueError(f"{request.symbol}: no quote to price the market order")
            slip = config.MAX_SLIPPAGE_PCT
            price = quote["ask"] * (1 + slip) if action == "BUY" else quote["bid"] * (1 - slip)
            order = LimitOrder(action, units, self._price(instrument, price), tif="IOC")
        else:
            order = MarketOrder(action, units, tif="DAY")
        order.orderRef = ref
        order.outsideRth = True
        placed = self._place(instrument.contract, order)
        if ref.startswith(ENTRY_TAG):
            self._remember_entry(request.symbol, ref)
        return placed

    async def _cancel_async(self, perm_id: int) -> bool:
        trades = [t for t in self._ib.openTrades() if t.order.permId == perm_id]
        if not trades:
            await self._ib.reqOpenOrdersAsync()
            trades = [t for t in self._ib.openTrades() if t.order.permId == perm_id]
        if not trades:
            return False
        trade = trades[0]
        self._ib.cancelOrder(trade.order)
        deadline = time.monotonic() + _ORDER_ACK_SECONDS
        while not trade.isDone() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return True

    def cancel_order(self, order_id: str) -> None:
        """Cancel and wait for TWS to confirm, so a replacement exit never doubles up."""
        if not self._call(self._cancel_async, int(order_id), timeout=_ORDER_ACK_SECONDS + _REQUEST_TIMEOUT):
            raise ValueError(
                f"Open order {order_id} not found; only orders placed with "
                f"IBKR_CLIENT_ID={config.IBKR_CLIENT_ID} can be cancelled by the bot"
            )

    def close_position(self, symbol: str) -> Order:
        held = self._held().get(symbol)
        if not held:
            raise ValueError(f"No open {symbol} position")
        # A leftover futures stop would open a new position once it triggers.
        for order in self.get_open_orders(symbol, nested=True):
            if order.client_order_id.startswith(EXIT_TAG):
                self.cancel_order(order.id)
        result: Order | None = None
        for contract, units, _ in held:
            order = MarketOrder(
                "SELL" if units > 0 else "BUY",
                abs(units),
                tif="IOC" if self.market == "spot" else "DAY",
            )
            order.orderRef = f"{CLOSE_TAG}{uuid4().hex[:16]}"
            order.outsideRth = True
            result = self._place(self._qualified(contract), order)
        self._closing[symbol] = time.monotonic()
        assert result is not None
        return result

    # ---- Managed exits ------------------------------------------------------------
    @staticmethod
    def _load_entries() -> dict[str, str]:
        try:
            return json.loads(_ENTRIES_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _remember_entry(self, symbol: str, ref: str) -> None:
        """
        Keep the last entry tag per symbol on disk.

        TWS only returns recent completed orders, so after a restart the entry
        of a position held for days may be gone; this file still has its stop.
        """
        entries = self._load_entries()
        entries[symbol] = ref
        try:
            _ENTRIES_FILE.parent.mkdir(exist_ok=True)
            _ENTRIES_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning(f"Could not save {_ENTRIES_FILE}: {exc}")

    def _forget_entries(self, keep: set[str]) -> None:
        """Drop saved entries of symbols with no position and no working entry."""
        entries = self._load_entries()
        stale = set(entries) - keep
        if stale:
            for symbol in stale:
                del entries[symbol]
            try:
                _ENTRIES_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")
            except OSError as exc:
                logger.warning(f"Could not save {_ENTRIES_FILE}: {exc}")

    def _entry_targets(self, open_orders: list[Order]) -> dict[str, tuple[float | None, float | None]]:
        """symbol -> (stop, target) from the most recent (partly) filled bot entry."""
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        entries = [
            o for o in open_orders + self.get_closed_orders()
            if o.client_order_id.startswith(ENTRY_TAG) and o.filled_qty > 0
        ]
        entries.sort(key=lambda o: o.submitted_at or epoch, reverse=True)
        targets: dict[str, tuple[float | None, float | None]] = {
            symbol: parse_entry_client_id(ref) for symbol, ref in self._load_entries().items()
        }
        for order in reversed(entries):     # newest wins
            targets[order.symbol] = parse_entry_client_id(order.client_order_id)
        return targets

    def ensure_protection(self) -> None:
        with self._protect_lock:
            self._ensure_protection()

    def _ensure_protection(self) -> None:
        positions = self.get_positions()
        open_orders = self.get_open_orders(nested=True)
        exits: dict[str, list[Order]] = {}
        entries: dict[str, list[Order]] = {}
        for order in open_orders:
            if order.client_order_id.startswith(EXIT_TAG):
                exits.setdefault(order.symbol, []).append(order)
            elif order.client_order_id.startswith(ENTRY_TAG):
                entries.setdefault(order.symbol, []).append(order)

        # Exits left behind by a closed position must go: a futures stop would
        # otherwise open a fresh position when it triggers.
        for symbol, orders in exits.items():
            if symbol not in positions:
                for order in orders:
                    logger.info(f"{symbol}: Position closed; cancelling leftover exit {order.id}")
                    self.cancel_order(order.id)
        self._forget_entries(keep=set(positions) | set(entries))
        if not positions:
            return

        targets = self._entry_targets(open_orders)
        held = self._held()
        for symbol, position in positions.items():
            # One coin's failure must not leave the other positions unchecked.
            try:
                self._protect(
                    position,
                    held[symbol][0][0],
                    exits.get(symbol, []),
                    entries.get(symbol, []),
                    *targets.get(symbol, (None, None)),
                )
            except Exception as exc:
                logger.error(f"{symbol}: Protective-exit check failed: {exc}")

    def _protect(
        self,
        position: Position,
        contract: Contract,
        current: list[Order],
        entries: list[Order],
        stop: float | None,
        target: float | None,
    ) -> None:
        symbol = position.symbol
        if time.monotonic() - self._closing.get(symbol, 0.0) < _CLOSE_COOLDOWN_SECONDS:
            return

        if self.market == "futures":
            expiry = _expiry(contract)
            if expiry and expiry <= date.today() + timedelta(days=config.IBKR_ROLL_DAYS):
                logger.warning(f"{symbol}: {contract.localSymbol} expires {expiry}; closing before expiry")
                for order in entries:
                    self.cancel_order(order.id)
                self.close_position(symbol)
                return

        if stop is None:
            logger.debug(f"{symbol}: Position has no bot entry with a stop; leaving it unmanaged")
            return

        quote = self.get_latest_quote(symbol)
        is_long = position.side == "long"
        price = quote["bid"] if is_long else quote["ask"]
        target_hit = target is not None and price > 0 and (price >= target if is_long else price <= target)
        stop_breached = price > 0 and (price <= stop if is_long else price >= stop)

        # Spot crypto has no stop orders: the bot is the stop.
        if self.market == "spot" or (stop_breached and not current):
            if target_hit or stop_breached:
                reason = "take-profit reached" if target_hit else "stop reached"
                logger.warning(f"{symbol}: {reason} (price={price:.8f}); closing position")
                for order in entries:
                    self.cancel_order(order.id)
                self.close_position(symbol)
            return

        if current and all(
            abs(order.qty - position.qty) <= position.qty * _QTY_TOLERANCE for order in current
        ):
            return
        for order in current:       # wrong size (partial fill, added contracts): replace
            self.cancel_order(order.id)
        self._place_exits(position, self._qualified(contract), stop, target)

    def _place_exits(self, position: Position, contract: Contract, stop: float, target: float | None) -> None:
        """Exchange-held stop, plus a take-profit in the same OCA group when there is a target."""
        # Every month of a product has the same tick size, so the front month's will do.
        tick = self._front(position.symbol).min_tick
        instrument = _Instrument(contract, tick, 1.0, 1.0, self._multiplier(contract))
        units = round(position.qty / instrument.multiplier)
        action = "SELL" if position.side == "long" else "BUY"
        group = uuid4().hex[:16]
        orders: list[IBOrder] = [StopOrder(action, units, self._price(instrument, stop), tif="GTC")]
        if target is not None:
            orders.append(LimitOrder(action, units, self._price(instrument, target), tif="GTC"))
        for i, order in enumerate(orders):
            order.orderRef = f"{EXIT_TAG}{group}-{i}"
            order.ocaGroup = f"acx-oca-{group}"
            order.ocaType = 1       # a fill cancels the other exit
            order.outsideRth = True
            placed = self._place(contract, order)
            logger.success(
                f"{position.symbol}: Protective {placed.order_type} placed qty={units} "
                f"{contract.localSymbol} price={order.auxPrice if order.orderType == 'STP' else order.lmtPrice} "
                f"id={placed.id}"
            )

    # ---- Market data ----------------------------------------------------------------
    def _data_contract(self, symbol: str) -> tuple[Contract, str, bool]:
        """(contract, whatToShow, can page back with endDateTime) for bars."""
        if self.market == "spot":
            return self._spot(symbol).contract, "AGGTRADES", True
        key = f"cont:{symbol}"
        if key not in self._instruments:
            product = self.futures_products.get(symbol)
            if not product:
                raise ValueError(f"No IBKR futures product configured for {symbol}")
            found = self._call(
                self._ib.qualifyContractsAsync, ContFuture(product, exchange=_FUTURES_EXCHANGE, currency="USD")
            )
            if not found or found[0] is None:
                raise ValueError(f"{symbol}: cannot resolve continuous {product} future")
            self._instruments[key] = _Instrument(found[0], 0.0, 0.0, 0.0, 1.0)
        # Continuous futures only accept an empty endDateTime (i.e. "now").
        return self._instruments[key].contract, "TRADES", False

    async def _history_async(
        self, contract: Contract, what: str, can_page: bool, bar_size: str, max_days: int,
        end: datetime | None, days: float,
    ) -> list[Any]:
        bars: list[Any] = []
        cursor: datetime | str = end or ""
        remaining = days
        while remaining > 0:
            span = min(remaining, max_days) if can_page else remaining
            chunk = await self._ib.reqHistoricalDataAsync(
                contract,
                endDateTime=cursor,
                durationStr=_duration(span),
                barSizeSetting=bar_size,
                whatToShow=what,
                useRTH=False,
                formatDate=2,
                timeout=_HISTORY_TIMEOUT - 10,
            )
            if not chunk:
                break
            bars = list(chunk) + bars
            remaining -= span
            if not can_page:
                break
            first = chunk[0].date
            cursor = first if isinstance(first, datetime) else datetime.combine(first, datetime.min.time(), timezone.utc)
        return bars

    def _history(self, symbol: str, timeframe: str, end: datetime | None, days: float) -> pd.DataFrame:
        bar_size, _, max_days = _BAR_SIZES.get(timeframe, _BAR_SIZES["15Min"])
        contract, what, can_page = self._data_contract(symbol)
        pages = math.ceil(days / max_days) if can_page else 1
        bars = self._call(
            self._history_async, contract, what, can_page, bar_size, max_days, end, days,
            timeout=_HISTORY_TIMEOUT * max(1, pages),
        )
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(
            {
                "open": [_num(b.open) for b in bars],
                "high": [_num(b.high) for b in bars],
                "low": [_num(b.low) for b in bars],
                "close": [_num(b.close) for b in bars],
                "volume": [_num(b.volume) for b in bars],
                "trade_count": [int(_num(b.barCount)) for b in bars],
                "vwap": [_num(b.average) for b in bars],
            },
            index=pd.to_datetime([b.date for b in bars], utc=True),
        )
        typical = (df["high"] + df["low"] + df["close"]) / 3.0
        df["vwap"] = df["vwap"].where(df["vwap"] > 0, typical)
        df.index.name = "timestamp"
        return df[~df.index.duplicated(keep="last")].sort_index()

    def get_bars(self, symbol: str, lookback: int, timeframe: str) -> pd.DataFrame:
        seconds = _BAR_SIZES.get(timeframe, _BAR_SIZES["15Min"])[1]
        # 50 % extra for gaps; futures also pause daily and at weekends.
        days = lookback * seconds * 1.5 / 86400 + (3 if self.market == "futures" else 0)
        return self._history(symbol, timeframe, None, days).tail(lookback)

    def get_bars_history(
        self, symbol: str, start: datetime, end: datetime, timeframe: str
    ) -> pd.DataFrame:
        if self.market == "futures":
            # Continuous futures can only be read back from now.
            days = (datetime.now(timezone.utc) - start).total_seconds() / 86400
            df = self._history(symbol, timeframe, None, days)
        else:
            df = self._history(symbol, timeframe, end, (end - start).total_seconds() / 86400)
        return df[(df.index >= start) & (df.index <= end)] if not df.empty else df

    async def _quote_async(self, contract: Contract) -> tuple[float, float]:
        ticker = self._tickers.get(contract.conId)
        if ticker is None:
            ticker = self._tickers[contract.conId] = self._ib.reqMktData(contract)
        deadline = time.monotonic() + _QUOTE_WAIT_SECONDS
        while not (_num(ticker.bid) > 0 and _num(ticker.ask) > 0) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return _num(ticker.bid), _num(ticker.ask)

    def get_latest_quote(self, symbol: str) -> dict[str, float]:
        contract = self._trade_instrument(symbol).contract
        bid, ask = self._call(self._quote_async, contract, timeout=_QUOTE_WAIT_SECONDS + _REQUEST_TIMEOUT)
        if bid <= 0 or ask <= 0:
            logger.warning(f"{symbol}: No IBKR quote (check market-data permissions)")
            return dict(BLOCKING_QUOTE)
        return {"bid": bid, "ask": ask, "spread_pct": spread_pct(bid, ask)}

    # ---- Streaming ----------------------------------------------------------------------
    def create_stream(
        self,
        symbols: list[str],
        on_bar_close: BarCloseCallback,
        on_trade_update: TradeUpdateCallback,
    ) -> StreamRunner:
        return IBKRStreamRunner(self, symbols, on_bar_close, on_trade_update)


class IBKRStreamRunner(PollingStreamRunner):
    """
    Bar polling (as for Coinbase) plus push updates when one of the bot's
    orders fills, so exits go on right after an entry fills. A dropped TWS
    connection counts as a dead stream, so systemd restarts the bot.
    """

    def __init__(
        self,
        broker: IBKRBroker,
        symbols: list[str],
        on_bar_close: BarCloseCallback,
        on_trade_update: TradeUpdateCallback,
    ) -> None:
        super().__init__(broker, symbols, on_bar_close, on_trade_update)
        self.ibkr = broker
        self._fills: queue.Queue[int] = queue.Queue()
        self._fill_thread: threading.Thread | None = None

    def _on_exec(self, trade: Trade, fill: Any) -> None:
        self._fills.put(fill.execution.permId)

    def _run_fills(self) -> None:
        while not self._stop.is_set():
            try:
                perm_id = self._fills.get(timeout=1)
            except queue.Empty:
                continue
            time.sleep(1)           # let a burst of partial fills arrive, then sync once
            while not self._fills.empty():
                perm_id = self._fills.get_nowait()
            try:
                self.on_trade_update(SimpleNamespace(event="fill", order=SimpleNamespace(id=str(perm_id))))
            except Exception as exc:
                logger.error(f"Order update handling failed: {exc}")

    def start(self) -> None:
        self.ibkr._call(self.ibkr._ib.execDetailsEvent.connect, self._on_exec)
        self._fill_thread = threading.Thread(target=self._run_fills, name="ibkr-fills", daemon=True)
        self._fill_thread.start()
        super().start()

    def stop(self) -> None:
        super().stop()
        if self._fill_thread is not None:
            self._fill_thread.join(timeout=5)

    def dead_streams(self) -> list[str]:
        dead = super().dead_streams()
        if not self.ibkr.connected:
            dead.append("ibkr-connection")
        if self._fill_thread is not None and not self._fill_thread.is_alive():
            dead.append(self._fill_thread.name)
        return dead
