"""Máquina de estados de órdenes (PRD §7 OrderRecord) y su persistencia.

Las órdenes simuladas solo pasan por DRAFT/CANCELLED. Las órdenes Paper de IBKR
recorren la máquina completa; sus cambios tras el envío los dicta el bróker
(``paper_orders.sync_order``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from .db import Database
from .domain import Money, OrderEvent, OrderRecord, OrderSide, OrderState

S = OrderState

TERMINAL_STATES = frozenset({S.FILLED, S.CANCELLED, S.REJECTED})
OPEN_STATES = frozenset(set(S) - TERMINAL_STATES)

ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    S.DRAFT: frozenset({S.RISK_APPROVED, S.CANCELLED, S.REJECTED}),
    S.RISK_APPROVED: frozenset({S.SUBMITTING, S.CANCELLED}),
    S.SUBMITTING: frozenset({S.ACKNOWLEDGED, S.REJECTED, S.UNKNOWN}),
    S.ACKNOWLEDGED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED, S.UNKNOWN}),
    S.PARTIALLY_FILLED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.UNKNOWN}),
    # UNKNOWN solo sale por reconciliación con el bróker y nunca vuelve a SUBMITTING:
    # una orden en estado desconocido no se reenvía automáticamente.
    S.UNKNOWN: frozenset({S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED}),
    S.FILLED: frozenset(),
    S.CANCELLED: frozenset(),
    S.REJECTED: frozenset(),
}


class OrderError(Exception):
    pass


class InvalidTransition(OrderError):
    pass


class TradingBlocked(OrderError):
    pass


def check_transition(current: OrderState, target: OrderState, *, via_reconciliation: bool = False) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"Transición no permitida: {current.value} → {target.value}")
    if current is S.UNKNOWN and not via_reconciliation:
        raise InvalidTransition("Una orden UNKNOWN solo puede cambiar de estado mediante reconciliación con el bróker")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _row_to_order(row) -> OrderRecord:
    commission = None
    if row["commission_amount"] is not None:
        commission = Money(amount=Decimal(row["commission_amount"]), currency=row["commission_currency"])
    return OrderRecord(
        client_order_id=row["client_order_id"],
        mode=row["mode"],
        is_mock=bool(row["is_mock"]),
        symbol=row["symbol"],
        side=OrderSide(row["side"]),
        quantity=Decimal(row["quantity"]),
        limit_price=Decimal(row["limit_price"]),
        currency=row["currency"],
        state=OrderState(row["state"]),
        filled_quantity=Decimal(row["filled_quantity"]),
        avg_fill_price=_dec(row["avg_fill_price"]),
        commission=commission,
        ibkr_order_id=row["ibkr_order_id"],
        ibkr_perm_id=row["ibkr_perm_id"],
        note=row["note"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class OrderStore:
    def __init__(self, db: Database):
        self.db = db

    def get(self, client_order_id: str) -> OrderRecord | None:
        rows = self.db.query("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))
        return _row_to_order(rows[0]) if rows else None

    def list(self, *, mode: str, is_mock: bool | None = None, open_only: bool = False) -> list[OrderRecord]:
        sql = "SELECT * FROM orders WHERE mode = ?"
        params: list = [mode]
        if is_mock is not None:
            sql += " AND is_mock = ?"
            params.append(int(is_mock))
        if open_only:
            sql += f" AND state IN ({','.join('?' * len(OPEN_STATES))})"
            params.extend(s.value for s in OPEN_STATES)
        sql += " ORDER BY created_at DESC"
        return [_row_to_order(r) for r in self.db.query(sql, tuple(params))]

    def events(self, client_order_id: str) -> list[OrderEvent]:
        rows = self.db.query(
            "SELECT * FROM order_events WHERE client_order_id = ? ORDER BY id", (client_order_id,)
        )
        return [
            OrderEvent(
                client_order_id=r["client_order_id"],
                from_state=OrderState(r["from_state"]) if r["from_state"] else None,
                to_state=OrderState(r["to_state"]),
                reason=r["reason"],
                at=datetime.fromisoformat(r["at"]),
            )
            for r in rows
        ]

    def create_draft(
        self,
        *,
        client_order_id: str,
        mode: str,
        is_mock: bool,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        limit_price: Decimal,
        currency: str,
        note: str | None = None,
        created_at: datetime | None = None,
    ) -> tuple[OrderRecord, bool]:
        """Crea un DRAFT. Idempotente por ``client_order_id``: devuelve (orden, creada)."""
        if mode != "paper":
            raise TradingBlocked("Solo se admiten órdenes Paper en esta versión")
        at = (created_at or _now()).isoformat()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
            if existing is not None:
                order = _row_to_order(existing)
                same = (order.symbol, order.side, order.quantity, order.limit_price) == (
                    symbol, side, quantity, limit_price,
                )
                if not same:
                    raise OrderError("client_order_id ya usado con otros parámetros")
                return order, False
            conn.execute(
                """INSERT INTO orders (client_order_id, mode, is_mock, symbol, side, quantity, limit_price,
                       currency, state, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (client_order_id, mode, int(is_mock), symbol, side.value, str(quantity), str(limit_price),
                 currency, S.DRAFT.value, note, at, at),
            )
            conn.execute(
                "INSERT INTO order_events (client_order_id, from_state, to_state, reason, at) VALUES (?, NULL, ?, ?, ?)",
                (client_order_id, S.DRAFT.value, "Borrador creado", at),
            )
            _audit(conn, "order.draft_created", {"client_order_id": client_order_id, "symbol": symbol, "mock": is_mock})
        return self.get(client_order_id), True

    def transition(
        self,
        client_order_id: str,
        target: OrderState,
        reason: str,
        *,
        via_reconciliation: bool = False,
        at: datetime | None = None,
    ) -> OrderRecord:
        with self.db.transaction() as conn:
            _transition(conn, client_order_id, target, reason, via_reconciliation, (at or _now()).isoformat())
        return self.get(client_order_id)

    def apply_fill(
        self,
        client_order_id: str,
        filled_quantity: Decimal,
        avg_fill_price: Decimal,
        commission: Money | None,
        *,
        at: datetime | None = None,
    ) -> OrderRecord:
        """Registra un llenado acumulado y mueve a PARTIALLY_FILLED/FILLED."""
        order = self.get(client_order_id)
        if order is None:
            raise OrderError(f"Orden desconocida: {client_order_id}")
        if filled_quantity <= order.filled_quantity or filled_quantity > order.quantity:
            raise OrderError("Cantidad llenada acumulada inválida")
        target = S.FILLED if filled_quantity == order.quantity else S.PARTIALLY_FILLED
        reconciling = order.state is S.UNKNOWN
        check_transition(order.state, target, via_reconciliation=reconciling)
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE orders SET filled_quantity = ?, avg_fill_price = ?, commission_amount = ?,
                       commission_currency = ? WHERE client_order_id = ?""",
                (str(filled_quantity), str(avg_fill_price),
                 None if commission is None else str(commission.amount),
                 None if commission is None else commission.currency, client_order_id),
            )
            _transition(conn, client_order_id, target, f"Llenado {filled_quantity}/{order.quantity}",
                        reconciling, (at or _now()).isoformat())
        return self.get(client_order_id)

    def set_broker_ids(self, client_order_id: str, order_id: int | None, perm_id: int | None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE orders SET ibkr_order_id = COALESCE(?, ibkr_order_id),
                       ibkr_perm_id = COALESCE(?, ibkr_perm_id) WHERE client_order_id = ?""",
                (order_id, perm_id, client_order_id),
            )

    def set_commission(self, client_order_id: str, commission: Money) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE orders SET commission_amount = ?, commission_currency = ? WHERE client_order_id = ?",
                (str(commission.amount), commission.currency, client_order_id),
            )


def _transition(conn, client_order_id: str, target: OrderState, reason: str,
                via_reconciliation: bool, when: str) -> None:
    row = conn.execute("SELECT state FROM orders WHERE client_order_id = ?", (client_order_id,)).fetchone()
    if row is None:
        raise OrderError(f"Orden desconocida: {client_order_id}")
    current = OrderState(row["state"])
    check_transition(current, target, via_reconciliation=via_reconciliation)
    conn.execute(
        "UPDATE orders SET state = ?, updated_at = ? WHERE client_order_id = ?",
        (target.value, when, client_order_id),
    )
    conn.execute(
        "INSERT INTO order_events (client_order_id, from_state, to_state, reason, at) VALUES (?, ?, ?, ?, ?)",
        (client_order_id, current.value, target.value, reason, when),
    )
    _audit(conn, "order.transition", {"client_order_id": client_order_id, "from": current.value,
                                      "to": target.value, "reason": reason})

def _audit(conn, action: str, detail: dict, actor: str = "service") -> None:
    conn.execute(
        "INSERT INTO audit_log (at, actor, action, detail) VALUES (?, ?, ?, ?)",
        (_now().isoformat(), actor, action, json.dumps(detail, default=str, ensure_ascii=False)),
    )
