from __future__ import annotations

import os
from pathlib import Path

from app.core.errors import WorkerError


class ProcessLock:
    """One process per persistent recovery volume, including accidental restarts."""

    def __init__(self, path: Path):
        self.path, self.stream = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        try:
            if os.fstat(self.stream.fileno()).st_size == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise WorkerError("INVALID_CONFIG", "Another Worker process owns this recovery volume") from None
        return self

    def __exit__(self, *_):
        if self.stream:
            self.stream.close()


def configure_imaging(data_path: Path, concurrency: int = 2, cache_memory_mb: int = 128):
    import tempfile

    temporary = data_path / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(temporary)
    os.environ["TMP"] = str(temporary)
    os.environ["TEMP"] = str(temporary)
    os.environ["VIPS_CONCURRENCY"] = str(concurrency)
    tempfile.tempdir = str(temporary)
    import pyvips

    pyvips.cache_set_max_mem(cache_memory_mb * 1024 * 1024)
    pyvips.cache_set_max(0)  # Do not retain prior production images between Jobs.
