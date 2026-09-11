"""Pure rectangle geometry and bounded overlay operations."""

import pyvips


def outline(image: pyvips.Image, rectangle: tuple[int, int, int, int], thickness: int) -> pyvips.Image:
    """Black inward outline, no fill, no extension of the source canvas."""
    x, y, width, height = rectangle
    if width <= 0 or height <= 0 or thickness <= 0:
        return image
    thickness = min(thickness, max(1, (min(width, height) + 1) // 2))
    # pyvips wraps mutable vips operations with copy-on-write and returns the
    # modified image. Retain each return value; discarding it loses the line.
    result = image.copy_memory()
    result = result.draw_rect([0, 0, 0], x, y, width, thickness, fill=True)
    result = result.draw_rect([0, 0, 0], x, y + height - thickness, width, thickness, fill=True)
    result = result.draw_rect([0, 0, 0], x, y, thickness, height, fill=True)
    result = result.draw_rect([0, 0, 0], x + width - thickness, y, thickness, height, fill=True)
    return result
