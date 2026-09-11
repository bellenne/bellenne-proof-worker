from app.imaging.analysis.features import AnalysisFeatures, CandidateFeatures


class SaliencyScorer:
    name = "saliency"

    def score(self, features: CandidateFeatures, context: AnalysisFeatures) -> float:
        c = context.config
        density = min(1.0, features.saliency_mean / c.saliency_mean_target)
        coverage = min(1.0, features.saliency_coverage / c.saliency_coverage_target)
        return c.saliency_density_weight * density + (1 - c.saliency_density_weight) * coverage
