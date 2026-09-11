from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProofArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
