"""Arranque del servicio: ``python -m tradingdesk``."""

from __future__ import annotations

import logging
import sys

import uvicorn

from .api import create_app
from .config import ConfigError, Settings
from .logging_setup import configure_logging, log_event


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuración rechazada: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_dir, settings.log_level)
    log = logging.getLogger("tradingdesk")
    log_event(log, "Arrancando servicio", host=settings.host, port=settings.port, mode=settings.mode,
              data_source=settings.data_source, data_dir=str(settings.data_dir))
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
