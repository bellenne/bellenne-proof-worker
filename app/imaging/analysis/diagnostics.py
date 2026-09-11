"""Optional diagnostic JPEG; never a source for the production renderer."""

from pathlib import Path

import cv2
import numpy as np

from app.core.errors import WorkerError
from app.imaging.analysis.config import ScoringConfig
from app.imaging.analysis.result import CropAnalysisResult


def diagnostic_preview(preview: np.ndarray, result: CropAnalysisResult, output: Path,
                       config: ScoringConfig | None = None) -> Path:
    config = config or ScoringConfig()
    canvas = preview.copy()
    height, width = canvas.shape[:2]
    colors = ((0, 255, 0), (255, 180, 0), (0, 200, 255))  # Explicit RGB values.
    # Draw the winner last so it remains visible wherever alternatives overlap.
    for index in reversed(range(len(result.candidates))):
        candidate = result.candidates[index]
        x, y, crop_width, crop_height = candidate.crop.to_pixels(width, height)
        color = colors[min(index, len(colors) - 1)]
        cv2.rectangle(canvas, (x, y), (x + crop_width - 1, y + crop_height - 1),
                      color, config.diagnostic_line_width)
        cv2.putText(canvas, f"{index + 1}: {candidate.score:.3f}",
                    (x + config.diagnostic_line_width, min(height - 1, y + 16)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
    try:
        # OpenCV's encoder expects BGR. This conversion is diagnostic-only.
        encoded, buffer = cv2.imencode(".jpg", cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR),
                                       [cv2.IMWRITE_JPEG_QUALITY, config.diagnostic_jpeg_quality])
        if not encoded:
            raise WorkerError("OUTPUT_WRITE_ERROR", "Could not encode the analysis diagnostic")
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(buffer.tobytes())
        return output
    except (OSError, cv2.error) as error:
        raise WorkerError("OUTPUT_WRITE_ERROR", "Could not save the analysis diagnostic") from error
