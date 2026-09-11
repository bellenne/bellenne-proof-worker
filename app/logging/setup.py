import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if any(s in k.casefold() for s in ("token", "password", "authorization", "secret", "credential"))
                else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", value)
    return value


def configure_logging(data_path: Path, level: str, worker_name: str, token: str):
    log_dir = data_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), RotatingFileHandler(log_dir / "worker.jsonl", maxBytes=10_000_000, backupCount=5, encoding="utf-8")]
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s", handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    def context(logger, method, event):
        event.setdefault("worker_id", worker_name)
        event.setdefault("job_id", None)
        event.setdefault("message", event.get("event", ""))
        event.setdefault("metadata", {})
        return redact(event, (token,))

    structlog.configure(processors=[structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True), context, structlog.processors.JSONRenderer(ensure_ascii=False)],
        logger_factory=structlog.stdlib.LoggerFactory(), cache_logger_on_first_use=True)
