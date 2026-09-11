"""Deterministic crop ranking over a reduced RGB analysis image."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

import cv2
import numpy as np

from app.core.errors import WorkerError
from app.imaging.analysis.candidates import generate_candidates, suppress_overlaps
from app.imaging.analysis.features import AnalysisFeatures
from app.imaging.analysis.result import CropAnalysisResult, CropCandidate
from app.imaging.analysis.saliency import OpenCvSaliencyProvider, SaliencyProvider
from app.imaging.analysis.scorers import (
    CenterBiasScorer,
    ColorScorer,
    CompositionScorer,
    ContentScorer,
    DetailScorer,
    SaliencyScorer,
)

if TYPE_CHECKING:
    from app.models.preset import Preset


class CropAnalyzer:
    """Select coordinates only; production pixels never enter this component."""

    algorithm_version = "classical-cv-1"

    def __init__(self, saliency_provider: SaliencyProvider | None = None):
        self.saliency_provider = saliency_provider or OpenCvSaliencyProvider()
        self.scorers = (ContentScorer(), DetailScorer(), ColorScorer(), SaliencyScorer(),
                        CompositionScorer(), CenterBiasScorer())

    def analyze(self, preview: np.ndarray, source_width: int, source_height: int,
                preset: Preset) -> CropAnalysisResult:
        if min(source_width, source_height) <= 0:
            raise WorkerError("INVALID_DIMENSIONS", "Source dimensions must be positive")
        required = {"width": preset.width_px, "height": preset.height_px}
        if source_width < required["width"] or source_height < required["height"]:
            raise WorkerError("SOURCE_TOO_SMALL", "Source cannot contain the proof at its original scale", {
                "source_dimensions": {"width": source_width, "height": source_height},
                "required_dimensions": required,
            })
        if (not isinstance(preview, np.ndarray) or preview.dtype != np.uint8
                or preview.ndim != 3 or preview.shape[2] != 3 or min(preview.shape[:2]) <= 0):
            raise WorkerError("ANALYSIS_ERROR", "Analysis preview must be a nonempty uint8 RGB array")
        if max(preview.shape[:2]) > preset.analysis_preview_max_side_px:
            raise WorkerError("ANALYSIS_ERROR", "Analysis preview exceeds the configured size limit")
        try:
            return self._rank(preview, source_width, source_height, preset)
        except WorkerError:
            raise
        except (ValueError, ArithmeticError, cv2.error) as error:
            raise WorkerError("ANALYSIS_ERROR", "Could not evaluate the RGB analysis preview") from error

    def _rank(self, preview: np.ndarray, source_width: int, source_height: int,
              preset: Preset) -> CropAnalysisResult:
        config = preset.scoring
        context = AnalysisFeatures(preview, config, self.saliency_provider)
        regions = generate_candidates(source_width, source_height, preset.width_px,
                                      preset.height_px, preset.candidate_overlap,
                                      config.max_candidates)
        weights = config.weights.model_dump()
        total_weight = sum(weights.values())
        valid: list[CropCandidate] = []
        rejected: Counter[str] = Counter()
        for region in regions:
            features = context.measure(region, source_width, source_height)
            scores = {scorer.name: float(scorer.score(features, context)) for scorer in self.scorers}
            if any(not np.isfinite(score) or not 0 <= score <= 1 for score in scores.values()):
                raise ValueError("A scorer returned a nonfinite or out-of-range value")
            reasons = []
            if features.flat_ratio >= config.maximum_flat_ratio:
                reasons.append("flat")
            if features.entropy < config.minimum_entropy:
                reasons.append("entropy")
            if scores["content"] < config.minimum_content_score:
                reasons.append("content")
            if reasons:
                rejected.update(reasons)
                continue
            score = sum(weights[name] * value for name, value in scores.items()) / total_weight
            valid.append(CropCandidate(crop=region.normalized(source_width, source_height),
                                       score=min(1.0, max(0.0, score)), component_scores=scores))
        diagnostics = {
            "algorithm_version": self.algorithm_version,
            "saliency_provider": type(self.saliency_provider).__name__,
            "source_dimensions": {"width": source_width, "height": source_height},
            "preview_dimensions": {"width": preview.shape[1], "height": preview.shape[0]},
            "required_dimensions": {"width": preset.width_px, "height": preset.height_px},
            "candidate_count": len(regions),
            "valid_candidate_count": len(valid),
            "rejected_candidate_count": len(regions) - len(valid),
            "rejection_reasons": dict(rejected),
            "weights": weights,
            "nms_iou_threshold": config.nms_iou_threshold,
        }
        if not valid:
            raise WorkerError("ANALYSIS_NO_VALID_CROP", "No candidate contains enough useful visual content",
                              diagnostics)
        candidates = suppress_overlaps(valid, config.nms_iou_threshold, config.top_candidates)
        best = candidates[0]
        # Compare spatially different alternatives. Neighbouring windows of the
        # same object are not independent evidence of an ambiguous choice.
        if len(candidates) > 1:
            margin = min(1.0, (best.score - candidates[1].score) / config.confidence_margin_target)
        elif len(regions) == 1:
            margin = config.confidence_single_candidate_margin
        else:
            margin = 0.0
        terms = {"score": best.score, "margin": margin,
                 "content": best.component_scores["content"],
                 "saliency": best.component_scores["saliency"]}
        confidence_weights = {
            "score": config.confidence_score_weight, "margin": config.confidence_margin_weight,
            "content": config.confidence_content_weight, "saliency": config.confidence_saliency_weight,
        }
        confidence = sum(confidence_weights[name] * value for name, value in terms.items())
        confidence /= sum(confidence_weights.values())
        confidence *= config.confidence_ambiguity_floor + (1 - config.confidence_ambiguity_floor) * margin
        confidence = min(1.0, max(0.0, confidence))
        diagnostics.update({
            "distinct_candidate_count": len(candidates), "confidence_terms": terms,
            "best_crop_pixels": list(best.crop.to_pixels(source_width, source_height)),
        })
        warnings = ["LOW_CONFIDENCE"] if confidence < preset.minimum_confidence else []
        result = CropAnalysisResult(best_candidate=best, candidates=candidates, confidence=confidence,
                                    candidate_count=len(regions), valid_candidate_count=len(valid),
                                    warnings=warnings, diagnostics=diagnostics)
        if warnings and preset.low_confidence_policy == "FAIL":
            raise WorkerError("LOW_CONFIDENCE", "Automatic crop confidence is below the preset minimum", {
                "confidence": confidence, "minimum_confidence": preset.minimum_confidence,
                "analysis": result.model_dump(),
            })
        return result
