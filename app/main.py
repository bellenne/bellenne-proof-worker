from __future__ import annotations

import asyncio
import json
import signal

from pydantic import ValidationError
import uvicorn

from app.api.wake import create_wake_app
from app.core.config import Settings
from app.core.errors import WorkerError
from app.core.lifecycle import ProcessLock, configure_imaging
from app.logging.setup import configure_logging
from app.storage.state import LocalState


async def serve(settings: Settings):
    from app.api.proof_core_client import ProofCoreClient
    from app.worker.service import WorkerService

    state = LocalState(settings.worker_data_path / "state.sqlite3")
    try:
        async with ProofCoreClient(settings.proof_core_url, settings.proof_worker_token, settings.http_timeout) as client:
            service = WorkerService(settings, state, client)
            wake_server = uvicorn.Server(uvicorn.Config(
                create_wake_app(settings.proof_worker_token, service.wake_event),
                host=settings.worker_listen_host,
                port=settings.worker_listen_port,
                log_config=None,
                access_log=False,
            ))
            wake_task = asyncio.create_task(wake_server.serve())
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, service.stop.set)
                except NotImplementedError:
                    signal.signal(sig, lambda *_: loop.call_soon_threadsafe(service.stop.set))
            try:
                await service.run()
            finally:
                wake_server.should_exit = True
                await wake_task
    finally:
        state.close()


def main():
    try:
        settings = Settings()
        settings.worker_data_path.mkdir(parents=True, exist_ok=True)
        settings.worker_output_path.mkdir(parents=True, exist_ok=True)
        configure_logging(settings.worker_data_path, settings.log_level, settings.proof_worker_name,
                          settings.proof_worker_token.get_secret_value())
        with ProcessLock(settings.worker_data_path / "worker.lock"):
            configure_imaging(settings.worker_data_path, settings.vips_concurrency, settings.vips_cache_memory_mb)
            asyncio.run(serve(settings))
    except ValidationError:
        # Pydantic input diagnostics can contain secrets from environment values.
        print(json.dumps({"event": "INVALID_CONFIG", "message": "Invalid or missing bootstrap configuration; see .env.example"}))
        raise SystemExit(2) from None
    except WorkerError as error:
        print(json.dumps({"event": error.code, "message": error.message}))
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
