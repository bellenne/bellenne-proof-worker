"""Optional real-Core TCP integration, with copied source and a disposable DB.

Set PROOF_CORE_SOURCE to a Core checkout and install its requirements into
dev-data/core-venv, or point PROOF_CORE_PYTHON at another isolated interpreter.
No live Core credentials, .env, production data or external service are used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

from app.api.proof_core_client import ProofCoreClient
from app.core.config import Settings
from app.core.errors import WorkerError
from app.models.preset import Preset
from app.storage.state import LocalState
from app.worker.heartbeat import heartbeat_loop
from app.worker.service import WorkerService


pytestmark = pytest.mark.skipif(not os.environ.get("PROOF_CORE_SOURCE"), reason="PROOF_CORE_SOURCE not set")
PROJECT = Path(__file__).resolve().parents[1]


def snapshot(database: Path, job_id: str) -> dict:
    # Test assertion only: WorkerService has no access to the server database.
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        job = dict(connection.execute("SELECT * FROM proof_jobs WHERE id=?", (job_id,)).fetchone())
        results = [dict(row) for row in connection.execute(
            "SELECT * FROM proof_results WHERE job_id=?", (job_id,)
        )]
        worker = dict(connection.execute("SELECT * FROM proof_workers WHERE id=?", (job["worker_id"],)).fetchone())
        events = [row[0] for row in connection.execute("SELECT event_type FROM proof_events WHERE job_id=?", (job_id,))]
    return {"job": job, "results": results, "worker": worker, "events": events}


@pytest.fixture
async def isolated_core(tmp_path):
    source = Path(os.environ["PROOF_CORE_SOURCE"]).resolve()
    if not (source / "app" / "main.py").is_file():
        pytest.fail("PROOF_CORE_SOURCE must point to the Proof Core module containing app/main.py")
    default_python = PROJECT / "dev-data" / "core-venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    interpreter = Path(os.environ.get("PROOF_CORE_PYTHON", str(default_python))).resolve()
    if not interpreter.is_file():
        pytest.fail("Install Core requirements into dev-data/core-venv or set PROOF_CORE_PYTHON; see docs/core-contract.md")

    server = tmp_path / "isolated-core"
    server.mkdir()
    shutil.copytree(source / "app", server / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    helper = PROJECT / "tests" / "integration" / "bootstrap_core.py"
    shutil.copyfile(helper, server / "bootstrap_core.py")
    server_data = server / "test-data"
    server_data.mkdir()
    database = server_data / "core.sqlite3"
    connection_file = server / "connection.json"
    preset = Preset(
        proof_width_mm=128, proof_height_mm=64, output_dpi=25.4,
        thumbnail_max_side_mm=20, thumbnail_left_offset_mm=10,
        analysis_preview_max_side_px=128,
    )
    (server / "fixture.json").write_text(json.dumps({
        "preset": preset.model_dump(mode="json"),
        "input": {
            "source_path": "synthetic-order", "layout_number": 3,
            "public_id": "synthetic-proof",
        },
    }), encoding="utf-8")
    # Whitelist basic OS variables, replacing every Core setting and Python path.
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL",
    }}
    env.update({
        "PYTHONPATH": str(server), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
        "DATA_DIR": str(server_data), "DATABASE_URL": f"sqlite:///{database.as_posix()}",
        "PROOF_RESULT_DIR": str(server_data / "results"), "MODULE_PREFIX": "",
        "APP_SECRET_KEY": "synthetic-test-secret-not-for-production", "CREDENTIALS_ENCRYPTION_KEY": "",
        "PROOF_API_DOCS_ENABLED": "false",
    })
    server_log = server / "server.log"
    with server_log.open("wb") as log:
        process = subprocess.Popen(
            [str(interpreter), "-B", "bootstrap_core.py", "--fixture", "fixture.json",
             "--connection", str(connection_file)],
            cwd=server, env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            async with httpx.AsyncClient(timeout=0.5, trust_env=False) as probe:
                for _ in range(200):
                    if process.poll() is not None:
                        pytest.fail(f"Isolated Core exited unexpectedly; inspect {server_log}")
                    if connection_file.is_file():
                        try:
                            identity = json.loads(connection_file.read_text(encoding="utf-8"))
                            base = f"http://127.0.0.1:{identity['port']}"
                            if (await probe.get(base + "/healthz")).status_code == 200:
                                break
                        except (ValueError, httpx.RequestError):
                            pass
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail(f"Isolated Core did not become ready; inspect {server_log}")
            yield {**identity, "url": base, "database": database, "result_root": server_data / "results"}
        finally:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)


class LoseFirstUploadAcknowledgement(ProofCoreClient):
    """Inject a lost response only after the real server has committed the file."""

    async def upload_result(self, *args, **kwargs):
        await super().upload_result(*args, **kwargs)
        raise WorkerError("UPLOAD_ERROR", "Synthetic response loss after real Core commit",
                          {"ambiguous": True, "operation": "result"}, retryable=True)


async def run_with_heartbeat(service):
    heartbeat = asyncio.create_task(heartbeat_loop(service))
    try:
        return await asyncio.wait_for(service.run_once(), timeout=30)
    finally:
        service.stop.set()
        await asyncio.wait_for(heartbeat, timeout=5)


async def test_live_core_processing_upload_restart_and_completion(isolated_core, tmp_path):
    core = isolated_core
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "synthetic-order" / "3" / "Исходник" / "Макет 3 synthetic-artwork.tif"
    source.parent.mkdir(parents=True)
    artwork = Image.new("RGB", (384, 192), (230, 190, 70))
    drawing = ImageDraw.Draw(artwork)
    for x in range(0, 384, 16):
        drawing.rectangle((x, 0, x + 7, 191), fill=(x % 255, 70, 180))
    drawing.ellipse((135, 45, 250, 150), fill=(20, 200, 180), outline=(0, 0, 0), width=4)
    artwork.save(source)
    settings = Settings(
        _env_file=None, proof_core_url=core["url"], proof_worker_token=core["token"],
        proof_worker_name="Synthetic integration Worker", worker_data_path=tmp_path / "worker-data",
        worker_output_path=tmp_path / "worker-output", worker_source_roots=[sources],
        heartbeat_interval=0.1, poll_interval=0.1, http_timeout=5,
    )
    journal = settings.worker_data_path / "state.sqlite3"
    state = LocalState(journal)
    try:
        async with LoseFirstUploadAcknowledgement(core["url"], core["token"], timeout=5) as client:
            service = WorkerService(settings, state, client)
            with pytest.raises(WorkerError, match="Synthetic response loss"):
                await run_with_heartbeat(service)
        record = state.get(core["job_id"], 1)
        assert record["stage"] == "UPLOADING"
        before_restart = snapshot(core["database"], core["job_id"])
        assert before_restart["job"]["processing_status"] == "running"
        assert len(before_restart["results"]) == 1
        assert before_restart["worker"]["hostname"]
    finally:
        state.close()

    # Reopen durable state with a new service/client, after withdrawing source.
    # The second run must recover the committed Result without re-rendering.
    source.rename(tmp_path / "source-withdrawn.tif")
    state = LocalState(journal)
    try:
        async with ProofCoreClient(core["url"], core["token"], timeout=5) as client:
            restarted = WorkerService(settings, state, client)
            assert await run_with_heartbeat(restarted)
            assert await client.claim() is None
        record = state.get(core["job_id"], 1)
        assert record["stage"] == "DONE"
        artifact = record["artifact"]
    finally:
        state.close()

    actual = snapshot(core["database"], core["job_id"])
    assert actual["job"]["processing_status"] == "completed"
    assert actual["job"]["progress"] == 100
    assert actual["job"]["delivery_status"] == "failed"  # Disabled CRM is independent of processing.
    assert actual["worker"]["current_job_id"] is None
    assert len(actual["results"]) == 1
    result = actual["results"][0]
    assert result["id"] == before_restart["results"][0]["id"]
    local_bytes = Path(artifact["path"]).read_bytes()
    stored_bytes = (core["result_root"] / result["file_path"]).read_bytes()
    assert local_bytes == stored_bytes
    assert result["sha256"] == artifact["sha256"] == hashlib.sha256(stored_bytes).hexdigest()
    assert result["idempotency_key"] == ProofCoreClient.result_key(core["job_id"], 1, result["sha256"])
    with Image.open(Path(artifact["path"])) as output:
        assert output.mode == "RGB"
        assert output.size == (128, 64)
    assert "processing.progress" in actual["events"]
    assert actual["events"].count("result.created") == 1
