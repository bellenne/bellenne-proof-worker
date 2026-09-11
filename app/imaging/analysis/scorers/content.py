from app.imaging.analysis.features import AnalysisFeatures, CandidateFeatures


class ContentScorer:
    name = "content"

    def score(self, features: CandidateFeatures, context: AnalysisFeatures) -> float:
        c = context.config
        weights = (c.content_luminance_weight, c.content_color_weight,
                   c.content_entropy_weight, c.content_nonflat_weight)
        values = (min(1.0, features.luminance_std / c.luminance_std_target),
                  min(1.0, features.color_std / c.color_std_target),
                  features.entropy, 1 - features.flat_ratio)
        return float(sum(w * v for w, v in zip(weights, values)) / sum(weights))
