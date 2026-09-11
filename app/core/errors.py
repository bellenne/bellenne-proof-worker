from __future__ import annotations

from typing import Any

RETRYABLE_CODES = {"SOURCE_STORAGE_UNAVAILABLE", "CORE_UNAVAILABLE", "UPLOAD_ERROR"}


class WorkerError(Exception):
    """Stable, safe public error; raw dependency exception text is never transmitted."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None,
                 retryable: bool | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message,
                "details": self.details, "retryable": self.retryable}

