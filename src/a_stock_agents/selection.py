"""Shared deterministic selection. Stable sort and arithmetic match the original engine."""
from typing import Any
from .models import get_model


def eligible_for_selection(row, config):
    return (float(row.get('amount') or 0.0) >= float(config['min_turnover_amount'])
            and abs(float(row.get('change_pct') or 0.0)) < float(config['max_daily_change_pct']))


def rank_candidates(features: list[dict[str, Any]], config: dict[str, Any], model_name: str):
    model = get_model(model_name)
    candidates = []
    for row in features:
        if not eligible_for_selection(row, config):
            continue
        candidates.append((model.score(row), row))
    candidates.sort(key=lambda entry: entry[0], reverse=True)
    return candidates
