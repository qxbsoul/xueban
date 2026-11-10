"""ValidationHookSpec schema tests — N1/002.

Covers:
  - shell / webhook / builtin happy paths
  - kind / on_fail enum validation
  - kind ↔ config block discriminator (must match, must be exclusive)
  - timeout / max_retries bounds
  - expose_error_details defaults False (PM Q12.3)
  - max_retries defaults 0 (PM Q12.2)
  - unique-id enforcement at the collection wrapper level
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xueban.runtime.validation_hooks import (
    BuiltinHookConfig,
    ShellHookConfig,
    ValidationHookSpec,
    ValidationHooksConfig,
    WebhookHookConfig,
)


# ---------------------------------------------------------------------------
# Happy paths — three kinds
# ---------------------------------------------------------------------------


class TestHappyPaths:
    def test_shell_hook_minimal(self):
        spec = ValidationHookSpec(
            id="tsc-check",
            kind="shell",
            shell=ShellHookConfig(cmd=["pnpm", "tsc", "--noEmit"]),
        )
        assert spec.kind == "shell"
        assert spec.shell is not None
        assert spec.shell.cmd == ["pnpm", "tsc", "--noEmit"]
        assert spec.shell.cwd == "${workspace}"  # default
        # PM defaults
        assert spec.on_fail == "blocker"
        assert spec.enabled is True
        assert spec.timeout_ms == 60_000
        assert spec.max_retries == 0  # PM Q12.2
        assert spec.expose_error_details is False  # PM Q12.3

    def test_webhook_hook_minimal(self):
        spec = ValidationHookSpec(
            id="deploy-ping",
            kind="webhook",
            on_fail="warn",
            webhook=WebhookHookConfig(url="https://ci/validate"),
        )
        assert spec.webhook is not None
        assert spec.webhook.method == "POST"
        assert spec.webhook.success_when.status_code == 200

    def test_builtin_hook_minimal(self):
        spec = ValidationHookSpec(
            id="files-exist",
            kind="builtin",
            builtin=BuiltinHookConfig(name="file-exists", args={"paths": ["x.md"]}),
        )
        assert spec.builtin is not None
        assert spec.builtin.name == "file-exists"
        assert spec.builtin.args == {"paths": ["x.md"]}


# ---------------------------------------------------------------------------
# Discriminator: kind must match present config block; only one block allowed
# ---------------------------------------------------------------------------


class TestKindDiscriminator:
    def test_kind_shell_without_shell_block_fails(self):
        with pytest.raises(ValidationError) as exc:
            ValidationHookSpec(id="bad", kind="shell")
        assert "requires a 'shell'" in str(exc.value)

    def test_kind_webhook_without_webhook_block_fails(self):
        with pytest.raises(ValidationError) as exc:
            ValidationHookSpec(id="bad", kind="webhook")
        assert "requires a 'webhook'" in str(exc.value)

    def test_kind_builtin_without_builtin_block_fails(self):
        with pytest.raises(ValidationError) as exc:
            ValidationHookSpec(id="bad", kind="builtin")
        assert "requires a 'builtin'" in str(exc.value)

    def test_extra_config_block_rejected(self):
        with pytest.raises(ValidationError) as exc:
            ValidationHookSpec(
                id="confused",
                kind="shell",
                shell=ShellHookConfig(cmd=["ls"]),
                webhook=WebhookHookConfig(url="https://x"),
            )
        assert "unrelated config" in str(exc.value)


# ---------------------------------------------------------------------------
# Enum / bounds
# ---------------------------------------------------------------------------


class TestEnumAndBounds:
    def test_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            ValidationHookSpec(id="x", kind="bogus")  # type: ignore[arg-type]

    def test_unknown_on_fail_rejected(self):
        with pytest.raises(ValidationError):
            ValidationHookSpec(
                id="x",
                kind="shell",
                on_fail="explode",  # type: ignore[arg-type]
                shell=ShellHookConfig(cmd=["ls"]),
            )

    def test_timeout_must_be_positive(self):
        with pytest.raises(ValidationError):
            ValidationHookSpec(
                id="x",
                kind="shell",
                timeout_ms=0,
                shell=ShellHookConfig(cmd=["ls"]),
            )

    def test_timeout_capped(self):
        # 10min is the soft cap; > rejected to prevent footguns
        with pytest.raises(ValidationError):
            ValidationHookSpec(
                id="x",
                kind="shell",
                timeout_ms=10 * 60 * 1000 + 1,
                shell=ShellHookConfig(cmd=["ls"]),
            )

    def test_max_retries_non_negative(self):
        with pytest.raises(ValidationError):
            ValidationHookSpec(
                id="x",
                kind="shell",
                max_retries=-1,
                shell=ShellHookConfig(cmd=["ls"]),
            )

    def test_shell_cmd_must_be_nonempty(self):
        with pytest.raises(ValidationError):
            ShellHookConfig(cmd=[])


# ---------------------------------------------------------------------------
# Collection: unique-id at top-level wrapper
# ---------------------------------------------------------------------------


class TestUniqueIds:
    def test_unique_ids_accepted(self):
        cfg = ValidationHooksConfig(
            validation_hooks=[
                ValidationHookSpec(
                    id="a",
                    kind="builtin",
                    builtin=BuiltinHookConfig(name="file-exists"),
                ),
                ValidationHookSpec(
                    id="b",
                    kind="builtin",
                    builtin=BuiltinHookConfig(name="file-exists"),
                ),
            ],
        )
        assert len(cfg.validation_hooks) == 2

    def test_duplicate_ids_rejected(self):
        with pytest.raises(ValidationError) as exc:
            ValidationHooksConfig(
                validation_hooks=[
                    ValidationHookSpec(
                        id="same",
                        kind="builtin",
                        builtin=BuiltinHookConfig(name="file-exists"),
                    ),
                    ValidationHookSpec(
                        id="same",
                        kind="builtin",
                        builtin=BuiltinHookConfig(name="file-exists"),
                    ),
                ],
            )
        assert "duplicate hook id" in str(exc.value)

    def test_empty_list_allowed(self):
        cfg = ValidationHooksConfig(validation_hooks=[])
        assert cfg.validation_hooks == []


# ---------------------------------------------------------------------------
# Extra fields rejected (catch typos / future schema drift early)
# ---------------------------------------------------------------------------


class TestExtraFieldsRejected:
    def test_unknown_top_level_field_rejected(self):
        with pytest.raises(ValidationError):
            ValidationHookSpec(
                id="x",
                kind="builtin",
                builtin=BuiltinHookConfig(name="file-exists"),
                mystery_field="oops",  # type: ignore[call-arg]
            )

    def test_unknown_shell_field_rejected(self):
        with pytest.raises(ValidationError):
            ShellHookConfig(cmd=["ls"], shell_typo="bad")  # type: ignore[call-arg]
