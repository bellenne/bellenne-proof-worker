from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.imaging.analysis.config import ScoringConfig


def mm_to_px(mm: float, dpi: float = 72) -> int:
    if not math.isfinite(mm) or not math.isfinite(dpi) or mm < 0 or dpi <= 0:
        raise ValueError("Dimensions must be finite and DPI positive")
    return round(mm / 25.4 * dpi)


class SearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roots: list[str] = Field(default_factory=list)
    # Accepted for compatibility with presets created before recursive revision search.
    source_directory_name: str = Field(
        default="Исходник", min_length=1, max_length=255,
        json_schema_extra={"deprecated": True},
    )
    # PNG/JPEG remain valid direct sources, but production search must opt into
    # them because they are commonly previews next to a TIFF.
    extension_priority: list[str] = Field(default_factory=lambda: [".tif", ".tiff"])
    recursive: bool = True
    root_priority: bool = False
    storage_marker: str | None = None

    @field_validator("extension_priority")
    @classmethod
    def extensions(cls, values: list[str]) -> list[str]:
        if not values or len(values) != len(set(values)) or any(
            v not in {".tif", ".tiff", ".png", ".jpg", ".jpeg"} for v in values
        ):
            raise ValueError("Use unique supported extensions")
        return values

    @field_validator("source_directory_name")
    @classmethod
    def source_directory_is_one_name(cls, value: str) -> str:
        value = value.strip()
        if value in {".", ".."} or any(character in value for character in "/\\\x00"):
            raise ValueError("Source directory must be one plain directory name")
        return value

class Preset(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    proof_width_mm: float = Field(default=600, gt=0)
    proof_height_mm: float = Field(default=300, gt=0)
    output_dpi: float = Field(default=72, gt=0)
    thumbnail_max_side_mm: float = Field(default=150, gt=0)
    thumbnail_left_offset_mm: float = Field(default=50, ge=0)
    thumbnail_vertical_alignment: Literal["top", "center", "bottom"] = "center"
    thumbnail_border_px: int = Field(default=2, ge=1)
    crop_rectangle_thickness_px: int = Field(default=2, ge=1)
    analysis_preview_max_side_px: int = Field(default=2000, ge=32, le=4096)
    candidate_overlap: float = Field(default=0.65, ge=0, lt=1)
    minimum_confidence: float = Field(default=0.4, ge=0, le=1)
    low_confidence_policy: Literal["CONTINUE_WITH_WARNING", "FAIL"] = "CONTINUE_WITH_WARNING"
    output_format: Literal["jpeg"] = "jpeg"
    jpeg_quality: int = Field(default=95, ge=1, le=100)
    alpha_background: tuple[int, int, int] = (255, 255, 255)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)

    @field_validator("alpha_background", mode="before")
    @classmethod
    def background(cls, value):
        if value == "white":
            return (255, 255, 255)
        if value == "black":
            return (0, 0, 0)
        return value

    @field_validator("alpha_background")
    @classmethod
    def channels(cls, value):
        if any(not 0 <= x <= 255 for x in value):
            raise ValueError("Background channels must be 0..255")
        return value

    @model_validator(mode="after")
    def layout(self):
        if min(self.width_px, self.height_px) < 1:
            raise ValueError("Proof must contain pixels")
        if mm_to_px(self.thumbnail_max_side_mm, self.output_dpi) < 1:
            raise ValueError("Thumbnail must contain pixels")
        if self.width_px * self.height_px > 100_000_000:
            raise ValueError("Proof exceeds the MVP 100 megapixel output limit")
        if mm_to_px(self.thumbnail_left_offset_mm, self.output_dpi) >= self.width_px:
            raise ValueError("Thumbnail offset lies outside proof")
        return self

    @property
    def width_px(self) -> int:
        return mm_to_px(self.proof_width_mm, self.output_dpi)

    @property
    def height_px(self) -> int:
        return mm_to_px(self.proof_height_mm, self.output_dpi)
