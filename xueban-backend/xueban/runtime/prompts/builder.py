"""SystemPromptBuilder — single source for agent system_prompt assembly.

Background (T1, docs/backend-capability-map-and-upgrade-plan.md §3.1):
    The runtime previously assembled system_prompt in three different places:
      - xueban/highlevel.py            TemplateCompiler._build_prompt  (rich, RoleSpec)
      - xueban/runtime/builder_service.py  _build_workflow_definition  (one-liner, RoleProfile)
      - xueban/runtime/context_builder.py  inferred upstream call sites
    This made versioning, A/B testing, and harness-rule injection impossible.

Design goals:
    1. Single entry: ``SystemPromptBuilder(version=...).build(role_profile, context)``.
    2. Transport-agnostic — no knowledge of CLI / Api / Acp / Mcp downstream.
    3. Section-based composition with stable ordering; future variants override
       individual section renderers without forking the whole builder.
    4. Versioning via ``version`` field so A/B test code can pin a vintage.
    5. Forward hooks for [[harness-rule]] injection and validation context
       (see ``BuildContext.extra_sections`` and the ``_section_hook`` slot).

Non-goals (this module deliberately does NOT do):
    - Token counting (a hook is reserved; no tiktoken dependency added).
    - Direct mutation of caller payloads (returns a pure string).
    - Knowledge of downstream message schema (caller wraps into {role,content}).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence


class PromptSection(str, Enum):
    """Ordered logical sections that compose a system prompt.

    Order here is the default render order; ``SystemPromptBuilder.section_order``
    can override it without losing the enum's identity.
    """

    IDENTITY = "identity"
    RESPONSIBILITIES = "responsibilities"
    CONSTRAINTS = "constraints"
    CAPABILITIES = "capabilities"
    CONTEXT = "context"
    HARNESS_RULES = "harness_rules"  # reserved hook (T2 / harness-rule injection)
    FOOTER = "footer"


_DEFAULT_SECTION_ORDER: tuple[PromptSection, ...] = (
    PromptSection.IDENTITY,
    PromptSection.RESPONSIBILITIES,
    PromptSection.CONSTRAINTS,
    PromptSection.CAPABILITIES,
    PromptSection.CONTEXT,
    PromptSection.HARNESS_RULES,
    PromptSection.FOOTER,
)


class _RoleProfileLike(Protocol):
    """Structural protocol so the builder works with both RoleProfile (contracts_builder)
    and RoleSpec (highlevel) without importing either — keeps the module free of
    upward dependencies on the higher-level packages.
    """

    name: str
    description: str
    responsibilities: List[str]
    constraints: List[str]


@dataclass
class BuildContext:
    """Optional ambient context the builder may weave into the prompt.

    All fields are optional; callers should populate only what is meaningful for
    the call site. Future T2/T3 work will extend this with harness rule payloads
    and validation scripts.
    """

    assignment: Optional[Dict[str, Any]] = None
    capabilities: Sequence[str] = field(default_factory=tuple)
    tools_summary: Sequence[str] = field(default_factory=tuple)
    # Reserved hooks (read by callers; builder only forwards values verbatim):
    harness_rules: Sequence[str] = field(default_factory=tuple)
    extra_sections: Dict[str, str] = field(default_factory=dict)
    # Free-form metadata (e.g. run_id, agent_id) — never injected into the
    # output string by default; available to custom section renderers.
    metadata: Dict[str, Any] = field(default_factory=dict)


SectionRenderer = Callable[[_RoleProfileLike, BuildContext], str]


class SystemPromptBuilder:
    """Compose a deterministic system_prompt from a RoleProfile (+ optional context).

    Usage::

        builder = SystemPromptBuilder()
        prompt = builder.build(role_profile, BuildContext(assignment=...))

    A/B testing::

        v1 = SystemPromptBuilder(version="v1")
        v2 = SystemPromptBuilder(version="v2-experimental",
                                 section_order=(PromptSection.IDENTITY, ...))
    """

    DEFAULT_VERSION: str = "v1"

    def __init__(
        self,
        *,
        version: str = DEFAULT_VERSION,
        section_order: Optional[Sequence[PromptSection]] = None,
        section_overrides: Optional[Dict[PromptSection, SectionRenderer]] = None,
    ) -> None:
        self.version = version
        self.section_order = tuple(section_order) if section_order else _DEFAULT_SECTION_ORDER
        self._overrides: Dict[PromptSection, SectionRenderer] = dict(section_overrides or {})

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def build(
        self,
        role_profile: _RoleProfileLike,
        context: Optional[BuildContext] = None,
    ) -> str:
        ctx = context or BuildContext()
        chunks: List[str] = []
        for section in self.section_order:
            renderer = self._overrides.get(section) or self._default_renderer(section)
            rendered = renderer(role_profile, ctx).strip()
            if rendered:
                chunks.append(rendered)
        return "\n\n".join(chunks)

    # Convenience for legacy call sites that only need an identity-flavoured string.
    def build_minimal(self, role_profile: _RoleProfileLike) -> str:
        return self._render_identity(role_profile, BuildContext())

    # ------------------------------------------------------------------
    # Reserved hooks for future T2/T3 work
    # ------------------------------------------------------------------

    def estimate_tokens(self, prompt: str) -> int:
        """Conservative ASCII/CJK-aware estimate; no external deps.

        Mirrors the heuristic in context_builder._estimate_tokens to stay
        consistent across the runtime. Replace with tiktoken when needed.
        """
        ascii_chars = sum(1 for c in prompt if ord(c) < 128)
        non_ascii = len(prompt) - ascii_chars
        return max(1, ascii_chars // 4 + non_ascii * 2 // 3)

    # ------------------------------------------------------------------
    # Default section renderers
    # ------------------------------------------------------------------

    def _default_renderer(self, section: PromptSection) -> SectionRenderer:
        return {
            PromptSection.IDENTITY: self._render_identity,
            PromptSection.RESPONSIBILITIES: self._render_responsibilities,
            PromptSection.CONSTRAINTS: self._render_constraints,
            PromptSection.CAPABILITIES: self._render_capabilities,
            PromptSection.CONTEXT: self._render_context,
            PromptSection.HARNESS_RULES: self._render_harness_rules,
            PromptSection.FOOTER: self._render_footer,
        }[section]

    @staticmethod
    def _render_identity(role: _RoleProfileLike, _: BuildContext) -> str:
        name = getattr(role, "name", "") or "Agent"
        description = getattr(role, "description", "") or ""
        persona = getattr(role, "persona", "") or ""
        lines = [f"You are {name}."]
        if description:
            lines.append(description)
        if persona:
            lines.append(f"Persona: {persona}")
        return "\n".join(lines)

    @staticmethod
    def _render_responsibilities(role: _RoleProfileLike, _: BuildContext) -> str:
        items = list(getattr(role, "responsibilities", []) or [])
        if not items:
            return ""
        return "Responsibilities:\n" + "\n".join(f"- {item}" for item in items)

    @staticmethod
    def _render_constraints(role: _RoleProfileLike, _: BuildContext) -> str:
        items = list(getattr(role, "constraints", []) or [])
        if not items:
            return ""
        return "Constraints:\n" + "\n".join(f"- {item}" for item in items)

    @staticmethod
    def _render_capabilities(role: _RoleProfileLike, ctx: BuildContext) -> str:
        # Prefer explicit context, fall back to role attribute if present.
        caps = list(ctx.capabilities) or list(getattr(role, "capabilities", []) or [])
        tools = list(ctx.tools_summary)
        if not caps and not tools:
            return ""
        chunks: List[str] = []
        if caps:
            chunks.append("Capabilities:\n" + "\n".join(f"- {c}" for c in caps))
        if tools:
            chunks.append("Available Tools:\n" + "\n".join(f"- {t}" for t in tools))
        return "\n\n".join(chunks)

    @staticmethod
    def _render_context(_: _RoleProfileLike, ctx: BuildContext) -> str:
        assignment = ctx.assignment or {}
        if not assignment:
            return ""
        lines: List[str] = ["Current Assignment:"]
        focus = assignment.get("focus")
        if isinstance(focus, str) and focus:
            lines.append(f"- Focus: {focus}")
        deliverable = assignment.get("deliverable")
        if isinstance(deliverable, str) and deliverable:
            lines.append(f"- Deliverable: {deliverable}")
        notes = assignment.get("notes")
        if isinstance(notes, str) and notes:
            lines.append(f"- Notes: {notes}")
        # Surface any remaining string fields verbatim so callers can pass
        # ad-hoc keys without us having to know them.
        for key, value in assignment.items():
            if key in {"focus", "deliverable", "notes"}:
                continue
            if isinstance(value, str) and value:
                lines.append(f"- {key}: {value}")
        return "\n".join(lines) if len(lines) > 1 else ""

    @staticmethod
    def _render_harness_rules(_: _RoleProfileLike, ctx: BuildContext) -> str:
        rules = list(ctx.harness_rules)
        if not rules:
            return ""
        # Format intentionally matches the [[harness-rule]] marker convention
        # so downstream parsers can locate the block.
        body = "\n".join(f"- {rule}" for rule in rules)
        return f"[[harness-rule]]\n{body}"

    @staticmethod
    def _render_footer(_: _RoleProfileLike, ctx: BuildContext) -> str:
        extra = ctx.extra_sections.get("footer", "")
        if extra:
            return extra
        return "Produce a clear, structured result for the current task input."
