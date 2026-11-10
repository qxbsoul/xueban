"""Tests for Story 3.6.7 — Template YAML Schema Extension.

Validates:
- AC1: 6 new optional fields on WorkflowTemplateSpec
- AC2: AgentRosterEntry + GroupTemplateSpec models
- AC3: 6 seed templates load correctly
- AC5: Backward compatibility with old YAML
- AC6: Cross-field validation (duplicate ids, unknown refs)
"""

from pathlib import Path

import pytest
import yaml

from xueban.highlevel import (
    AgentRosterEntry,
    GroupTemplateSpec,
    WorkflowTemplateSpec,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
LEGACY_TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "highlevel"
    / "minimal-registry"
    / "templates"
    / "docs-review-template.yaml"
)

SEED_TEMPLATES = [
    "solo-company.yaml",
    "academic-paper.yaml",
    "newsroom.yaml",
    "modern-startup.yaml",
    "consulting.yaml",
    "blank.yaml",
]


# ---------------------------------------------------------------------------
# AC2: Model unit tests
# ---------------------------------------------------------------------------


class TestAgentRosterEntry:
    def test_minimal(self):
        entry = AgentRosterEntry(id="ceo", name="CEO")
        assert entry.id == "ceo"
        assert entry.soul == ""
        assert entry.llm == ""
        assert entry.tools == []

    def test_full(self):
        entry = AgentRosterEntry(
            id="pi", name="PI", soul="Research lead", llm="claude-sonnet-4-6", tools=["search"]
        )
        assert entry.llm == "claude-sonnet-4-6"
        assert entry.tools == ["search"]


class TestGroupTemplateSpec:
    def test_minimal(self):
        g = GroupTemplateSpec(id="room", name="Room")
        assert g.agents == []
        assert g.policy_matrix == ""

    def test_full(self):
        g = GroupTemplateSpec(id="room", name="Room", agents=["a", "b"], policy_matrix="default")
        assert g.agents == ["a", "b"]


# ---------------------------------------------------------------------------
# AC1 + AC5: Default values and backward compatibility
# ---------------------------------------------------------------------------


class TestNewFieldDefaults:
    """Verify that a minimal spec (no new fields) still validates."""

    def test_defaults_on_minimal_spec(self):
        spec = WorkflowTemplateSpec(
            template_id="test",
            version="0.1",
            name="Test",
            flow={"entrypoint": "a", "edges": [{"from": "a", "to": "END", "type": "final"}]},
            agents=[{"id": "a", "ref": "a"}],
        )
        assert spec.user_role == "Owner"
        assert spec.default_ops_room_name == ""
        assert spec.brief_board_alias == "BriefBoard"
        assert spec.agent_roster == []
        assert spec.group_roster == []
        assert spec.theme_color == "#6366F1"

    @pytest.mark.skipif(not LEGACY_TEMPLATE.exists(), reason="legacy template not present")
    def test_backward_compat_legacy_yaml(self):
        """AC5: Old-format YAML without new fields loads with defaults."""
        with open(LEGACY_TEMPLATE, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        spec = WorkflowTemplateSpec.model_validate(data)
        assert spec.user_role == "Owner"
        assert spec.agent_roster == []
        assert spec.group_roster == []


# ---------------------------------------------------------------------------
# AC3: Seed template loading
# ---------------------------------------------------------------------------


class TestSeedTemplates:
    @pytest.mark.parametrize("filename", SEED_TEMPLATES)
    def test_seed_template_loads(self, filename: str):
        """AC3: Each seed template model_validates without error."""
        path = TEMPLATES_DIR / filename
        assert path.exists(), f"Seed template missing: {filename}"
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        spec = WorkflowTemplateSpec.model_validate(data)
        assert spec.template_id, "template_id must be set"
        assert spec.name, "name must be set"

    def test_solo_company_fields(self):
        with open(TEMPLATES_DIR / "solo-company.yaml", encoding="utf-8") as f:
            spec = WorkflowTemplateSpec.model_validate(yaml.safe_load(f))
        assert spec.user_role == "CEO"
        assert spec.default_ops_room_name == "CEO Ops Room"
        assert len(spec.agent_roster) == 8
        assert len(spec.group_roster) >= 1

    def test_academic_paper_fields(self):
        with open(TEMPLATES_DIR / "academic-paper.yaml", encoding="utf-8") as f:
            spec = WorkflowTemplateSpec.model_validate(yaml.safe_load(f))
        assert spec.user_role == "PI"
        assert len(spec.agent_roster) == 6
        # AC3: CitationReviewer replaces Compliance (decision 8)
        roster_ids = {e.id for e in spec.agent_roster}
        assert "citation-reviewer" in roster_ids
        assert "compliance" not in roster_ids

    def test_consulting_replaces_ming_cabinet(self):
        """Decision 3: no borrowed system terms; consulting replaces ming-cabinet."""
        assert (TEMPLATES_DIR / "consulting.yaml").exists()
        assert not (TEMPLATES_DIR / "ming-cabinet.yaml").exists()
        with open(TEMPLATES_DIR / "consulting.yaml", encoding="utf-8") as f:
            spec = WorkflowTemplateSpec.model_validate(yaml.safe_load(f))
        assert spec.user_role == "Engagement Partner"

    def test_blank_template_empty_roster(self):
        with open(TEMPLATES_DIR / "blank.yaml", encoding="utf-8") as f:
            spec = WorkflowTemplateSpec.model_validate(yaml.safe_load(f))
        assert spec.user_role == "Owner"
        assert spec.agent_roster == []
        assert spec.group_roster == []


# ---------------------------------------------------------------------------
# AC6: Cross-field validation
# ---------------------------------------------------------------------------


class TestCrossFieldValidation:
    def _base(self, **overrides):
        data = {
            "template_id": "test",
            "version": "0.1",
            "name": "Test",
            "flow": {"entrypoint": "a", "edges": [{"from": "a", "to": "END", "type": "final"}]},
            "agents": [{"id": "a", "ref": "a"}],
        }
        data.update(overrides)
        return data

    def test_duplicate_agent_roster_ids_rejected(self):
        data = self._base(
            agent_roster=[
                {"id": "x", "name": "X"},
                {"id": "x", "name": "X duplicate"},
            ]
        )
        with pytest.raises(ValueError, match="agent_roster ids must be unique"):
            WorkflowTemplateSpec.model_validate(data)

    def test_group_roster_unknown_agent_rejected(self):
        data = self._base(
            agent_roster=[{"id": "x", "name": "X"}],
            group_roster=[{"id": "g", "name": "G", "agents": ["x", "nonexistent"]}],
        )
        with pytest.raises(ValueError, match="group_roster 'g' references unknown agent ids"):
            WorkflowTemplateSpec.model_validate(data)

    def test_group_roster_can_reference_agents_field(self):
        """group_roster agents can reference ids from self.agents (not just agent_roster)."""
        data = self._base(
            group_roster=[{"id": "g", "name": "G", "agents": ["a"]}],
        )
        # 'a' is in self.agents, should pass
        spec = WorkflowTemplateSpec.model_validate(data)
        assert len(spec.group_roster) == 1

    def test_group_roster_can_reference_roster_ids(self):
        data = self._base(
            agent_roster=[{"id": "r1", "name": "R1"}],
            group_roster=[{"id": "g", "name": "G", "agents": ["r1"]}],
        )
        spec = WorkflowTemplateSpec.model_validate(data)
        assert spec.group_roster[0].agents == ["r1"]

    def test_duplicate_group_roster_ids_rejected(self):
        data = self._base(
            group_roster=[
                {"id": "g", "name": "G1"},
                {"id": "g", "name": "G2"},
            ],
        )
        with pytest.raises(ValueError, match="group_roster ids must be unique"):
            WorkflowTemplateSpec.model_validate(data)
