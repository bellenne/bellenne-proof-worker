from typing import Protocol

import cv2
import numpy as np

from app.imaging.analysis.config import ScoringConfig


class SaliencyProvider(Protocol):
    def compute(self, rgb: np.ndarray, config: ScoringConfig) -> np.ndarray:
        """Return finite float saliency in [0, 1], with the input's height/width."""
        ...


class OpenCvSaliencyProvider:
    """Deterministic frequency-tuned color contrast, using only classical OpenCV.

    Lab exists only in this reduced analysis buffer. Original source pixels and
    production ICC profiles are never passed through this provider.
    """

    def compute(self, rgb: np.ndarray, config: ScoringConfig) -> np.ndarray:
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
        smooth = cv2.GaussianBlur(lab, (0, 0), config.saliency_blur_sigma)
        broad = cv2.GaussianBlur(lab, (0, 0), config.saliency_context_sigma)
        reference = np.median(lab.reshape(-1, 3), axis=0)
        global_contrast = np.linalg.norm(smooth - reference, axis=2)
        local_contrast = np.linalg.norm(smooth - broad, axis=2)
        result = (config.saliency_global_weight * global_contrast
                  + (1 - config.saliency_global_weight) * local_contrast)
        scale = float(np.quantile(result, config.saliency_normalization_quantile))
        if scale <= np.finfo(np.float32).eps:
            scale = float(result.max())
        if scale <= np.finfo(np.float32).eps:
            return np.zeros(rgb.shape[:2], dtype=np.float32)
        return np.clip(result / scale, 0, 1).astype(np.float32)
