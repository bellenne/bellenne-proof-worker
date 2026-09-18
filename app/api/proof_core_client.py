"""The only HTTP boundary of Proof Worker, matching the existing Proof Core API.

No request is silently retried. The caller journals intent and drives bounded
backoff/reconciliation, especially around claim and ambiguous result uploads.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Self, TypeVar

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from app.api.dto import (
    EventAck,
    EventRequest,
    FailureRequest,
    HeartbeatRequest,
    JobDTO,
    ProgressAck,
    ProgressRequest,
    ResultAck,
    WorkerDTO,
)
from app.core.errors import WorkerError

API_PREFIX = "api/v1"
ENDPOINTS = {
    "heartbeat": f"{API_PREFIX}/workers/heartbeat",
    "claim": f"{API_PREFIX}/jobs/claim",
    **{action: f"{API_PREFIX}/jobs/{{job_id}}/{action}" for action in (
        "start", "progress", "events", "result", "complete", "fail"
    )},
}
_DTO = TypeVar("_DTO", bound=BaseModel)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SECRET_KEY = re.compile(r"token|authorization|password|passwd|secret|credential|api[_-]?key", re.IGNORECASE)
_WORKER_SECRET = re.compile(r"\bproof_(?:worker|hook)_[A-Za-z0-9_-]+")


class ProofCoreClient:
    def __init__(
        self,
        base_url: str,
        token: str | SecretStr,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        raw_token = token.get_secret_value() if isinstance(token, SecretStr) else token
        try:
            url = httpx.URL(base_url)
            if (
                url.scheme not in {"http", "https"} or not url.host
                or url.userinfo or url.query or url.fragment or not math.isfinite(timeout) or timeout <= 0
                or not isinstance(raw_token, str) or not raw_token
                or not raw_token.isascii() or any(not 33 <= ord(char) <= 126 for char in raw_token)
            ):
                raise ValueError
        except (ValueError, TypeError, httpx.InvalidURL):
            raise WorkerError("INVALID_CONFIG", "Invalid Proof Core connection configuration.") from None
        self._token = SecretStr(raw_token)
        # A path prefix (e.g. /proof/) is preserved for the Bellenne gateway.
        self._http = httpx.AsyncClient(
            base_url=str(url).rstrip("/") + "/",
            headers={"Authorization": f"Bearer {raw_token}", "Accept": "application/json"},
            timeout=httpx.Timeout(timeout),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else self._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            return _WORKER_SECRET.sub("[REDACTED]", value.replace(self._token.get_secret_value(), "[REDACTED]"))
        return value

    @staticmethod
    def _path(operation: str, job_id: str | None = None) -> str:
        if job_id is not None and not _IDENTIFIER.fullmatch(job_id):
            raise WorkerError("INVALID_CONFIG", "Invalid Core Job identifier.")
        return ENDPOINTS[operation].format(job_id=job_id)

    async def _request(
        self, operation: str, job_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any] | None:
        if "json" in kwargs:
            kwargs["json"] = self._redact(kwargs["json"])
        error_code = "UPLOAD_ERROR" if operation == "result" else "CORE_UNAVAILABLE"
        try:
            response = await self._http.post(self._path(operation, job_id), **kwargs)
        except (httpx.RequestError, httpx.InvalidURL):
            # Even a timeout may follow a committed mutation. Never include
            # HTTP request/response objects, body text, headers or original cause.
            raise WorkerError(
                error_code,
                "Proof Core request did not return an acknowledgement.",
                {"operation": operation, "ambiguous": True},
                retryable=True,
            ) from None
        status = response.status_code
        if not 200 <= status < 300:
            retryable = status in {408, 425, 429} or status >= 500
            code = "JOB_STATE_ERROR" if status in {404, 409} else error_code
            if status in {400, 401, 403, 405, 422} or 300 <= status < 400:
                code = "INVALID_CONFIG"
            raise WorkerError(
                code,
                "Proof Core rejected the request.",
                {"operation": operation, "http_status": status, "ambiguous": retryable},
                retryable=retryable,
            )
        if status == 204 and operation == "claim":
            return None
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError
        except (ValueError, TypeError, UnicodeError):
            raise WorkerError(
                "JOB_STATE_ERROR", "Proof Core returned an invalid acknowledgement.",
                {"operation": operation, "ambiguous": True}, retryable=True,
            ) from None
        return payload

    @staticmethod
    def _decode(model: type[_DTO], payload: Any, operation: str) -> _DTO:
        try:
            return model.model_validate(payload)
        except (ValidationError, ValueError, TypeError):
            raise WorkerError(
                "JOB_STATE_ERROR", "Proof Core response does not match its v1 contract.",
                {"operation": operation, "ambiguous": True}, retryable=True,
            ) from None

    async def heartbeat(
        self, *, hostname: str, version: str, state: str = "IDLE",
        current_job_id: str | None = None, capabilities: list[str] | None = None,
        last_error_code: str | None = None, last_error_message: str | None = None,
        worker_name: str | None = None,
    ) -> WorkerDTO:
        # Core name belongs to the provisioned token identity. Its v1 heartbeat
        # cannot rename that identity; the supplied name is intentionally local.
        del worker_name
        availability = {"IDLE": "available", "BUSY": "busy", "ERROR": "error"}.get(state, state)
        try:
            request = HeartbeatRequest(
                hostname=hostname, version=version, availability=availability,
                current_job_id=current_job_id, capabilities=capabilities or [],
                last_error_code=last_error_code, last_error_message=last_error_message,
            )
        except ValidationError:
            raise WorkerError("INVALID_CONFIG", "Invalid Worker heartbeat configuration.") from None
        return self._decode(WorkerDTO, await self._request("heartbeat", json=request.model_dump()), "heartbeat")

    async def claim(self) -> JobDTO | None:
        """Return the current assignment, or atomically claim work when none exists.

        This is Core v1's synchronization boundary, not a lookup of an old Job.
        Journal the returned identity and attempt before other Job mutations.
        """
        payload = await self._request("claim")
        if payload is None:
            return None
        result = self._decode(JobDTO, payload, "claim")
        if result.processing_status not in {"assigned", "running"}:
            raise WorkerError("JOB_STATE_ERROR", "Core claim did not return active work.", retryable=False)
        return result

    async def start(self, job_id: str) -> JobDTO:
        result = self._decode(JobDTO, await self._request("start", job_id), "start")
        self._check_job_id(result, job_id)
        if result.processing_status != "running":
            raise WorkerError("JOB_STATE_ERROR", "Core did not acknowledge Job start.", retryable=False)
        return result

    @staticmethod
    def _check_job_id(result: JobDTO, job_id: str) -> None:
        if result.id != job_id:
            raise WorkerError("JOB_STATE_ERROR", "Core acknowledgement references a different Job.", retryable=False)

    async def progress(self, job_id: str, progress: int, stage: str) -> None:
        try:
            payload = ProgressRequest(progress=progress, current_stage=stage).model_dump()
        except ValidationError:
            raise WorkerError("INVALID_CONFIG", "Invalid Job progress update.") from None
        result = self._decode(
            ProgressAck, await self._request("progress", job_id, json=payload), "progress"
        )
        if result.job_id != job_id or result.progress != progress or result.current_stage != stage:
            raise WorkerError("JOB_STATE_ERROR", "Core did not acknowledge Job progress.", retryable=False)

    async def event(
        self, job_id: str, event_type: str, message: str, *, level: str = "info",
        details: dict[str, Any] | None = None, error_code: str | None = None,
    ) -> None:
        try:
            payload = EventRequest(
                event_type=event_type, message=message, level=level,
                details=details or {}, error_code=error_code,
            ).model_dump()
        except ValidationError:
            raise WorkerError("INVALID_CONFIG", "Invalid Job event.") from None
        self._decode(EventAck, await self._request("events", job_id, json=payload), "events")

    @staticmethod
    def result_key(job_id: str, attempt: int, sha256: str) -> str:
        """Core keys are scoped by Worker; attempt avoids reusing an old result."""
        return hashlib.sha256(f"proof-worker-v1:{job_id}:{attempt}:{sha256}".encode()).hexdigest()

    async def upload_result(
        self, job_id: str, path: Path | str, sha256: str, attempt: int,
        metadata: dict[str, Any] | None = None, *, idempotency_key: str | None = None,
    ) -> ResultAck:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256) or attempt < 1:
            raise WorkerError("INVALID_CONFIG", "A valid result checksum and attempt are required.")
        key = idempotency_key or self.result_key(job_id, attempt, sha256)
        if not _IDENTIFIER.fullmatch(key):
            raise WorkerError("INVALID_CONFIG", "Invalid result idempotency key.")
        output = Path(path)
        try:
            metadata_json = json.dumps(
                self._redact({**(metadata or {}), "sha256": sha256, "attempt": attempt}),
                ensure_ascii=False, allow_nan=False,
            )
        except (ValueError, TypeError):
            raise WorkerError("INVALID_CONFIG", "Result metadata is not JSON serializable.") from None
        try:
            # HTTPX multipart expects a regular file object. Hash the potentially
            # large local Result in a thread so heartbeats stay responsive.
            with output.open("rb") as stream:  # noqa: ASYNC230
                digest = await asyncio.to_thread(hashlib.file_digest, stream, "sha256")
                if digest.hexdigest() != sha256:
                    raise WorkerError(
                        "JOB_STATE_ERROR", "Saved Result checksum changed before upload.", retryable=False,
                    )
                stream.seek(0)
                payload = await self._request(
                    "result", job_id, headers={"Idempotency-Key": key},
                    data={"metadata_json": metadata_json},
                    files={
                        "file": (
                            output.name,
                            stream,
                            "application/zip" if output.suffix.casefold() == ".zip" else "image/jpeg",
                        )
                    },
                )
        except OSError:
            raise WorkerError("UPLOAD_ERROR", "Cannot read the saved Result for upload.", retryable=False) from None
        acknowledgement = self._decode(ResultAck, payload, "result")
        if acknowledgement.sha256 != sha256:
            raise WorkerError(
                "JOB_STATE_ERROR", "Core Result checksum does not match the saved Result.", retryable=False,
            )
        return acknowledgement

    async def complete(self, job_id: str) -> JobDTO:
        result = self._decode(JobDTO, await self._request("complete", job_id), "complete")
        self._check_job_id(result, job_id)
        if result.processing_status != "completed":
            raise WorkerError("JOB_STATE_ERROR", "Core did not acknowledge Job completion.", retryable=False)
        return result

    async def fail(
        self, job_id: str, error_code: str, message: str, details: dict[str, Any] | None = None
    ) -> JobDTO:
        try:
            payload = FailureRequest(error_code=error_code, message=message, details=details or {}).model_dump()
        except ValidationError:
            raise WorkerError("INVALID_CONFIG", "Invalid Job failure report.") from None
        result = self._decode(JobDTO, await self._request("fail", job_id, json=payload), "fail")
        self._check_job_id(result, job_id)
        if result.processing_status != "failed":
            raise WorkerError("JOB_STATE_ERROR", "Core did not acknowledge Job failure.", retryable=False)
        return result
