from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

from pydantic import ValidationError

from app.core.errors import WorkerError
from app.files.revisions import parse_revision_name
from app.imaging.caption import save_captioned_preview
from app.models.job import Job
from app.models.result import ProofArtifact
from app.storage.workspace import Workspace, atomic_json, file_sha256, sync_directory


class OrderPublisher:
    """Publish source and captioned preview into the next order revision."""

    marker_name = ".proof-worker-publication.json"
    source_directory_name = "Исходник"
    preview_directory_name = "Превью"

    @staticmethod
    def _revision(name: str) -> int | None:
        return parse_revision_name(name)

    @staticmethod
    def _dimension(value_mm: float) -> str:
        value_cm = value_mm / 10
        if value_cm.is_integer():
            return str(int(value_cm))
        return f"{value_cm:g}".replace(".", ",")

    @classmethod
    def filename(cls, job: Job) -> str:
        return cls.filename_for(job, job.layout_number)

    @classmethod
    def filename_for(cls, job: Job, layout_number: int) -> str:
        return cls.filename_for_variant(job, layout_number, job.proof_variant)

    @classmethod
    def filename_for_variant(
        cls,
        job: Job,
        layout_number: int,
        proof_variant: str,
    ) -> str:
        if proof_variant == "thumbnail":
            return f"ЦП Макет {layout_number} Миниатюра.jpg"
        width_mm = job.preset.proof_width_mm
        height_mm = job.preset.proof_height_mm
        if proof_variant == "fragment_30x30":
            width_mm /= 2
        elif proof_variant == "fragment_90x30":
            width_mm *= 1.5
        width = cls._dimension(width_mm)
        height = cls._dimension(height_mm)
        return f"ЦП Макет {layout_number} {width}х{height}.jpg"

    @staticmethod
    def _write_preview_archive(destination: Path, previews: list[Path]) -> None:
        temporary = destination.with_name(f".{destination.name}.tmp")
        try:
            temporary.unlink(missing_ok=True)
            with zipfile.ZipFile(
                temporary,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                for preview in previews:
                    info = zipfile.ZipInfo(preview.name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o600 << 16
                    archive.writestr(info, preview.read_bytes())
            os.replace(temporary, destination)
            sync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def publish_many(
        self,
        job: Job,
        workspace: Workspace,
        artifact: ProofArtifact,
        allowed_roots: list[Path],
    ) -> ProofArtifact:
        """Atomically publish every rendered layout into one order revision."""
        items = artifact.metadata.get("batch_artifacts")
        order_value = artifact.metadata.get("source_order_path")
        if not isinstance(items, list) or not items or not isinstance(order_value, str):
            raise WorkerError("JOB_STATE_ERROR", "Rendered batch metadata is incomplete")
        roots = [Path(root).resolve() for root in allowed_roots]
        try:
            order = self._validate_order_path(Path(order_value), roots)
            rendered: list[dict[str, object]] = []
            used_filenames: dict[str, int] = {}
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise WorkerError("JOB_STATE_ERROR", "Rendered batch item is invalid")
                layout_number = item.get("layout_number")
                position = item.get("position", index)
                item_id = item.get("item_id", f"legacy-{index + 1}")
                proof_variant = item.get("proof_variant", job.proof_variant)
                path_value = item.get("path")
                digest = item.get("sha256")
                item_order = item.get("source_order_path")
                if (
                    isinstance(layout_number, bool)
                    or not isinstance(layout_number, int)
                    or isinstance(position, bool)
                    or not isinstance(position, int)
                    or position != index
                    or not isinstance(item_id, str)
                    or not item_id
                    or not isinstance(proof_variant, str)
                    or not isinstance(path_value, str)
                    or not isinstance(digest, str)
                    or item_order != order_value
                ):
                    raise WorkerError("JOB_STATE_ERROR", "Rendered batch item is invalid")
                path = Path(path_value).resolve(strict=True)
                if not path.is_file() or file_sha256(path) != digest:
                    raise WorkerError("JOB_STATE_ERROR", "Rendered proof changed before publication")
                filename_value = item.get("filename")
                filename = (
                    filename_value
                    if isinstance(filename_value, str)
                    else self.filename_for_variant(job, layout_number, proof_variant)
                )
                if Path(filename).name != filename or Path(filename).suffix.casefold() != ".jpg":
                    raise WorkerError("JOB_STATE_ERROR", "Rendered batch filename is invalid")
                filename_key = filename.casefold()
                occurrence = used_filenames.get(filename_key, 0) + 1
                used_filenames[filename_key] = occurrence
                if occurrence > 1:
                    filename = f"{Path(filename).stem} ({occurrence}).jpg"
                rendered.append({
                    "item_id": item_id,
                    "position": position,
                    "layout_number": layout_number,
                    "proof_variant": proof_variant,
                    "path": path,
                    "sha256": digest,
                    "filename": filename,
                })

            identity = {
                "job_id": job.job_id,
                "attempt": job.attempt,
                "files": [
                    {
                        "item_id": item["item_id"],
                        "position": item["position"],
                        "layout_number": item["layout_number"],
                        "proof_variant": item["proof_variant"],
                        "sha256": item["sha256"],
                        "filename": item["filename"],
                    }
                    for item in rendered
                ],
            }
            key = hashlib.sha256(f"{job.job_id}\0{job.attempt}".encode()).hexdigest()[:24]
            staging = order / f".proof-worker-{key}"
            plan = self._load_json(workspace.publication_path)
            expected_plan = {
                **identity,
                "order_path": str(order),
                "staging_path": str(staging),
            }
            if plan is not None and any(
                plan.get(field) != value for field, value in expected_plan.items()
            ):
                raise WorkerError("JOB_STATE_ERROR", "Saved publication plan does not match this batch")
            if plan is None:
                plan = expected_plan
                atomic_json(workspace.publication_path, plan)

            published_directory: Path | None = None
            published_value = plan.get("published_directory")
            if isinstance(published_value, str):
                published_directory = Path(published_value).resolve(strict=True)
                if published_directory.parent != order or self._revision(published_directory.name) is None:
                    raise WorkerError("JOB_STATE_ERROR", "Saved publication directory is invalid")
            if published_directory is None:
                published_directory = self._matching_published_directory(order, identity)
            if published_directory is None:
                staging.mkdir(exist_ok=True)
                if staging.is_symlink() or not staging.resolve().is_relative_to(order):
                    raise WorkerError("JOB_STATE_ERROR", "Publication staging path is not safe")
                source_dir = staging / self.source_directory_name
                preview_dir = staging / self.preview_directory_name
                for directory in (source_dir, preview_dir):
                    directory.mkdir(exist_ok=True)
                    if directory.is_symlink() or not directory.resolve().is_relative_to(staging):
                        raise WorkerError("JOB_STATE_ERROR", "Publication subdirectory is not safe")
                marker = staging / self.marker_name
                existing_marker = self._load_json(marker)
                if existing_marker not in (None, identity):
                    raise WorkerError("JOB_STATE_ERROR", "Publication staging belongs to another execution")
                for item in rendered:
                    path = item["path"]
                    digest = item["sha256"]
                    filename = item["filename"]
                    if not isinstance(path, Path) or not isinstance(digest, str) or not isinstance(filename, str):
                        raise WorkerError("JOB_STATE_ERROR", "Rendered batch item is invalid")
                    source = source_dir / filename
                    preview = preview_dir / filename
                    if source.is_symlink() or preview.is_symlink():
                        raise WorkerError("JOB_STATE_ERROR", "Publication destination cannot be a symbolic link")
                    if not source.is_file() or file_sha256(source) != digest:
                        self._copy_verified(path, source, digest)
                    save_captioned_preview(
                        source,
                        preview,
                        Path(filename).stem,
                        jpeg_quality=job.preset.jpeg_quality,
                    )
                atomic_json(marker, identity)
                while True:
                    revisions = [
                        revision
                        for entry in order.iterdir()
                        if (revision := self._revision(entry.name)) is not None
                        and entry.is_dir()
                        and not entry.is_symlink()
                    ]
                    published_directory = order / str(max(revisions, default=0) + 1)
                    try:
                        staging.rename(published_directory)
                        sync_directory(order)
                        break
                    except FileExistsError:
                        continue
                plan["published_directory"] = str(published_directory.resolve())
                atomic_json(workspace.publication_path, plan)

            published_files = []
            previews = []
            for item in rendered:
                number = item["layout_number"]
                digest = item["sha256"]
                filename = item["filename"]
                if not isinstance(number, int) or not isinstance(digest, str) or not isinstance(filename, str):
                    raise WorkerError("JOB_STATE_ERROR", "Rendered batch item is invalid")
                source, preview = self._publication_paths(published_directory, filename)
                source = source.resolve(strict=True)
                preview = preview.resolve(strict=True)
                if (
                    source.parent.parent != published_directory
                    or preview.parent.parent != published_directory
                    or file_sha256(source) != digest
                ):
                    raise WorkerError("JOB_STATE_ERROR", "Published batch does not match its journal")
                preview_digest = file_sha256(preview)
                previews.append(preview)
                published_files.append({
                    "item_id": item["item_id"],
                    "position": item["position"],
                    "layout_number": number,
                    "proof_variant": item["proof_variant"],
                    "filename": filename,
                    "source_path": str(source),
                    "preview_path": str(preview),
                    "preview_sha256": preview_digest,
                    "preview_size_bytes": preview.stat().st_size,
                })
            self._write_preview_archive(workspace.archive_path, previews)
            digest = file_sha256(workspace.archive_path)
            (published_directory / self.marker_name).unlink(missing_ok=True)
            sync_directory(published_directory)
            return ProofArtifact(
                path=workspace.archive_path,
                sha256=digest,
                size_bytes=workspace.archive_path.stat().st_size,
                metadata={
                    **artifact.metadata,
                    "result_kind": "preview_archive",
                    "published_revision": int(published_directory.name),
                    "published_files": published_files,
                    "published_filename": previews[0].name if len(previews) == 1 else "",
                },
            )
        except WorkerError:
            raise
        except PermissionError:
            raise WorkerError("FILE_ACCESS_DENIED", "Order directory cannot be written") from None
        except (OSError, ValueError, zipfile.BadZipFile):
            raise WorkerError(
                "SOURCE_STORAGE_UNAVAILABLE",
                "Source storage became unavailable while saving the proof batch",
            ) from None

    @staticmethod
    def _load_json(path: Path) -> dict | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
        temporary = destination.with_name(destination.name + ".tmp")
        try:
            temporary.unlink(missing_ok=True)
            with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if file_sha256(temporary) != expected_sha256:
                raise OSError("Published proof digest mismatch")
            os.replace(temporary, destination)
            sync_directory(destination.parent)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _validate_order_path(order: Path, allowed_roots: list[Path]) -> Path:
        resolved = order.resolve(strict=True)
        if not resolved.is_dir() or resolved.is_symlink() or not any(
            resolved == root or resolved.is_relative_to(root) for root in allowed_roots
        ):
            raise WorkerError(
                "INVALID_CONFIG",
                "Resolved order directory is outside configured source roots",
            )
        return resolved

    def _matching_published_directory(self, order: Path, identity: dict) -> Path | None:
        for entry in order.iterdir():
            if self._revision(entry.name) is None or not entry.is_dir() or entry.is_symlink():
                continue
            marker = self._load_json(entry / self.marker_name)
            if marker == identity:
                return entry
        return None

    @classmethod
    def _publication_paths(cls, revision_directory: Path, filename: str) -> tuple[Path, Path]:
        return (
            revision_directory / cls.source_directory_name / filename,
            revision_directory / cls.preview_directory_name / filename,
        )

    @classmethod
    def _validate_publication(
        cls,
        artifact: ProofArtifact,
        order: Path,
        source: Path,
        preview: Path,
        expected_preview_sha256: str | None = None,
    ) -> tuple[Path, Path, str]:
        resolved_source = source.resolve(strict=True)
        resolved_preview = preview.resolve(strict=True)
        revision_directory = resolved_source.parent.parent
        if (
            not resolved_source.is_file()
            or not resolved_preview.is_file()
            or resolved_source.parent.name != cls.source_directory_name
            or resolved_preview.parent.name != cls.preview_directory_name
            or resolved_preview.parent.parent != revision_directory
            or revision_directory.parent != order
            or cls._revision(revision_directory.name) is None
            or resolved_source.name != resolved_preview.name
            or file_sha256(resolved_source) != artifact.sha256
        ):
            raise WorkerError("JOB_STATE_ERROR", "Published proof does not match its journal")
        preview_sha256 = file_sha256(resolved_preview)
        if expected_preview_sha256 and preview_sha256 != expected_preview_sha256:
            raise WorkerError("JOB_STATE_ERROR", "Published preview changed after publication")
        return resolved_source, resolved_preview, preview_sha256

    def publish(
        self,
        job: Job,
        workspace: Workspace,
        artifact: ProofArtifact,
        order_path: Path,
        allowed_roots: list[Path],
    ) -> ProofArtifact:
        roots = [Path(root).resolve() for root in allowed_roots]
        identity = {
            "job_id": job.job_id,
            "attempt": job.attempt,
            "sha256": artifact.sha256,
            "filename": self.filename(job),
        }
        try:
            order = self._validate_order_path(order_path, roots)
            key = hashlib.sha256(f"{job.job_id}\0{job.attempt}".encode()).hexdigest()[:24]
            staging = order / f".proof-worker-{key}"
            plan = self._load_json(workspace.publication_path)
            expected_plan = {
                **identity,
                "order_path": str(order),
                "staging_path": str(staging),
            }
            if plan is not None and any(
                plan.get(field) != value for field, value in expected_plan.items()
            ):
                raise WorkerError(
                    "JOB_STATE_ERROR",
                    "Saved publication plan does not match this execution",
                )
            if plan is None:
                plan = expected_plan
                atomic_json(workspace.publication_path, plan)

            published_path = plan.get("published_path")
            published_source_path = plan.get("published_source_path")
            if published_path or published_source_path:
                if not isinstance(published_path, str) or not isinstance(
                    published_source_path, str
                ):
                    raise WorkerError("JOB_STATE_ERROR", "Saved publication plan is incomplete")
                source, preview, preview_sha256 = self._validate_publication(
                    artifact,
                    order,
                    Path(published_source_path),
                    Path(published_path),
                    plan.get("preview_sha256"),
                )
                return self._with_publication(artifact, source, preview, preview_sha256)

            published_directory = self._matching_published_directory(order, identity)
            if published_directory is None:
                staging.mkdir(exist_ok=True)
                if (
                    staging.is_symlink()
                    or not staging.is_dir()
                    or not staging.resolve().is_relative_to(order)
                ):
                    raise WorkerError(
                        "JOB_STATE_ERROR",
                        "Publication staging path is not a safe directory",
                    )
                source, preview = self._publication_paths(staging, identity["filename"])
                for directory in (source.parent, preview.parent):
                    directory.mkdir(exist_ok=True)
                    if directory.is_symlink() or not directory.resolve().is_relative_to(staging):
                        raise WorkerError(
                            "JOB_STATE_ERROR",
                            "Publication subdirectory is not a safe directory",
                        )
                marker_path = staging / self.marker_name
                existing_marker = self._load_json(marker_path)
                if existing_marker not in (None, identity):
                    raise WorkerError(
                        "JOB_STATE_ERROR",
                        "Publication staging directory belongs to another execution",
                    )
                if source.is_symlink() or preview.is_symlink():
                    raise WorkerError(
                        "JOB_STATE_ERROR",
                        "Publication destination cannot be a symbolic link",
                    )
                if not source.is_file() or file_sha256(source) != artifact.sha256:
                    self._copy_verified(artifact.path, source, artifact.sha256)
                save_captioned_preview(
                    source,
                    preview,
                    Path(identity["filename"]).stem,
                    jpeg_quality=job.preset.jpeg_quality,
                )
                atomic_json(marker_path, identity)

                while True:
                    revisions = [
                        revision
                        for entry in order.iterdir()
                        if (revision := self._revision(entry.name)) is not None
                        and entry.is_dir()
                        and not entry.is_symlink()
                    ]
                    published_directory = order / str(max(revisions, default=0) + 1)
                    try:
                        staging.rename(published_directory)
                        sync_directory(order)
                        break
                    except FileExistsError:
                        continue

            source, preview = self._publication_paths(
                published_directory, identity["filename"]
            )
            source, preview, preview_sha256 = self._validate_publication(
                artifact, order, source, preview
            )
            plan.update(
                {
                    "published_path": str(preview),
                    "published_source_path": str(source),
                    "preview_sha256": preview_sha256,
                }
            )
            atomic_json(workspace.publication_path, plan)
            (published_directory / self.marker_name).unlink(missing_ok=True)
            sync_directory(published_directory)
            return self._with_publication(artifact, source, preview, preview_sha256)
        except WorkerError:
            raise
        except PermissionError:
            raise WorkerError(
                "FILE_ACCESS_DENIED", "Order directory cannot be written"
            ) from None
        except OSError:
            raise WorkerError(
                "SOURCE_STORAGE_UNAVAILABLE",
                "Source storage became unavailable while saving the proof",
            ) from None

    @classmethod
    def result_for_core(
        cls, artifact: ProofArtifact, allowed_roots: list[Path]
    ) -> ProofArtifact:
        try:
            metadata = artifact.metadata
            order_value = metadata.get("source_order_path")
            source_value = metadata.get("published_source_path")
            preview_value = metadata.get("published_preview_path")
            expected_sha256 = metadata.get("published_preview_sha256")
            if not all(
                isinstance(value, str) and value
                for value in (order_value, source_value, preview_value, expected_sha256)
            ):
                raise WorkerError("JOB_STATE_ERROR", "Published preview metadata is incomplete")
            roots = [Path(root).resolve() for root in allowed_roots]
            order = cls._validate_order_path(Path(order_value), roots)
            _source, preview, preview_sha256 = cls._validate_publication(
                artifact,
                order,
                Path(source_value),
                Path(preview_value),
                expected_sha256,
            )
            return ProofArtifact(
                path=preview,
                sha256=preview_sha256,
                size_bytes=preview.stat().st_size,
                metadata=metadata,
            )
        except WorkerError:
            raise
        except (OSError, ValidationError, ValueError):
            raise WorkerError(
                "JOB_STATE_ERROR", "Published preview is unavailable or invalid"
            ) from None

    @classmethod
    def _with_publication(
        cls,
        artifact: ProofArtifact,
        source: Path,
        preview: Path,
        preview_sha256: str,
    ) -> ProofArtifact:
        metadata = {
            **artifact.metadata,
            "published_path": str(preview),
            "published_source_path": str(source),
            "published_preview_path": str(preview),
            "published_revision": int(preview.parent.parent.name),
            "published_filename": preview.name,
            "published_preview_sha256": preview_sha256,
            "published_preview_size_bytes": preview.stat().st_size,
        }
        try:
            return artifact.model_copy(update={"metadata": metadata})
        except ValidationError:
            raise WorkerError(
                "JOB_STATE_ERROR", "Published Result metadata is invalid"
            ) from None
