from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """全局 limiter 隔离：每个测试前后清空 slowapi MemoryStorage 计数，
    防止跨测试文件的限速状态积累导致非限速测试误收 429。
    """
    try:
        from xueban.api._limiter import limiter
        limiter.reset()
    except Exception:
        pass
    yield
    try:
        from xueban.api._limiter import limiter
        limiter.reset()
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _isolate_xueban_data(tmp_path_factory):
    """把所有 .xueban 数据目录重定向到一个 session 级 tmp 根。

    背景：测试用 TestClient 调真实端点（POST /api/groups 等）时，持久化会落到
    仓库根的真实 ``.xueban/``。历史上这让 dev app 的 /chat 攒了几十个
    "Test Group" / "Group A" / "Trimmed" 等垃圾会话（用户 2026-05-29 反馈）。

    用 **session 级共享 root**（而非每函数独立 tmp）以保留现有测试"跨用例共享
    目录"的语义——等价于原来共享真实 .xueban，只是搬到 tmp，零行为破坏。
    端点在运行时通过模块级常量（_GROUPS_DIR 等）读目录，故 setattr 模块属性即可
    生效；单测若自带更细的 monkeypatch（如 test_groups_reactions.py）会在 function
    层覆盖本 fixture，互不冲突。
    """
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    root = tmp_path_factory.mktemp("xueban_data")
    try:
        from xueban.api import agents, groups, schedules, teams

        mp.setattr(groups, "_GROUPS_DIR", root / "groups", raising=False)
        mp.setattr(agents, "_AGENTS_DIR", root / "agents", raising=False)
        mp.setattr(teams, "_TEAMS_DIR", root / "teams", raising=False)
        mp.setattr(schedules, "_SCHEDULES_DIR", root / "schedules", raising=False)
    except Exception:
        pass
    try:
        # XUEBAN OS（2026-11-04）：独立 xueban.sqlite 同样重定向到 tmp 根，
        # 防止 TestClient 触发的种子数据写进仓库真实 .xueban/。
        from xueban.services import xueban_store

        mp.setattr(xueban_store, "_BASE_DIR", root, raising=False)
        mp.setattr(xueban_store, "_DB_PATH", root / "xueban.sqlite", raising=False)
    except Exception:
        pass
    yield
    mp.undo()


PHASE1_BASELINE_TEST_FILES = {
    "test_runtime_contract.py",
    "test_runtime_examples.py",
}

LEGACY_DIR_NAME = "legacy"


def pytest_ignore_collect(collection_path, config: pytest.Config) -> bool:
    if config.getoption("--run-legacy"):
        return False
    path = Path(str(collection_path))
    return LEGACY_DIR_NAME in path.parts


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    run_legacy = config.getoption("--run-legacy")
    if run_legacy:
        return

    skip_legacy = pytest.mark.skip(
        reason="legacy test baseline; excluded from Phase 1 contract baseline unless --run-legacy is set"
    )
    for item in items:
        path = Path(str(item.fspath))
        if LEGACY_DIR_NAME in path.parts:
            item.add_marker("legacy")
            item.add_marker(skip_legacy)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-legacy",
        action="store_true",
        default=False,
        help="include legacy tests that are not part of the Phase 1 contract baseline",
    )
