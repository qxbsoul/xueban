"""Story 2.8 AC1 — document structure + code alignment checks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CONTRACT_PATH = Path(__file__).parent.parent / "docs" / "AGENT_PLUGIN_CONTRACT.md"
README_PATH = Path(__file__).parent.parent / "README.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc_text() -> str:
    return CONTRACT_PATH.read_text(encoding="utf-8")


def _readme_text() -> str:
    return README_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Document structure — 9 required top-level sections (AC1)
# ---------------------------------------------------------------------------

REQUIRED_SECTIONS = [
    r"## 1\.",   # 概述
    r"## 2\.",   # AgentExecutor ABC
    r"## 3\.",   # 四种 Kind
    r"## 4\.",   # 三通道契约
    r"## 5\.",   # YAML 样板
    r"## 6\.",   # AgentEvent 命名空间
    r"## 7\.",   # 新 provider preset
    r"## 8\.",   # 健康检查与降级
    r"## 9\.",   # 参考
]


@pytest.mark.parametrize("pattern", REQUIRED_SECTIONS)
def test_required_section_present(pattern: str) -> None:
    text = _doc_text()
    assert re.search(pattern, text, re.MULTILINE), (
        f"AGENT_PLUGIN_CONTRACT.md missing section matching {pattern!r}"
    )


def test_document_has_nine_top_level_sections() -> None:
    text = _doc_text()
    sections = re.findall(r"^## \d+\.", text, re.MULTILINE)
    assert len(sections) == 9, f"Expected 9 numbered sections, found {len(sections)}: {sections}"


# ---------------------------------------------------------------------------
# Code alignment — 7 core event constants (AC1)
# ---------------------------------------------------------------------------

SEVEN_CORE_CONSTANTS = [
    "agent.dispatched",
    "agent.thinking",
    "agent.tool_called",
    "agent.tool_result",
    "agent.completed",
    "agent.failed",
    "agent.rejected",
]


@pytest.mark.parametrize("event_type", SEVEN_CORE_CONSTANTS)
def test_core_event_type_in_document(event_type: str) -> None:
    text = _doc_text()
    assert event_type in text, (
        f"AGENT_PLUGIN_CONTRACT.md missing core event type {event_type!r}"
    )


def test_all_seven_core_constants_in_code() -> None:
    from xueban.runtime.events import AgentEventType

    expected = set(SEVEN_CORE_CONSTANTS)
    code_values = {
        AgentEventType.DISPATCHED,
        AgentEventType.THINKING,
        AgentEventType.TOOL_CALLED,
        AgentEventType.TOOL_RESULT,
        AgentEventType.COMPLETED,
        AgentEventType.FAILED,
        AgentEventType.REJECTED,
    }
    assert code_values == expected, (
        f"Code constants mismatch. Missing from code: {expected - code_values}"
    )


# ---------------------------------------------------------------------------
# ABC method names alignment
# ---------------------------------------------------------------------------

ABC_METHODS = ["dispatch", "stream_events", "capabilities"]


@pytest.mark.parametrize("method", ABC_METHODS)
def test_abc_method_in_document(method: str) -> None:
    text = _doc_text()
    assert method in text, (
        f"AGENT_PLUGIN_CONTRACT.md missing ABC method name {method!r}"
    )


def test_abc_methods_in_code() -> None:
    from xueban.runtime.executors import AgentExecutor
    import inspect

    for method in ABC_METHODS:
        assert hasattr(AgentExecutor, method), (
            f"AgentExecutor missing method {method!r}"
        )
        func = getattr(AgentExecutor, method)
        assert getattr(func, "__isabstractmethod__", False), (
            f"AgentExecutor.{method} should be an abstractmethod"
        )


# ---------------------------------------------------------------------------
# YAML preset field alignment
# ---------------------------------------------------------------------------

YAML_PRESET_FIELDS = ["command", "args_template", "parse_format", "stdin_format"]


@pytest.mark.parametrize("field", YAML_PRESET_FIELDS)
def test_yaml_preset_field_in_document(field: str) -> None:
    text = _doc_text()
    assert field in text, (
        f"AGENT_PLUGIN_CONTRACT.md missing YAML preset field {field!r}"
    )


# ---------------------------------------------------------------------------
# README integration (AC1)
# ---------------------------------------------------------------------------

def test_readme_has_how_to_plug_section() -> None:
    text = _readme_text()
    assert "How to Plug Your Agent" in text, (
        "README.md missing 'How to Plug Your Agent' section"
    )


def test_readme_links_to_agent_plugin_contract() -> None:
    text = _readme_text()
    assert "AGENT_PLUGIN_CONTRACT.md" in text, (
        "README.md does not link to docs/AGENT_PLUGIN_CONTRACT.md"
    )


# ---------------------------------------------------------------------------
# Cross-reference links present
# ---------------------------------------------------------------------------

CROSS_REFS = [
    "HERMES_INTEGRATION_SPIKE.md",  # AC1 explicit requirement (Sprint 0 deliverable)
    "HERMES_CLAW_SPIKE.md",
    "SHADOWSOUL_RUNTIME_SPIKE.md",
]


@pytest.mark.parametrize("ref", CROSS_REFS)
def test_cross_reference_present(ref: str) -> None:
    text = _doc_text()
    assert ref in text, (
        f"AGENT_PLUGIN_CONTRACT.md missing cross-reference to {ref!r}"
    )


# ---------------------------------------------------------------------------
# AgentEventType.ALL alignment + AgentEvent.ts field documented (review patch)
# ---------------------------------------------------------------------------

def test_agent_event_type_all_frozenset_in_code() -> None:
    from xueban.runtime.events import AgentEventType

    assert isinstance(AgentEventType.ALL, frozenset)
    # 7 core + 3 additional = 10 canonical types
    assert len(AgentEventType.ALL) == 10


def test_doc_mentions_agent_event_type_all() -> None:
    text = _doc_text()
    assert "AgentEventType.ALL" in text, (
        "AGENT_PLUGIN_CONTRACT.md should document the canonical `AgentEventType.ALL` frozenset"
    )


def test_doc_mentions_event_ts_field() -> None:
    """AgentEvent serializes a `ts` field; wire format must document it."""
    from xueban.runtime.contracts import AgentEvent

    assert "ts" in AgentEvent.model_fields
    text = _doc_text()
    # §6.3 SSE Wire Format must mention the `ts` field so consumers parse it.
    assert "`ts`" in text or "\"ts\"" in text, (
        "AGENT_PLUGIN_CONTRACT.md §6.3 should document the `ts` field in SSE wire format"
    )


def test_doc_imports_pydantic_models_from_contracts_module() -> None:
    """ABC snippet must import AgentTask/AgentHandle/AgentCapabilities from contracts, not executors."""
    text = _doc_text()
    assert "xueban.runtime.contracts" in text, (
        "AGENT_PLUGIN_CONTRACT.md §2.1 should import Pydantic models from "
        "`xueban.runtime.contracts`, not `executors`"
    )


# ---------------------------------------------------------------------------
# Four kind values present
# ---------------------------------------------------------------------------

FOUR_KINDS = ["api", "cli", "mcp", "acp"]


@pytest.mark.parametrize("kind", FOUR_KINDS)
def test_kind_described_in_document(kind: str) -> None:
    text = _doc_text()
    assert kind in text, (
        f"AGENT_PLUGIN_CONTRACT.md missing kind description for {kind!r}"
    )
