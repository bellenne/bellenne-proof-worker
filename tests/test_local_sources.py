from types import SimpleNamespace

import pytest

from app.core.errors import WorkerError
from app.files.local_sources import LocalSources


def test_copy_is_independent_reused_and_cleaned(tmp_path):
    source = tmp_path / "Макет 1.tif"
    source.write_bytes(b"source pixels")
    with LocalSources(tmp_path / "job") as cache:
        local = cache.copy(source)
        assert local.name == source.name
        assert local.read_bytes() == source.read_bytes()
        assert cache.copy(source, lambda *_: pytest.fail("Copied twice")) == local
        source.unlink()
        assert local.read_bytes() == b"source pixels"
    assert not cache.directory.exists()


def test_changed_source_is_rejected_and_partial_copy_removed(tmp_path):
    source = tmp_path / "source.tif"
    source.write_bytes(b"original")
    with LocalSources(tmp_path / "job") as cache:
        def change_source(*_):
            source.write_bytes(b"changed!")
            import os
            stat = source.stat()
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        with pytest.raises(WorkerError, match="Source changed"):
            cache.copy(source, change_source)
        assert not list(cache.directory.rglob("*.part"))
        assert not list(cache.directory.rglob("*.tif"))


def test_low_disk_space_fails_before_copy(tmp_path, monkeypatch):
    source = tmp_path / "source.tif"
    source.write_bytes(b"pixels")
    monkeypatch.setattr("app.files.local_sources.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    with LocalSources(tmp_path / "job") as cache:
        with pytest.raises(WorkerError) as error:
            cache.copy(source)
        assert error.value.code == "LOCAL_STORAGE_FULL"
        assert not list(cache.directory.rglob("*.part"))
    assert source.read_bytes() == b"pixels"


def test_failed_job_and_previous_crash_copies_are_cleaned(tmp_path):
    directory = tmp_path / "job" / "source-cache"
    directory.mkdir(parents=True)
    (directory / "leftover.part").write_bytes(b"partial")
    with pytest.raises(RuntimeError):
        with LocalSources(tmp_path / "job") as cache:
            assert list(directory.iterdir()) == []
            (directory / "copy.tif").write_bytes(b"pixels")
            raise RuntimeError("decoder failed")
    assert not cache.directory.exists()
