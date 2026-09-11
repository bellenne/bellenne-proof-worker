from dataclasses import dataclass

import cv2
import numpy as np

from app.imaging.analysis.candidates import PixelRegion
from app.imaging.analysis.config import ScoringConfig
from app.imaging.analysis.saliency import SaliencyProvider


@dataclass(frozen=True)
class CandidateFeatures:
    luminance_std: float
    color_std: float
    entropy: float
    flat_ratio: float
    edge_density: float
    laplacian_variance: float
    color_histogram: np.ndarray
    saliency_mean: float
    saliency_coverage: float
    boundary_importance: float
    center_distance: float


class AnalysisFeatures:
    """Shared preview maps; candidate statistics never touch the original image."""

    def __init__(self, rgb: np.ndarray, config: ScoringConfig, provider: SaliencyProvider):
        self.config = config
        self.rgb = rgb
        self.height, self.width = rgb.shape[:2]
        self.gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        self.saliency = provider.compute(rgb, config)
        if (self.saliency.shape != rgb.shape[:2] or not np.isfinite(self.saliency).all()
                or float(self.saliency.min()) < 0 or float(self.saliency.max()) > 1):
            raise ValueError("SaliencyProvider returned an invalid map")
        self.saliency_total = float(np.sum(self.saliency, dtype=np.float64))
        gray_float = self.gray.astype(np.float32)
        rgb_float = rgb.astype(np.float32)
        window = (config.local_variance_window, config.local_variance_window)
        local_mean = cv2.blur(rgb_float, window)
        local_variance = np.maximum(cv2.blur(rgb_float ** 2, window) - local_mean ** 2, 0)
        self.flat = np.mean(local_variance, axis=2) <= config.flat_std_threshold ** 2
        smooth = cv2.GaussianBlur(gray_float, (0, 0), config.detail_blur_sigma)
        dx = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3)
        dy = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3)
        self.edges = cv2.magnitude(dx, dy) > config.edge_gradient_threshold
        self.laplacian = cv2.Laplacian(smooth, cv2.CV_32F)
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV).astype(np.int32)
        hue = np.minimum(hsv[..., 0] * config.hue_bins // 180, config.hue_bins - 1)
        saturation = np.minimum(hsv[..., 1] * config.saturation_bins // 256,
                                config.saturation_bins - 1)
        value = np.minimum(hsv[..., 2] * config.value_bins // 256, config.value_bins - 1)
        self.color_bin_count = config.hue_bins * config.saturation_bins * config.value_bins
        self.color_bins = ((hue * config.saturation_bins + saturation)
                           * config.value_bins + value).astype(np.uint16)
        self.global_histogram = self.histogram(self.color_bins)
        saturated_bins = np.bincount(
            self.color_bins[hsv[..., 1] >= 255 * config.accent_saturation_threshold],
            minlength=self.color_bin_count,
        ).astype(np.float64) / self.color_bins.size
        self.accent_weights = np.where(
            saturated_bins >= config.accent_min_global_fraction,
            saturated_bins ** config.accent_frequency_exponent, 0,
        )
        accent_total = float(self.accent_weights.sum())
        if accent_total:
            self.accent_weights /= accent_total

    def histogram(self, bins: np.ndarray) -> np.ndarray:
        return np.bincount(bins.ravel(), minlength=self.color_bin_count) / bins.size

    def measure(self, region: PixelRegion, source_width: int,
                source_height: int) -> CandidateFeatures:
        x, y, right, bottom = region.preview_bounds(source_width, source_height,
                                                    self.width, self.height)
        patch = np.s_[y:bottom, x:right]
        gray = self.gray[patch]
        quantized = gray.astype(np.uint16) * self.config.entropy_bins // 256
        counts = np.bincount(quantized.ravel(), minlength=self.config.entropy_bins)
        probabilities = counts[counts > 0] / gray.size
        entropy = float(-np.sum(probabilities * np.log2(probabilities))
                        / np.log2(self.config.entropy_bins))
        saliency_patch = self.saliency[patch]
        border = max(1, round(min(right - x, bottom - y)
                              * self.config.composition_border_fraction))
        borders = []
        # The artwork's own boundary is not a cut introduced by this crop.
        if region.x > 0:
            borders.append(saliency_patch[:, :border].ravel())
        if region.x + region.width < source_width:
            borders.append(saliency_patch[:, -border:].ravel())
        if region.y > 0:
            borders.append(saliency_patch[:border, :].ravel())
        if region.y + region.height < source_height:
            borders.append(saliency_patch[-border:, :].ravel())
        importance = (float(np.mean(np.concatenate(borders)
                                    >= self.config.composition_importance_threshold))
                      if borders else 0.0)
        cx = (region.x + region.width / 2) / source_width
        cy = (region.y + region.height / 2) / source_height
        distance = np.hypot(cx - 0.5, cy - 0.5) / np.hypot(0.5, 0.5)
        return CandidateFeatures(
            luminance_std=float(np.std(gray)),
            color_std=float(np.sqrt(np.mean(np.var(self.rgb[patch], axis=(0, 1))))),
            entropy=entropy,
            flat_ratio=float(np.mean(self.flat[patch])),
            edge_density=float(np.mean(self.edges[patch])),
            laplacian_variance=float(np.var(self.laplacian[patch])),
            color_histogram=self.histogram(self.color_bins[patch]),
            saliency_mean=float(np.mean(saliency_patch)),
            saliency_coverage=(float(np.sum(saliency_patch, dtype=np.float64))
                               / self.saliency_total if self.saliency_total else 0.0),
            boundary_importance=importance,
            center_distance=float(distance),
        )
