from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pyvips

from app.core.errors import WorkerError
from app.storage.workspace import sync_directory


def save_captioned_preview(
    source: Path,
    destination: Path,
    caption: str,
    *,
    jpeg_quality: int,
) -> None:
    """Append a white caption strip without covering any proof pixels."""
    temporary: Path | None = None
    try:
        image = pyvips.Image.new_from_file(
            str(source), access="sequential", fail_on="error"
        ).copy_memory()
        if image.bands != 3:
            raise ValueError("Published proof must be an RGB image")

        strip_height = max(48, round(image.height * 0.12))
        horizontal_padding = max(12, image.width // 40)
        vertical_padding = max(6, strip_height // 8)
        font_size = max(20, min(72, round(strip_height * 0.52)))
        mask = pyvips.Image.text(caption, font=f"DejaVu Sans {font_size}")
        scale = min(
            1.0,
            (image.width - 2 * horizontal_padding) / mask.width,
            (strip_height - 2 * vertical_padding) / mask.height,
        )
        if scale <= 0:
            raise ValueError("Proof is too small for a caption")
        if scale < 1:
            mask = mask.resize(scale)

        text = mask.ifthenelse([0, 0, 0], [255, 255, 255], blend=True)
        strip = pyvips.Image.black(image.width, strip_height, bands=3) + 255
        strip = strip.insert(
            text,
            (image.width - text.width) // 2,
            (strip_height - text.height) // 2,
            expand=False,
        )
        preview = image.join(strip, "vertical").copy(
            xres=image.xres,
            yres=image.yres,
            interpretation="srgb",
        )

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        preview.jpegsave(
            str(temporary),
            Q=jpeg_quality,
            subsample_mode="off",
            optimize_coding=True,
            interlace=False,
        )
        with temporary.open("r+b") as saved:
            os.fsync(saved.fileno())
        os.replace(temporary, destination)
        temporary = None
        sync_directory(destination.parent)
    except WorkerError:
        raise
    except (OSError, ValueError, pyvips.Error) as error:
        raise WorkerError(
            "OUTPUT_WRITE_ERROR",
            "Could not create the captioned proof preview",
            {"reason": str(error)[:500]},
            retryable=isinstance(error, OSError),
        ) from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
