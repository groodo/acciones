"""Configuración del servicio local.

Solo se admite modo Paper. La fuente de datos puede ser simulada (``mock``) o la
cuenta Paper de IBKR en solo lectura (``ibkr``). Cualquier otro valor se rechaza
al arrancar: la habilitación live no existe todavía en el backend (PRD §14
SAFETY-02), así que no basta con editar la UI o un archivo.
"""

from __future__ import annotations

import ipaddress
import os
import platform
from dataclasses import dataclass
from pathlib import Path

ALLOWED_MODES = frozenset({"paper"})
ALLOWED_DATA_SOURCES = frozenset({"mock", "ibkr"})
DEFAULT_PORT = 8765
# Puertos por defecto de IB Gateway / TWS. Los de cuenta real se rechazan.
IB_PAPER_GATEWAY_PORT = 4002
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
        if self.ib_port in IB_LIVE_PORTS:
            raise ConfigError(f"El puerto {self.ib_port} es el de cuenta real de IBKR. Usa el de Paper "
                              f"({IB_PAPER_GATEWAY_PORT} en IB Gateway o 7497 en TWS).")
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

        return cls(
            data_dir=Path(env.get("TRADINGDESK_DATA_DIR") or default_data_dir()).expanduser(),
            host=env.get("TRADINGDESK_HOST", "127.0.0.1"),
            port=number("TRADINGDESK_PORT", DEFAULT_PORT),
            mode=env.get("TRADINGDESK_MODE", "paper").lower(),
            data_source=env.get("TRADINGDESK_DATA_SOURCE", "mock").lower(),
            log_level=env.get("TRADINGDESK_LOG_LEVEL", "INFO").upper(),
            ib_host=env.get("TRADINGDESK_IB_HOST", "127.0.0.1"),
            ib_port=number("TRADINGDESK_IB_PORT", IB_PAPER_GATEWAY_PORT),
            ib_client_id=number("TRADINGDESK_IB_CLIENT_ID", 17),
        ).validate()


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"
