"""Source diagnostics without serializing raw ICC bytes."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import WorkerError


class SourceMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    extension: str
    size_bytes: int
    mtime_ns: int
    width: int
    height: int
    format: str
    color_space: str
    channels: int
    has_alpha: bool
    sample_format: str
    icc_present: bool
    icc_name: str | None = None
    icc_profile: bytes | None = Field(default=None, exclude=True, repr=False)
    dpi_metadata: dict[str, float | str] | None = None
    warnings: list[str] = Field(default_factory=list)

    def to_processing_dict(self) -> dict[str, Any]:
        data = self.model_dump(exclude={"warnings", "mtime_ns"})
        return {**{f"source_{key}": value for key, value in data.items()}, "warnings": list(self.warnings)}

    def matches_file(self, path: Path) -> bool:
        try:
            stat = path.stat()
        except OSError as error:
            raise WorkerError("SOURCE_STORAGE_UNAVAILABLE", "Source became unavailable after validation", retryable=True) from error
        return stat.st_size == self.size_bytes and stat.st_mtime_ns == self.mtime_ns
