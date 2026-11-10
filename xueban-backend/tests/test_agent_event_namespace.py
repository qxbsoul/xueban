"""Story 2.6 — AgentEventType 常量完整性测试 (AC1)。"""
from __future__ import annotations

import pytest

from xueban.runtime.events import AgentEventType


class TestAgentEventTypeConstants:
    def test_dispatched_exists(self):
        assert AgentEventType.DISPATCHED == "agent.dispatched"

    def test_thinking_exists(self):
        assert AgentEventType.THINKING == "agent.thinking"

    def test_tool_called_exists(self):
        assert AgentEventType.TOOL_CALLED == "agent.tool_called"

    def test_tool_result_exists(self):
        assert AgentEventType.TOOL_RESULT == "agent.tool_result"

    def test_completed_exists(self):
        assert AgentEventType.COMPLETED == "agent.completed"

    def test_failed_exists(self):
        assert AgentEventType.FAILED == "agent.failed"

    def test_rejected_exists(self):
        assert AgentEventType.REJECTED == "agent.rejected"

    def test_all_seven_core_types_in_all_set(self):
        core = {
            AgentEventType.DISPATCHED, AgentEventType.THINKING,
            AgentEventType.TOOL_CALLED, AgentEventType.TOOL_RESULT,
            AgentEventType.COMPLETED, AgentEventType.FAILED,
            AgentEventType.REJECTED,
        }
        assert core.issubset(AgentEventType.ALL)

    def test_all_values_start_with_agent_prefix(self):
        for val in AgentEventType.ALL:
            assert val.startswith("agent."), f"{val!r} should start with 'agent.'"

    def test_importable_from_runtime(self):
        from xueban.runtime import AgentEventType as _AgentEventType
        assert _AgentEventType.COMPLETED == "agent.completed"
