from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.dto import JobDTO
from app.api.wake import create_wake_app
from app.core.config import Settings
from app.storage.state import LocalState
from app.worker.service import WorkerService


def make_settings(tmp_path) -> Settings:
    source = tmp_path / "source"
    source.mkdir()
    return Settings(
        _env_file=None,
        proof_core_url="http://core",
        proof_worker_token="worker-token",
        proof_worker_name="test-worker",
        worker_data_path=tmp_path / "data",
        worker_output_path=tmp_path / "output",
        worker_source_roots=[source],
    )


def runtime_configuration(source_root) -> dict:
    return {
        "heartbeat_interval": 20,
        "poll_interval": 4,
        "retry_initial_seconds": 2,
        "retry_max_seconds": 40,
        "storage_retry_limit": 7,
        "file_not_found_retry_limit": 1,
        "health_interval": 6,
        "health_max_age": 100,
        "wake_timeout_seconds": 3,
        "path_mappings": [{
            "source_prefix": r"\\10.0.0.8\дизайн отдел",
            "local_root": str(source_root),
        }],
    }


def test_core_configuration_maps_full_unc_order_path(tmp_path) -> None:
    settings = make_settings(tmp_path)
    state = LocalState(settings.worker_data_path / "state.sqlite3")
    try:
        service = WorkerService(settings, state, object())
        service.apply_core_configuration(2, runtime_configuration(settings.worker_source_roots[0]))
        dto = JobDTO.model_validate({
            "id": "job-1",
            "processing_status": "assigned",
            "delivery_status": "pending",
            "attempt": 1,
            "input": {
                "source_path": r"\\10.0.0.8\дизайн отдел\Макеты (опт)\Сентябрь 2026\33860843",
                "layout_number": 3,
            },
            "preset": {"id": "preset", "name": "Proof", "version": 1, "parameters": {}},
        })

        job = service.map_job_source(dto.to_domain())

        assert job.source_path == "Макеты (опт)/Сентябрь 2026/33860843"
        assert job.preset.search.roots == [str(settings.worker_source_roots[0])]
        assert job.metadata["source_path_original"].startswith(r"\\10.0.0.8")
        assert settings.poll_interval == 4
        assert service.configuration_version == 2
    finally:
        state.close()


async def test_claim_applies_core_mapping_before_job_processing(tmp_path) -> None:
    settings = make_settings(tmp_path)
    state = LocalState(settings.worker_data_path / "state.sqlite3")
    calls = []
    dto = JobDTO.model_validate({
        "id": "job-1",
        "processing_status": "assigned",
        "delivery_status": "pending",
        "attempt": 1,
        "input": {
            "source_path": r"\\10.0.0.8\дизайн отдел\Макеты (опт)\Сентябрь 2026\33860843",
            "layout_number": 3,
        },
        "preset": {"id": "preset", "name": "Proof", "version": 1, "parameters": {}},
    })

    class Core:
        async def claim(self):
            calls.append("claim")
            return dto

        async def heartbeat(self, **kwargs):
            calls.append(("heartbeat", kwargs["current_job_id"]))
            return SimpleNamespace(
                configuration_version=2,
                configuration=runtime_configuration(settings.worker_source_roots[0]),
                heartbeat_timeout_seconds=120,
            )

        async def start(self, job_id):
            calls.append(("start", job_id))
            return SimpleNamespace(attempt=1, processing_status="running")

    service = WorkerService(settings, state, Core())

    async def run(job):
        calls.append(("run", job.source_path, job.preset.search.roots))

    service.runner.run = run
    try:
        assert await service.run_once()
    finally:
        state.close()

    assert calls == [
        "claim",
        ("heartbeat", "job-1"),
        ("start", "job-1"),
        ("run", "Макеты (опт)/Сентябрь 2026/33860843", [str(settings.worker_source_roots[0])]),
    ]


def test_signed_wake_interrupts_polling() -> None:
    token = SecretStr("worker-token")

    class WakeEvent:
        called = False

        def set(self) -> None:
            self.called = True

    event = WakeEvent()
    app = create_wake_app(token, event)
    body = json.dumps(
        {"event": "queue.changed", "job_id": "job-1"},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    timestamp = str(int(time.time()))
    key = hashlib.sha256(token.get_secret_value().encode("utf-8")).digest()
    signature = hmac.new(key, timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()

    with TestClient(app) as client:
        response = client.post(
            "/wake",
            content=body,
            headers={"X-Proof-Timestamp": timestamp, "X-Proof-Signature": signature},
        )
        rejected = client.post(
            "/wake",
            content=body,
            headers={"X-Proof-Timestamp": timestamp, "X-Proof-Signature": "0" * 64},
        )

    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert event.called is True
    assert rejected.status_code == 401
