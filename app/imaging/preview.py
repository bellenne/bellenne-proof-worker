from pathlib import Path

import numpy as np
import pyvips

from app.core.errors import WorkerError
from app.imaging.color.manager import ColorManager
from app.imaging.loader import open_source
from app.models.preset import Preset


def reduced_rgb(path: Path, max_side: int, background: tuple[int, int, int]) -> pyvips.Image:
    """Stream native RGB through flatten/resize without a profile conversion.

    Flattening before reduction avoids colored fringes in transparent pixels.
    libvips streams scanlines; full source pixels never enter a numpy array.
    """
    source = ColorManager.rgb8(open_source(Path(path)), background)
    factor = min(1.0, max_side / max(source.width, source.height))
    return source.resize(factor, kernel="lanczos3") if factor < 1 else source


class PreviewGenerator:
    def generate(self, path: Path, preset: Preset) -> np.ndarray:
        try:
            preview = reduced_rgb(path, preset.analysis_preview_max_side_px, preset.alpha_background)
            # The sole production-to-numpy boundary: reduced, uint8, RGB.
            buffer = preview.write_to_memory()
            return np.frombuffer(buffer, dtype=np.uint8).reshape(preview.height, preview.width, 3).copy()
        except WorkerError:
            raise
        except (pyvips.Error, ValueError) as error:
            raise WorkerError("PREVIEW_ERROR", "Could not generate the reduced RGB preview", {"reason": str(error)[:500]}) from error
