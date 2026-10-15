"""Conexión de SOLO LECTURA con IB Gateway (cuenta Paper).

Hito B, primera parte: se leen saldo, posiciones y cotizaciones de la cuenta
Paper a través de la TWS API oficial (biblioteca ``ib_async``). Este módulo no
contiene ninguna llamada capaz de crear, modificar o cancelar órdenes, y la
conexión se abre con ``readonly=True``. Si la cuenta conectada no es Paper
(los identificadores Paper empiezan por ``DU``), se desconecta y no reintenta.

La API de IB es asíncrona; se ejecuta en un hilo propio con su bucle de eventos
y publica instantáneas inmutables que la API HTTP lee sin bloquear.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable, Literal, Protocol

from .domain import AccountSnapshot, Freshness, FxRate, Money, Position
from .logging_setup import log_event

log = logging.getLogger("tradingdesk.broker")
# ib_async registra cada intento fallido como ERROR; ya lo resume nuestro aviso de reconexión.
logging.getLogger("ib_async.client").setLevel(logging.CRITICAL)

CENT = Decimal("0.01")
REFRESH_SECONDS = 5
RECONNECT_SECONDS = (2, 5, 10, 30)
# Tipos de datos de mercado de la TWS API: 1 tiempo real, 2 congelados, 3 diferidos, 4 diferidos congelados.
_FRESHNESS_BY_TYPE = {1: Freshness.REAL_TIME, 2: Freshness.REAL_TIME, 3: Freshness.DELAYED, 4: Freshness.DELAYED}

BrokerLink = Literal["conectado", "desconectado", "reconectando"]


class NotPaperAccountError(RuntimeError):
    """La sesión de IB Gateway no es de una cuenta Paper."""


def is_paper_account(account_id: str) -> bool:
    return account_id.upper().startswith("DU")


def mask_account(account_id: str) -> str:
    if len(account_id) <= 5:
        return "*" * len(account_id)
    prefix = "DU" if account_id.upper().startswith("DU") else account_id[:1]
    return prefix + "*" * (len(account_id) - len(prefix) - 3) + account_id[-3:]


def to_decimal(value: object) -> Decimal | None:
    """Convierte números de IB (float o texto) a Decimal; ``None`` si falta o no es finito."""
    if value is None or value == "":
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Quote:
    last: Decimal | None
    close: Decimal | None
    freshness: Freshness


@dataclass(frozen=True)
class BrokerSnapshot:
    link: BrokerLink
    detail: str
    account: AccountSnapshot | None = None
    positions: list[Position] = field(default_factory=list)
    quotes: Freshness = Freshness.UNAVAILABLE
    updated_at: datetime | None = None


class BrokerAdapter(Protocol):
    def snapshot(self) -> BrokerSnapshot: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


# --- Conversión de datos de IB a los contratos del PRD ------------------------------------------


def build_account(values: Iterable, *, account_id: str, daily_pnl: Decimal | None, quotes: Freshness,
                  now: datetime) -> AccountSnapshot:
    """Construye ``AccountSnapshot`` a partir de ``AccountValue`` (account, tag, value, currency)."""
    base: dict[str, Decimal] = {}
    base_currency = None
    cash_by_currency: dict[str, Decimal] = {}
    fx_to_base: dict[str, Decimal] = {}
    for v in values:
        if v.account != account_id:
            continue
        amount = to_decimal(v.value)
        if amount is None:
            continue
        if v.tag == "ExchangeRate" and v.currency not in ("", "BASE"):
            fx_to_base[v.currency] = amount
        elif v.tag == "CashBalance" and v.currency not in ("", "BASE"):
            cash_by_currency[v.currency] = amount
        elif v.tag == "NetLiquidation" and v.currency not in ("", "BASE"):
            base_currency = v.currency
            base[v.tag] = amount
        elif v.tag in {"AvailableFunds", "BuyingPower", "UnrealizedPnL", "RealizedPnL", "GrossPositionValue",
                       "TotalCashValue"} and v.currency not in ("BASE",):
            base[v.tag] = amount
    if base_currency is None or "NetLiquidation" not in base:
        raise ValueError("IB Gateway aún no ha enviado el valor liquidativo de la cuenta")

    def money(tag: str) -> Money:
        return Money(amount=_q(base.get(tag, Decimal("0"))), currency=base_currency)

    nlv = base["NetLiquidation"]
    fx: FxRate | None = None
    if base_currency == "EUR":
        nlv_eur = nlv
        usd_rate = fx_to_base.get("USD")  # EUR por USD
        if usd_rate:
            fx = FxRate(pair="EUR.USD", rate=(Decimal(1) / usd_rate).quantize(Decimal("0.0001")),
                        source="IBKR (tipo de la cuenta)", as_of=now)
    else:
        eur_rate = fx_to_base.get("EUR")  # moneda base por EUR
        nlv_eur = nlv / eur_rate if eur_rate else None
        if eur_rate:
            fx = FxRate(pair=f"EUR.{base_currency}", rate=eur_rate.quantize(Decimal("0.0001")),
                        source="IBKR (tipo de la cuenta)", as_of=now)
    daily = daily_pnl if daily_pnl is not None else Decimal("0")
    prev = nlv - daily
    exposure = base.get("GrossPositionValue", Decimal("0"))
    unrealized = base.get("UnrealizedPnL", Decimal("0"))
    return AccountSnapshot(
        account_id_masked=mask_account(account_id),
        mode="paper",
        timestamp=now,
        base_currency=base_currency,
        net_liquidation=Money(amount=_q(nlv), currency=base_currency),
        net_liquidation_eur=Money(amount=_q(nlv_eur) if nlv_eur is not None else _q(nlv),
                                  currency="EUR" if nlv_eur is not None else base_currency),
        fx=fx,
        settled_cash=[Money(amount=_q(a), currency=c) for c, a in sorted(cash_by_currency.items())]
        or [money("TotalCashValue")],
        available_funds=money("AvailableFunds"),
        buying_power=money("BuyingPower"),
        unrealized_pnl=money("UnrealizedPnL"),
        realized_pnl=money("RealizedPnL"),
        realized_fees=Money(amount=Decimal("0.00"), currency=base_currency),
        daily_pnl=Money(amount=_q(daily), currency=base_currency),
        daily_pnl_pct=_q(daily / prev * 100) if prev else Decimal("0.00"),
        daily_pnl_basis=("P&L diario de IBKR / (valor liquidativo − P&L diario). Incluye aportaciones o retiradas "
                         "del día si las hubo.") if daily_pnl is not None
        else "IBKR aún no ha enviado el P&L diario.",
        invested_capital=Money(amount=_q(exposure - unrealized), currency=base_currency),
        exposure=Money(amount=_q(exposure), currency=base_currency),
        risk_budget=Money(amount=Decimal("0.00"), currency=base_currency),
        freshness=quotes,
    )


def build_positions(items: Iterable, quotes: dict[int, Quote]) -> list[Position]:
    """Convierte ``PortfolioItem`` de IB en ``Position``. La variación diaria sale del cierre anterior."""
    result = []
    for item in items:
        qty = to_decimal(item.position)
        if not qty:
            continue
        c = item.contract
        quote = quotes.get(c.conId)
        last = (quote.last if quote and quote.last is not None else None) or to_decimal(item.marketPrice)
        avg = to_decimal(item.averageCost) or Decimal("0")
        mult = to_decimal(getattr(c, "multiplier", "") or None) or Decimal("1")
        if mult != 1:
            avg = avg / mult
        change = None
        if quote and quote.close and last is not None:
            change = _q((last - quote.close) / quote.close * 100)
        result.append(Position(
            symbol=c.localSymbol or c.symbol,
            name=getattr(c, "description", "") or c.symbol,
            quantity=qty,
            currency=c.currency,
            avg_price=_q(avg),
            last_price=_q(last) if last is not None else Decimal("0.00"),
            unrealized_pnl=Money(amount=_q(to_decimal(item.unrealizedPNL) or Decimal("0")), currency=c.currency),
            change_pct=change,
        ))
    return sorted(result, key=lambda p: p.symbol)


def overall_freshness(quotes: Iterable[Quote]) -> Freshness:
    values = [q.freshness for q in quotes]
    if not values:
        return Freshness.UNAVAILABLE
    if all(v is Freshness.REAL_TIME for v in values):
        return Freshness.REAL_TIME
    if any(v in (Freshness.REAL_TIME, Freshness.DELAYED) for v in values):
        return Freshness.DELAYED
    return Freshness.UNAVAILABLE


# --- Conexión ------------------------------------------------------------------------------------


class IbkrPaperBroker:
    """Mantiene una conexión de solo lectura con IB Gateway Paper en un hilo propio."""

    def __init__(self, host: str, port: int, client_id: int):
        self.host, self.port, self.client_id = host, port, client_id
        self._lock = threading.Lock()
        self._snapshot = BrokerSnapshot(link="desconectado", detail="Conectando con IB Gateway…")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def snapshot(self) -> BrokerSnapshot:
        with self._lock:
            return self._snapshot

    def _publish(self, snap: BrokerSnapshot) -> None:
        with self._lock:
            self._snapshot = snap

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="ibkr-paper", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        from ib_async import IB
        from ib_async.ib import StartupFetch

        attempt = 0
        while not self._stop.is_set():
            ib = IB()
            try:
                await ib.connectAsync(self.host, self.port, clientId=self.client_id, readonly=True, timeout=8,
                                      fetchFields=StartupFetch.POSITIONS | StartupFetch.ACCOUNT_UPDATES)
                accounts = ib.managedAccounts()
                if not accounts or not all(is_paper_account(a) for a in accounts):
                    raise NotPaperAccountError(
                        "La sesión de IB Gateway no es de una cuenta Paper. Conexión cerrada: esta versión solo "
                        "admite Paper. Inicia sesión en IB Gateway eligiendo «Paper Trading».")
                attempt = 0
                log_event(log, "Conectado a IB Gateway (solo lectura)", port=self.port, account=accounts[0])
                await self._poll(ib, accounts[0])
            except NotPaperAccountError as exc:
                log_event(log, "Cuenta no Paper: conexión rechazada", logging.ERROR)
                self._publish(BrokerSnapshot(link="desconectado", detail=str(exc)))
                ib.disconnect()
                return
            except Exception as exc:  # noqa: BLE001 - cualquier fallo de red se reintenta
                log_event(log, "Sin conexión con IB Gateway", logging.WARNING, error=str(exc), port=self.port)
                detail = (f"No se puede conectar con IB Gateway en {self.host}:{self.port}. Comprueba que está "
                          "abierto, con la sesión Paper iniciada y la API activada.")
                self._publish(BrokerSnapshot(link="reconectando" if attempt else "desconectado", detail=detail))
            finally:
                if ib.isConnected():
                    ib.disconnect()
            delay = RECONNECT_SECONDS[min(attempt, len(RECONNECT_SECONDS) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _poll(self, ib, account_id: str) -> None:
        # 3 = diferidos si no hay suscripción; IB devuelve tiempo real cuando la cuenta la tiene.
        ib.reqMarketDataType(3)
        pnl = ib.reqPnL(account_id)
        tickers: dict[int, object] = {}
        while ib.isConnected() and not self._stop.is_set():
            items = [i for i in ib.portfolio(account_id) if i.position]
            for item in items:
                con = item.contract
                if con.conId not in tickers:
                    if not con.exchange:
                        con.exchange = "SMART"
                    tickers[con.conId] = ib.reqMktData(con, "", False, False)
            quotes = {cid: Quote(last=to_decimal(t.marketPrice()), close=to_decimal(t.close),
                                 freshness=_FRESHNESS_BY_TYPE.get(t.marketDataType, Freshness.UNAVAILABLE))
                      for cid, t in tickers.items()}
            freshness = overall_freshness(quotes.values())
            now = datetime.now(timezone.utc)
            try:
                account = build_account(ib.accountValues(account_id), account_id=account_id,
                                        daily_pnl=to_decimal(pnl.dailyPnL), quotes=freshness, now=now)
            except ValueError as exc:
                self._publish(BrokerSnapshot(link="conectado", detail=str(exc), quotes=freshness, updated_at=now))
            else:
                self._publish(BrokerSnapshot(
                    link="conectado",
                    detail=f"IB Gateway Paper ({self.host}:{self.port}), conexión de solo lectura",
                    account=account,
                    positions=build_positions(items, quotes),
                    quotes=freshness,
                    updated_at=now,
                ))
            await asyncio.sleep(REFRESH_SECONDS)
        raise ConnectionError("IB Gateway cerró la conexión")
