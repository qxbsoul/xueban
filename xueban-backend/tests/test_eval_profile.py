"""EvalProfile validation tests — Story 9.5 AC6.

Covers:
  - EvalProfile with valid data is accepted
  - test_prompts must contain at least one prompt (ValueError)
  - test_prompts entries must be non-empty strings
  - blocking_metrics must be in success_metrics metric_ids
  - latency_budget_ms must be >= 0
  - model_validator fires after field defaults are set
  - EvalMetric weight defaults to 1.0
  - FailureThresholds defaults (max_failed_metrics=1, blocking_metrics=[])
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from xueban.runtime.eval_service import (
    EvalMetric,
    EvalProfile,
    FailureThresholds,
    MetricType,
)


def _make_metric(metric_id: str = "aabbccdd00112233445566778899aabb") -> EvalMetric:
    return EvalMetric(
        metric_id=metric_id,
        name="task pass rate",
        metric_type=MetricType.task_completion,
        threshold=0.8,
    )


def _make_profile(**overrides) -> EvalProfile:
    defaults: dict = {
        "name": "smoke-test",
        "test_prompts": ["請幫我總結這篇論文"],
        "success_metrics": [_make_metric()],
    }
    defaults.update(overrides)
    return EvalProfile(**defaults)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_eval_profile_valid():
    p = _make_profile()
    assert p.name == "smoke-test"
    assert len(p.test_prompts) == 1
    assert p.citation_checks is False
    assert p.latency_budget_ms == 0


def test_eval_profile_default_ids_are_set():
    p = _make_profile()
    assert p.profile_id
    assert len(p.profile_id) == 32  # UUID hex


def test_eval_metric_weight_default():
    m = _make_metric()
    assert m.weight == 1.0


def test_failure_thresholds_defaults():
    ft = FailureThresholds()
    assert ft.max_failed_metrics == 1
    assert ft.blocking_metrics == []


# ---------------------------------------------------------------------------
# test_prompts validation
# ---------------------------------------------------------------------------


def test_empty_test_prompts_raises():
    with pytest.raises(ValidationError, match="test_prompts"):
        _make_profile(test_prompts=[])


def test_blank_test_prompt_raises():
    with pytest.raises(ValidationError, match="non-empty"):
        _make_profile(test_prompts=["   "])


def test_multiple_prompts_accepted():
    p = _make_profile(test_prompts=["p1", "p2", "p3"])
    assert len(p.test_prompts) == 3


# ---------------------------------------------------------------------------
# blocking_metrics validation
# ---------------------------------------------------------------------------


def test_blocking_metric_not_in_success_metrics_raises():
    metric_id = "aabbccdd00112233445566778899aabb"
    unknown_id = "00000000000000000000000000000000"
    with pytest.raises(ValidationError, match="blocking_metric"):
        EvalProfile(
            name="test",
            test_prompts=["hello"],
            success_metrics=[_make_metric(metric_id)],
            failure_thresholds=FailureThresholds(
                max_failed_metrics=1,
                blocking_metrics=[unknown_id],
            ),
        )


def test_blocking_metric_in_success_metrics_accepted():
    metric_id = "aabbccdd00112233445566778899aabb"
    p = EvalProfile(
        name="test",
        test_prompts=["hello"],
        success_metrics=[_make_metric(metric_id)],
        failure_thresholds=FailureThresholds(
            max_failed_metrics=1,
            blocking_metrics=[metric_id],
        ),
    )
    assert p.failure_thresholds.blocking_metrics == [metric_id]


# ---------------------------------------------------------------------------
# latency_budget_ms validation
# ---------------------------------------------------------------------------


def test_negative_latency_budget_raises():
    with pytest.raises(ValidationError):
        _make_profile(latency_budget_ms=-1)


def test_zero_latency_budget_accepted():
    p = _make_profile(latency_budget_ms=0)
    assert p.latency_budget_ms == 0


def test_positive_latency_budget_accepted():
    p = _make_profile(latency_budget_ms=5000)
    assert p.latency_budget_ms == 5000


# ---------------------------------------------------------------------------
# MetricType enum
# ---------------------------------------------------------------------------


def test_all_metric_types_accepted():
    for mt in MetricType:
        m = EvalMetric(
            name=mt.value,
            metric_type=mt,
            threshold=0.5,
        )
        assert m.metric_type == mt
