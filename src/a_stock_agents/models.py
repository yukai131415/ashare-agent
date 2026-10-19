from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


# 新闻继续用于展示；此版本的所有模型均不将新闻情绪计入评分。
SCORING_VERSION = "1.2-capped-valuation-no-news"


def valuation_factor(features):
    """极端低PE不作便宜的证据；估值标准分限制在±3。"""
    pe = features.get('pe_ratio')
    if pe not in (None, ''):
        try:
            if float(pe) < 1:
                return 0.0
        except (ValueError, TypeError):
            return 0.0
    return max(-3.0, min(3.0, _f(features, 'valuation_z')))


class ScoringModel(Protocol):
    name: str
    version: str

    def score(self, features: dict[str, float | str | None]) -> float: ...


def _f(features: dict[str, float | str | None], name: str) -> float:
    value = features.get(name)
    return float(value) if isinstance(value, (int, float)) else 0.0


@dataclass(frozen=True, slots=True)
class BalancedModel:
    name: str = "balanced"
    version: str = SCORING_VERSION

    def score(self, features: dict[str, float | str | None]) -> float:
        return (
            0.28 * _f(features, "momentum_z")
            + 0.20 * _f(features, "volume_ratio_z")
            + 0.18 * _f(features, "liquidity_z")
            + 0.12 * _f(features, "turnover_z")
            + 0.05 * valuation_factor(features)
            - 0.08 * max(_f(features, "amplitude_z"), 0.0)
        )


@dataclass(frozen=True, slots=True)
class MomentumModel:
    name: str = "momentum"
    version: str = SCORING_VERSION

    def score(self, features: dict[str, float | str | None]) -> float:
        return (
            0.44 * _f(features, "momentum_z")
            + 0.24 * _f(features, "volume_ratio_z")
            + 0.12 * _f(features, "liquidity_z")
            + 0.08 * _f(features, "change_speed_z")
        )


@dataclass(frozen=True, slots=True)
class ValueReversalModel:
    name: str = "value_reversal"
    version: str = SCORING_VERSION

    def score(self, features: dict[str, float | str | None]) -> float:
        return (
            -0.28 * _f(features, "momentum_z")
            + 0.28 * valuation_factor(features)
            + 0.18 * _f(features, "liquidity_z")
            + 0.10 * _f(features, "volume_ratio_z")
            - 0.08 * max(_f(features, "amplitude_z"), 0.0)
        )


MODELS: dict[str, ScoringModel] = {
    model.name: model for model in (BalancedModel(), MomentumModel(), ValueReversalModel())
}


def get_model(name: str) -> ScoringModel:
    try:
        return MODELS[name]
    except KeyError as exc:
        raise ValueError(f"未知推荐模型: {name}") from exc
