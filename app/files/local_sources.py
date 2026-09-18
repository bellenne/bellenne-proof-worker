from __future__ import annotations

import errno
import hashlib
import os
import shutil
from pathlib import Path

from app.core.errors import WorkerError


class LocalSources:
    """Per-job scratch copies; remote originals are never modified."""

    def __init__(self, workspace: Path):
        self.directory = workspace / "source-cache"
        self.copied: dict[Path, tuple[tuple[int, int], Path]] = {}

    def __enter__(self):
        # Also discard incomplete copies left by a terminated process.
        self.cleanup()
        self.directory.mkdir(parents=True)
        return self

    def cleanup(self):
        if self.directory.is_symlink():
            self.directory.unlink()
        elif self.directory.exists():
            shutil.rmtree(self.directory)

    def __exit__(self, *exc):
        self.cleanup()

    def copy(self, source: Path, progress=None) -> Path:
        temporary = None
        try:
            before = source.stat()
            identity = (before.st_size, before.st_mtime_ns)
            cached = self.copied.get(source)
            if cached and cached[0] == identity and cached[1].is_file():
                return cached[1]
            key = hashlib.sha256(str(source).encode()).hexdigest()[:24]
            target = self.directory / key / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(self.directory).free < before.st_size + 64 * 1024 * 1024:
                raise WorkerError("LOCAL_STORAGE_FULL", "Not enough local disk space to copy source",
                                  {"required_bytes": before.st_size})
            temporary = target.with_name(target.name + ".part")
            copied = 0
            with source.open("rb") as reader, temporary.open("wb") as writer:
                while chunk := reader.read(4 * 1024 * 1024):
                    writer.write(chunk)
                    copied += len(chunk)
                    if progress:
                        progress(copied, before.st_size)
                writer.flush()
                os.fsync(writer.fileno())
            after = source.stat()
            if (after.st_size, after.st_mtime_ns) != identity or copied != before.st_size:
                raise WorkerError("SOURCE_STORAGE_UNAVAILABLE", "Source changed while being copied",
                                  retryable=True)
            os.replace(temporary, target)
            self.copied[source] = (identity, target)
            return target
        except OSError as error:
            if error.errno == errno.ENOSPC:
                raise WorkerError("LOCAL_STORAGE_FULL", "Local disk filled while copying source") from error
            raise WorkerError("SOURCE_STORAGE_UNAVAILABLE", "Could not copy source to local storage",
                              {"errno": error.errno}, retryable=True) from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
