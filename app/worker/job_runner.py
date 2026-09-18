from __future__ import annotations

import asyncio
from pathlib import Path
from time import perf_counter

import structlog

from app.api.proof_core_client import ProofCoreClient
from app.core.config import Settings
from app.core.errors import WorkerError
from app.files.finder import FileFinder
from app.files.local_sources import LocalSources
from app.files.publisher import OrderPublisher
from app.imaging.analysis.analyzer import CropAnalyzer
from app.imaging.loader import SourceValidator
from app.imaging.preview import PreviewGenerator
from app.imaging.renderer.proof_renderer import ProofRenderer
from app.models.job import Job, ProofItem
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
        if artifact.metadata.get("result_kind") == "render_batch":
            await self.report(job, "SAVING_TO_ORDER", 85)
            artifact = await asyncio.to_thread(
                self.publisher.publish_many,
                job,
                workspace,
                artifact,
                self.settings.worker_source_roots,
            )
            atomic_json(workspace.manifest_path, artifact.model_dump(mode="json"))
        if artifact.metadata.get("result_kind") == "preview_archive":
            core_artifact = artifact
        else:
            core_artifact = await self._publish_legacy(job, workspace, artifact)

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

    async def _publish_legacy(
        self, job: Job, workspace: Workspace, artifact: ProofArtifact
    ) -> ProofArtifact:
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
        return core_artifact

    async def process(self, job: Job, workspace: Workspace) -> ProofArtifact:
        proof_items = job.execution_items()
        artifacts: list[ProofArtifact] = []
        with LocalSources(workspace.path) as local_sources:
            for index, proof_item in enumerate(proof_items):
                output = (
                    workspace.result_path
                    if index == 0
                    else workspace.result_path_for_item(index, proof_item.layout_number)
                )
                artifacts.append(
                    await self._process_layout(job, workspace, proof_item, output, local_sources)
                )
        order_paths = {item.metadata.get("source_order_path") for item in artifacts}
        if len(order_paths) != 1 or not all(isinstance(value, str) for value in order_paths):
            raise WorkerError("INVALID_CONFIG", "All requested layouts must belong to one order directory")
        primary = artifacts[0]
        metadata = {
            **primary.metadata,
            "result_kind": "render_batch",
            "layout_numbers": [item.layout_number for item in proof_items],
            "proof_variants": [item.proof_variant for item in proof_items],
            "batch_artifacts": [
                {
                    "item_id": proof_item.id,
                    "position": proof_item.position,
                    "layout_number": proof_item.layout_number,
                    "proof_variant": proof_item.proof_variant,
                    "filename": self.publisher.filename_for_variant(
                        job, proof_item.layout_number, proof_item.proof_variant
                    ),
                    "path": str(artifact.path),
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "source_order_path": artifact.metadata["source_order_path"],
                    "output_width": artifact.metadata["output_width"],
                    "output_height": artifact.metadata["output_height"],
                    "output_dpi": artifact.metadata["output_dpi"],
                }
                for proof_item, artifact in zip(proof_items, artifacts, strict=True)
            ],
        }
        artifact = primary.model_copy(update={"metadata": metadata})
        atomic_json(workspace.manifest_path, artifact.model_dump(mode="json"))
        self.state.update(
            job.job_id,
            job.attempt,
            artifact=artifact.model_dump(mode="json"),
            stage="READY_TO_UPLOAD",
        )
        return artifact

    async def _process_layout(
        self,
        job: Job,
        workspace: Workspace,
        proof_item: ProofItem,
        output: Path,
        local_sources: LocalSources,
    ) -> ProofArtifact:
        layout_number = proof_item.layout_number
        proof_variant = proof_item.proof_variant
        metrics = {}
        started = perf_counter()

        async def timed(name, function, *args, **kwargs):
            step_started = perf_counter()
            task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                # A thread keeps running after cancellation. Do not delete its
                # local source until the copy/decoder has released the file.
                try:
                    await task
                finally:
                    raise
            metrics[f"{name}_duration_ms"] = (perf_counter() - step_started) * 1000
            return result

        await self.report(job, "SEARCHING_FILE", 5)
        source = await timed(
            "search", self.finder.find, job.source_path, layout_number, job.preset.search
        )
        await self.event(job, "SOURCE_FOUND", source.diagnostics)
        # Keep Core's existing stage names; copying is visible in local logs.
        self.log.info("COPYING_SOURCE", job_id=job.job_id,
                      metadata={"layout_number": layout_number, "source": str(source.path)})
        last_copy_log = perf_counter()

        def copy_progress(copied, total):
            nonlocal last_copy_log
            now = perf_counter()
            if now - last_copy_log >= 10:
                self.log.info("SOURCE_COPY_PROGRESS", job_id=job.job_id,
                              metadata={"copied_bytes": copied, "total_bytes": total})
                last_copy_log = now

        local_path = await timed("source_copy", local_sources.copy, source.path, copy_progress)
        self.log.info("SOURCE_COPIED", job_id=job.job_id,
                      metadata={"path": str(local_path),
                                "duration_ms": metrics["source_copy_duration_ms"]})
        await self.report(job, "VALIDATING_SOURCE", 15)
        analysis_preset = job.preset
        validation_preset = job.preset
        if proof_variant == "thumbnail":
            validation_preset = job.preset.model_copy(update={
                "proof_width_mm": 1,
                "proof_height_mm": 1,
                "thumbnail_max_side_mm": 1,
                "thumbnail_left_offset_mm": 0,
            })
        elif proof_variant != "fragment_60x30":
            analysis_preset = job.preset.model_copy(
                update={"proof_width_mm": job.preset.proof_width_mm / 2}
            )
            validation_preset = analysis_preset
        metadata = await timed("validation", self.validator.validate, local_path, validation_preset)
        await self.report(job, "READING_METADATA", 20)
        metadata_started = perf_counter()
        source_metadata = metadata.to_processing_dict()
        metrics["metadata_duration_ms"] = (perf_counter() - metadata_started) * 1000
        await self.event(job, "SOURCE_VALIDATED", source_metadata)
        if proof_variant == "thumbnail":
            for warning in metadata.warnings:
                await self.event(job, warning, level="warning")
            await self.report(job, "RENDERING", 70)
            artifact = await timed(
                "render",
                self.renderer.render_thumbnail,
                local_path,
                job.preset,
                output,
                metadata,
            )
            processing = {
                **source_metadata,
                **metrics,
                **artifact.metadata,
                "warnings": metadata.warnings,
                "job_id": job.job_id,
                "attempt": job.attempt,
                "source_path": job.source_path,
                "layout_number": layout_number,
                "source_order_path": str(source.order_path),
                "source_revision": source.revision,
                "public_id": job.public_id,
                "order_number": job.order_number,
                "worker_name": self.settings.proof_worker_name,
                "preset": job.preset.model_dump(mode="json"),
                "processing_duration_ms": (perf_counter() - started) * 1000,
            }
            artifact = artifact.model_copy(update={"metadata": processing})
            self.log.info(
                "RESULT_SAVED",
                job_id=job.job_id,
                metadata={"sha256": artifact.sha256, "path": str(artifact.path)},
            )
            return artifact
        await self.report(job, "CREATING_PREVIEW", 30)
        preview = await timed("preview", self.preview.generate, local_path, analysis_preset)
        await self.report(job, "ANALYZING", 45)
        analysis = await timed(
            "analysis",
            self.analyzer.analyze,
            preview,
            metadata.width,
            metadata.height,
            analysis_preset,
        )
        del preview
        await self.report(job, "SELECTING_CROP", 60)
        atomic_json(workspace.path / "analysis.json", analysis.model_dump(mode="json"))
        await self.event(job, "CROP_SELECTED", analysis.model_dump(mode="json"))
        warnings = list(dict.fromkeys(metadata.warnings + analysis.warnings))
        for warning in warnings:
            await self.event(job, warning, level="warning")
        processing = {**source_metadata, **metrics, "warnings": warnings,
            "job_id": job.job_id, "attempt": job.attempt,
            "source_path": job.source_path, "layout_number": layout_number,
            "proof_item_id": proof_item.id, "proof_item_position": proof_item.position,
            "proof_variant": proof_variant,
            "source_order_path": str(source.order_path), "source_revision": source.revision,
            "public_id": job.public_id, "order_number": job.order_number,
            "worker_name": self.settings.proof_worker_name,
            "preset": job.preset.model_dump(mode="json"), "analysis": analysis.model_dump(mode="json"),
            "selected_crop": analysis.best_candidate.crop.model_dump(), "candidate_count": analysis.candidate_count,
            "best_score": analysis.best_candidate.score, "confidence": analysis.confidence,
            "output_width": job.preset.width_px, "output_height": job.preset.height_px,
            "output_dpi": job.preset.output_dpi, "output_color_space": "RGB", "output_icc_present": metadata.icc_present}
        await self.report(job, "RENDERING", 70)
        crops = [analysis.best_candidate.crop]
        if proof_variant == "two_fragments_30x30":
            if len(analysis.candidates) < 2:
                raise WorkerError(
                    "ANALYSIS_NO_VALID_CROP",
                    "Two spatially distinct 30x30 fragments could not be found",
                    {"layout_number": layout_number, "candidate_count": len(analysis.candidates)},
                )
            crops = [candidate.crop for candidate in analysis.candidates[:2]]
        if proof_variant == "fragment_30x30":
            render_preset = analysis_preset
        elif proof_variant == "fragment_90x30":
            render_preset = job.preset.model_copy(
                update={"proof_width_mm": job.preset.proof_width_mm * 1.5}
            )
        else:
            render_preset = job.preset
        processing["output_width"] = render_preset.width_px
        processing["output_height"] = render_preset.height_px
        atomic_json(
            workspace.path / "render-plan.json",
            {"job_id": job.job_id, "attempt": job.attempt, "metadata": processing},
        )
        artifact = await timed(
            "render",
            self.renderer.render_variant,
            local_path,
            crops,
            render_preset,
            output,
            metadata,
            variant=proof_variant,
            brightness_direction=proof_item.brightness_direction,
            brightness_percent=proof_item.brightness_percent,
            fragments=[
                fragment.model_dump(mode="json")
                for fragment in (proof_item.fragments or [])
            ] or None,
            fragment_width_px=(
                analysis_preset.width_px
                if proof_variant in {
                    "two_fragments_30x30",
                    "fragment_30x30_color",
                    "fragment_90x30",
                }
                else None
            ),
        )
        # Commit the artifact manifest before any subsequent await/network call.
        processing.update(artifact.metadata)
        processing["warnings"] = warnings
        processing["processing_duration_ms"] = (perf_counter() - started) * 1000
        artifact = artifact.model_copy(update={"metadata": processing})
        self.log.info("RESULT_SAVED", job_id=job.job_id, metadata={"sha256": artifact.sha256, "path": str(artifact.path)})
        return artifact
