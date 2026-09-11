from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NormalizedCrop(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def bounded(self):
        # Round-trip floating point divisions can differ by one ULP.
        if self.x + self.width > 1.000000000001 or self.y + self.height > 1.000000000001:
            raise ValueError("Crop extends outside source")
        return self

    @classmethod
    def from_pixels(cls, x: int, y: int, width: int, height: int,
                    source_width: int, source_height: int) -> "NormalizedCrop":
        if min(source_width, source_height) <= 0:
            raise ValueError("Source dimensions must be positive")
        return cls(x=x / source_width, y=y / source_height,
                   width=width / source_width, height=height / source_height)

    def to_pixels(self, source_width: int, source_height: int) -> tuple[int, int, int, int]:
        if min(source_width, source_height) <= 0:
            raise ValueError("Source dimensions must be positive")
        width = max(1, min(source_width, round(self.width * source_width)))
        height = max(1, min(source_height, round(self.height * source_height)))
        x = min(source_width - width, round(self.x * source_width))
        y = min(source_height - height, round(self.y * source_height))
        return x, y, width, height


class CropCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)
    crop: NormalizedCrop
    score: float = Field(ge=0, le=1)
    component_scores: dict[str, float]


class CropAnalysisResult(BaseModel):
    best_candidate: CropCandidate
    candidates: list[CropCandidate]
    confidence: float = Field(ge=0, le=1)
    candidate_count: int = Field(ge=1)
    valid_candidate_count: int = Field(ge=1)
    warnings: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
