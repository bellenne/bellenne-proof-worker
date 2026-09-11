from dataclasses import replace

import cv2
import numpy as np
import pytest
from pydantic import ValidationError

from app.core.errors import WorkerError
from app.imaging.analysis import CropAnalyzer, diagnostic_preview
from app.imaging.analysis.candidates import (
    PixelRegion,
    generate_candidates,
    intersection_over_union,
    suppress_overlaps,
)
from app.imaging.analysis.config import ScoreWeights, ScoringConfig
from app.imaging.analysis.features import AnalysisFeatures
from app.imaging.analysis.result import CropCandidate, NormalizedCrop
from app.imaging.analysis.saliency import OpenCvSaliencyProvider
from app.imaging.analysis.scorers import (
    CenterBiasScorer,
    ColorScorer,
    CompositionScorer,
    ContentScorer,
    DetailScorer,
    SaliencyScorer,
)
from app.models.preset import Preset


def small_preset(**changes):
    return Preset(proof_width_mm=200, proof_height_mm=100, output_dpi=25.4, **changes)


def patterned_image(width=600, height=300):
    y, x = np.indices((height, width))
    return np.stack(((x // 15 % 2) * 180 + 30, (y // 12 % 2) * 160 + 30,
                     ((x + y) // 20 % 2) * 140 + 40), axis=-1).astype(np.uint8)


@pytest.fixture
def measured():
    rgb = patterned_image(200, 100)
    context = AnalysisFeatures(rgb, ScoringConfig(), OpenCvSaliencyProvider())
    return context.measure(PixelRegion(0, 0, 200, 100), 200, 100), context


def test_normalized_source_preview_roundtrip():
    region = PixelRegion(5678, 4321, 1701, 850)
    crop = region.normalized(12345, 9876)
    assert crop.to_pixels(12345, 9876) == (5678, 4321, 1701, 850)
    left, top, right, bottom = region.preview_bounds(12345, 9876, 2000, 1600)
    assert left <= crop.x * 2000 < left + 1
    assert top <= crop.y * 1600 < top + 1
    assert right - 1 < (crop.x + crop.width) * 2000 <= right
    assert bottom - 1 < (crop.y + crop.height) * 1600 <= bottom
    with pytest.raises(ValidationError):
        NormalizedCrop(x=0.9, y=0, width=0.2, height=0.5)


@pytest.mark.parametrize("dimensions", [(10000, 10000), (10**12, 850), (1701, 10**12)])
def test_candidates_cover_edges_with_fixed_size_and_bounded_count(dimensions):
    width, height = dimensions
    candidates = generate_candidates(width, height, 1701, 850, max_candidates=128)
    assert 1 <= len(candidates) <= 128
    assert len(set(candidates)) == len(candidates)
    assert candidates[0] == PixelRegion(0, 0, 1701, 850)
    assert candidates[-1] == PixelRegion(width - 1701, height - 850, 1701, 850)
    assert all(c.width == 1701 and c.height == 850 and c.x >= 0 and c.y >= 0
               and c.x + c.width <= width and c.y + c.height <= height for c in candidates)


def test_sliding_grid_is_dense_and_small_source_has_no_candidate():
    assert len(generate_candidates(10000, 10000, 1701, 850)) > 9
    assert generate_candidates(1700, 850, 1701, 850) == []
    assert generate_candidates(1701, 850, 1701, 850) == [PixelRegion(0, 0, 1701, 850)]
    tiny = PixelRegion(90000, 90000, 2, 1)
    x, y, right, bottom = tiny.preview_bounds(100000, 100000, 20, 20)
    assert right > x and bottom > y


def test_nonmaximum_suppression_discards_near_duplicate():
    def candidate(x, y, score):
        return CropCandidate(crop=NormalizedCrop(x=x, y=y, width=0.3, height=0.3),
                             score=score, component_scores={})
    best = candidate(0, 0, 0.9)
    duplicate = candidate(0.01, 0, 0.89)
    second, third = candidate(0.4, 0, 0.8), candidate(0, 0.5, 0.7)
    assert suppress_overlaps([third, duplicate, second, best]) == [best, second, third]
    assert intersection_over_union(best.crop, duplicate.crop) > 0.9


def test_content_scorer_rejects_flat_regions(measured):
    rich, context = measured
    flat = replace(rich, luminance_std=0, color_std=0, entropy=0, flat_ratio=1)
    assert ContentScorer().score(flat, context) == 0
    assert ContentScorer().score(rich, context) > 0.5


def test_detail_scorer_prefers_moderate_detail_to_noise(measured):
    features, context = measured
    moderate = replace(features, edge_density=0.1, laplacian_variance=150)
    noise = replace(features, edge_density=0.9, laplacian_variance=10000)
    flat = replace(features, edge_density=0, laplacian_variance=0)
    scorer = DetailScorer()
    assert scorer.score(moderate, context) > scorer.score(noise, context) > scorer.score(flat, context)


def test_color_scorer_rewards_small_saturated_accent():
    rgb = np.full((100, 200, 3), 240, dtype=np.uint8)
    rgb[20:30, 20:30] = (230, 10, 10)
    context = AnalysisFeatures(rgb, ScoringConfig(), OpenCvSaliencyProvider())
    accent = context.measure(PixelRegion(0, 0, 100, 100), 200, 100)
    blank = context.measure(PixelRegion(100, 0, 100, 100), 200, 100)
    assert ColorScorer().score(accent, context) > ColorScorer().score(blank, context)


def test_saliency_composition_and_center_scorers(measured):
    features, context = measured
    important = replace(features, saliency_mean=0.5, saliency_coverage=0.6)
    empty = replace(features, saliency_mean=0, saliency_coverage=0)
    assert SaliencyScorer().score(important, context) > SaliencyScorer().score(empty, context)
    assert CompositionScorer().score(replace(features, boundary_importance=0), context) > \
        CompositionScorer().score(replace(features, boundary_importance=1), context)
    assert CenterBiasScorer().score(replace(features, center_distance=0), context) > \
        CenterBiasScorer().score(replace(features, center_distance=0.8), context)


def test_analysis_is_deterministic_reports_six_scores_and_distinct_top_three():
    preview, preset = patterned_image(), small_preset()
    analyzer = CropAnalyzer()
    result = analyzer.analyze(preview, 600, 300, preset)
    assert result.model_dump() == analyzer.analyze(preview, 600, 300, preset).model_dump()
    assert len(result.candidates) == 3
    assert result.best_candidate == result.candidates[0]
    for index, candidate in enumerate(result.candidates):
        assert set(candidate.component_scores) == {"content", "detail", "color", "saliency", "composition", "center"}
        assert all(0 <= score <= 1 for score in candidate.component_scores.values())
        weights = preset.scoring.weights.model_dump()
        expected = sum(weights[name] * score for name, score in candidate.component_scores.items()) / sum(weights.values())
        assert candidate.score == pytest.approx(expected)
        assert candidate.crop.to_pixels(600, 300)[2:] == (200, 100)
        for other in result.candidates[index + 1:]:
            assert intersection_over_union(candidate.crop, other.crop) <= preset.scoring.nms_iou_threshold
    assert result.confidence != result.best_candidate.score


def test_nonempty_off_center_region_beats_blank_center():
    rgb = np.full((300, 600, 3), 230, dtype=np.uint8)
    rgb[20:120, 0:180] = patterned_image(180, 100)
    result = CropAnalyzer().analyze(rgb, 600, 300, small_preset())
    x, y, width, height = result.best_candidate.crop.to_pixels(600, 300)
    assert x < 180 and y < 120
    assert result.valid_candidate_count < result.candidate_count
    assert (width, height) == (200, 100)


@pytest.mark.parametrize("color", [(255, 255, 255), (0, 0, 0), (0, 120, 0), (190, 180, 160)])
def test_all_flat_colors_raise_no_valid_crop(color):
    with pytest.raises(WorkerError) as caught:
        CropAnalyzer().analyze(np.full((300, 600, 3), color, dtype=np.uint8), 600, 300, small_preset())
    assert caught.value.code == "ANALYSIS_NO_VALID_CROP"
    assert caught.value.details["valid_candidate_count"] == 0


def test_low_confidence_continue_and_fail_policies():
    rgb = patterned_image()
    warning = CropAnalyzer().analyze(rgb, 600, 300, small_preset(minimum_confidence=1))
    assert warning.warnings == ["LOW_CONFIDENCE"]
    with pytest.raises(WorkerError) as caught:
        CropAnalyzer().analyze(rgb, 600, 300, small_preset(minimum_confidence=1, low_confidence_policy="FAIL"))
    assert caught.value.code == "LOW_CONFIDENCE"
    assert caught.value.details["analysis"]["best_candidate"] == warning.best_candidate.model_dump()


def test_single_possible_region_does_not_invent_alternatives():
    result = CropAnalyzer().analyze(patterned_image(200, 100), 200, 100, small_preset())
    assert result.candidate_count == result.valid_candidate_count == len(result.candidates) == 1
    assert result.best_candidate.crop.to_pixels(200, 100) == (0, 0, 200, 100)


def test_small_source_and_invalid_preview_are_controlled_errors():
    with pytest.raises(WorkerError) as caught:
        CropAnalyzer().analyze(patterned_image(), 199, 300, small_preset())
    assert caught.value.code == "SOURCE_TOO_SMALL"
    assert caught.value.details["required_dimensions"] == {"width": 200, "height": 100}
    with pytest.raises(WorkerError, match="uint8 RGB"):
        CropAnalyzer().analyze(np.zeros((100, 200, 4), dtype=np.uint8), 200, 100, small_preset())


def test_invalid_saliency_provider_is_controlled_error():
    class InvalidSaliency:
        def compute(self, rgb, config):
            return np.full(rgb.shape[:2], np.nan)

    with pytest.raises(WorkerError) as caught:
        CropAnalyzer(InvalidSaliency()).analyze(patterned_image(), 600, 300, small_preset())
    assert caught.value.code == "ANALYSIS_ERROR"


def test_diagnostic_has_rectangles_preserves_rgb_order_and_input(tmp_path):
    rgb = patterned_image()
    result = CropAnalyzer().analyze(rgb, 600, 300, small_preset())
    red = np.full_like(rgb, (230, 20, 10))
    original = red.copy()
    path = diagnostic_preview(red, result, tmp_path / "диагностика.jpg")
    decoded = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert np.array_equal(red, original)
    assert decoded.shape == red.shape
    assert np.median(decoded[..., 2]) > 200  # Encoder received BGR, so red stays red.
    assert np.median(decoded[..., 0]) < 30
    assert np.any(decoded[..., 1] > 200)  # Visible green winner rectangle.


@pytest.mark.parametrize("values", [{"content": 0, "detail": 0, "color": 0, "saliency": 0,
                                    "composition": 0, "center": 0}, {"content": float("inf")}])
def test_invalid_weights_rejected(values):
    with pytest.raises(ValidationError):
        ScoreWeights(**values)
