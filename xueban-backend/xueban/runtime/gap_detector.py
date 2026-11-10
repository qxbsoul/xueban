from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

_logger = logging.getLogger("xueban.gap_detector")

_MAX_DEPTH = 10
_MAX_KEYS = 100


def _check_depth(obj: Any, depth: int = 0) -> None:
    """Guard against deeply nested or oversized dicts that could cause stack exhaustion."""
    if depth > _MAX_DEPTH:
        raise ValueError(f"Input nesting depth exceeds {_MAX_DEPTH}")
    if isinstance(obj, dict):
        if len(obj) > _MAX_KEYS:
            raise ValueError(f"Input dict key count exceeds {_MAX_KEYS}")
        for v in obj.values():
            _check_depth(v, depth + 1)
    elif isinstance(obj, list):
        if len(obj) > _MAX_KEYS:
            raise ValueError(f"Input list length exceeds {_MAX_KEYS}")
        for item in obj:
            _check_depth(item, depth + 1)


DEFAULT_CHOICES: List[Dict[str, str]] = [
    {"id": "A", "label": "补充数据", "action": "pause"},
    {"id": "B", "label": "从论文移除此对比", "action": "drop"},
    {"id": "C", "label": "注释为 'will be updated'", "action": "annotate"},
]


def detect_gap(inputs: Dict[str, Any], node_config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Return a structured gap description when the step input is incomplete.

    Heuristics are intentionally conservative: false positives are preferable to
    silently inventing missing paper evidence.
    """
    # N2 fix: guard against oversized/deeply nested inputs (DoS prevention)
    try:
        _check_depth(inputs)
        if node_config:
            _check_depth(node_config)
    except ValueError as exc:
        _logger.warning("gap_detector: input rejected (bounds exceeded) — %s", exc)
        return None

    config = node_config or {}
    detector_cfg = config.get("gap_detection") if isinstance(config.get("gap_detection"), dict) else {}

    experiment_log = inputs.get("experiment_log")
    if not isinstance(experiment_log, dict):
        experiment_log = {}

    missing_data = _detect_missing_data(inputs, experiment_log, detector_cfg)
    if missing_data is not None:
        return missing_data

    broken_ref = _detect_broken_ref(inputs, detector_cfg)
    if broken_ref is not None:
        return broken_ref

    incomplete_log = _detect_incomplete_log(experiment_log, detector_cfg)
    if incomplete_log is not None:
        return incomplete_log

    return None


def _detect_missing_data(
    inputs: Dict[str, Any],
    experiment_log: Dict[str, Any],
    detector_cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    required_ids = _coerce_list(
        detector_cfg.get("required_data_ids")
        or inputs.get("required_data_ids")
        or inputs.get("data_refs")
    )
    if not required_ids:
        return None

    available_ids = set(
        _coerce_list(experiment_log.get("data_ids"))
        or _coerce_list(experiment_log.get("available_data_ids"))
    )
    missing_ids = [item for item in required_ids if item not in available_ids]
    if not missing_ids:
        return None

    return {
        "gap_type": "missing_data",
        "description": f"实验日志缺少数据集引用: {', '.join(missing_ids)}。",
        "choices": list(DEFAULT_CHOICES),
    }


def _detect_broken_ref(inputs: Dict[str, Any], detector_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    required_refs = _coerce_list(
        detector_cfg.get("required_refs")
        or inputs.get("reference_ids")
        or inputs.get("figure_refs")
    )
    if not required_refs:
        return None

    assets = inputs.get("assets")
    if not isinstance(assets, dict):
        assets = {}

    available_refs = set(_coerce_list(assets.get("figures")))
    available_refs.update(_coerce_list(assets.get("tables")))
    available_refs.update(_coerce_list(assets.get("refs")))
    if not available_refs:
        available_refs.update(str(key) for key in assets.keys())

    missing_refs = [item for item in required_refs if item not in available_refs]
    if not missing_refs:
        return None

    return {
        "gap_type": "broken_ref",
        "description": f"引用的图表/资源不存在: {', '.join(missing_refs)}。",
        "choices": list(DEFAULT_CHOICES),
    }


def _detect_incomplete_log(
    experiment_log: Dict[str, Any],
    detector_cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not experiment_log:
        return None
    baseline_key = str(detector_cfg.get("baseline_key") or "baseline")
    baseline = experiment_log.get(baseline_key)

    if isinstance(baseline, dict):
        missing_fields = [key for key, value in baseline.items() if value in (None, "", [], {})]
        if missing_fields:
            return {
                "gap_type": "incomplete_log",
                "description": f"实验日志 baseline 字段不完整: {', '.join(missing_fields)}。",
                "choices": list(DEFAULT_CHOICES),
            }

    if baseline in (None, "", [], {}):
        return {
            "gap_type": "incomplete_log",
            "description": "实验日志缺少 baseline 数据。",
            "choices": list(DEFAULT_CHOICES),
        }

    return None


def _coerce_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []
