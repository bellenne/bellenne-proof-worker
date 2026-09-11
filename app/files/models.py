from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class SourceFile(BaseModel):
    path: Path
    order_path: Path
    revision: int
    diagnostics: dict[str, Any] = Field(default_factory=dict)
