"""
xueban/assembly/learner.py

Phase 3 Step 1: Contextual Bandit for block activation.

This module wraps ActivationSelector with a learning layer. When sufficient
training data is available, it uses learned goal→block affinity scores to
influence block selection. Otherwise it falls back to the greedy set-cover
selector from Phase 1.

The bandit learns from ActivationTrainingDataset samples:
  - Each sample records which block was activated for which goal, and the
    resulting reward_hints (artifact_count, continued_flow, etc.)
  - The bandit builds a simple affinity model: for each (goal_token, block_id)
    pair, track average reward. At selection time, score each block by how well
    its historically-associated tokens match the current goal.

This is intentionally simple (no neural networks, no embeddings). The point is
to establish the learning loop and prove it works. Phase 3 Step 2 will upgrade
to policy gradient with proper embeddings.

Evolution path:
  v1 (this file): token-level affinity table (Contextual Bandit)
  v2: TF-IDF or LLM embedding + linear policy
  v3: Policy Gradient on block selection + topology
  v4: Graph-RL / GNN (full spontaneous assembly)
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, TYPE_CHECKING

from xueban.assembly.activation import ActivationResult, ActivationSelector, CatalogActivationCandidate

if TYPE_CHECKING:
    from xueban.highlevel import WorkflowBlockSpec
    from xueban.runtime.contracts import ActivationTrainingDataset


# ---------------------------------------------------------------------------
# Reward computation
# ---------------------------------------------------------------------------

# Default weights for reward_hints → scalar reward.
# These match the signals produced by RuntimeService._build_step_feedback_record.
DEFAULT_REWARD_WEIGHTS: Dict[str, float] = {
    "artifact_count": 0.5,
    "delegated_run": 0.2,
    "continued_flow": 1.0,
    "review_gate_triggered": -0.2,
    "selected_candidates": 0.1,
}


def compute_reward(
    hints: Dict[str, float],
    weights: Dict[str, float] | None = None,
) -> float:
    """Compute a scalar reward from reward_hints."""
    w = weights or DEFAULT_REWARD_WEIGHTS
    return sum(hints.get(k, 0.0) * v for k, v in w.items())


# ---------------------------------------------------------------------------
# Token extraction (reused from activation.py)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    tokens = re.findall(r"[\u4e00-\u9fff]{1,4}|[a-zA-Z0-9]+", text)
    return [t.lower() for t in tokens]


# ---------------------------------------------------------------------------
# ActivationBandit
# ---------------------------------------------------------------------------

class ActivationBandit:
    """
    Contextual Bandit for block selection.

    Maintains a token→block→average_reward affinity table. At selection time:
    1. Tokenize goal
    2. For each block, compute score = sum of affinity[token][block] for matching tokens
    3. Select blocks with score > threshold (or fallback to greedy)

    Falls back to ActivationSelector (greedy set-cover) when:
    - No training data
    - Insufficient samples (< min_samples)
    - All scores are zero (OOD goal)
    """

    def __init__(
        self,
        *,
        min_samples: int = 50,
        selection_threshold: float = 0.3,
    ) -> None:
        self._greedy = ActivationSelector()
        self.min_samples = min_samples
        self.selection_threshold = selection_threshold

        # Affinity table: token → block_id → (total_reward, count)
        self._affinity: Dict[str, Dict[str, tuple[float, int]]] = defaultdict(
            lambda: defaultdict(lambda: (0.0, 0))
        )
        self._trained_sample_count = 0

    @property
    def is_trained(self) -> bool:
        return self._trained_sample_count >= self.min_samples

    def train(self, dataset: "ActivationTrainingDataset") -> None:
        """Learn token→block affinities from training samples."""
        for sample in dataset.samples:
            if sample.assembly_block_id is None or sample.assembly_goal is None:
                continue  # skip non-assembly samples

            reward = compute_reward(sample.reward_hints)
            # Add step_status bonus: succeeded gets +1, failed gets -0.5
            if sample.step_status == "succeeded":
                reward += 1.0
            elif sample.step_status == "failed":
                reward -= 0.5

            tokens = _tokenize(sample.assembly_goal)
            block_id = sample.assembly_block_id

            for token in tokens:
                prev_total, prev_count = self._affinity[token][block_id]
                self._affinity[token][block_id] = (prev_total + reward, prev_count + 1)

        self._trained_sample_count += len(dataset.samples)

    def get_block_scores(self, goal: str) -> Dict[str, float]:
        """
        Compute per-block affinity scores for a goal string.
        Returns {block_id: average_score}.
        """
        if not self.is_trained:
            return {}

        tokens = set(_tokenize(goal))
        block_scores: Dict[str, float] = defaultdict(float)
        block_token_count: Dict[str, int] = defaultdict(int)

        for token in tokens:
            if token not in self._affinity:
                continue
            for block_id, (total, count) in self._affinity[token].items():
                if count > 0:
                    avg = total / count
                    block_scores[block_id] += avg
                    block_token_count[block_id] += 1

        # Normalize by number of matching tokens to avoid bias toward blocks
        # that appeared with many different tokens
        for block_id in block_scores:
            if block_token_count[block_id] > 0:
                block_scores[block_id] /= block_token_count[block_id]

        return dict(block_scores)

    def select(
        self,
        goal: str,
        catalog: Dict[str, "WorkflowBlockSpec"],
    ) -> ActivationResult:
        """
        Select blocks for a goal. Uses learned scores when trained,
        falls back to greedy set-cover otherwise.
        """
        if not self.is_trained:
            return self._greedy.select(goal, catalog)

        scores = self.get_block_scores(goal)

        # No scores at all → OOD, fallback to greedy
        if not scores or max(scores.values()) <= 0:
            return self._greedy.select(goal, catalog)

        # Select blocks above threshold
        max_score = max(scores.values())
        threshold = max_score * self.selection_threshold

        selected: List[CatalogActivationCandidate] = []
        for block_id, score in sorted(scores.items(), key=lambda x: -x[1]):
            if score >= threshold and block_id in catalog:
                block = catalog[block_id]
                selected.append(
                    CatalogActivationCandidate(
                        block_id=block_id,
                        matched_capabilities=list(block.capabilities),
                    )
                )

        if not selected:
            return self._greedy.select(goal, catalog)

        # Check coverage
        all_caps = set()
        for c in selected:
            all_caps.update(c.matched_capabilities)

        return ActivationResult(
            candidates=selected,
            complete=len(selected) > 0,
            missing_capabilities=[],
            fallback_policy="surface_to_user",
        )
