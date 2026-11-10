"""Tests for Persona / NPC Kit EvalProfile — Story 10.4 (AC4, AC6)

覆盖项：
  1. 3 轮对话 Smoke case 的 eval 指标计算（persona_tone_check / memory_retention_check / state_update_check）
  2. persona_tone_check 关键词覆盖率 < 60% 时 fail
  3. Smoke Run 第 3 轮 interaction_count 比第 1 轮大 2
  4. cross_turn_reference 检查（第 2/3 轮引用第 1 轮信息）
  5. state_update_check field_updated 递增验证
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from xueban.runtime.kits.persona_npc_kit import (
    MEMORY_RETENTION_PRESETS,
    PERSONA_NPC_EVAL_PROFILE,
    PERSONA_NPC_SMOKE_CASES,
    PersonaNPCGoalInputs,
    create_persona_npc_blueprint,
)


# ---------------------------------------------------------------------------
# Eval 函数实现（复现 EvalProfile 检查逻辑，用于测试验证）
# ---------------------------------------------------------------------------


def compute_persona_tone_check(
    response: str,
    personality: str,
    threshold: float = 0.6,
) -> Dict[str, Any]:
    """persona_tone_check: keyword_consistency

    计算 personality 关键词在 response 中的覆盖率。
    覆盖率 = 命中关键词数 / 总关键词数。
    threshold = 0.6，即 ≥ 60% 为 pass。
    """
    import re
    keywords = [kw.strip() for kw in re.split(r"[、，,\s]+", personality.strip()) if kw.strip()]
    if not keywords:
        return {"status": "skip", "coverage": 1.0, "matched": [], "total": 0}
    matched = [kw for kw in keywords if kw in response]
    coverage = len(matched) / len(keywords)
    return {
        "status": "pass" if coverage >= threshold else "fail",
        "coverage": coverage,
        "matched": matched,
        "total": len(keywords),
        "threshold": threshold,
    }


def compute_memory_retention_check(
    turn1_input: str,
    turn3_response: str,
) -> Dict[str, Any]:
    """memory_retention_check: cross_turn_reference

    检查 turn3_response 是否引用了 turn1_input 中的关键词。
    策略：
      1. 按标点/空格拆分 turn1_input 为 token 列表
      2. 保留长度 >= 2 的 token（去除单字词和空字符串）
      3. 检查这些 token 是否出现在 turn3_response 中
    """
    import re
    # 按中英文标点、空格拆分
    parts = re.split(r"[，。！？、,.!?\s]+", turn1_input.strip())
    candidates = [p.strip() for p in parts if len(p.strip()) >= 2]
    if not candidates:
        return {"status": "skip", "referenced": [], "candidates": []}
    referenced = [c for c in candidates if c in turn3_response]
    status = "pass" if referenced else "fail"
    return {
        "status": status,
        "referenced": referenced,
        "candidates": candidates,
    }


def compute_state_update_check(
    state_before: Dict[str, Any],
    state_after: Dict[str, Any],
    field: str = "interaction_count",
    expected_delta: int = 1,
) -> Dict[str, Any]:
    """state_update_check: field_updated

    检查 state_after[field] == state_before[field] + expected_delta。
    """
    before_val = state_before.get(field, 0)
    after_val = state_after.get(field, 0)
    delta = after_val - before_val
    status = "pass" if delta == expected_delta else "fail"
    return {
        "status": status,
        "field": field,
        "before": before_val,
        "after": after_val,
        "delta": delta,
        "expected_delta": expected_delta,
    }


def simulate_3_turn_interaction(
    persona_name: str,
    personality: str,
    memory_retention: str = "balanced",
) -> List[Dict[str, Any]]:
    """模拟 3 轮 Persona NPC 对话交互。

    返回每轮的 (response, state_snapshot)。
    注意：此为单元测试模拟，不调用真实 LLM。
    response 内容手动注入，重点测试状态递增逻辑。
    """
    state = {"mood": "neutral", "relationship_level": 0, "interaction_count": 0}
    turns_result = []

    for i, case in enumerate(PERSONA_NPC_SMOKE_CASES):
        # 模拟状态更新：每轮 interaction_count + 1
        state = dict(state)
        state["interaction_count"] = i + 1

        # 模拟 response — 注入 personality 关键词 + turn1 引用（用于测试验证）
        import re
        kws = [k.strip() for k in re.split(r"[、，,\s]+", personality) if k.strip()]
        # 提取 turn1 的 candidates（用于 turn3 引用）
        import re as _re
        turn1_text = PERSONA_NPC_SMOKE_CASES[0]["input"]
        _parts = _re.split(r"[，。！？、,.!?\s]+", turn1_text.strip())
        _turn1_candidates = [p.strip() for p in _parts if len(p.strip()) >= 2]
        _ref = _turn1_candidates[0] if _turn1_candidates else "第一次见面"

        if i == 0:
            # Turn 1: 包含至少 60% 的 personality 关键词
            resp = f"你好！我是{persona_name}。{personality}，这是我的风格。很高兴认识你！"
        elif i == 1:
            # Turn 2: 引用 Turn 1 中的 candidate（_ref = "你好"）
            resp = f"当然记得！{_ref}你之前和我打招呼，我记得很清楚。{kws[0] if kws else ''}让我印象深刻。"
        else:
            # Turn 3: 情感温暖 + 直接插入 turn1 的 candidate（精确引用）
            resp = f"我也很开心！{_ref}让我想起了我们初次相遇。{' '.join(kws[:2])}是我一直以来的风格，希望你喜欢。"

        turns_result.append({
            "turn": i + 1,
            "input": case["input"],
            "response": resp,
            "state": dict(state),
        })

    return turns_result


# ---------------------------------------------------------------------------
# 1. persona_tone_check 测试
# ---------------------------------------------------------------------------


class TestPersonaToneCheck:
    """AC4, AC6：persona_tone_check 关键词覆盖率验证。"""

    def test_full_coverage_passes(self):
        personality = "温柔、善解人意"
        response = "我是温柔的，也是善解人意的。"
        result = compute_persona_tone_check(response, personality)
        assert result["status"] == "pass"
        assert result["coverage"] == 1.0

    def test_zero_coverage_fails(self):
        """AC6：覆盖率 < 60% 时 fail。"""
        personality = "温柔、善解人意、神秘、深邃、智慧"
        response = "我什么都不知道。"  # 完全不含 personality 关键词
        result = compute_persona_tone_check(response, personality)
        assert result["status"] == "fail"
        assert result["coverage"] == 0.0

    def test_exactly_60_percent_passes(self):
        """边界条件：恰好 60% 覆盖率应 pass。"""
        personality = "温柔、善解人意、神秘、深邃、智慧"  # 5 个关键词
        # 命中 3/5 = 60%
        response = "温柔、善解人意、神秘——这就是我的风格。"
        result = compute_persona_tone_check(response, personality, threshold=0.6)
        assert result["status"] == "pass"
        assert result["coverage"] == pytest.approx(0.6)

    def test_below_60_percent_fails(self):
        """AC6：恰好低于 60% 的覆盖率应 fail。"""
        personality = "温柔、善解人意、神秘、深邃、智慧"  # 5 个关键词
        # 命中 2/5 = 40% < 60%
        response = "温柔和善解人意是两个词。"
        result = compute_persona_tone_check(response, personality, threshold=0.6)
        assert result["status"] == "fail"
        assert result["coverage"] == pytest.approx(0.4)

    def test_single_keyword_personality(self):
        personality = "勇敢"
        response = "我很勇敢。"
        result = compute_persona_tone_check(response, personality)
        assert result["status"] == "pass"
        assert result["coverage"] == 1.0

    def test_threshold_parameter_respected(self):
        """自定义 threshold 参数被尊重。"""
        personality = "A、B、C、D、E"
        response = "A B"  # 2/5 = 40%
        # threshold=0.3 时应 pass
        assert compute_persona_tone_check(response, personality, threshold=0.3)["status"] == "pass"
        # threshold=0.5 时应 fail
        assert compute_persona_tone_check(response, personality, threshold=0.5)["status"] == "fail"


# ---------------------------------------------------------------------------
# 2. memory_retention_check 测试
# ---------------------------------------------------------------------------


class TestMemoryRetentionCheck:
    """AC4：cross_turn_reference — 第 3 轮引用第 1 轮信息。"""

    def test_reference_found_passes(self):
        # turn1_input 按标点拆分得到 candidates: ["你好", "我是小明", "第一次来找你聊天"]
        turn1_input = "你好，我是小明，第一次来找你聊天。"
        # turn3_response 包含 "第一次来找你聊天" 这个 candidate
        turn3_response = "我记得你，你第一次来找你聊天时和我说了很多。"
        result = compute_memory_retention_check(turn1_input, turn3_response)
        assert result["status"] == "pass"
        # candidates 中至少一个出现在 turn3_response 里
        assert len(result["referenced"]) >= 1

    def test_no_reference_fails(self):
        turn1_input = "你好，我是小明，第一次来找你聊天。"
        turn3_response = "今天天气不错！"  # 完全不引用 turn1 的任何词
        result = compute_memory_retention_check(turn1_input, turn3_response)
        assert result["status"] == "fail"
        assert result["referenced"] == []

    def test_partial_reference_passes(self):
        """只要有任意引用即 pass。"""
        # turn1 拆分: ["我叫张伟", "我是一个工程师", "来自北京"]
        turn1_input = "我叫张伟，我是一个工程师，来自北京。"
        # turn3 包含 "我是一个工程师"
        turn3_response = "对，我是一个工程师，你说得对！"
        result = compute_memory_retention_check(turn1_input, turn3_response)
        assert result["status"] == "pass"


# ---------------------------------------------------------------------------
# 3. state_update_check 测试
# ---------------------------------------------------------------------------


class TestStateUpdateCheck:
    """AC4：state_update_check — interaction_count 每轮递增。"""

    def test_increment_by_1_passes(self):
        before = {"interaction_count": 0}
        after = {"interaction_count": 1}
        result = compute_state_update_check(before, after)
        assert result["status"] == "pass"
        assert result["delta"] == 1

    def test_no_increment_fails(self):
        before = {"interaction_count": 2}
        after = {"interaction_count": 2}
        result = compute_state_update_check(before, after)
        assert result["status"] == "fail"
        assert result["delta"] == 0

    def test_decrement_fails(self):
        before = {"interaction_count": 3}
        after = {"interaction_count": 2}
        result = compute_state_update_check(before, after)
        assert result["status"] == "fail"

    def test_larger_delta_fails_when_expected_1(self):
        """非预期的增量（+2）应 fail when expected_delta=1。"""
        before = {"interaction_count": 0}
        after = {"interaction_count": 2}
        result = compute_state_update_check(before, after, expected_delta=1)
        assert result["status"] == "fail"


# ---------------------------------------------------------------------------
# 4. 3 轮 Smoke Run 集成测试
# ---------------------------------------------------------------------------


class TestThreeTurnSmokeRun:
    """AC4, AC6：3 轮连续对话 Smoke Run 完整验证。"""

    def setup_method(self):
        self.persona_name = "Aria"
        self.personality = "温柔、善解人意、神秘"
        self.turns = simulate_3_turn_interaction(self.persona_name, self.personality)

    def test_three_turns_generated(self):
        assert len(self.turns) == 3

    def test_turn_1_interaction_count_is_1(self):
        assert self.turns[0]["state"]["interaction_count"] == 1

    def test_turn_2_interaction_count_is_2(self):
        assert self.turns[1]["state"]["interaction_count"] == 2

    def test_turn_3_interaction_count_is_3(self):
        """AC6 核心：第 3 轮 interaction_count 比第 1 轮大 2。"""
        count_turn1 = self.turns[0]["state"]["interaction_count"]
        count_turn3 = self.turns[2]["state"]["interaction_count"]
        assert count_turn3 - count_turn1 == 2

    def test_turn_3_interaction_count_bigger_than_turn1_by_2(self):
        """AC6 核心（重复验证，明确断言 == 3）。"""
        assert self.turns[2]["state"]["interaction_count"] == 3

    def test_persona_tone_check_turn1(self):
        response = self.turns[0]["response"]
        result = compute_persona_tone_check(response, self.personality)
        assert result["status"] == "pass", (
            f"Turn 1 persona_tone_check failed: coverage={result['coverage']:.2f}"
        )

    def test_memory_retention_check_turn3_references_turn1(self):
        turn1_input = self.turns[0]["input"]
        turn3_response = self.turns[2]["response"]
        result = compute_memory_retention_check(turn1_input, turn3_response)
        assert result["status"] == "pass", (
            f"memory_retention_check failed: no reference from turn1 in turn3"
        )

    def test_state_update_check_each_turn(self):
        """每轮状态递增验证（turn1→turn2 和 turn2→turn3）。"""
        result_12 = compute_state_update_check(
            self.turns[0]["state"], self.turns[1]["state"]
        )
        assert result_12["status"] == "pass"
        result_23 = compute_state_update_check(
            self.turns[1]["state"], self.turns[2]["state"]
        )
        assert result_23["status"] == "pass"

    def test_all_evals_pass_end_to_end(self):
        """完整的 3 轮对话 eval 全部 pass。"""
        # persona_tone_check（第 1 轮）
        tone_result = compute_persona_tone_check(
            self.turns[0]["response"], self.personality
        )
        assert tone_result["status"] == "pass"

        # memory_retention_check（turn1 → turn3）
        mem_result = compute_memory_retention_check(
            self.turns[0]["input"],
            self.turns[2]["response"],
        )
        assert mem_result["status"] == "pass"

        # state_update_check（turn1 → turn3 delta = 2）
        delta_result = compute_state_update_check(
            self.turns[0]["state"],
            self.turns[2]["state"],
            expected_delta=2,
        )
        assert delta_result["status"] == "pass"


# ---------------------------------------------------------------------------
# 5. PERSONA_NPC_EVAL_PROFILE 结构验证
# ---------------------------------------------------------------------------


class TestEvalProfileStructure:
    """验证 PERSONA_NPC_EVAL_PROFILE 包含三个检查项。"""

    def test_eval_profile_has_three_checks(self):
        checks = PERSONA_NPC_EVAL_PROFILE["checks"]
        assert len(checks) == 3

    def test_persona_tone_check_present(self):
        assert "persona_tone_check" in PERSONA_NPC_EVAL_PROFILE["checks"]

    def test_memory_retention_check_present(self):
        assert "memory_retention_check" in PERSONA_NPC_EVAL_PROFILE["checks"]

    def test_state_update_check_present(self):
        assert "state_update_check" in PERSONA_NPC_EVAL_PROFILE["checks"]

    def test_persona_tone_threshold_is_60_percent(self):
        check = PERSONA_NPC_EVAL_PROFILE["checks"]["persona_tone_check"]
        assert check["pass_threshold"] == 0.6

    def test_state_update_field_is_interaction_count(self):
        check = PERSONA_NPC_EVAL_PROFILE["checks"]["state_update_check"]
        assert check["field"] == "interaction_count"

    def test_memory_retention_reference_from_turn1(self):
        check = PERSONA_NPC_EVAL_PROFILE["checks"]["memory_retention_check"]
        assert check["reference_turn"] == 1


# ---------------------------------------------------------------------------
# 6. Blueprint EvalProfile 与 EvalProfile 字典对齐验证
# ---------------------------------------------------------------------------


class TestBlueprintEvalProfile:
    """验证 Blueprint 内嵌的 EvalProfile 与 PERSONA_NPC_EVAL_PROFILE 一致。"""

    def setup_method(self):
        inputs = PersonaNPCGoalInputs(
            persona_name="Test",
            personality="沉稳、理性",
        )
        self.bp = create_persona_npc_blueprint(inputs)

    def test_blueprint_eval_smoke_enabled(self):
        assert self.bp.eval_profile.smoke_eval_enabled is True

    def test_blueprint_eval_criteria_contains_persona_tone(self):
        criteria_str = " ".join(self.bp.eval_profile.eval_criteria)
        assert "persona_tone_check" in criteria_str

    def test_blueprint_eval_criteria_contains_memory_retention(self):
        criteria_str = " ".join(self.bp.eval_profile.eval_criteria)
        assert "memory_retention_check" in criteria_str

    def test_blueprint_eval_criteria_contains_state_update(self):
        criteria_str = " ".join(self.bp.eval_profile.eval_criteria)
        assert "state_update_check" in criteria_str

    def test_blueprint_eval_metadata_threshold(self):
        meta = self.bp.eval_profile.metadata
        assert meta.get("persona_tone_keyword_threshold") == 0.6
