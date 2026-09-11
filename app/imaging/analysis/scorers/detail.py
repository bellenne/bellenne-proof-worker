import math

from app.imaging.analysis.features import AnalysisFeatures, CandidateFeatures


class DetailScorer:
    name = "detail"

    def score(self, features: CandidateFeatures, context: AnalysisFeatures) -> float:
        c = context.config
        density = features.edge_density
        if density < c.detail_density_low:
            suitability = density / c.detail_density_low
        elif density <= c.detail_density_high:
            suitability = 1.0
        else:
            suitability = math.exp(-c.detail_noise_decay * (density - c.detail_density_high)
                                    / (1 - c.detail_density_high))
        sharpness = min(1.0, features.laplacian_variance / c.detail_laplacian_target)
        # Sharpness cannot rescue dense noise: the density penalty multiplies both terms.
        return suitability * (c.detail_edge_weight + (1 - c.detail_edge_weight) * sharpness)
