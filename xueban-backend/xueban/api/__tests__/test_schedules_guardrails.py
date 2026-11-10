"""OpenBot routines 防呆三件套（2026-11-06 对齐批）契约测试。

覆盖：
  - 创建路径启用数上限：MAX_ENABLED_SCHEDULES 个启用计划后 POST → 422
    enabled_limit_reached
  - PATCH 停用→启用 同样受限
  - cron 最小间隔：*/5 * * * * → 422 cron_interval_too_short；*/20 → 通过
  - 连续失败熔断：bridge 抛错 × MAX_CONSECUTIVE_FAILURES → enabled=False +
    auto_disabled 标记；人工重新启用 → 标记清除（熔断复位）
  - 无 bridge：触发记 failed，禁止伪造成功执行
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import xueban.api.schedules as schedules_mod
from xueban.server import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(schedules_mod, "_SCHEDULES_DIR", tmp_path / "schedules")
    from xueban.services import schedule_secrets
    monkeypatch.setattr(schedule_secrets, "_KEY_PATH", tmp_path / "secret-key")
    # APScheduler 未随测试启动（scheduler.running 为 False），落盘即真源。
    api_client = TestClient(app)
    response = api_client.post("/api/xueban/auth/guest")
    assert response.status_code == 200
    api_client.headers.update({"Authorization": f"Bearer {response.json()['token']}"})
    return api_client


def _future_iso(hours: int = 24) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _create(client: TestClient, **overrides) -> dict:
    payload = {
        "group_id": "g-test",
        "start_at": _future_iso(),
        "task_description": "防呆测试计划",
    }
    payload.update(overrides)
    r = client.post("/api/schedules", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]


class TestEnabledCap:
    def test_create_rejected_at_cap(self, client, monkeypatch):
        """启用计划达上限 → POST 422 enabled_limit_reached。"""
        monkeypatch.setattr(schedules_mod, "MAX_ENABLED_SCHEDULES", 2)
        _create(client)
        _create(client)
        r = client.post(
            "/api/schedules",
            json={"group_id": "g", "start_at": _future_iso()},
        )
        assert r.status_code == 422
        body = r.json()
        assert body["detail"]["error"] == "enabled_limit_reached"

    def test_disabled_schedules_do_not_count(self, client, monkeypatch):
        """已停用计划不占名额：cap=1，停用第一个后可继续创建。"""
        monkeypatch.setattr(schedules_mod, "MAX_ENABLED_SCHEDULES", 1)
        first = _create(client)
        r = client.patch(
            f"/api/schedules/{first['schedule_id']}", json={"enabled": False}
        )
        assert r.status_code == 200
        second = _create(client)
        assert second["enabled"] is True

    def test_patch_enable_rejected_at_cap(self, client, monkeypatch):
        """停用→启用 达上限 → PATCH 422。"""
        monkeypatch.setattr(schedules_mod, "MAX_ENABLED_SCHEDULES", 1)
        first = _create(client)
        # 停用 first 腾出名额，才能建 second（second 占用唯一的启用位）
        client.patch(f"/api/schedules/{first['schedule_id']}", json={"enabled": False})
        second = _create(client)
        assert second["enabled"] is True
        # second 仍启用（占位中）→ 启用 first 即超限 → 422
        r = client.patch(
            f"/api/schedules/{first['schedule_id']}", json={"enabled": True}
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "enabled_limit_reached"


class TestCronMinInterval:
    def test_too_short_rejected(self, client):
        """*/5 分钟级高频 cron → 422 cron_interval_too_short。"""
        r = client.post(
            "/api/schedules",
            json={"group_id": "g", "cron_expression": "*/5 * * * *"},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "cron_interval_too_short"

    def test_star_step_15_allowed(self, client):
        """*/15 = 最小允许间隔 → 201。"""
        data = _create(client, cron_expression="*/15 * * * *")
        assert data["cron_expression"] == "*/15 * * * *"

    def test_daily_allowed(self, client):
        """0 9 * * * 每日一次 → 201。"""
        data = _create(client, cron_expression="0 9 * * *")
        assert data["cron_expression"] == "0 9 * * *"

    def test_listed_minutes_short_gap_rejected(self, client):
        """0,3,6... 列表分钟最短间隔 3 分钟 → 422。"""
        r = client.post(
            "/api/schedules",
            json={"group_id": "g", "cron_expression": "0,3,6 * * * *"},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "cron_interval_too_short"


class TestConsecutiveFailureBreaker:
    def test_auto_disable_after_threshold(self, client, monkeypatch):
        """bridge 连续失败达阈值 → enabled=False + auto_disabled 标记。"""
        monkeypatch.setattr(schedules_mod, "MAX_CONSECUTIVE_FAILURES", 3)

        def boom(record):
            raise RuntimeError("llm exploded")

        monkeypatch.setattr(schedules_mod, "_RUN_EXECUTOR_BRIDGE", boom)
        data = _create(client, cron_expression="0 9 * * *")
        sid = data["schedule_id"]
        for _ in range(3):
            schedules_mod._schedule_job(sid)
        rec = schedules_mod._load_schedule(sid)
        assert rec["enabled"] is False
        assert rec["auto_disabled"] is True
        assert "连续失败" in rec["auto_disabled_reason"]
        assert all(r["status"] == "failed" for r in rec["runs"][:3])

    def test_manual_reenable_clears_breaker(self, client, monkeypatch):
        """人工重新启用 → auto_disabled 标记清除（熔断复位）。"""
        monkeypatch.setattr(schedules_mod, "MAX_CONSECUTIVE_FAILURES", 2)
        monkeypatch.setattr(
            schedules_mod, "_RUN_EXECUTOR_BRIDGE", lambda record: (_ for _ in ()).throw(RuntimeError("x"))
        )
        data = _create(client, cron_expression="0 9 * * *")
        sid = data["schedule_id"]
        schedules_mod._schedule_job(sid)
        schedules_mod._schedule_job(sid)
        assert schedules_mod._load_schedule(sid)["auto_disabled"] is True
        r = client.patch(f"/api/schedules/{sid}", json={"enabled": True})
        assert r.status_code == 200
        rec = r.json()["data"]
        assert rec["enabled"] is True
        assert "auto_disabled" not in rec

    def test_success_resets_nothing_needed(self, client, monkeypatch):
        """成功穿插 → 不触发熔断（连续计数被成功打断）。"""
        monkeypatch.setattr(schedules_mod, "MAX_CONSECUTIVE_FAILURES", 2)
        calls = {"n": 0}

        def flaky(record):
            calls["n"] += 1
            if calls["n"] % 2 == 1:
                raise RuntimeError("odd fails")

        monkeypatch.setattr(schedules_mod, "_RUN_EXECUTOR_BRIDGE", flaky)
        data = _create(client, cron_expression="0 9 * * *")
        sid = data["schedule_id"]
        for _ in range(4):
            schedules_mod._schedule_job(sid)
        rec = schedules_mod._load_schedule(sid)
        assert rec["enabled"] is True  # 失败/成功交替，永不连续达 2
        assert "auto_disabled" not in rec

    def test_no_bridge_fails_closed(self, client, monkeypatch):
        """无 bridge → 触发记 failed，不再伪造纯提醒执行成功。"""
        monkeypatch.setattr(schedules_mod, "_RUN_EXECUTOR_BRIDGE", None)
        data = _create(client, cron_expression="0 9 * * *")
        sid = data["schedule_id"]
        schedules_mod._schedule_job(sid)
        rec = schedules_mod._load_schedule(sid)
        assert rec["runs"][0]["status"] == "failed"
        assert "未执行" in rec["runs"][0]["error"]
        assert rec["enabled"] is True
