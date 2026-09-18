from __future__ import annotations

import json
import zipfile
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
        ambiguous_upload = record.get("upload_intent", False) or record["stage"] in {"UPLOADING", "COMPLETING"}
        saved = record.get("artifact")
        if saved is None and workspace.manifest_path.is_file():
            try:
                saved = json.loads(workspace.manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = None
        path = workspace.result_path
        if isinstance(saved, dict) and saved.get("metadata", {}).get("result_kind") == "preview_archive":
            saved_path = saved.get("path")
            if isinstance(saved_path, str):
                path = Path(saved_path)
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
            if artifact.metadata.get("result_kind") == "preview_archive":
                self._validate_archive(path)
            elif artifact.metadata.get("result_kind") == "render_batch":
                self._validate_render_batch(artifact, job)
            else:
                self._validate_jpeg(path, job)
            atomic_json(workspace.manifest_path, artifact.model_dump(mode="json"))
            return artifact
        except (OSError, ValueError, KeyError, ValidationError, WorkerError):
            if ambiguous_upload:
                raise WorkerError("JOB_STATE_ERROR", "Saved Result is invalid after an ambiguous upload; retained for inspection") from None
            # Preserve damaged output for inspection; only this execution's own
            # fixed output path is renamed. Never touch production source files.
            path.replace(path.with_name(f"result.corrupt-{uuid4().hex}{path.suffix}"))
            return None

    @staticmethod
    def _validate_archive(path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                entries = [item for item in archive.infolist() if not item.is_dir()]
                if (
                    not entries
                    or len({item.filename.casefold() for item in entries}) != len(entries)
                    or any(
                        Path(item.filename).name != item.filename
                        or Path(item.filename).suffix.casefold() not in {".jpg", ".jpeg"}
                        for item in entries
                    )
                ):
                    raise ValueError("Unexpected preview archive members")
                for entry in entries:
                    with archive.open(entry) as file:
                        while file.read(1024 * 1024):
                            pass
        except (OSError, zipfile.BadZipFile, RuntimeError):
            raise ValueError("Saved preview archive is invalid") from None

    @classmethod
    def _validate_render_batch(cls, artifact: ProofArtifact, job: Job) -> None:
        items = artifact.metadata.get("batch_artifacts")
        if not isinstance(items, list) or not items:
            raise ValueError("Saved render batch has no items")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Saved render batch item is invalid")
            path_value = item.get("path")
            expected_sha256 = item.get("sha256")
            if not isinstance(path_value, str) or not isinstance(expected_sha256, str):
                raise ValueError("Saved render batch item is invalid")
            path = Path(path_value)
            if not path.is_file() or file_sha256(path) != expected_sha256:
                raise ValueError("Saved render batch item changed")
            width = item.get("output_width")
            height = item.get("output_height")
            dpi = item.get("output_dpi")
            if (
                isinstance(width, bool)
                or not isinstance(width, int)
                or isinstance(height, bool)
                or not isinstance(height, int)
                or isinstance(dpi, bool)
                or not isinstance(dpi, (int, float))
            ):
                raise ValueError("Saved render batch dimensions are invalid")
            cls._validate_jpeg(path, job, width=width, height=height, dpi=float(dpi))

    @staticmethod
    def _validate_jpeg(
        path: Path,
        job: Job,
        *,
        width: int | None = None,
        height: int | None = None,
        dpi: float | None = None,
    ):
        import pyvips

        try:
            image = pyvips.Image.new_from_file(str(path), access="sequential", fail_on="error")
            if image.get("vips-loader") != "jpegload" or image.bands != 3:
                raise ValueError("Expected RGB JPEG")
            expected_dimensions = (
                width if width is not None else job.preset.width_px,
                height if height is not None else job.preset.height_px,
            )
            expected_dpi = dpi if dpi is not None else job.preset.output_dpi
            if (image.width, image.height) != expected_dimensions:
                raise ValueError("Unexpected proof dimensions")
            if abs(image.xres * 25.4 - expected_dpi) > 1 or abs(image.yres * 25.4 - expected_dpi) > 1:
                raise ValueError("Unexpected proof DPI")
            # Proof is bounded by Preset, unlike source; force decoding to detect
            # truncated or corrupt data before submitting a recovered file.
            image.avg()
        except pyvips.Error:
            raise ValueError("Saved JPEG cannot be decoded") from None
