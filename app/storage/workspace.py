from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, allow_nan=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


class Workspace:
    def __init__(self, data_root: Path, output_root: Path, job_id: str, attempt: int):
        key = hashlib.sha256(job_id.encode()).hexdigest()[:32]
        self.path = data_root.resolve() / "jobs" / key / str(attempt)
        self.output_dir = output_root.resolve() / key / str(attempt)
        self.path.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.result_path = self.output_dir / "result.jpg"
        self.archive_path = self.output_dir / "result.zip"
        self.manifest_path = self.path / "artifact.json"
        self.publication_path = self.path / "publication.json"

    def result_path_for(self, layout_number: int) -> Path:
        return self.output_dir / f"result-{layout_number}.jpg"

    def result_path_for_item(self, position: int, layout_number: int) -> Path:
        return self.output_dir / f"result-{position + 1}-{layout_number}.jpg"
