"""Configuración del servicio local.

Hito A solo admite modo Paper y fuente de datos simulada (MOCK). Cualquier otro
valor se rechaza al arrancar: la habilitación live no existe todavía en el
backend (PRD §14 SAFETY-02), así que no basta con editar la UI o un archivo.
"""

from __future__ import annotations

import ipaddress
import os
import platform
from dataclasses import dataclass
from pathlib import Path

ALLOWED_MODES = frozenset({"paper"})
ALLOWED_DATA_SOURCES = frozenset({"mock"})
DEFAULT_PORT = 8765


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
            raise ConfigError(f"Fuente de datos '{self.data_source}' no disponible en Hito A (solo 'mock').")
        try:
            is_loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            is_loopback = self.host == "localhost"
        if not is_loopback:
            raise ConfigError(f"El servicio solo escucha en localhost; '{self.host}' no es una dirección local.")
        if not (1024 <= self.port <= 65535):
            raise ConfigError(f"Puerto fuera de rango: {self.port}")
        return self

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        try:
            port = int(env.get("TRADINGDESK_PORT", DEFAULT_PORT))
        except ValueError as exc:
            raise ConfigError("TRADINGDESK_PORT debe ser un número") from exc
        return cls(
            data_dir=Path(env.get("TRADINGDESK_DATA_DIR") or default_data_dir()).expanduser(),
            host=env.get("TRADINGDESK_HOST", "127.0.0.1"),
            port=port,
            mode=env.get("TRADINGDESK_MODE", "paper").lower(),
            data_source=env.get("TRADINGDESK_DATA_SOURCE", "mock").lower(),
            log_level=env.get("TRADINGDESK_LOG_LEVEL", "INFO").upper(),
        ).validate()
