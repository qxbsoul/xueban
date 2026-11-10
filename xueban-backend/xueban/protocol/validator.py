from typing import Dict, Any, Optional, Callable, List
from dataclasses import dataclass
from enum import Enum


class ValidationSeverity(Enum):
    """验证严重程度"""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationIssue:
    """验证问题"""
    severity: ValidationSeverity
    code: str
    message: str
    suggestion: Optional[str] = None


@dataclass
class ValidationResult:
    """验证结果"""
    is_valid: bool
    issues: List[ValidationIssue]
    score: float = 1.0  # 0-1 的质量分数


class ResultValidator:
    """结果验证器，支持多种验证策略"""

    def __init__(self, strict_mode: bool = False):
        self._validators: List[Callable] = []
        self.strict_mode = strict_mode

    def add_validator(self, validator: Callable):
        """添加自定义验证器"""
        self._validators.append(validator)

    def validate(
        self,
        result: str,
        context: Optional[Dict[str, Any]] = None
    ) -> ValidationResult:
        """
        验证结果
        返回: ValidationResult
        """
        issues = []
        context = context or {}

        # 内置验证
        issues.extend(self._validate_basic(result, context))
        issues.extend(self._validate_content(result, context))
        issues.extend(self._validate_format(result, context))

        # 自定义验证器
        for validator in self._validators:
            try:
                validator_result = validator(result, context)
                if isinstance(validator_result, tuple):
                    is_valid, message = validator_result
                    if not is_valid:
                        issues.append(ValidationIssue(
                            severity=ValidationSeverity.ERROR,
                            code="CUSTOM_VALIDATION",
                            message=message
                        ))
                elif isinstance(validator_result, list):
                    issues.extend(validator_result)
            except Exception as e:
                if self.strict_mode:
                    raise

        is_valid = not any(
            issue.severity == ValidationSeverity.ERROR
            for issue in issues
        )

        # 计算质量分数
        score = self._calculate_score(issues)

        return ValidationResult(is_valid=is_valid, issues=issues, score=score)

    def _validate_basic(self, result: str, context: Dict[str, Any]) -> List[ValidationIssue]:
        """基础验证：空值、长度等"""
        issues = []

        if self.is_empty(result):
            issues.append(ValidationIssue(
                severity=ValidationSeverity.ERROR,
                code="EMPTY_RESULT",
                message="结果为空",
                suggestion="请提供有效的响应内容"
            ))
        elif self.is_too_short(result, min_length=5):
            issues.append(ValidationIssue(
                severity=ValidationSeverity.WARNING,
                code="SHORT_RESULT",
                message=f"结果过短（{len(result)} 字符）"
            ))

        return issues

    def _validate_content(self, result: str, context: Dict[str, Any]) -> List[ValidationIssue]:
        """内容验证：关键词、语义等"""
        issues = []

        # 检查拒绝响应
        refusal_keywords = ["无法", "不能", "抱歉", "我不知道"]
        if self.contains_keywords(result, refusal_keywords):
            issues.append(ValidationIssue(
                severity=ValidationSeverity.WARNING,
                code="POTENTIAL_REFUSAL",
                message="可能包含拒绝响应"
            ))

        # 检查占位符
        placeholder_keywords = ["TODO", "FIXME", "待补充", "..."]
        if self.contains_keywords(result, placeholder_keywords):
            issues.append(ValidationIssue(
                severity=ValidationSeverity.WARNING,
                code="PLACEHOLDER_DETECTED",
                message="包含待补充的占位符"
            ))

        return issues

    def _validate_format(self, result: str, context: Dict[str, Any]) -> List[ValidationIssue]:
        """格式验证：Markdown、JSON 等"""
        issues = []

        # 检查未闭合的代码块
        code_block_count = result.count("```")
        if code_block_count % 2 != 0:
            issues.append(ValidationIssue(
                severity=ValidationSeverity.ERROR,
                code="UNCLOSED_CODE_BLOCK",
                message="存在未闭合的代码块",
                suggestion="请确保所有 ``` 都有成对的闭合标记"
            ))

        # 检查未闭合的括号（简单检查）
        brackets = {"(": ")", "[": "]", "{": "}"}
        for open_bracket, close_bracket in brackets.items():
            if result.count(open_bracket) != result.count(close_bracket):
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    code="UNMATCHED_BRACKETS",
                    message=f"`{open_bracket}` 和 `{close_bracket}` 数量不匹配"
                ))

        return issues

    def _calculate_score(self, issues: List[ValidationIssue]) -> float:
        """计算质量分数"""
        if not issues:
            return 1.0

        # 基于问题严重程度计算分数
        error_count = sum(1 for i in issues if i.severity == ValidationSeverity.ERROR)
        warning_count = sum(1 for i in issues if i.severity == ValidationSeverity.WARNING)

        score = 1.0
        score -= error_count * 0.5  # 每个 ERROR 扣 0.5
        score -= warning_count * 0.1  # 每个 WARNING 扣 0.1

        return max(0.0, score)

    def is_empty(self, result: str) -> bool:
        return not result or not result.strip()

    def is_too_short(self, result: str, min_length: int = 10) -> bool:
        return len(result) < min_length

    def contains_keywords(self, result: str, keywords: list[str]) -> bool:
        result_lower = result.lower()
        return any(keyword.lower() in result_lower for keyword in keywords)

    def format_report(self, result: ValidationResult) -> str:
        """格式化验证报告"""
        if result.is_valid and result.score == 1.0:
            return "验证通过，质量评分: 1.00"

        lines = [f"验证结果: {'通过' if result.is_valid else '失败'}"]
        lines.append(f"质量评分: {result.score:.2f}")
        lines.append(f"问题数量: {len(result.issues)}")

        for issue in result.issues:
            icon = "❌" if issue.severity == ValidationSeverity.ERROR else "⚠️"
            lines.append(f"\n{icon} [{issue.code}] {issue.message}")
            if issue.suggestion:
                lines.append(f"   建议: {issue.suggestion}")

        return "\n".join(lines)