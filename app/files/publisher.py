from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pydantic import ValidationError

from app.core.errors import WorkerError
from app.models.job import Job
from app.models.result import ProofArtifact
from app.storage.workspace import Workspace, atomic_json, file_sha256, sync_directory


class OrderPublisher:
    """Publish one rendered proof into the next numbered order revision."""

    marker_name = ".proof-worker-publication.json"

    @staticmethod
    def _revision(name: str) -> int | None:
        stripped = name.strip()
        if not stripped.isdecimal():
            return None
        value = int(stripped)
        return value if value > 0 else None

    @staticmethod
    def _dimension(value_mm: float) -> str:
        value_cm = value_mm / 10
        if value_cm.is_integer():
            return str(int(value_cm))
        return f"{value_cm:g}".replace(".", ",")

    @classmethod
    def filename(cls, job: Job) -> str:
        width = cls._dimension(job.preset.proof_width_mm)
        height = cls._dimension(job.preset.proof_height_mm)
        return f"ЦП Макет {job.layout_number} {width}х{height}.jpg"

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
            raise WorkerError("INVALID_CONFIG", "Resolved order directory is outside configured source roots")
        return resolved

    def _matching_published_directory(self, order: Path, identity: dict) -> Path | None:
        for entry in order.iterdir():
            if self._revision(entry.name) is None or not entry.is_dir() or entry.is_symlink():
                continue
            marker = self._load_json(entry / self.marker_name)
            if marker == identity:
                return entry
        return None

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
            if plan is not None and any(plan.get(field) != value for field, value in expected_plan.items()):
                raise WorkerError("JOB_STATE_ERROR", "Saved publication plan does not match this execution")
            if plan is None:
                plan = expected_plan
                atomic_json(workspace.publication_path, plan)

            published_path = plan.get("published_path")
            if published_path:
                destination = Path(published_path)
                resolved_destination = destination.resolve(strict=True)
                if (
                    not resolved_destination.is_file()
                    or not resolved_destination.is_relative_to(order)
                    or resolved_destination.parent.parent != order
                    or file_sha256(resolved_destination) != artifact.sha256
                ):
                    raise WorkerError("JOB_STATE_ERROR", "Published proof no longer matches its journal")
                return self._with_publication(artifact, resolved_destination)

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
                marker_path = staging / self.marker_name
                existing_marker = self._load_json(marker_path)
                if existing_marker not in (None, identity):
                    raise WorkerError("JOB_STATE_ERROR", "Publication staging directory belongs to another execution")
                destination = staging / identity["filename"]
                if destination.is_symlink():
                    raise WorkerError(
                        "JOB_STATE_ERROR", "Publication destination cannot be a symbolic link"
                    )
                if not destination.is_file() or file_sha256(destination) != artifact.sha256:
                    self._copy_verified(artifact.path, destination, artifact.sha256)
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

            destination = published_directory / identity["filename"]
            resolved_destination = destination.resolve(strict=True)
            if (
                not resolved_destination.is_file()
                or resolved_destination.parent != published_directory.resolve()
                or file_sha256(resolved_destination) != artifact.sha256
            ):
                raise WorkerError("JOB_STATE_ERROR", "Published proof does not match the rendered Result")
            plan["published_path"] = str(destination)
            atomic_json(workspace.publication_path, plan)
            (published_directory / self.marker_name).unlink(missing_ok=True)
            sync_directory(published_directory)
            return self._with_publication(artifact, resolved_destination)
        except WorkerError:
            raise
        except PermissionError:
            raise WorkerError("FILE_ACCESS_DENIED", "Order directory cannot be written") from None
        except OSError:
            raise WorkerError(
                "SOURCE_STORAGE_UNAVAILABLE", "Source storage became unavailable while saving the proof"
            ) from None

    @classmethod
    def _with_publication(cls, artifact: ProofArtifact, destination: Path) -> ProofArtifact:
        metadata = {
            **artifact.metadata,
            "published_path": str(destination),
            "published_revision": int(destination.parent.name),
            "published_filename": destination.name,
        }
        try:
            return artifact.model_copy(update={"metadata": metadata})
        except ValidationError:
            raise WorkerError("JOB_STATE_ERROR", "Published Result metadata is invalid") from None
