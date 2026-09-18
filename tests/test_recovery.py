from __future__ import annotations

import asyncio
import json
import zipfile
from types import SimpleNamespace

import numpy as np
import pyvips
import pytest

from app.api.dto import JobDTO, ResultAck
from app.core.config import Settings
from app.core.errors import WorkerError
from app.files.publisher import OrderPublisher
from app.models.result import ProofArtifact
from app.models.preset import Preset
from app.models.job import ProofItem
from app.recovery.recovery_manager import RecoveryManager
from app.storage.state import LocalState
from app.storage.workspace import Workspace, atomic_json, file_sha256
from app.worker.service import WorkerService


@pytest.mark.parametrize("variant", ["fragment_60x30", "thumbnail", "fragment_90x30"])
async def test_processing_uses_local_copy_and_publishes_to_original_order(settings, monkeypatch, variant):
    core = FakeCore()
    job = core.job.to_domain().model_copy(update={
        "proof_variant": variant,
        "preset": Preset(proof_width_mm=128, proof_height_mm=64, output_dpi=25.4,
                         thumbnail_max_side_mm=20, thumbnail_left_offset_mm=10,
                         analysis_preview_max_side_px=128),
    })
    if variant == "fragment_90x30":
        job = job.model_copy(update={"items": [ProofItem(
            id="item-1", position=0, layout_number=3, proof_variant=variant,
            fragments=[
                {"id": "a", "position": 0, "proof_variant": "fragment_30x30"},
                {"id": "b", "position": 1, "proof_variant": "fragment_30x30_color",
                 "brightness_direction": "add", "brightness_percent": 10},
                {"id": "c", "position": 2, "proof_variant": "fragment_30x30_color",
                 "brightness_direction": "subtract", "brightness_percent": 10},
            ],
        )]})
    source = settings.worker_source_roots[0] / "order" / "1 Иванов" / "Исходник" / "Макет 3.tif"
    source.parent.mkdir(parents=True)
    pixels = np.random.default_rng(5).integers(0, 256, (256, 256, 3), dtype=np.uint8)
    pyvips.Image.new_from_memory(pixels.tobytes(), 256, 256, 3, "uchar").copy(
        interpretation="srgb"
    ).tiffsave(str(source))
    original_digest = file_sha256(source)
    state = LocalState(settings.worker_data_path / "state.db")
    state.claim(core.job.model_dump(mode="json"))
    runner = WorkerService(settings, state, core).runner
    visited = []

    def assert_local(function):
        def wrapped(path, *args, **kwargs):
            assert path.is_relative_to(settings.worker_data_path)
            assert file_sha256(path) == original_digest
            assert path != source
            visited.append(path)
            return function(path, *args, **kwargs)
        return wrapped

    for owner, method in [(runner.validator, "validate"), (runner.preview, "generate"),
                          (runner.renderer, "render_variant"), (runner.renderer, "render_thumbnail")]:
        monkeypatch.setattr(owner, method, assert_local(getattr(owner, method)))
    try:
        artifact = await runner.run(job)
        assert len(visited) == (2 if variant == "thumbnail" else 3)
        assert len(set(visited)) == 1
        assert not visited[0].exists()
        assert file_sha256(source) == original_digest
        assert artifact.metadata["published_revision"] == 2
        assert list((source.parents[2] / "2" / "Исходник").glob("*.jpg"))
        assert list((source.parents[2] / "2" / "Превью").glob("*.jpg"))
        assert core.uploads
    finally:
        state.close()


def payload(attempt=1, source_path="order", layout_number=3, job_id="job-1"):
    return {
        "id": job_id, "attempt": attempt, "processing_status": "assigned",
        "input": {"source_path": source_path, "layout_number": layout_number},
        "preset": {"parameters": {}},
    }


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, proof_core_url="http://core", proof_worker_token="test-token",
                    proof_worker_name="test", worker_data_path=tmp_path / "data",
                    worker_output_path=tmp_path / "output", worker_source_roots=[tmp_path / "sources"],
                    poll_interval=.01, heartbeat_interval=.02, health_interval=.02)


class FakeCore:
    def __init__(self):
        self.job = JobDTO.model_validate(payload())
        self.uploads = []
        self.accepted = None
        self.upload_timeout = False
        self.complete_timeout = False
        self.fail_timeout = False
        self.failures = []
        self.heartbeat_count = 0

    async def claim(self):
        return self.job

    async def start(self, job_id):
        self.job = self.job.model_copy(update={"processing_status": "running"})
        return self.job

    async def progress(self, *args):
        pass

    async def event(self, *args, **kwargs):
        pass

    async def upload_result(self, job_id, path, sha256, attempt, metadata):
        self.uploads.append(sha256)
        duplicate = self.accepted is not None
        self.accepted = sha256
        if self.upload_timeout:
            self.upload_timeout = False
            raise WorkerError("UPLOAD_ERROR", "timeout after server accepted", {"ambiguous": True})
        return ResultAck(result_id="result-1", duplicate=duplicate, sha256=sha256)

    async def complete(self, job_id):
        completed = self.job.model_copy(update={"processing_status": "completed"})
        self.job = None
        if self.complete_timeout:
            self.complete_timeout = False
            raise WorkerError("CORE_UNAVAILABLE", "timeout after complete", {"ambiguous": True})
        return completed

    async def fail(self, job_id, code, message, details):
        self.failures.append(code)
        failed = self.job.model_copy(update={"processing_status": "failed"})
        if self.fail_timeout:
            self.fail_timeout = False
            raise WorkerError("CORE_UNAVAILABLE", "fail unavailable", {"ambiguous": True})
        self.job = None
        return failed

    async def heartbeat(self, **kwargs):
        self.heartbeat_count += 1
        return SimpleNamespace(heartbeat_timeout_seconds=120)


def write_artifact(settings, dto, stage="READY_TO_UPLOAD", manifest=True):
    job = dto.to_domain()
    workspace = Workspace(settings.worker_data_path, settings.worker_output_path, job.job_id, job.attempt)
    rgb = np.zeros((job.preset.height_px, job.preset.width_px, 3), dtype=np.uint8)
    rgb[:, :, 0] = 180
    image = pyvips.Image.new_from_memory(rgb.tobytes(), rgb.shape[1], rgb.shape[0], 3, "uchar")
    image.copy(interpretation="srgb", xres=72 / 25.4, yres=72 / 25.4).jpegsave(str(workspace.result_path), Q=95)
    artifact = ProofArtifact(
        path=workspace.result_path,
        sha256=file_sha256(workspace.result_path),
        size_bytes=workspace.result_path.stat().st_size,
        metadata={"test": True},
    )
    order = settings.worker_source_roots[0] / "order"
    (order / "3").mkdir(parents=True)
    artifact = artifact.model_copy(
        update={
            "metadata": {
                **artifact.metadata,
                "source_order_path": str(order),
            }
        }
    )
    artifact = OrderPublisher().publish(
        job, workspace, artifact, order, settings.worker_source_roots
    )
    if manifest:
        atomic_json(workspace.manifest_path, artifact.model_dump(mode="json"))
    atomic_json(
        workspace.path / "render-plan.json",
        {"job_id": job.job_id, "attempt": job.attempt, "metadata": artifact.metadata},
    )
    return workspace, artifact


def test_recovery_accepts_committed_preview_archive(settings):
    dto = JobDTO.model_validate(payload())
    job = dto.to_domain()
    workspace = Workspace(
        settings.worker_data_path,
        settings.worker_output_path,
        job.job_id,
        job.attempt,
    )
    with zipfile.ZipFile(workspace.archive_path, "w") as archive:
        archive.writestr("ЦП Макет 3 60х30.jpg", b"preview")
    artifact = ProofArtifact(
        path=workspace.archive_path,
        sha256=file_sha256(workspace.archive_path),
        size_bytes=workspace.archive_path.stat().st_size,
        metadata={"result_kind": "preview_archive", "published_revision": 4},
    )
    atomic_json(workspace.manifest_path, artifact.model_dump(mode="json"))

    recovered = RecoveryManager().recover_artifact(
        job,
        workspace,
        {"stage": "READY_TO_UPLOAD", "upload_intent": False, "artifact": None},
    )

    assert recovered == artifact


@pytest.mark.parametrize("stage", ["RENDERING", "SAVING", "READY_TO_UPLOAD", "UPLOADING", "COMPLETING"])
async def test_saved_result_recovery_skips_source(settings, stage):
    core = FakeCore()
    state = LocalState(settings.worker_data_path / "state.db")
    state.claim(core.job.model_dump(mode="json"))
    workspace, artifact = write_artifact(settings, core.job)
    state.update(core.job.id, 1, stage=stage, artifact=artifact.model_dump(mode="json"))
    service = WorkerService(settings, state, core)
    # There is deliberately no source storage mounted after restart.
    assert await service.run_once()
    assert core.uploads == [artifact.metadata["published_preview_sha256"]]
    assert state.get("job-1", 1)["stage"] == "DONE"
    assert workspace.result_path.is_file()
    state.close()


async def test_crash_after_atomic_save_before_manifest(settings):
    core = FakeCore()
    state = LocalState(settings.worker_data_path / "state.db")
    state.claim(core.job.model_dump(mode="json"))
    workspace, artifact = write_artifact(settings, core.job, manifest=False)
    state.update("job-1", 1, stage="RENDERING")
    await WorkerService(settings, state, core).run_once()
    assert core.uploads == [artifact.metadata["published_preview_sha256"]]
    assert json.loads(workspace.manifest_path.read_text(encoding="utf-8"))["metadata"][
        "recovered_after_save"
    ]
    state.close()


async def test_upload_timeout_after_accept_reuses_identical_result(settings):
    core = FakeCore()
    state = LocalState(settings.worker_data_path / "state.db")
    state.claim(core.job.model_dump(mode="json"))
    workspace, artifact = write_artifact(settings, core.job)
    core.upload_timeout = True
    with pytest.raises(WorkerError):
        await WorkerService(settings, state, core).run_once()
    assert state.get("job-1", 1)["stage"] == "UPLOADING"
    state.close()
    state = LocalState(settings.worker_data_path / "state.db")
    await WorkerService(settings, state, core).run_once()
    assert core.uploads == [artifact.metadata["published_preview_sha256"]] * 2
    assert state.get("job-1", 1)["stage"] == "DONE"
    assert workspace.result_path.is_file()
    state.close()


async def test_completion_timeout_never_mutates_unassigned_job(settings):
    core = FakeCore()
    state = LocalState(settings.worker_data_path / "state.db")
    state.claim(core.job.model_dump(mode="json"))
    workspace, _ = write_artifact(settings, core.job)
    core.complete_timeout = True
    service = WorkerService(settings, state, core)
    with pytest.raises(WorkerError):
        await service.run_once()
    assert state.get("job-1", 1)["stage"] == "COMPLETING"
    assert not await service.run_once()
    assert state.get("job-1", 1)["stage"] == "DETACHED"
    assert workspace.result_path.is_file()
    state.close()


async def test_new_attempt_never_reuses_old_artifact(settings):
    core = FakeCore()
    state = LocalState(settings.worker_data_path / "state.db")
    state.claim(core.job.model_dump(mode="json"))
    workspace, _ = write_artifact(settings, core.job)
    core.job = JobDTO.model_validate(payload(attempt=2))
    service = WorkerService(settings, state, core)
    settings.storage_retry_limit = 0
    await service.run_once()
    assert not core.uploads
    assert state.get("job-1", 1)["stage"] == "DETACHED"
    assert state.get("job-1", 2)["stage"] == "FAILED"
    assert workspace.result_path.exists()
    state.close()


@pytest.mark.parametrize("stage", ["SEARCHING_FILE", "ANALYZING", "RENDERING"])
async def test_crash_before_result_restarts_processing(settings, stage):
    core = FakeCore()
    state = LocalState(settings.worker_data_path / "state.db")
    state.claim(core.job.model_dump(mode="json"))
    state.update("job-1", 1, stage=stage)
    service = WorkerService(settings, state, core)
    called = []
    async def process(job, workspace):
        called.append(True)
        return write_artifact(settings, core.job)[1]
    service.runner.process = process
    await service.run_once()
    assert called == [True] and state.get("job-1", 1)["stage"] == "DONE"
    state.close()


def test_corrupt_after_ambiguous_upload_is_retained(settings):
    core = FakeCore()
    workspace, artifact = write_artifact(settings, core.job)
    workspace.result_path.write_bytes(b"damaged")
    record = {"stage": "UPLOADING", "artifact": artifact.model_dump(mode="json")}
    with pytest.raises(WorkerError) as exc:
        RecoveryManager().recover_artifact(core.job.to_domain(), workspace, record)
    assert exc.value.code == "JOB_STATE_ERROR"
    assert workspace.result_path.read_bytes() == b"damaged"


async def test_failure_reporting_retries_without_processing(settings):
    core = FakeCore()
    core.job = JobDTO.model_validate(payload(source_path=""))
    core.fail_timeout = True
    state = LocalState(settings.worker_data_path / "state.db")
    service = WorkerService(settings, state, core)
    with pytest.raises(WorkerError):
        await service.run_once()
    assert state.get("job-1", 1)["stage"] == "FAILING"
    await service.run_once()
    assert state.get("job-1", 1)["stage"] == "FAILED"
    assert core.failures == ["INVALID_CONFIG", "INVALID_CONFIG"]
    state.close()


async def test_heartbeat_independent_of_long_processing_and_shutdown(settings):
    core = FakeCore()
    state = LocalState(settings.worker_data_path / "state.db")
    service = WorkerService(settings, state, core)
    async def run(job):
        import time
        await asyncio.to_thread(time.sleep, .15)
        service.stop.set()
    service.runner.run = run
    await service.run()
    assert core.heartbeat_count >= 3
    assert state.get("job-1", 1)["stage"] == "CLAIMED"
    state.close()
