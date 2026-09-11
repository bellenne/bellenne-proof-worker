from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app.core.errors import WorkerError
from app.models.job import Job
from app.models.result import ProofArtifact
from app.storage.workspace import Workspace, atomic_json, file_sha256


class RecoveryManager:
    """Never consult source storage when a committed local Result can be reused."""

    def recover_artifact(self, job: Job, workspace: Workspace, record: dict) -> ProofArtifact | None:
        path = workspace.result_path
        ambiguous_upload = record.get("upload_intent", False) or record["stage"] in {"UPLOADING", "COMPLETING"}
        saved = record.get("artifact")
        if saved is None and workspace.manifest_path.is_file():
            try:
                saved = json.loads(workspace.manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = None
        if not path.is_file():
            if ambiguous_upload:
                raise WorkerError("JOB_STATE_ERROR", "An upload may have been accepted but its local Result is missing")
            return None
        try:
            digest = file_sha256(path)
            if saved:
                artifact = ProofArtifact.model_validate(saved)
                if artifact.path.resolve() != path.resolve() or artifact.sha256 != digest or artifact.size_bytes != path.stat().st_size:
                    raise ValueError("Result no longer matches its manifest")
            else:
                # Crash between atomic JPEG rename and journal/manifest update.
                plan = json.loads((workspace.path / "render-plan.json").read_text(encoding="utf-8"))
                if plan["job_id"] != job.job_id or plan["attempt"] != job.attempt:
                    raise ValueError("Plan does not belong to this execution")
                artifact = ProofArtifact(path=path, sha256=digest, size_bytes=path.stat().st_size,
                                         metadata={**plan["metadata"], "recovered_after_save": True})
            self._validate_jpeg(path, job)
            atomic_json(workspace.manifest_path, artifact.model_dump(mode="json"))
            return artifact
        except (OSError, ValueError, KeyError, ValidationError, WorkerError):
            if ambiguous_upload:
                raise WorkerError("JOB_STATE_ERROR", "Saved Result is invalid after an ambiguous upload; retained for inspection") from None
            # Preserve damaged output for inspection; only this execution's own
            # fixed output path is renamed. Never touch production source files.
            path.replace(path.with_name(f"result.corrupt-{uuid4().hex}.jpg"))
            return None

    @staticmethod
    def _validate_jpeg(path: Path, job: Job):
        import pyvips

        try:
            image = pyvips.Image.new_from_file(str(path), access="sequential", fail_on="error")
            if image.get("vips-loader") != "jpegload" or image.bands != 3:
                raise ValueError("Expected RGB JPEG")
            if (image.width, image.height) != (job.preset.width_px, job.preset.height_px):
                raise ValueError("Unexpected proof dimensions")
            if abs(image.xres * 25.4 - job.preset.output_dpi) > 1 or abs(image.yres * 25.4 - job.preset.output_dpi) > 1:
                raise ValueError("Unexpected proof DPI")
            # Proof is bounded by Preset, unlike source; force decoding to detect
            # truncated or corrupt data before submitting a recovered file.
            image.avg()
        except pyvips.Error:
            raise ValueError("Saved JPEG cannot be decoded") from None
