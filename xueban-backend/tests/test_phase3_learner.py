"""
Phase 3 tests: Contextual Bandit learner for block activation.

Tests that ActivationBandit:
- Falls back to greedy selector when no training data
- Learns from training samples and shifts block selection probabilities
- Computes reward correctly from reward_hints
- Integrates with the existing ActivationSelector interface
"""
from __future__ import annotations

from typing import Dict, List

import pytest

from xueban.highlevel import build_builtin_block_catalog


# ---------------------------------------------------------------------------
# 1. Reward computation
# ---------------------------------------------------------------------------

def test_compute_reward_from_hints():
    from xueban.assembly.learner import compute_reward

    hints = {
        "artifact_count": 1.0,
        "delegated_run": 0.0,
        "continued_flow": 1.0,
        "review_gate_triggered": 0.0,
        "selected_candidates": 2.0,
    }
    reward = compute_reward(hints)
    assert isinstance(reward, float)
    assert reward > 0


def test_compute_reward_empty_hints():
    from xueban.assembly.learner import compute_reward

    reward = compute_reward({})
    assert reward == 0.0


# ---------------------------------------------------------------------------
# 2. ActivationBandit fallback to greedy
# ---------------------------------------------------------------------------

def test_bandit_fallback_to_greedy_without_data():
    from xueban.assembly.learner import ActivationBandit

    catalog = build_builtin_block_catalog()
    bandit = ActivationBandit()

    result = bandit.select("plan the task", catalog)
    # Should behave exactly like greedy selector
    assert result.complete is True
    block_ids = {c.block_id for c in result.candidates}
    assert "plan" in block_ids


def test_bandit_fallback_on_insufficient_data():
    from xueban.assembly.learner import ActivationBandit
    from xueban.runtime.contracts import ActivationTrainingDataset

    catalog = build_builtin_block_catalog()
    # Only 2 samples — below default threshold of 50
    dataset = ActivationTrainingDataset(
        samples=[
            _make_sample("plan", "plan the work", "succeeded"),
            _make_sample("execute", "run the task", "succeeded"),
        ]
    )
    bandit = ActivationBandit(min_samples=50)
    bandit.train(dataset)

    result = bandit.select("plan the task", catalog)
    # Still greedy — not enough data
    assert result.complete is True


# ---------------------------------------------------------------------------
# 3. Training and learned selection
# ---------------------------------------------------------------------------

def test_bandit_trains_from_dataset():
    from xueban.assembly.learner import ActivationBandit
    from xueban.runtime.contracts import ActivationTrainingDataset

    catalog = build_builtin_block_catalog()

    # Generate enough samples to pass threshold
    samples = []
    for i in range(60):
        samples.append(_make_sample("plan", "plan the work", "succeeded", reward_hints={"continued_flow": 1.0}))
        samples.append(_make_sample("execute", "execute the task", "succeeded", reward_hints={"artifact_count": 1.0}))

    dataset = ActivationTrainingDataset(samples=samples)
    bandit = ActivationBandit(min_samples=50)
    bandit.train(dataset)

    assert bandit.is_trained is True


def test_bandit_learned_select_returns_valid_result():
    from xueban.assembly.learner import ActivationBandit
    from xueban.runtime.contracts import ActivationTrainingDataset

    catalog = build_builtin_block_catalog()

    samples = []
    for i in range(60):
        samples.append(_make_sample("plan", "plan the work", "succeeded", reward_hints={"continued_flow": 1.0}))

    dataset = ActivationTrainingDataset(samples=samples)
    bandit = ActivationBandit(min_samples=50)
    bandit.train(dataset)

    result = bandit.select("plan something", catalog)
    # Should return a valid ActivationResult
    assert hasattr(result, "complete")
    assert hasattr(result, "candidates")


def test_bandit_prefers_historically_successful_blocks():
    """
    If training data shows 'plan' succeeds often with goal containing 'plan',
    the bandit should prefer selecting 'plan' for similar goals.
    """
    from xueban.assembly.learner import ActivationBandit
    from xueban.runtime.contracts import ActivationTrainingDataset

    catalog = build_builtin_block_catalog()

    # plan block: high reward when goal contains "plan"
    samples = []
    for i in range(60):
        samples.append(_make_sample(
            "plan", "plan the work", "succeeded",
            reward_hints={"continued_flow": 1.0, "artifact_count": 1.0},
        ))
    # review block: low reward
    for i in range(60):
        samples.append(_make_sample(
            "review", "review something", "failed",
            reward_hints={"continued_flow": 0.0, "artifact_count": 0.0},
        ))

    dataset = ActivationTrainingDataset(samples=samples)
    bandit = ActivationBandit(min_samples=50)
    bandit.train(dataset)

    # For a "plan" goal, plan block should have higher score
    scores = bandit.get_block_scores("plan the task")
    assert scores.get("plan", 0) > scores.get("review", 0)


# ---------------------------------------------------------------------------
# 4. Score inspection
# ---------------------------------------------------------------------------

def test_bandit_get_block_scores_untrained():
    from xueban.assembly.learner import ActivationBandit

    bandit = ActivationBandit()
    scores = bandit.get_block_scores("plan the task")
    # Untrained: all scores should be empty or zero
    assert isinstance(scores, dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sample(
    block_id: str,
    goal: str,
    status: str,
    reward_hints: Dict[str, float] | None = None,
):
    from xueban.runtime.contracts import ActivationTrainingSample

    return ActivationTrainingSample(
        sample_id=f"s-{block_id}-{id(goal)}",
        run_id="run-test",
        workflow_id="wf-test",
        node_id=block_id,
        step_status=status,
        activation_mode="always",
        activation_decision="activated",
        assembly_block_id=block_id,
        assembly_goal=goal,
        reward_hints=reward_hints or {},
    )
