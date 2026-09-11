import numpy as np

from app.imaging.analysis.features import AnalysisFeatures, CandidateFeatures


class ColorScorer:
    name = "color"

    def score(self, features: CandidateFeatures, context: AnalysisFeatures) -> float:
        c = context.config
        representative = float(np.sqrt(features.color_histogram * context.global_histogram).sum())
        if not context.accent_weights.any():
            return min(1.0, representative)
        present = features.color_histogram >= c.accent_min_candidate_fraction
        accent = float(context.accent_weights[present].sum())
        return min(1.0, c.color_histogram_weight * representative
                   + (1 - c.color_histogram_weight) * accent)
