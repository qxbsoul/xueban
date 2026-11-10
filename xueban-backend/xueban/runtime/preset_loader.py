"""Provider preset loader for CliAgentExecutor (Story 2.2).

Loads provider_presets.yaml and merges user overrides on top.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from xueban.runtime.contracts import ProviderPreset

_PRESETS_PATH = Path(__file__).parent / "provider_presets.yaml"
_cached: Optional[Dict[str, ProviderPreset]] = None


def load_presets(presets_path: Optional[Path] = None) -> Dict[str, ProviderPreset]:
    """Load and cache provider presets from YAML."""
    global _cached
    path = presets_path or _PRESETS_PATH
    if _cached is not None and presets_path is None:
        return _cached
    with open(path, encoding="utf-8") as f:
        raw: Dict[str, Any] = yaml.safe_load(f) or {}
    result = {name: ProviderPreset.model_validate(data) for name, data in raw.items()}
    if presets_path is None:
        _cached = result
    return result


def clear_cache() -> None:
    """Invalidate the module-level preset cache (testing / hot-reload)."""
    global _cached
    _cached = None


def resolve_preset(provider: str, user_override: Dict[str, Any]) -> ProviderPreset:
    """Return the preset for a provider, with user_override applied on top.

    Merge semantics (Code Review 2026-04-22):
      - ``env`` is shallow-merged onto the preset env (user keys override,
        preset keys kept).
      - All other fields replace wholesale. ``None`` is treated as a legitimate
        override (e.g. explicit ``workspace_template=None`` clears the preset
        default) rather than being silently filtered out.

    If the provider is unknown in the default presets, a bare preset is built
    from user_override alone (which must supply at least 'command').
    """
    presets = load_presets()
    base_data: Dict[str, Any] = {}
    if provider in presets:
        base_data = presets[provider].model_dump()
    merged: Dict[str, Any] = {**base_data}
    for k, v in user_override.items():
        if k == "env" and isinstance(v, dict) and isinstance(merged.get("env"), dict):
            merged["env"] = {**merged["env"], **v}
        else:
            merged[k] = v
    return ProviderPreset.model_validate(merged)
