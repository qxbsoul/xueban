"""Story 4.8 — Archive API tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from xueban.api.archive import ArchiveService, set_service
from xueban.server import app


def _mock_run_result(run_id: str, started: datetime, ended: datetime | None, status: str = "succeeded", fails: int = 0):
    run = MagicMock()
    run.run_id = run_id
    run.workflow_id = "wf-test"
    run.status = status
    run.started_at = started
    run.ended_at = ended
    run.metadata = {"intent": f"intent-{run_id}", "template": "academic_paper"}

    result = MagicMock()
    result.run = run
    steps = []
    for i in range(fails):
        step = MagicMock()
        step.status = "failed"
        step.metadata = {}
        steps.append(step)
    result.steps = steps
    return result


@pytest.fixture
def client():
    now = datetime.now(timezone.utc)
    runs = {
        "r1": _mock_run_result("r1", now - timedelta(hours=1), now - timedelta(minutes=50), fails=2),
        "r2": _mock_run_result("r2", now - timedelta(days=10), now - timedelta(days=10)),
        "r3": _mock_run_result("r3", now - timedelta(hours=2), now - timedelta(hours=1)),
    }
    rt = MagicMock()
    rt._runs = runs
    svc = ArchiveService(runtime_service=rt)
    set_service(svc)
    return TestClient(app)


class TestArchiveList:
    def test_returns_runs_sorted_by_completed_at_desc(self, client):
        r = client.get("/archive/runs")
        assert r.status_code == 200
        data = r.json()["data"]
        ids = [x["run_id"] for x in data["runs"]]
        # r1 completed most recently, then r3, then r2
        assert ids[0] == "r1"
        assert "r3" in ids

    def test_badges_reflect_failures(self, client):
        r = client.get("/archive/runs")
        runs = r.json()["data"]["runs"]
        row = next(x for x in runs if x["run_id"] == "r1")
        assert row["badges"]["rejections"] == 2

    def test_window_24h_filters_old(self, client):
        r = client.get("/archive/runs?window=24h")
        ids = [x["run_id"] for x in r.json()["data"]["runs"]]
        assert "r2" not in ids
        assert "r1" in ids

    def test_search_filter(self, client):
        r = client.get("/archive/runs?search=intent-r1")
        data = r.json()["data"]
        assert len(data["runs"]) == 1
        assert data["runs"][0]["run_id"] == "r1"

    def test_limit_is_capped(self, client):
        r = client.get("/archive/runs?limit=200")
        # caller sends limit>100 — FastAPI returns 422
        assert r.status_code == 422

    def test_invalid_window(self, client):
        r = client.get("/archive/runs?window=1y")
        assert r.status_code == 422
