import asyncio
import socket

import structlog

from app import __version__
from app.core.errors import WorkerError

CAPABILITIES = ["file_search", "rgb_image_processing", "automatic_crop", "proof_render"]


async def interruptible_wait(stop: asyncio.Event, seconds: float):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def heartbeat_once(service):
    """Synchronize Worker state and runtime configuration with Core once."""
    async with service.heartbeat_lock:
        worker = await service.client.heartbeat(
            hostname=socket.gethostname(),
            version=__version__,
            state=service.worker_state,
            current_job_id=service.current_job_id,
            capabilities=CAPABILITIES,
            worker_name=service.settings.proof_worker_name,
            last_error_code=service.last_error_code,
        )
        service.core_connected = True
        # Older Core/test doubles do not expose runtime configuration yet.
        # Keep the heartbeat contract backward compatible during rolling deploys.
        service.apply_core_configuration(
            getattr(worker, "configuration_version", 0),
            getattr(worker, "configuration", {}),
        )
        return worker


async def heartbeat_loop(service):
    log = structlog.get_logger()
    while not service.stop.is_set():
        if not service.assignment_ready.is_set():
            try:
                await asyncio.wait_for(service.assignment_ready.wait(), timeout=1)
            except TimeoutError:
                continue
            if service.stop.is_set():
                break
        try:
            worker = await heartbeat_once(service)
            if worker.heartbeat_timeout_seconds <= service.settings.heartbeat_interval:
                log.warning("HEARTBEAT_TIMEOUT_TOO_SHORT", message="Core heartbeat timeout must exceed the configured interval")
        except WorkerError as error:
            service.core_connected = False
            # A claim or complete may race a heartbeat. Next heartbeat uses the
            # synchronized local assignment; it must never change Core ownership.
            log.warning("HEARTBEAT_FAILED", metadata={"code": error.code, **error.details})
        await interruptible_wait(service.stop, service.settings.heartbeat_interval)
