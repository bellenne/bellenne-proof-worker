from __future__ import annotations

import hashlib
import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest

from app.api.dto import JobDTO
from app.api.proof_core_client import ProofCoreClient
from app.core.errors import WorkerError

TOKEN = "proof_worker_test_secret"
BASE = "https://core.example/proof/"
JOB_ID = "bf6f62de-4a6c-4d8e-b9c3-3930e2a6cb66"


def job_payload(status="assigned", **changes):
    # Mirrors apps/proof/app/main.py:job_payload, including preset snapshot.
    return {
        "id": JOB_ID, "source": "amocrm", "external_event_id": "evt-1",
        "crm_entity_type": "leads", "crm_entity_id": "9", "crm_order_id": "order-12",
        "processing_status": status, "delivery_status": "pending", "attempt": 1,
        "progress": None, "current_stage": "",
        "input": {"source_path": "2026/Заказ 12", "layout_number": 3},
        "preset": {"id": "preset-rgb", "name": "Default", "version": 3, "parameters": {}},
        "created_at": "2026-09-07T11:00:00", "queued_at": None, "assigned_at": None,
        "started_at": None, "completed_at": None, **changes,
    }


def worker_payload():
    # Core's token identifies the registered worker, including its name.
    return {
        "id": "worker-1", "name": "Registered name", "hostname": "press-1",
        "online": True, "availability": "busy", "current_job_id": JOB_ID,
        "version": "0.1.0", "last_heartbeat_at": "2026-09-07T11:00:00",
        "heartbeat_timeout_seconds": 60, "capabilities": ["rgb"],
    }


def multipart(request):
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
    )
    return {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}


async def test_v1_routes_headers_payloads_and_preset_conversion():
    seen = []

    def handle(request):
        seen.append(request)
        assert request.method == "POST"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.path.startswith("/proof/api/v1/")
        action = request.url.path.rsplit("/", 1)[1]
        if action == "heartbeat":
            return httpx.Response(200, json=worker_payload())
        if action == "claim":
            return httpx.Response(200, json=job_payload())
        if action == "progress":
            return httpx.Response(200, json={"job_id": JOB_ID, **json.loads(request.content)})
        if action == "events":
            return httpx.Response(200, json={"status": "recorded"})
        state = {"start": "running", "complete": "completed", "fail": "failed"}[action]
        return httpx.Response(200, json=job_payload(state))

    async with ProofCoreClient(BASE, TOKEN, transport=httpx.MockTransport(handle)) as client:
        worker = await client.heartbeat(
            hostname="press-1", version="0.1.0", state="BUSY", current_job_id=JOB_ID,
            capabilities=["rgb"], worker_name="Local configured name",
        )
        assert worker.name == "Registered name"
        claimed = await client.claim()
        domain = claimed.to_domain()
        assert domain.job_id == JOB_ID
        assert domain.source_path == "2026/Заказ 12"
        assert domain.layout_number == 3
        assert domain.order_number == "order-12"
        assert domain.preset.proof_width_mm == 600
        assert claimed.preset.version == 3
        assert (await client.start(JOB_ID)).processing_status == "running"
        await client.progress(JOB_ID, 65, "RENDERING")
        await client.event(JOB_ID, "render.started", "Rendering", details={"width": 1701})
        assert (await client.complete(JOB_ID)).processing_status == "completed"
        assert (await client.fail(JOB_ID, "FILE_NOT_FOUND", "Source absent")).processing_status == "failed"

    body = json.loads(seen[0].content)
    assert body == {
        "hostname": "press-1", "version": "0.1.0", "availability": "busy",
        "capabilities": ["rgb"], "current_job_id": JOB_ID,
        "last_error_code": None, "last_error_message": None,
    }
    assert json.loads(seen[3].content) == {"progress": 65, "current_stage": "RENDERING"}
    assert seen[1].content == b""
    assert seen[2].content == b""


async def test_empty_queue_and_current_running_assignment():
    replies = iter([httpx.Response(204), httpx.Response(200, json=job_payload("running"))])
    async with ProofCoreClient(BASE, TOKEN, transport=httpx.MockTransport(lambda _: next(replies))) as client:
        assert await client.claim() is None
        assert (await client.claim()).processing_status == "running"


@pytest.mark.parametrize("status,code,retryable", [
    (301, "INVALID_CONFIG", False), (400, "INVALID_CONFIG", False),
    (401, "INVALID_CONFIG", False), (403, "INVALID_CONFIG", False),
    (404, "JOB_STATE_ERROR", False), (409, "JOB_STATE_ERROR", False),
    (422, "INVALID_CONFIG", False), (429, "CORE_UNAVAILABLE", True),
    (503, "CORE_UNAVAILABLE", True),
])
async def test_http_errors_redact_response_and_never_retry(status, code, retryable):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://other.example"}, json={"detail": TOKEN})

    async with ProofCoreClient(BASE, TOKEN, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(WorkerError) as caught:
            await client.claim()
    assert len(requests) == 1
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert caught.value.details["http_status"] == status
    assert TOKEN not in repr(caught.value.as_dict())


async def test_transport_failure_is_ambiguous_and_safe():
    def handle(request):
        raise httpx.ReadTimeout(f"token {TOKEN}", request=request)

    async with ProofCoreClient(BASE, TOKEN, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(WorkerError) as caught:
            await client.claim()
    assert caught.value.code == "CORE_UNAVAILABLE"
    assert caught.value.retryable
    assert caught.value.details == {"operation": "claim", "ambiguous": True}
    assert TOKEN not in repr(caught.value.as_dict())
    assert caught.value.__suppress_context__


async def test_response_decoding_failure_is_ambiguous_and_safe():
    def handle(request):
        raise httpx.DecodingError(TOKEN, request=request)

    async with ProofCoreClient(BASE, TOKEN, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(WorkerError) as caught:
            await client.claim()
    assert caught.value.code == "CORE_UNAVAILABLE"
    assert caught.value.details["ambiguous"]
    assert TOKEN not in repr(caught.value.as_dict())


@pytest.mark.parametrize("reply", [
    httpx.Response(200, content=b"not-json"), httpx.Response(200, json=[]),
    httpx.Response(200, json={"secret": TOKEN}),
])
async def test_invalid_success_acknowledgement_is_ambiguous(reply):
    async with ProofCoreClient(BASE, TOKEN, transport=httpx.MockTransport(lambda _: reply)) as client:
        with pytest.raises(WorkerError) as caught:
            await client.claim()
    assert caught.value.retryable
    assert caught.value.details["ambiguous"] is True
    assert TOKEN not in repr(caught.value.as_dict())


async def test_events_redact_nested_secrets_before_transmission():
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "recorded"})

    async with ProofCoreClient(BASE, TOKEN, transport=httpx.MockTransport(handle)) as client:
        await client.event(
            JOB_ID, "warning", f"Dependency echoed {TOKEN} and proof_hook_other_secret",
            details={"Authorization": "Bearer different-value", "nested": [{"api_key": "another"}],
                     "keep": "useful", "echo": TOKEN},
        )
    assert bodies[0]["details"]["keep"] == "useful"
    assert bodies[0]["details"]["nested"] == [{"api_key": "[REDACTED]"}]
    assert TOKEN not in repr(bodies)
    assert "different-value" not in repr(bodies)
    assert "proof_hook_other_secret" not in repr(bodies)


async def test_result_timeout_replay_uses_same_key_bytes_and_metadata(tmp_path):
    content = b"saved-jpeg-output"
    path = tmp_path / "proof.jpg"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            # The server may already have committed this result.
            raise httpx.ReadTimeout(TOKEN, request=request)
        return httpx.Response(200, json={"result_id": "result-1", "duplicate": True, "sha256": digest})

    async with ProofCoreClient(BASE, TOKEN, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(WorkerError) as caught:
            await client.upload_result(JOB_ID, path, digest, 2, {"token": "secret"})
        assert caught.value.code == "UPLOAD_ERROR"
        assert caught.value.details["ambiguous"]
        assert len(requests) == 1
        ack = await client.upload_result(JOB_ID, path, digest, 2, {"token": "secret"})
    assert ack.duplicate
    keys = [request.headers["Idempotency-Key"] for request in requests]
    assert keys == [ProofCoreClient.result_key(JOB_ID, 2, digest)] * 2
    assert keys[0] != ProofCoreClient.result_key(JOB_ID, 3, digest)
    for request in requests:
        assert request.url.path == f"/proof/api/v1/jobs/{JOB_ID}/result"
        parts = multipart(request)
        assert parts["file"].get_payload(decode=True) == content
        assert parts["file"].get_filename() == "proof.jpg"
        assert parts["file"].get_content_type() == "image/jpeg"
        assert json.loads(parts["metadata_json"].get_payload(decode=True)) == {
            "sha256": digest, "attempt": 2, "token": "[REDACTED]",
        }


async def test_changed_saved_file_is_never_uploaded(tmp_path):
    path = tmp_path / "proof.jpg"
    path.write_bytes(b"different result")
    requests = []
    async with ProofCoreClient(BASE, TOKEN, transport=httpx.MockTransport(requests.append)) as client:
        with pytest.raises(WorkerError, match="checksum changed") as caught:
            await client.upload_result(JOB_ID, path, "0" * 64, 1)
    assert not caught.value.retryable
    assert caught.value.code == "JOB_STATE_ERROR"
    assert requests == []


async def test_result_ack_checksum_must_match(tmp_path):
    path = tmp_path / "proof.jpg"
    path.write_bytes(b"result")
    digest = hashlib.sha256(b"result").hexdigest()
    transport = httpx.MockTransport(lambda _: httpx.Response(201, json={
        "result_id": "result-1", "duplicate": False, "sha256": "0" * 64,
    }))
    async with ProofCoreClient(BASE, TOKEN, transport=transport) as client:
        with pytest.raises(WorkerError, match="checksum does not match") as caught:
            await client.upload_result(JOB_ID, path, digest, 1)
    assert not caught.value.retryable


@pytest.mark.parametrize("job_id", ["../old", "a/b", "a?token=secret", "a#b", "", "a" * 129])
async def test_job_identifier_cannot_escape_route(job_id):
    async with ProofCoreClient(BASE, TOKEN) as client:
        with pytest.raises(WorkerError, match="identifier"):
            await client.start(job_id)


@pytest.mark.parametrize("url,token,timeout", [
    ("file:///secret", TOKEN, 30), ("https://user:password@core.example", TOKEN, 30),
    (BASE + "?token=secret", TOKEN, 30), (BASE, "bad\r\nHeader: value", 30),
    (BASE, "кириллица", 30), (BASE, "", 30), (BASE, TOKEN, 0), (BASE, TOKEN, float("nan")),
])
def test_invalid_connection_config_is_safe(url, token, timeout):
    with pytest.raises(WorkerError) as caught:
        ProofCoreClient(url, token, timeout)
    assert caught.value.code == "INVALID_CONFIG"
    assert "password" not in str(caught.value)


@pytest.mark.parametrize("operation,payload", [
    ("start", job_payload("running", id="another-job")),
    ("start", job_payload("assigned")), ("claim", job_payload("completed")),
    ("complete", job_payload("running")), ("fail", job_payload("running")),
])
async def test_job_acknowledgement_identity_and_state(operation, payload):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    async with ProofCoreClient(BASE, TOKEN, transport=transport) as client:
        with pytest.raises(WorkerError) as caught:
            if operation == "claim":
                await client.claim()
            elif operation == "fail":
                await client.fail(JOB_ID, "FILE_NOT_FOUND", "Source absent")
            else:
                await getattr(client, operation)(JOB_ID)
    assert caught.value.code == "JOB_STATE_ERROR"
    assert not caught.value.retryable


def test_invalid_job_contract_does_not_echo_inputs():
    payload = job_payload(input={
        "source_path": "2026/Заказ 12", "layout_number": 3, "metadata": TOKEN,
    })
    with pytest.raises(WorkerError) as caught:
        JobDTO.model_validate(payload).to_domain()
    assert caught.value.code == "INVALID_CONFIG"
    assert TOKEN not in repr(caught.value.as_dict())
