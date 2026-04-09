"""
Estimates task complexity and recommends a minimum Claude model.
"""

from app.config import (
    MODELS, MODEL_DEFAULT, MODEL_THRESHOLDS,
    TOKENS_PER_TEST_INPUT, TOKENS_PER_TEST_OUTPUT, COST_PER_1K_SONNET,
)
from dataclasses import dataclass


@dataclass
class ModelRecommendation:
    chosen: str
    recommended: str
    minimum: str
    is_sufficient: bool
    warning: str
    estimated_tests: int
    estimated_tokens: int
    estimated_cost_usd: float


def estimate(
    url_count: int,
    include_positive: bool,
    include_negative: bool,
    chosen_model: str,
    max_positive: int = 0,
    max_negative: int = 0,
) -> ModelRecommendation:
    pos_per_url = 3   # happy path + 2 variants
    neg_per_url = 4   # empty, invalid, boundary, auth guard

    tests_per_url = 0
    if include_positive:
        pos = max_positive if max_positive > 0 else pos_per_url
        tests_per_url += pos
    if include_negative:
        neg = max_negative if max_negative > 0 else neg_per_url
        tests_per_url += neg

    estimated_tests = url_count * tests_per_url
    estimated_tokens = estimated_tests * (TOKENS_PER_TEST_INPUT + TOKENS_PER_TEST_OUTPUT)
    estimated_cost = (estimated_tokens / 1000) * COST_PER_1K_SONNET

    recommended = _recommend_model(estimated_tests)
    minimum = _minimum_model(estimated_tests)

    chosen_order = list(MODELS.keys())
    chosen_idx = chosen_order.index(chosen_model) if chosen_model in chosen_order else 1
    min_idx = chosen_order.index(minimum)
    is_sufficient = chosen_idx >= min_idx

    warning = ""
    if not is_sufficient:
        warning = (
            f"Selected model '{chosen_model}' may not handle this task well. "
            f"Minimum recommended: '{minimum}' for {estimated_tests} estimated tests."
        )

    return ModelRecommendation(
        chosen=chosen_model,
        recommended=recommended,
        minimum=minimum,
        is_sufficient=is_sufficient,
        warning=warning,
        estimated_tests=estimated_tests,
        estimated_tokens=estimated_tokens,
        estimated_cost_usd=round(estimated_cost, 3),
    )


def _recommend_model(test_count: int) -> str:
    for model, (lo, hi) in MODEL_THRESHOLDS.items():
        if lo <= test_count <= hi:
            return model
    return MODEL_DEFAULT


def _minimum_model(test_count: int) -> str:
    if test_count < 10:
        return "haiku"
    if test_count < 40:
        return "sonnet"
    return "opus"
