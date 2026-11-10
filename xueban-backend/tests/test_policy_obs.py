"""Story 4.9 — PolicyObsAggregator + /policy/stats endpoint tests."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from xueban.api.policy_observability import PolicyObsAggregator, set_aggregator
from xueban.runtime.events import RunEventBus
from xueban.server import app


def _publish_violation(bus: RunEventBus, run_id: str, policy: str, stage: str, outcome: str = "retry_ok", loops: int = 2):
    bus.publish(run_id, {
        "type": "policy.violation",
        "run_id": run_id,
        "policy": policy,
        "stage": stage,
        "reason": f"{policy} rejected at {stage}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "loops": loops,
    })


@pytest.fixture
def setup():
    bus = RunEventBus()
    rt = MagicMock()
    rt.list_runs = MagicMock(return_value=[])
    agg = PolicyObsAggregator(event_bus=bus, runtime_service=rt)
    set_aggregator(agg)
    return bus, agg


class TestEmpty:
    def test_empty_summary(self, setup):
        client = TestClient(app)
        r = client.get("/policy/stats?window=7d")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["summary"]["total_rejections"] == 0
        assert data["heatmap"] == []


class TestAggregation:
    def test_counts_heatmap(self, setup):
        bus, agg = setup
        agg.clear_cache()
        _publish_violation(bus, "r1", "legal_review", "review")
        _publish_violation(bus, "r1", "legal_review", "review")
        _publish_violation(bus, "r2", "legal_review", "plan")
        _publish_violation(bus, "r3", "brand_guideline", "deliver", outcome="aborted", loops=1)

        client = TestClient(app)
        r = client.get("/policy/stats?window=all")
        data = r.json()["data"]
        assert data["summary"]["total_rejections"] == 4
        rows = {row["policy"]: row for row in data["heatmap"]}
        assert rows["legal_review"]["counts"]["review"] == 2
        assert rows["legal_review"]["counts"]["plan"] == 1
        assert rows["brand_guideline"]["counts"]["deliver"] == 1

    def test_recovered_rate(self, setup):
        bus, agg = setup
        agg.clear_cache()
        for i in range(3):
            _publish_violation(bus, f"r{i}", "policy-a", "review", outcome="retry_ok")
        _publish_violation(bus, "r9", "policy-a", "review", outcome="aborted")
        client = TestClient(app)
        r = client.get("/policy/stats?window=all")
        summary = r.json()["data"]["summary"]
        assert summary["recovered_rate_pct"] == 75.0

    def test_top_policy_and_stage(self, setup):
        bus, agg = setup
        agg.clear_cache()
        _publish_violation(bus, "r1", "policy-a", "review")
        _publish_violation(bus, "r2", "policy-a", "review")
        _publish_violation(bus, "r3", "policy-b", "plan")

        client = TestClient(app)
        r = client.get("/policy/stats?window=all")
        summary = r.json()["data"]["summary"]
        assert summary["top_policy"]["name"] == "policy-a"
        assert summary["top_policy"]["count"] == 2
        assert summary["top_stage"]["name"] == "review"


class TestCache:
    def test_cached_result_is_reused(self, setup):
        bus, agg = setup
        agg.clear_cache()
        _publish_violation(bus, "r1", "policy-a", "review")

        stats1 = agg.aggregate("7d")
        # Mutate bus state, but cache should still return prior snapshot
        _publish_violation(bus, "r2", "policy-b", "review")
        stats2 = agg.aggregate("7d")
        assert stats1 is stats2

    def test_rejects_bad_window(self, setup):
        client = TestClient(app)
        r = client.get("/policy/stats?window=forever")
        assert r.status_code == 422
