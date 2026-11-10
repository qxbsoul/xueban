"""Story 2.2 — provider preset loading + user override 测试。"""

from __future__ import annotations

from pathlib import Path
import tempfile

import pytest
import yaml

from xueban.runtime.preset_loader import load_presets, resolve_preset
from xueban.runtime.contracts import ProviderPreset


# ---------------------------------------------------------------------------
# load_presets
# ---------------------------------------------------------------------------

class TestLoadPresets:
    def test_loads_builtin_presets(self):
        presets = load_presets()
        # The five built-in presets
        for name in ("claude", "codex", "openclaw", "hermes", "shadowsoul"):
            assert name in presets, f"missing preset: {name}"

    def test_each_preset_is_provider_preset_instance(self):
        presets = load_presets()
        for name, preset in presets.items():
            assert isinstance(preset, ProviderPreset), f"{name} is not ProviderPreset"

    def test_presets_have_commands(self):
        presets = load_presets()
        for name, preset in presets.items():
            assert isinstance(preset.command, list) and len(preset.command) > 0, \
                f"{name} has no command"

    def test_load_from_custom_path(self, tmp_path):
        custom_yaml = {"my-agent": {"command": ["my-agent-cli"], "stdin_format": "raw", "parse_format": "stdout-text"}}
        f = tmp_path / "presets.yaml"
        f.write_text(yaml.dump(custom_yaml))
        presets = load_presets(f)
        assert "my-agent" in presets
        assert presets["my-agent"].command == ["my-agent-cli"]


# ---------------------------------------------------------------------------
# resolve_preset
# ---------------------------------------------------------------------------

class TestResolvePreset:
    def test_no_override_returns_base_preset(self):
        preset = resolve_preset("claude", {})
        assert preset.command[0] == "claude"

    def test_user_override_command(self):
        override = {"command": ["/usr/local/bin/claude"]}
        preset = resolve_preset("claude", override)
        assert preset.command == ["/usr/local/bin/claude"]

    def test_user_override_parse_format(self):
        override = {"parse_format": "stdout-text"}
        preset = resolve_preset("openclaw", override)
        assert preset.parse_format == "stdout-text"

    def test_user_override_args_template(self):
        override = {"args_template": ["--custom", "{id}"]}
        preset = resolve_preset("hermes", override)
        assert preset.args_template == ["--custom", "{id}"]

    def test_user_override_env(self):
        override = {"env": {"MY_KEY": "abc"}}
        preset = resolve_preset("openclaw", override)
        assert preset.env["MY_KEY"] == "abc"

    def test_unknown_provider_uses_override_only(self):
        override = {"command": ["my-custom-cli"], "stdin_format": "raw", "parse_format": "stdout-text"}
        preset = resolve_preset("my-unknown-provider", override)
        assert preset.command == ["my-custom-cli"]

    def test_none_values_in_override_apply_and_clear_optional_fields(self):
        """Code Review 2026-04-22: None is a legitimate override, not silently filtered.

        - Passing None for an optional field with a default (e.g. workspace_template)
          clears it, allowing users to override an inherited non-None default.
        - Passing None for a required field (e.g. command) raises ValidationError,
          surfacing the user's mistake rather than hiding it behind "silently preserved default".
        """
        from pydantic import ValidationError

        # Clearing an optional field now works (openclaw preset has workspace_template set)
        preset = resolve_preset("openclaw", {"workspace_template": None})
        assert preset.workspace_template is None

        # Explicitly passing None for a required field raises (was silently ignored before)
        with pytest.raises(ValidationError):
            resolve_preset("claude", {"command": None})

    def test_openclaw_preset_has_workspace_template(self):
        preset = resolve_preset("openclaw", {})
        assert preset.workspace_template is not None
        assert "openclaw" in preset.workspace_template.lower()
