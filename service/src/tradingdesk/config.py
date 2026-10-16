"""Configuración del servicio local.

Las órdenes solo pueden ir a la cuenta Paper. La fuente de datos puede ser
simulada (``mock``) o IBKR (``ibkr``). Con IBKR, ``ib_account`` elige la cuenta:

- ``paper`` (por defecto): cuenta Paper, con envío de órdenes limitadas si está habilitado.
- ``real-readonly``: cuenta REAL en solo lectura. Saldo, posiciones, cotizaciones y
  velas para Kronos; el envío de órdenes queda desactivado sin excepción.
- ``real-trading``: cuenta REAL con órdenes limitadas, cada una confirmada por el
  usuario en la app y con un tope por orden en euros (``TRADINGDESK_MAX_REAL_ORDER_EUR``,
  como máximo ``REAL_ORDER_CEILING_EUR``, el importe que el usuario autorizó por escrito).

La cuenta se elige con ``TRADINGDESK_IB_ACCOUNT`` o con el archivo ``ib-account`` de la
carpeta de datos.
"""

from __future__ import annotations

import ipaddress
import re
import os
import platform
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

ALLOWED_MODES = frozenset({"paper"})
ALLOWED_DATA_SOURCES = frozenset({"mock", "ibkr"})
ALLOWED_IB_ACCOUNTS = frozenset({"paper", "real-readonly", "real-trading"})
REAL_ACCOUNTS = frozenset({"real-readonly", "real-trading"})
# Tope máximo por orden real que el usuario autorizó por escrito (16-10-2026). Subirlo exige cambiar el código.
REAL_ORDER_CEILING_EUR = Decimal("1000")
IB_ACCOUNT_FILE = "ib-account"
DEFAULT_PORT = 8765
# Puertos por defecto de IB Gateway / TWS. Los de cuenta real se rechazan.
IB_PAPER_GATEWAY_PORT = 4002
IB_LIVE_GATEWAY_PORT = 4001
IB_LIVE_PORTS = frozenset({4001, 7496})


class ConfigError(ValueError):
    """Configuración inválida o insegura."""


def default_data_dir() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "AITradingDesk"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ai-trading-desk"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    mode: str = "paper"
    data_source: str = "mock"
    log_level: str = "INFO"
    ib_host: str = "127.0.0.1"
    ib_port: int = IB_PAPER_GATEWAY_PORT
    ib_client_id: int = 17
    ib_account: str = "paper"
    max_real_order_eur: Decimal = REAL_ORDER_CEILING_EUR
    # Envío de órdenes limitadas a la cuenta Paper (solo con data_source="ibkr").
    paper_orders: bool = True
    max_order_value: Decimal = Decimal("1000")
    # Análisis con Kronos (solo con data_source="ibkr").
    watchlist: tuple[str, ...] = ("AAPL", "MSFT", "NVDA", "KO", "SPY")
    signal_interval_s: int = 300

    @property
    def real_account(self) -> bool:
        """Cuenta REAL de IBKR (en solo lectura o con órdenes)."""
        return self.data_source == "ibkr" and self.ib_account in REAL_ACCOUNTS

    @property
    def real_trading(self) -> bool:
        """Cuenta REAL con envío de órdenes limitadas confirmadas una a una."""
        return self.data_source == "ibkr" and self.ib_account == "real-trading"

    @property
    def alpaca_keys_path(self) -> Path:
        return self.data_dir / "alpaca-keys.json"

    @property
    def kronos_dir(self) -> Path:
        return self.data_dir / "kronos"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tradingdesk.sqlite3"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "api-token"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def validate(self) -> "Settings":
        if self.mode not in ALLOWED_MODES:
            raise ConfigError(
                f"Modo '{self.mode}' no permitido. Hito A solo admite Paper; "
                "el modo real requiere un proceso de habilitación que aún no existe."
            )
        if self.data_source not in ALLOWED_DATA_SOURCES:
            raise ConfigError(f"Fuente de datos '{self.data_source}' no disponible (solo 'mock' o 'ibkr').")
        if self.ib_account not in ALLOWED_IB_ACCOUNTS:
            raise ConfigError(f"Cuenta IBKR '{self.ib_account}' no válida "
                              "(solo 'paper', 'real-readonly' o 'real-trading').")
        if self.ib_account in REAL_ACCOUNTS and self.paper_orders:
            raise ConfigError("Con la cuenta real no hay órdenes Paper: TRADINGDESK_PAPER_ORDERS debe estar desactivado.")
        if not (Decimal("0") < self.max_real_order_eur <= REAL_ORDER_CEILING_EUR):
            raise ConfigError(f"TRADINGDESK_MAX_REAL_ORDER_EUR debe estar entre 0 y {REAL_ORDER_CEILING_EUR} €.")
        if self.ib_account == "paper" and self.ib_port in IB_LIVE_PORTS:
            raise ConfigError(f"El puerto {self.ib_port} es el de cuenta real de IBKR. Usa el de Paper "
                              f"({IB_PAPER_GATEWAY_PORT} en IB Gateway o 7497 en TWS).")
        if not (Decimal("0") < self.max_order_value <= Decimal("100000")):
            raise ConfigError("TRADINGDESK_MAX_ORDER_VALUE debe estar entre 0 y 100000.")
        if not (1 <= len(self.watchlist) <= 10) or not all(re.fullmatch(r"[A-Z.]{1,12}", s) for s in self.watchlist):
            raise ConfigError("TRADINGDESK_WATCHLIST debe tener entre 1 y 10 símbolos, p. ej. AAPL,MSFT.")
        if self.signal_interval_s < 60:
            raise ConfigError("TRADINGDESK_SIGNAL_INTERVAL debe ser de al menos 60 segundos.")
        if not _is_loopback(self.ib_host):
            raise ConfigError(f"IB Gateway debe estar en este Mac; '{self.ib_host}' no es una dirección local.")
        if not _is_loopback(self.host):
            raise ConfigError(f"El servicio solo escucha en localhost; '{self.host}' no es una dirección local.")
        if not (1024 <= self.port <= 65535):
            raise ConfigError(f"Puerto fuera de rango: {self.port}")
        return self

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        def number(name: str, default: int) -> int:
            try:
                return int(env.get(name, default))
            except ValueError as exc:
                raise ConfigError(f"{name} debe ser un número") from exc

        data_dir = Path(env.get("TRADINGDESK_DATA_DIR") or default_data_dir()).expanduser()
        ib_account = (env.get("TRADINGDESK_IB_ACCOUNT") or _read_account_file(data_dir) or "paper").strip().lower()
        real = ib_account in REAL_ACCOUNTS
        return cls(
            data_dir=data_dir,
            host=env.get("TRADINGDESK_HOST", "127.0.0.1"),
            port=number("TRADINGDESK_PORT", DEFAULT_PORT),
            mode=env.get("TRADINGDESK_MODE", "paper").lower(),
            data_source=env.get("TRADINGDESK_DATA_SOURCE", "mock").lower(),
            log_level=env.get("TRADINGDESK_LOG_LEVEL", "INFO").upper(),
            ib_host=env.get("TRADINGDESK_IB_HOST", "127.0.0.1"),
            ib_port=number("TRADINGDESK_IB_PORT", IB_LIVE_GATEWAY_PORT if real else IB_PAPER_GATEWAY_PORT),
            ib_client_id=number("TRADINGDESK_IB_CLIENT_ID", 17),
            ib_account=ib_account,
            max_real_order_eur=_decimal(env.get("TRADINGDESK_MAX_REAL_ORDER_EUR", str(REAL_ORDER_CEILING_EUR))),
            # Con la cuenta real no hay envío de órdenes, se pida lo que se pida.
            paper_orders=not real and env.get("TRADINGDESK_PAPER_ORDERS", "1").lower() not in ("0", "false", "no"),
            max_order_value=_decimal(env.get("TRADINGDESK_MAX_ORDER_VALUE", "1000")),
            watchlist=tuple(s.strip().upper() for s in env.get("TRADINGDESK_WATCHLIST", "AAPL,MSFT,NVDA,KO,SPY")
                            .split(",") if s.strip()),
            signal_interval_s=number("TRADINGDESK_SIGNAL_INTERVAL", 300),
        ).validate()


def _read_account_file(data_dir: Path) -> str | None:
    try:
        return (data_dir / IB_ACCOUNT_FILE).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ConfigError(f"Importe no válido: {text}") from exc


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"
