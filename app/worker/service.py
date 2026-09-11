from __future__ import annotations

import asyncio
import os
import time

import structlog
from pydantic import ValidationError

from app import __version__
from app.api.proof_core_client import ProofCoreClient
from app.core.config import Settings
from app.core.config import RuntimeConfiguration
from app.core.errors import WorkerError
from app.logging.setup import redact
from app.storage.state import LocalState
from app.storage.workspace import atomic_json
from app.worker.heartbeat import heartbeat_loop, heartbeat_once, interruptible_wait
from app.worker.job_runner import JobRunner, StopRequested


class WorkerService:
    def __init__(self, settings: Settings, state: LocalState, client: ProofCoreClient):
        self.settings, self.state, self.client = settings, state, client
        self.stop = asyncio.Event()
        self.wake_event = asyncio.Event()
        self.assignment_ready = asyncio.Event()
        self.heartbeat_lock = asyncio.Lock()
        self.current_job_id = None
        self.worker_state = "IDLE"
        self.last_error_code = None
        self.core_connected = False
        self.fatal = False
        self.runner = JobRunner(settings, state, client, self.stop)
        self.log = structlog.get_logger()
        self.configuration_version = 0
        self.runtime_configuration = RuntimeConfiguration(
            heartbeat_interval=settings.heartbeat_interval,
            poll_interval=settings.poll_interval,
            retry_initial_seconds=settings.retry_initial_seconds,
            retry_max_seconds=settings.retry_max_seconds,
            storage_retry_limit=settings.storage_retry_limit,
            file_not_found_retry_limit=settings.file_not_found_retry_limit,
            health_interval=settings.health_interval,
            health_max_age=settings.health_max_age,
            path_mappings=[],
        )

    def apply_core_configuration(self, version: int, raw: dict) -> None:
        if version <= self.configuration_version or not raw:
            return
        try:
            configuration = RuntimeConfiguration.model_validate(raw)
        except ValidationError:
            self.log.error("CORE_CONFIGURATION_REJECTED", metadata={"configuration_version": version})
            return
        allowed = [root.resolve() for root in self.settings.worker_source_roots]
        if any(mapping.local_root.resolve() not in allowed for mapping in configuration.path_mappings):
            self.log.error("CORE_CONFIGURATION_REJECTED", metadata={
                "configuration_version": version, "reason": "mapped root is not a configured mount",
            })
            return
        self.runtime_configuration = configuration
        for name in (
            "heartbeat_interval", "poll_interval", "retry_initial_seconds", "retry_max_seconds",
            "storage_retry_limit", "file_not_found_retry_limit", "health_interval", "health_max_age",
        ):
            setattr(self.settings, name, getattr(configuration, name))
        self.configuration_version = version
        self.state.set_flag("configuration_version", version)
        self.log.info("CORE_CONFIGURATION_APPLIED", metadata={"configuration_version": version})

    def map_job_source(self, job):
        raw = job.source_path.strip()
        normalized = raw.replace("/", "\\")
        matches = [mapping for mapping in self.runtime_configuration.path_mappings if (
            normalized.casefold() == mapping.source_prefix.casefold()
            or normalized.casefold().startswith(mapping.source_prefix.casefold() + "\\")
        )]
        if not matches:
            return job
        mapping = max(matches, key=lambda item: len(item.source_prefix))
        relative = normalized[len(mapping.source_prefix):].lstrip("\\/").replace("\\", "/")
        if not relative:
            raise WorkerError("INVALID_CONFIG", "Mapped order path has no relative directory.")
        search = job.preset.search.model_copy(update={"roots": [str(mapping.local_root)]})
        preset = job.preset.model_copy(update={"search": search})
        metadata = {**job.metadata, "source_path_original": raw}
        return job.model_copy(update={"source_path": relative, "preset": preset, "metadata": metadata})

    async def health_loop(self):
        while not self.stop.is_set():
            atomic_json(self.settings.worker_data_path / "health.json", {
                "timestamp": time.time(), "pid": os.getpid(), "version": __version__,
                "fatal": self.fatal, "current_job_id": self.current_job_id,
                "core_connected": self.core_connected, "state": self.worker_state,
            })
            await interruptible_wait(self.stop, self.settings.health_interval)

    async def run_once(self) -> bool:
        if self.stop.is_set():
            return False
        # Durable intent before claim: a timeout is not evidence of an empty
        # queue. Core's next claim returns its current assignment for this token.
        self.state.set_flag("claim_pending", True)
        dto = await self.client.claim()
        self.core_connected = True
        if dto is None:
            self.current_job_id = None
            self.worker_state = "IDLE"
            self.state.detach_except(None, None)
            self.state.set_flag("claim_pending", False)
            self.assignment_ready.set()
            return False
        record = self.state.claim(dto.model_dump(mode="json"))
        self.current_job_id = dto.id
        self.worker_state = "BUSY"
        self.state.detach_except(dto.id, dto.attempt)
        self.state.set_flag("claim_pending", False)
        self.assignment_ready.set()
        if self.stop.is_set():
            return True
        self.log.info("JOB_CLAIMED", job_id=dto.id, metadata={"attempt": dto.attempt})
        # Core returns path mappings in heartbeat. Synchronize them after claim,
        # when current_job_id is known, and before interpreting input.source_path.
        await heartbeat_once(self)
        if record["stage"] == "BLOCKED":
            self.worker_state = "ERROR"
            return False
        try:
            if record["stage"] == "FAILING" and record["error"]:
                failure = record["error"]
                await self.fail(dto, WorkerError(failure["code"], failure["message"], failure["details"]))
                return True
            job = self.map_job_source(dto.to_domain())
            started = await self.client.start(job.job_id)
            if started.attempt != job.attempt or started.processing_status != "running":
                raise WorkerError("JOB_STATE_ERROR", "Core assignment changed before processing")
            await self.runner.run(job)
            self.last_error_code = None
            self.current_job_id = None
            self.worker_state = "IDLE"
            return True
        except StopRequested:
            return True
        except WorkerError as error:
            self.last_error_code = error.code
            if error.code == "JOB_STATE_ERROR" and not error.retryable:
                self.state.update(dto.id, dto.attempt, stage="BLOCKED", error=error.as_dict())
                self.worker_state = "ERROR"
                self.log.error("JOB_STATE_ERROR", job_id=dto.id, message=error.message, metadata=error.details)
                return False
            if error.code in {"CORE_UNAVAILABLE", "UPLOAD_ERROR"} and error.retryable or error.details.get("ambiguous"):
                raise
            retries = record["retries"]
            limit = self.settings.file_not_found_retry_limit if error.code == "FILE_NOT_FOUND" else self.settings.storage_retry_limit
            if (error.retryable or error.code == "FILE_NOT_FOUND") and retries < limit:
                self.state.update(dto.id, dto.attempt, retries=retries + 1, error=error.as_dict())
                raise WorkerError(error.code, error.message, error.details, retryable=True) from None
            await self.fail(dto, error)
            return True
        except (OSError, ValueError) as error:
            # Output/journal failures must not silently fail a business job or
            # continue claiming when local durability is uncertain.
            raise WorkerError("INTERNAL_ERROR", "Local execution storage failed", {"exception_type": type(error).__name__}) from None

    async def fail(self, dto, error: WorkerError):
        safe = redact(error.as_dict(), (self.settings.proof_worker_token.get_secret_value(),))
        self.state.update(dto.id, dto.attempt, stage="FAILING", error=safe)
        response = await self.client.fail(dto.id, error.code, safe["message"], safe["details"])
        if response.attempt != dto.attempt:
            raise WorkerError("JOB_STATE_ERROR", "Failure acknowledgement refers to a different attempt")
        self.state.update(dto.id, dto.attempt, stage="FAILED")
        self.current_job_id = None
        self.worker_state = "IDLE"
        self.log.error("JOB_FAILED", job_id=dto.id, metadata=safe)

    async def run(self):
        background = [asyncio.create_task(heartbeat_loop(self)), asyncio.create_task(self.health_loop())]
        delay = self.settings.retry_initial_seconds
        self.log.info("WORKER_STARTED", metadata={"version": __version__})
        try:
            while not self.stop.is_set():
                # Detect a dead heartbeat/health task; image processing runs in
                # threads so it does not starve these event-loop tasks.
                for task in background:
                    if task.done():
                        task.result()
                        raise WorkerError("INTERNAL_ERROR", "A background lifecycle task stopped")
                try:
                    worked = await self.run_once()
                    delay = self.settings.retry_initial_seconds
                    if not worked:
                        try:
                            await asyncio.wait_for(self.wake_event.wait(), timeout=self.settings.poll_interval)
                        except TimeoutError:
                            pass
                        self.wake_event.clear()
                except WorkerError as error:
                    self.last_error_code = error.code
                    self.log.error("WORKER_ERROR", message=error.message, metadata={"code": error.code, **error.details})
                    if not error.retryable:
                        self.fatal = True
                        raise
                    await interruptible_wait(self.stop, delay)
                    delay = min(delay * 2, self.settings.retry_max_seconds)
        finally:
            self.stop.set()
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            atomic_json(self.settings.worker_data_path / "health.json", {
                "timestamp": time.time(), "pid": os.getpid(), "fatal": True, "state": "STOPPED"})
