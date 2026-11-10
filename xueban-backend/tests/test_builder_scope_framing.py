"""Story 13.5: Agent Scope Framing — 后端测试

测试点：
  1. CollaborationContract 模型默认值与字段
  2. RoleProfile.collaboration_contract 字段（可选，向后兼容）
  3. 带 team_member_candidate scope 的 Blueprint 发布后 scope_hint 写入 Catalog
  4. standalone Blueprint 发布后 scope_hint 不写入（None）
  5. _infer_scope_hint 逻辑正确
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    CollaborationContract,
    RoleProfile,
)
from xueban.runtime.catalog_service import (
    CatalogService,
    RegisterPublishedAppRequest,
    _infer_scope_hint,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_role(name: str = "test_agent", **kwargs) -> RoleProfile:
    return RoleProfile(name=name, **kwargs)


def make_blueprint(roles: list[RoleProfile], mode: str = "single") -> AgentBlueprint:
    return AgentBlueprint(
        name="Test Blueprint",
        goal="Test goal for scope framing",
        mode=mode,
        role_profiles=roles,
    )


# ---------------------------------------------------------------------------
# 1. CollaborationContract 模型
# ---------------------------------------------------------------------------

class TestCollaborationContract:
    def test_default_values(self):
        cc = CollaborationContract()
        assert cc.scope == "standalone"
        assert cc.accepts_from == []
        assert cc.delivers_to == []
        assert cc.collaboration_style == "push"

    def test_team_member_candidate(self):
        cc = CollaborationContract(
            scope="team_member_candidate",
            accepts_from=["planner"],
            delivers_to=["reviewer"],
            collaboration_style="pull",
        )
        assert cc.scope == "team_member_candidate"
        assert cc.accepts_from == ["planner"]
        assert cc.delivers_to == ["reviewer"]
        assert cc.collaboration_style == "pull"

    def test_invalid_scope_raises(self):
        with pytest.raises(Exception):
            CollaborationContract(scope="invalid_scope")  # type: ignore[arg-type]

    def test_invalid_style_raises(self):
        with pytest.raises(Exception):
            CollaborationContract(collaboration_style="broadcast")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. RoleProfile.collaboration_contract
# ---------------------------------------------------------------------------

class TestRoleProfileCollaborationContract:
    def test_defaults_to_none(self):
        """缺省时 collaboration_contract 为 None（向后兼容）。"""
        role = make_role()
        assert role.collaboration_contract is None

    def test_accepts_collaboration_contract(self):
        cc = CollaborationContract(
            scope="team_member_candidate",
            accepts_from=["planner"],
            delivers_to=[],
            collaboration_style="push",
        )
        role = make_role(collaboration_contract=cc)
        assert role.collaboration_contract is not None
        assert role.collaboration_contract.scope == "team_member_candidate"

    def test_serialization_round_trip(self):
        """model_dump → model_validate 往返不丢失 collaboration_contract。"""
        cc = CollaborationContract(
            scope="team_member_candidate",
            accepts_from=["planner", "manager"],
            delivers_to=["reviewer"],
            collaboration_style="pull",
        )
        role = make_role(collaboration_contract=cc)
        dumped = role.model_dump(mode="json")
        restored = RoleProfile.model_validate(dumped)
        assert restored.collaboration_contract is not None
        assert restored.collaboration_contract.scope == "team_member_candidate"
        assert restored.collaboration_contract.accepts_from == ["planner", "manager"]


# ---------------------------------------------------------------------------
# 3. _infer_scope_hint 逻辑
# ---------------------------------------------------------------------------

class TestInferScopeHint:
    def test_standalone_returns_none(self):
        roles = [make_role()]
        bp = make_blueprint(roles)
        assert _infer_scope_hint(bp) is None

    def test_team_member_candidate_returns_hint(self):
        cc = CollaborationContract(scope="team_member_candidate")
        roles = [make_role(collaboration_contract=cc)]
        bp = make_blueprint(roles)
        assert _infer_scope_hint(bp) == "team_member_candidate"

    def test_mixed_roles_any_team_returns_hint(self):
        """若有任意一个 role 是 team_member_candidate，则返回提示。"""
        standalone_role = make_role(name="standalone")
        team_role = make_role(
            name="team_member",
            collaboration_contract=CollaborationContract(scope="team_member_candidate"),
        )
        bp = make_blueprint([standalone_role, team_role], mode="team")
        assert _infer_scope_hint(bp) == "team_member_candidate"

    def test_explicit_standalone_contract_returns_none(self):
        """显式设置 standalone scope 也应返回 None（不写 scope_hint）。"""
        cc = CollaborationContract(scope="standalone")
        roles = [make_role(collaboration_contract=cc)]
        bp = make_blueprint(roles)
        # standalone 不触发 scope_hint
        assert _infer_scope_hint(bp) is None


# ---------------------------------------------------------------------------
# 4. CatalogService.register_published_app — scope_hint 写入
# ---------------------------------------------------------------------------

class TestCatalogScopeHintRegistration:
    def _make_service(self, tmp_path: Path) -> CatalogService:
        return CatalogService(storage_dir=tmp_path / "catalog")

    def test_team_candidate_blueprint_writes_scope_hint(self, tmp_path: Path):
        """发布带 team_member_candidate scope 的 Blueprint → scope_hint = 'team_member_candidate'。"""
        svc = self._make_service(tmp_path)

        cc = CollaborationContract(
            scope="team_member_candidate",
            accepts_from=["planner"],
            delivers_to=["reviewer"],
            collaboration_style="push",
        )
        role = make_role(collaboration_contract=cc)
        bp = make_blueprint([role])

        req = RegisterPublishedAppRequest(
            blueprint=bp,
            template_id="tmpl-001",
            workflow_id="wf-001",
            author="test_user",
            kit_type="custom",
        )
        summary = svc.register_published_app(req)
        assert summary.scope_hint == "team_member_candidate"

    def test_standalone_blueprint_no_scope_hint(self, tmp_path: Path):
        """发布 standalone Blueprint → scope_hint = None（不写入）。"""
        svc = self._make_service(tmp_path)

        role = make_role()
        bp = make_blueprint([role])

        req = RegisterPublishedAppRequest(
            blueprint=bp,
            template_id="tmpl-002",
            workflow_id="wf-002",
            author="test_user",
            kit_type="custom",
        )
        summary = svc.register_published_app(req)
        assert summary.scope_hint is None

    def test_scope_hint_persisted_in_record(self, tmp_path: Path):
        """scope_hint 写入磁盘后，list_apps 的摘要中也包含该字段。"""
        svc = self._make_service(tmp_path)

        cc = CollaborationContract(scope="team_member_candidate")
        role = make_role(collaboration_contract=cc)
        bp = make_blueprint([role])

        req = RegisterPublishedAppRequest(blueprint=bp, author="tester")
        svc.register_published_app(req)

        result = svc.list_apps()
        apps = result["apps"]
        assert len(apps) == 1
        assert apps[0]["scope_hint"] == "team_member_candidate"

    def test_standalone_no_scope_hint_in_list(self, tmp_path: Path):
        """standalone App 的 list_apps 摘要中 scope_hint 为 None。"""
        svc = self._make_service(tmp_path)

        role = make_role()
        bp = make_blueprint([role])

        req = RegisterPublishedAppRequest(blueprint=bp, author="tester")
        svc.register_published_app(req)

        result = svc.list_apps()
        apps = result["apps"]
        assert len(apps) == 1
        # None is acceptable (field absent or explicitly None)
        assert apps[0].get("scope_hint") is None

    def test_multiple_roles_any_team_triggers_hint(self, tmp_path: Path):
        """多角色 Blueprint，只要一个是 team_member_candidate，就触发 scope_hint。"""
        svc = self._make_service(tmp_path)

        role_a = make_role(name="planner")
        role_b = make_role(
            name="executor",
            collaboration_contract=CollaborationContract(scope="team_member_candidate"),
        )
        bp = make_blueprint([role_a, role_b], mode="team")

        req = RegisterPublishedAppRequest(blueprint=bp, author="tester", kit_type="custom")
        summary = svc.register_published_app(req)
        assert summary.scope_hint == "team_member_candidate"
