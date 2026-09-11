from typing import Any

from pydantic import BaseModel, Field

from app.models.preset import Preset


class Job(BaseModel):
    job_id: str
    public_id: str = ""
    source_path: str = Field(min_length=1, max_length=2048)
    layout_number: int = Field(ge=1, le=999_999)
    order_number: str = ""
    preset: Preset
    metadata: dict[str, Any] = Field(default_factory=dict)
    attempt: int = Field(default=1, ge=1)
