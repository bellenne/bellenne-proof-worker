"""Tunable analysis parameters. No production color conversion happens here."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScoreWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    content: float = Field(default=0.25, ge=0)
    detail: float = Field(default=0.20, ge=0)
    color: float = Field(default=0.20, ge=0)
    saliency: float = Field(default=0.20, ge=0)
    composition: float = Field(default=0.10, ge=0)
    center: float = Field(default=0.05, ge=0)

    @model_validator(mode="after")
    def nonzero(self):
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("At least one scoring weight must be positive")
        return self


class ScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    weights: ScoreWeights = Field(default_factory=ScoreWeights)
    max_candidates: int = Field(default=2048, ge=4, le=50000)
    top_candidates: int = Field(default=3, ge=3, le=20)
    nms_iou_threshold: float = Field(default=0.65, gt=0, lt=1)

    luminance_std_target: float = Field(default=35, gt=0)
    color_std_target: float = Field(default=35, gt=0)
    entropy_bins: int = Field(default=16, ge=2, le=256)
    local_variance_window: int = Field(default=5, ge=3, le=31)
    flat_std_threshold: float = Field(default=4, ge=0)
    content_luminance_weight: float = Field(default=0.30, ge=0)
    content_color_weight: float = Field(default=0.25, ge=0)
    content_entropy_weight: float = Field(default=0.25, ge=0)
    content_nonflat_weight: float = Field(default=0.20, ge=0)
    minimum_content_score: float = Field(default=0.08, ge=0, le=1)
    minimum_entropy: float = Field(default=0.015, ge=0, le=1)
    maximum_flat_ratio: float = Field(default=0.997, ge=0, le=1)

    detail_blur_sigma: float = Field(default=0.8, gt=0)
    edge_gradient_threshold: float = Field(default=30, gt=0)
    detail_density_low: float = Field(default=0.015, gt=0, lt=1)
    detail_density_high: float = Field(default=0.25, gt=0, lt=1)
    detail_noise_decay: float = Field(default=4, gt=0)
    detail_laplacian_target: float = Field(default=100, gt=0)
    detail_edge_weight: float = Field(default=0.75, ge=0, le=1)

    hue_bins: int = Field(default=12, ge=2, le=36)
    saturation_bins: int = Field(default=3, ge=2, le=8)
    value_bins: int = Field(default=3, ge=2, le=8)
    color_histogram_weight: float = Field(default=0.60, ge=0, le=1)
    accent_saturation_threshold: float = Field(default=0.25, ge=0, le=1)
    accent_min_global_fraction: float = Field(default=0.001, ge=0, le=1)
    accent_min_candidate_fraction: float = Field(default=0.003, ge=0, le=1)
    accent_frequency_exponent: float = Field(default=0.5, gt=0, le=1)

    saliency_blur_sigma: float = Field(default=3, gt=0)
    saliency_context_sigma: float = Field(default=15, gt=0)
    saliency_global_weight: float = Field(default=0.70, ge=0, le=1)
    saliency_normalization_quantile: float = Field(default=0.995, gt=0.5, le=1)
    saliency_mean_target: float = Field(default=0.30, gt=0, le=1)
    saliency_coverage_target: float = Field(default=0.20, gt=0, le=1)
    saliency_density_weight: float = Field(default=0.65, ge=0, le=1)
    composition_border_fraction: float = Field(default=0.04, gt=0, lt=0.5)
    composition_importance_threshold: float = Field(default=0.45, gt=0, le=1)
    composition_penalty: float = Field(default=0.90, ge=0, le=1)

    confidence_score_weight: float = Field(default=0.40, ge=0)
    confidence_margin_weight: float = Field(default=0.20, ge=0)
    confidence_content_weight: float = Field(default=0.25, ge=0)
    confidence_saliency_weight: float = Field(default=0.15, ge=0)
    confidence_margin_target: float = Field(default=0.15, gt=0)
    confidence_single_candidate_margin: float = Field(default=0.5, ge=0, le=1)
    confidence_ambiguity_floor: float = Field(default=0.75, ge=0, le=1)
    diagnostic_line_width: int = Field(default=2, ge=1, le=10)
    diagnostic_jpeg_quality: int = Field(default=90, ge=1, le=100)

    @model_validator(mode="after")
    def coherent_ranges(self):
        if self.local_variance_window % 2 != 1:
            raise ValueError("local_variance_window must be odd")
        if self.detail_density_low >= self.detail_density_high:
            raise ValueError("detail density range must be increasing")
        if self.saliency_context_sigma <= self.saliency_blur_sigma:
            raise ValueError("saliency_context_sigma must exceed saliency_blur_sigma")
        content_sum = (self.content_luminance_weight + self.content_color_weight
                       + self.content_entropy_weight + self.content_nonflat_weight)
        confidence_sum = (self.confidence_score_weight + self.confidence_margin_weight
                          + self.confidence_content_weight + self.confidence_saliency_weight)
        if content_sum <= 0 or confidence_sum <= 0:
            raise ValueError("Content and confidence weights must have positive sums")
        return self
