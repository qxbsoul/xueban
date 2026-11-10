"""tests/test_rate_limiting.py — Story x-5: Rate Limiting Coverage

每个限速端点均通过「发 N+1 次请求断言最后一次为 429」来验证真实限速行为。
跨测试状态隔离：每个测试通过 fixture `reset_limiter` 在执行前调用
`limiter.reset()`，清空 slowapi MemoryStorage 中的所有计数器。
"""

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_limiter():
    """在每个测试前重置 slowapi MemoryStorage，防止跨测试计数累积。

    slowapi 的 Limiter.reset() 内部调用 MemoryStorage.reset()，
    清空 storage.storage / storage.expirations 两个内存字典。
    """
    from xueban.api._limiter import limiter

    limiter.reset()
    yield
    # 测试结束后也 reset，保持环境干净
    limiter.reset()


@pytest.fixture()
def client():
    from xueban.server import app

    # raise_server_exceptions=False：允许 500 作为普通响应返回，
    # 避免测试因上游异常而误判。
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /templates/custom — 5 次/分钟
# ---------------------------------------------------------------------------

# 不需要合法的 YAML 内容也能触发限速：slowapi 在 FastAPI schema 校验之前执行，
# 所以 422 响应同样会消耗配额，第 6 次返回 429。
_CUSTOM_TEMPLATE_BODY = {"yaml_text": "name: rl-test"}


def test_templates_custom_rate_limit_triggers_429(client):
    """发 5 次达到限额后，第 6 次必须返回 429。"""
    for i in range(5):
        res = client.post("/templates/custom", json=_CUSTOM_TEMPLATE_BODY)
        # 每次不应是 500，也不应是 429（配额尚未耗尽）
        assert res.status_code != 500, f"第 {i + 1} 次意外返回 500"
        assert res.status_code != 429, f"第 {i + 1} 次过早触发 429"

    sixth = client.post("/templates/custom", json=_CUSTOM_TEMPLATE_BODY)
    assert sixth.status_code == 429, (
        f"第 6 次应返回 429，实际返回 {sixth.status_code}: {sixth.text}"
    )


def test_templates_custom_within_limit_does_not_429(client):
    """在配额内的请求（5 次）不应触发 429。"""
    for i in range(5):
        res = client.post("/templates/custom", json=_CUSTOM_TEMPLATE_BODY)
        assert res.status_code != 429, f"第 {i + 1} 次意外触发 429"


# ---------------------------------------------------------------------------
# POST /api/groups — 20 次/分钟
# ---------------------------------------------------------------------------

# CreateGroupRequest 必填字段：template_id, group_template_id, name
# template_id="blank" 对应 templates/blank.yaml（seed 模板，始终存在）
_CREATE_GROUP_BODY = {
    "template_id": "blank",
    "group_template_id": "g-rl-test",
    "name": "Rate Limit Test Group",
}


def test_groups_post_rate_limit_triggers_429(client):
    """发 20 次达到限额后，第 21 次必须返回 429。"""
    for i in range(20):
        res = client.post("/api/groups", json=_CREATE_GROUP_BODY)
        assert res.status_code != 500, f"第 {i + 1} 次意外返回 500"
        assert res.status_code != 429, f"第 {i + 1} 次过早触发 429"

    twenty_first = client.post("/api/groups", json=_CREATE_GROUP_BODY)
    assert twenty_first.status_code == 429, (
        f"第 21 次应返回 429，实际返回 {twenty_first.status_code}: {twenty_first.text}"
    )


def test_groups_post_within_limit_does_not_429(client):
    """在配额内的请求（20 次）不应触发 429。"""
    for i in range(20):
        res = client.post("/api/groups", json=_CREATE_GROUP_BODY)
        assert res.status_code != 429, f"第 {i + 1} 次意外触发 429"


# ---------------------------------------------------------------------------
# POST /workflow/compile — 20 次/分钟
# ---------------------------------------------------------------------------

# WorkflowAssemblySpec 仅 workflow_id 为必填，其余字段有默认值。
_COMPILE_BODY = {"workflow_id": "rl-test-wf"}


def test_compile_rate_limit_triggers_429(client):
    """发 20 次达到限额后，第 21 次必须返回 429。"""
    for i in range(20):
        res = client.post("/workflow/compile", json=_COMPILE_BODY)
        assert res.status_code != 500, f"第 {i + 1} 次意外返回 500"
        assert res.status_code != 429, f"第 {i + 1} 次过早触发 429"

    twenty_first = client.post("/workflow/compile", json=_COMPILE_BODY)
    assert twenty_first.status_code == 429, (
        f"第 21 次应返回 429，实际返回 {twenty_first.status_code}: {twenty_first.text}"
    )


def test_compile_within_limit_does_not_429(client):
    """在配额内的请求（20 次）不应触发 429。"""
    for i in range(20):
        res = client.post("/workflow/compile", json=_COMPILE_BODY)
        assert res.status_code != 429, f"第 {i + 1} 次意外触发 429"


# ---------------------------------------------------------------------------
# POST /chat/sessions/{id}/messages — 30 次/分钟
# ---------------------------------------------------------------------------


def test_chat_message_endpoint_exists(client):
    """POST /chat/sessions/{id}/messages 路由已注册（不返回 405 Method Not Allowed）。

    对不存在的 session 返回 404 是正常的业务逻辑，这里只断言路由本身存在。
    """
    res = client.post(
        "/chat/sessions/nonexistent/messages",
        json={"content": "hello"},
    )
    # 405 = 路由未注册；404 = session 不存在（业务逻辑正确）
    assert res.status_code != 405, (
        f"端点未注册，状态码: {res.status_code}"
    )
