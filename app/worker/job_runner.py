from __future__ import annotations

import asyncio
from pathlib import Path
from time import perf_counter

import structlog

from app.api.proof_core_client import ProofCoreClient
from app.core.config import Settings
from app.core.errors import WorkerError
from app.files.finder import FileFinder
from app.files.publisher import OrderPublisher
from app.imaging.analysis.analyzer import CropAnalyzer
from app.imaging.loader import SourceValidator
from app.imaging.preview import PreviewGenerator
from app.imaging.renderer.proof_renderer import ProofRenderer
from app.models.job import Job
from app.models.result import ProofArtifact
from app.recovery.recovery_manager import RecoveryManager
from app.storage.state import LocalState
from app.storage.workspace import Workspace, atomic_json, file_sha256


class StopRequested(Exception):
    pass


class JobRunner:
    def __init__(self, settings: Settings, state: LocalState, client: ProofCoreClient, stop: asyncio.Event):
        self.settings, self.state, self.client, self.stop = settings, state, client, stop
        self.finder = FileFinder(settings.worker_source_roots)
        self.publisher = OrderPublisher()
        self.validator, self.preview = SourceValidator(), PreviewGenerator()
        self.analyzer, self.renderer = CropAnalyzer(), ProofRenderer()
        self.recovery = RecoveryManager()
        self.log = structlog.get_logger()

    async def report(self, job: Job, stage: str, progress: int):
        if self.stop.is_set():
            raise StopRequested()
        self.state.update(job.job_id, job.attempt, stage=stage)
        self.log.info(stage, job_id=job.job_id, metadata={"progress": progress, "attempt": job.attempt})
        try:
            await self.client.progress(job.job_id, progress, stage)
        except WorkerError as error:
            if not error.retryable:
                raise
            self.log.warning("PROGRESS_DEFERRED", job_id=job.job_id, metadata={"code": error.code})

    async def event(self, job: Job, event: str, details=None, level="info"):
        try:
            await self.client.event(job.job_id, event, event, details=details, level=level)
        except WorkerError as error:
            if not error.retryable:
                raise
            self.log.warning("EVENT_NOT_ACKNOWLEDGED", job_id=job.job_id, metadata={"event_type": event})

    async def run(self, job: Job) -> ProofArtifact:
        started = perf_counter()
        workspace = Workspace(self.settings.worker_data_path, self.settings.worker_output_path, job.job_id, job.attempt)
        record = self.state.get(job.job_id, job.attempt)
        artifact = await asyncio.to_thread(self.recovery.recover_artifact, job, workspace, record)
        if artifact is None:
            artifact = await self.process(job, workspace)
        already_published = (
            isinstance(artifact.metadata.get("published_revision"), int)
            and artifact.metadata["published_revision"] > 0
            and isinstance(artifact.metadata.get("published_filename"), str)
            and isinstance(artifact.metadata.get("published_path"), str)
            and isinstance(artifact.metadata.get("published_source_path"), str)
            and isinstance(artifact.metadata.get("published_preview_path"), str)
            and isinstance(artifact.metadata.get("published_preview_sha256"), str)
        )
        if not already_published:
            order_path_value = artifact.metadata.get("source_order_path")
            if not isinstance(order_path_value, str) or not order_path_value:
                raise WorkerError("JOB_STATE_ERROR", "Saved Result does not identify its order directory")
            await self.report(job, "SAVING_TO_ORDER", 85)
            artifact = await asyncio.to_thread(
                self.publisher.publish,
                job,
                workspace,
                artifact,
                Path(order_path_value),
                self.settings.worker_source_roots,
            )
            atomic_json(workspace.manifest_path, artifact.model_dump(mode="json"))
        core_artifact = await asyncio.to_thread(
            self.publisher.result_for_core,
            artifact,
            self.settings.worker_source_roots,
        )
        self.state.update(job.job_id, job.attempt, artifact=artifact.model_dump(mode="json"), stage="READY_TO_UPLOAD", error=None)
        await self.report(job, "UPLOADING", 90)
        self.state.update(job.job_id, job.attempt, upload_intent=1)
        # Recheck digest after recovery/processing and before network submission.
        if await asyncio.to_thread(file_sha256, core_artifact.path) != core_artifact.sha256:
            raise WorkerError("JOB_STATE_ERROR", "Saved Result changed before upload")
        upload_started = perf_counter()
        ack = await self.client.upload_result(
            job.job_id,
            core_artifact.path,
            core_artifact.sha256,
            job.attempt,
            core_artifact.metadata,
        )
        upload_ms = (perf_counter() - upload_started) * 1000
        self.state.update(job.job_id, job.attempt, stage="COMPLETING")
        atomic_json(workspace.path / "upload.json", ack.model_dump(mode="json"))
        await self.event(job, "UPLOAD_COMPLETED", {"sha256": core_artifact.sha256, "result_id": ack.result_id,
            "upload_duration_ms": upload_ms, "total_duration_ms": (perf_counter() - started) * 1000})
        # Do not send progress after completion: Core has released the Worker.
        complete = await self.client.complete(job.job_id)
        if complete.attempt != job.attempt:
            raise WorkerError("JOB_STATE_ERROR", "Completion acknowledgement refers to a different attempt")
        self.state.update(job.job_id, job.attempt, stage="DONE")
        self.log.info("JOB_COMPLETED", job_id=job.job_id, metadata={"sha256": core_artifact.sha256,
            "upload_duration_ms": upload_ms, "total_duration_ms": (perf_counter() - started) * 1000})
        return core_artifact

    async def process(self, job: Job, workspace: Workspace) -> ProofArtifact:
        metrics = {}
        started = perf_counter()

        async def timed(name, function, *args):
            step_started = perf_counter()
            result = await asyncio.to_thread(function, *args)
            metrics[f"{name}_duration_ms"] = (perf_counter() - step_started) * 1000
            return result

        await self.report(job, "SEARCHING_FILE", 5)
        source = await timed(
            "search", self.finder.find, job.source_path, job.layout_number, job.preset.search
        )
        await self.event(job, "SOURCE_FOUND", source.diagnostics)
        await self.report(job, "VALIDATING_SOURCE", 15)
        metadata = await timed("validation", self.validator.validate, source.path, job.preset)
        await self.report(job, "READING_METADATA", 20)
        metadata_started = perf_counter()
        source_metadata = metadata.to_processing_dict()
        metrics["metadata_duration_ms"] = (perf_counter() - metadata_started) * 1000
        await self.event(job, "SOURCE_VALIDATED", source_metadata)
        await self.report(job, "CREATING_PREVIEW", 30)
        preview = await timed("preview", self.preview.generate, source.path, job.preset)
        await self.report(job, "ANALYZING", 45)
        analysis = await timed("analysis", self.analyzer.analyze, preview, metadata.width, metadata.height, job.preset)
        del preview
        await self.report(job, "SELECTING_CROP", 60)
        atomic_json(workspace.path / "analysis.json", analysis.model_dump(mode="json"))
        await self.event(job, "CROP_SELECTED", analysis.model_dump(mode="json"))
        warnings = list(dict.fromkeys(metadata.warnings + analysis.warnings))
        for warning in warnings:
            await self.event(job, warning, level="warning")
        processing = {**source_metadata, **metrics, "warnings": warnings,
            "job_id": job.job_id, "attempt": job.attempt,
            "source_path": job.source_path, "layout_number": job.layout_number,
            "source_order_path": str(source.order_path), "source_revision": source.revision,
            "public_id": job.public_id, "order_number": job.order_number,
            "worker_name": self.settings.proof_worker_name,
            "preset": job.preset.model_dump(mode="json"), "analysis": analysis.model_dump(mode="json"),
            "selected_crop": analysis.best_candidate.crop.model_dump(), "candidate_count": analysis.candidate_count,
            "best_score": analysis.best_candidate.score, "confidence": analysis.confidence,
            "output_width": job.preset.width_px, "output_height": job.preset.height_px,
            "output_dpi": job.preset.output_dpi, "output_color_space": "RGB", "output_icc_present": metadata.icc_present}
        atomic_json(workspace.path / "render-plan.json", {"job_id": job.job_id, "attempt": job.attempt, "metadata": processing})
        await self.report(job, "RENDERING", 70)
        artifact = await timed("render", self.renderer.render, source.path, analysis.best_candidate.crop,
                               job.preset, workspace.result_path, metadata)
        # Commit the artifact manifest before any subsequent await/network call.
        processing.update(artifact.metadata)
        processing["warnings"] = warnings
        processing["processing_duration_ms"] = (perf_counter() - started) * 1000
        artifact = artifact.model_copy(update={"metadata": processing})
        atomic_json(workspace.manifest_path, artifact.model_dump(mode="json"))
        self.state.update(job.job_id, job.attempt, artifact=artifact.model_dump(mode="json"), stage="READY_TO_UPLOAD")
        self.log.info("RESULT_SAVED", job_id=job.job_id, metadata={"sha256": artifact.sha256, "path": str(artifact.path)})
        return artifact
