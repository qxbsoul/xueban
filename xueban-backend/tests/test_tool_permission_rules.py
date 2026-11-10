"""tests/test_tool_permission_rules.py — AC4, AC7: deny > ask > allow 规则评估"""
from __future__ import annotations

import pytest
from xueban.runtime.contracts_builder import (
    PermissionRule,
    ToolPolicy,
    evaluate_permission,
)


def make_policy(rules: list, default: str = "allow") -> ToolPolicy:
    return ToolPolicy(
        tool_id="test:tool",
        permission_rules=[PermissionRule(**r) for r in rules],
        default_permission=default,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Default permission (no rules)
# ---------------------------------------------------------------------------


def test_no_rules_returns_default_allow():
    policy = make_policy([], default="allow")
    assert evaluate_permission(policy, {}) == "allow"


def test_no_rules_returns_default_ask():
    policy = make_policy([], default="ask")
    assert evaluate_permission(policy, {}) == "ask"


def test_no_rules_returns_default_deny():
    policy = make_policy([], default="deny")
    assert evaluate_permission(policy, {}) == "deny"


# ---------------------------------------------------------------------------
# deny > ask > allow precedence
# ---------------------------------------------------------------------------


def test_deny_wins_over_ask_and_allow():
    rules = [
        {"permission": "allow", "arg_pattern": ""},
        {"permission": "ask", "arg_pattern": ""},
        {"permission": "deny", "arg_pattern": ""},
    ]
    assert evaluate_permission(make_policy(rules), {}) == "deny"


def test_ask_wins_when_no_deny():
    rules = [
        {"permission": "allow", "arg_pattern": ""},
        {"permission": "ask", "arg_pattern": ""},
    ]
    assert evaluate_permission(make_policy(rules), {}) == "ask"


def test_allow_wins_when_only_allow():
    rules = [{"permission": "allow", "arg_pattern": ""}]
    assert evaluate_permission(make_policy(rules), {}) == "allow"


# ---------------------------------------------------------------------------
# arg_pattern matching
# ---------------------------------------------------------------------------


def test_exact_arg_pattern_match():
    rules = [{"permission": "deny", "arg_pattern": "query:小红书"}]
    assert evaluate_permission(make_policy(rules), {"query": "小红书"}) == "deny"
    assert evaluate_permission(make_policy(rules), {"query": "其他"}) == "allow"


def test_glob_wildcard_in_pattern():
    rules = [{"permission": "ask", "arg_pattern": "query:*小红书*"}]
    assert evaluate_permission(make_policy(rules), {"query": "搜索小红书热帖"}) == "ask"
    assert evaluate_permission(make_policy(rules), {"query": "GitHub API"}) == "allow"


def test_empty_arg_pattern_matches_all():
    rules = [{"permission": "deny", "arg_pattern": ""}]
    assert evaluate_permission(make_policy(rules), {}) == "deny"
    assert evaluate_permission(make_policy(rules), {"query": "anything"}) == "deny"


def test_missing_arg_key_treated_as_empty_string():
    rules = [{"permission": "deny", "arg_pattern": "lang:bash"}]
    # 'lang' not in args → treated as '' → does not match 'bash'
    assert evaluate_permission(make_policy(rules), {}) == "allow"
    assert evaluate_permission(make_policy(rules), {"lang": "bash"}) == "deny"
    assert evaluate_permission(make_policy(rules), {"lang": "python"}) == "allow"


# ---------------------------------------------------------------------------
# Scenario from AC4 story example
# ---------------------------------------------------------------------------


def test_story_scenario_web_search_rules():
    """
    Rules from story AC4:
      web_search(query:*小红书*) → ask
      code_executor(lang:python) → allow
      code_executor(lang:bash)   → deny
    Default: allow
    """
    web_search_policy = ToolPolicy(
        tool_id="builtin:web_search",
        permission_rules=[
            PermissionRule(permission="ask", arg_pattern="query:*小红书*"),
        ],
        default_permission="allow",
    )
    assert evaluate_permission(web_search_policy, {"query": "小红书美妆"}) == "ask"
    assert evaluate_permission(web_search_policy, {"query": "GitHub Copilot"}) == "allow"

    code_executor_policy = ToolPolicy(
        tool_id="builtin:code_executor",
        permission_rules=[
            PermissionRule(permission="deny", arg_pattern="lang:bash"),
            PermissionRule(permission="allow", arg_pattern="lang:python"),
        ],
        default_permission="ask",
    )
    assert evaluate_permission(code_executor_policy, {"lang": "bash"}) == "deny"
    assert evaluate_permission(code_executor_policy, {"lang": "python"}) == "allow"
    assert evaluate_permission(code_executor_policy, {"lang": "javascript"}) == "ask"
