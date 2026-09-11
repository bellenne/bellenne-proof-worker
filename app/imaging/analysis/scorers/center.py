from app.imaging.analysis.features import AnalysisFeatures, CandidateFeatures


class CenterBiasScorer:
    name = "center"

    def score(self, features: CandidateFeatures, context: AnalysisFeatures) -> float:
        return max(0.0, 1 - features.center_distance)
