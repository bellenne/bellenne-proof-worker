from app.imaging.analysis.features import AnalysisFeatures, CandidateFeatures


class CompositionScorer:
    name = "composition"

    def score(self, features: CandidateFeatures, context: AnalysisFeatures) -> float:
        return max(0.0, 1 - context.config.composition_penalty * features.boundary_importance)
