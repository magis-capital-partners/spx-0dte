"""Candidate score weights and lightweight logistic regression (stdlib only)."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence


@dataclass(frozen=True)
class CandidateScoreWeights:
    premium: float = 0.45
    term: float = 0.25
    realized: float = 0.20
    trend: float = 0.40
    skew: float = 0.35
    delta: float = 0.20
    credit: float = 0.35
    distance: float = 0.25
    stop: float = 0.25
    intercept: float = 0.0

    def as_tuple(self) -> tuple[float, ...]:
        return (
            self.premium,
            self.term,
            self.realized,
            self.trend,
            self.skew,
            self.delta,
            self.credit,
            self.distance,
            self.stop,
        )

    def weighted_sum(self, components: Sequence[float]) -> float:
        weights = self.as_tuple()
        total = self.intercept
        for weight, value in zip(weights, components):
            total += weight * value
        return total


DEFAULT_SCORE_WEIGHTS = CandidateScoreWeights()


def sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def fit_logistic_regression(
    features: Sequence[Sequence[float]],
    labels: Sequence[int],
    learning_rate: float = 0.05,
    epochs: int = 2500,
    l2: float = 0.10,
) -> tuple[List[float], float]:
    if not features:
        return [], 0.0
    dimension = len(features[0])
    weights = [0.0] * dimension
    bias = 0.0
    n = len(features)

    for _ in range(epochs):
        grad_w = [0.0] * dimension
        grad_b = 0.0
        for row, label in zip(features, labels):
            logit = bias + sum(w * x for w, x in zip(weights, row))
            prediction = sigmoid(logit)
            error = prediction - label
            for index in range(dimension):
                grad_w[index] += error * row[index]
            grad_b += error
        for index in range(dimension):
            weights[index] -= learning_rate * ((grad_w[index] / n) + l2 * weights[index])
        bias -= learning_rate * (grad_b / n)
    return weights, bias


def probability_to_score(probability: float, base: float = 2.0, span: float = 1.5) -> float:
    clamped = max(0.0, min(1.0, probability))
    return base + span * clamped


def score_to_probability(score: float, base: float = 2.0, span: float = 1.5) -> float:
    return max(0.0, min(1.0, (score - base) / span))


def components_to_score(components: Sequence[float], weights: CandidateScoreWeights) -> float:
    return weights.weighted_sum(components)


def save_score_weights(path: Path, weights: CandidateScoreWeights, metadata: Optional[dict] = None) -> None:
    payload = {"weights": asdict(weights), "metadata": metadata or {}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_score_weights(path: Path) -> CandidateScoreWeights:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return CandidateScoreWeights(**payload["weights"])


def weights_from_logistic(
    feature_weights: Sequence[float],
    bias: float,
    scale: float = 1.0,
) -> CandidateScoreWeights:
    if len(feature_weights) != 9:
        raise ValueError("Expected 9 feature weights for candidate score components.")
    scaled = [value * scale for value in feature_weights]
    return CandidateScoreWeights(
        premium=scaled[0],
        term=scaled[1],
        realized=scaled[2],
        trend=scaled[3],
        skew=scaled[4],
        delta=scaled[5],
        credit=scaled[6],
        distance=scaled[7],
        stop=scaled[8],
        intercept=bias * scale,
    )
