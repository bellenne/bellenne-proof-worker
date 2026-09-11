from pathlib import Path

import pyvips

from app.core.errors import WorkerError
from app.imaging.analysis.result import NormalizedCrop
from app.imaging.preview import reduced_rgb
from app.imaging.renderer.overlay import outline
from app.models.preset import Preset


def thumbnail_dimensions(width: int, height: int, max_side: int) -> tuple[int, int]:
    scale = min(1.0, max_side / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def thumbnail_position(width: int, height: int, preset: Preset) -> tuple[int, int]:
    x = round(preset.thumbnail_left_offset_mm / 25.4 * preset.output_dpi)
    alignment = preset.thumbnail_vertical_alignment
    y = {"top": 0, "center": (preset.height_px - height) // 2, "bottom": preset.height_px - height}[alignment]
    if x < 0 or y < 0 or x + width > preset.width_px or y + height > preset.height_px:
        raise WorkerError("RENDER_ERROR", "Preset positions the full-source thumbnail outside the proof", {"thumbnail_width": width, "thumbnail_height": height, "thumbnail_x": x, "thumbnail_y": y})
    return x, y


def crop_rectangle(crop: NormalizedCrop, width: int, height: int) -> tuple[int, int, int, int]:
    left = min(width - 1, max(0, round(crop.x * width)))
    top = min(height - 1, max(0, round(crop.y * height)))
    right = min(width, max(left + 1, round((crop.x + crop.width) * width)))
    bottom = min(height, max(top + 1, round((crop.y + crop.height) * height)))
    return left, top, right - left, bottom - top


def create_thumbnail(path: Path, crop: NormalizedCrop, preset: Preset) -> tuple[pyvips.Image, tuple[int, int], tuple[int, int, int, int]]:
    max_side = round(preset.thumbnail_max_side_mm / 25.4 * preset.output_dpi)
    thumb = reduced_rgb(path, max_side, preset.alpha_background)
    rectangle = crop_rectangle(crop, thumb.width, thumb.height)
    thumb = outline(thumb, (0, 0, thumb.width, thumb.height), preset.thumbnail_border_px)
    thumb = outline(thumb, rectangle, preset.crop_rectangle_thickness_px)
    return thumb, thumbnail_position(thumb.width, thumb.height, preset), rectangle
