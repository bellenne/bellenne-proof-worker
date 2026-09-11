"""Run only inside the copied Core app, using an isolated test environment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--connection", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))

    # These imports deliberately resolve to the copied SERVER app, not Worker.
    from app.database import init_database
    from app.main import app, engine, session_factory
    from app.models import ProofIntegration
    from app.services import create_job_from_webhook, create_preset, json_dump, register_worker

    init_database(engine)
    with session_factory() as session:
        worker, token = register_worker(session, 1, "Synthetic integration Worker", 60)
        preset = create_preset(session, 1, "Synthetic RGB proof", fixture["preset"])
        # Disabled integration guarantees complete has no CRM delivery call.
        integration = ProofIntegration(
            owner_external_user_id=1, kind="amocrm", enabled=False,
            trigger_events_json=json_dump(["synthetic.created"]), default_preset_id=preset.id,
            delivery_url="", credentials_encrypted="", webhook_secret_digest="unused",
            webhook_secret_prefix="unused", webhook_secret_last_four="used",
        )
        session.add(integration)
        session.commit()
        job, duplicate, accepted = create_job_from_webhook(
            session, integration, idempotency_key="synthetic-event-1",
            event_type="synthetic.created", crm_entity_type="leads", crm_entity_id="synthetic-1",
            crm_order_id="synthetic-order", preset=preset, input_payload=fixture["input"],
            original_payload={"synthetic": True},
        )
        assert accepted and not duplicate and job is not None
        session.commit()
        identity = {"worker_id": worker.id, "job_id": job.id, "token": token}

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", access_log=False)
    bound_socket = config.bind_socket()
    try:
        identity["port"] = bound_socket.getsockname()[1]
        args.connection.write_text(json.dumps(identity), encoding="utf-8")
        uvicorn.Server(config).run(sockets=[bound_socket])
    finally:
        bound_socket.close()


if __name__ == "__main__":
    main()
