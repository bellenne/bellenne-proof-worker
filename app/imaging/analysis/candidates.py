"""Generate fixed-size source rectangles and rank spatially distinct alternatives."""

import math
from dataclasses import dataclass

import numpy as np

from app.imaging.analysis.result import CropCandidate, NormalizedCrop


@dataclass(frozen=True)
class PixelRegion:
    x: int
    y: int
    width: int
    height: int

    def normalized(self, source_width: int, source_height: int) -> NormalizedCrop:
        return NormalizedCrop.from_pixels(self.x, self.y, self.width, self.height,
                                          source_width, source_height)

    def preview_bounds(self, source_width: int, source_height: int,
                       preview_width: int, preview_height: int) -> tuple[int, int, int, int]:
        # Evaluate every source region even if its preview representation is subpixel.
        x = min(preview_width - 1, int(self.x * preview_width / source_width))
        y = min(preview_height - 1, int(self.y * preview_height / source_height))
        right = min(preview_width, math.ceil((self.x + self.width) * preview_width / source_width))
        bottom = min(preview_height, math.ceil((self.y + self.height) * preview_height / source_height))
        return x, y, max(x + 1, right), max(y + 1, bottom)


def generate_candidates(source_width: int, source_height: int, crop_width: int,
                        crop_height: int, overlap: float = 0.65,
                        max_candidates: int = 2048) -> list[PixelRegion]:
    if min(source_width, source_height, crop_width, crop_height) <= 0:
        raise ValueError("Image and crop dimensions must be positive")
    if not 0 <= overlap < 1 or max_candidates < 4:
        raise ValueError("Invalid overlap or candidate limit")
    if crop_width > source_width or crop_height > source_height:
        return []
    span_x, span_y = source_width - crop_width, source_height - crop_height
    step_x = max(1, round(crop_width * (1 - overlap)))
    step_y = max(1, round(crop_height * (1 - overlap)))
    nx = math.ceil(span_x / step_x) + 1
    ny = math.ceil(span_y / step_y) + 1
    # Reduce step density, never crop size. Endpoints cover every source edge.
    if nx * ny > max_candidates:
        minimum_x, minimum_y = (2 if span_x else 1), (2 if span_y else 1)
        # A bounded search also handles extremely long, narrow sources without
        # decrementing millions of grid positions one at a time.
        low, high = 1.0, float(max(nx, ny))
        for _ in range(64):
            scale = (low + high) / 2
            count_x = max(minimum_x, math.floor(nx / scale))
            count_y = max(minimum_y, math.floor(ny / scale))
            if count_x * count_y > max_candidates:
                low = scale
            else:
                high = scale
        nx = max(minimum_x, math.floor(nx / high))
        ny = max(minimum_y, math.floor(ny / high))
    xs = np.rint(np.linspace(0, span_x, nx)).astype(np.int64)
    ys = np.rint(np.linspace(0, span_y, ny)).astype(np.int64)
    return [PixelRegion(int(x), int(y), crop_width, crop_height)
            for y in ys for x in xs]


def intersection_over_union(a: NormalizedCrop, b: NormalizedCrop) -> float:
    width = max(0.0, min(a.x + a.width, b.x + b.width) - max(a.x, b.x))
    height = max(0.0, min(a.y + a.height, b.y + b.height) - max(a.y, b.y))
    intersection = width * height
    return intersection / (a.width * a.height + b.width * b.height - intersection)


def suppress_overlaps(candidates: list[CropCandidate], threshold: float = 0.65,
                      limit: int = 3) -> list[CropCandidate]:
    ranked = sorted(candidates, key=lambda c: (-c.score, c.crop.y, c.crop.x))
    selected: list[CropCandidate] = []
    for candidate in ranked:
        if all(intersection_over_union(candidate.crop, kept.crop) <= threshold
               for kept in selected):
            selected.append(candidate)
            if len(selected) == limit:
                break
    return selected
