"""Proof Core v1 wire objects; conversion to execution models is explicit."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.errors import WorkerError
from app.models.job import Job
from app.models.preset import Preset


class CoreDTO(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class PresetDTO(CoreDTO):
    id: str = ""
    name: str = ""
    version: int = Field(default=1, ge=1)
    parameters: dict[str, Any] = Field(default_factory=dict)


class JobDTO(CoreDTO):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    processing_status: Literal[
        "received", "queued", "assigned", "running", "completed", "failed", "cancelled", "retrying"
    ]
    delivery_status: Literal["pending", "delivering", "delivered", "failed", "retrying"] = "pending"
    attempt: int = Field(ge=1)
    input: dict[str, Any] = Field(default_factory=dict)
    preset: PresetDTO
    crm_order_id: str = ""
    source: str = ""
    progress: int | None = Field(default=None, ge=0, le=100)
    current_stage: str = ""

    @property
    def job_id(self) -> str:
        return self.id

    def to_domain(self) -> Job:
        """Do not let wire JSON or Pydantic input dumps leak into execution errors."""
        source_path = self.input.get("source_path")
        layout_number = self.input.get("layout_number")
        if not isinstance(source_path, str) or not source_path.strip():
            raise WorkerError("INVALID_CONFIG", "Core Job must provide a nonempty input.source_path.")
        if isinstance(layout_number, bool) or not isinstance(layout_number, int):
            raise WorkerError("INVALID_CONFIG", "Core Job must provide an integer input.layout_number.")
        try:
            return Job(
                job_id=self.id,
                public_id=self.input.get("public_id", ""),
                source_path=source_path,
                layout_number=layout_number,
                order_number=self.input.get("order_number", self.crm_order_id),
                preset=Preset.model_validate(self.preset.parameters),
                metadata=self.input.get("metadata", {}),
                attempt=self.attempt,
            )
        except (ValueError, TypeError, ValidationError):
            raise WorkerError("INVALID_CONFIG", "Core Job or Preset does not match the Worker contract.") from None


class WorkerDTO(CoreDTO):
    id: str = Field(min_length=1, max_length=128)
    name: str
    hostname: str = ""
    version: str = ""
    online: bool = False
    availability: Literal["available", "busy", "error"]
    current_job_id: str | None = None
    heartbeat_timeout_seconds: int = Field(ge=1)
    capabilities: list[str] = Field(default_factory=list)
    configuration_version: int = Field(default=0, ge=0)
    configuration: dict[str, Any] = Field(default_factory=dict)


class HeartbeatRequest(CoreDTO):
    hostname: str = Field(min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=80)
    availability: Literal["available", "busy", "error"]
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    current_job_id: str | None = None
    last_error_code: str | None = Field(default=None, max_length=120)
    last_error_message: str | None = Field(default=None, max_length=2000)


class ProgressRequest(CoreDTO):
    progress: int = Field(ge=0, le=100)
    current_stage: str = Field(max_length=160)


class EventRequest(CoreDTO):
    event_type: str = Field(min_length=1, max_length=120)
    level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    message: str = Field(min_length=1, max_length=4000)
    error_code: str | None = Field(default=None, max_length=120)
    details: dict[str, Any] = Field(default_factory=dict)


class FailureRequest(CoreDTO):
    error_code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=4000)
    details: dict[str, Any] = Field(default_factory=dict)


class ResultAck(CoreDTO):
    result_id: str = Field(min_length=1, max_length=128)
    duplicate: bool
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProgressAck(CoreDTO):
    job_id: str
    progress: int = Field(ge=0, le=100)
    current_stage: str


class EventAck(CoreDTO):
    status: Literal["recorded"]
