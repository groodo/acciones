"""Logging estructurado (JSON por línea) con redacción de secretos."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from datetime import datetime, timezone
from pathlib import Path

_SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"((?:token|password|passwd|secret|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", re.IGNORECASE),
]
# Identificadores de cuenta IBKR: U/DU + dígitos. Se enmascaran salvo los 3 últimos.
_ACCOUNT_ID = re.compile(r"\b(D?U)(\d+)(\d{3})\b")


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTADO]", text)
    return _ACCOUNT_ID.sub(lambda m: f"{m.group(1)}{'*' * len(m.group(2))}{m.group(3)}", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload["fields"] = json.loads(redact(json.dumps(extra, default=str)))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(log_dir: Path | None, level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = JsonFormatter()
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_dir / "service.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields: object) -> None:
    logger.log(level, msg, extra={"fields": fields})
