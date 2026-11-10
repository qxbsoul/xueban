from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from xueban.runtime.contracts import ActivationTrainingSample


def _normalize_whitespace(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _normalize_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    breakdown = candidate.get("scoring_breakdown") or {}
    return {
        "candidate_type": candidate.get("candidate_type"),
        "candidate_ref": candidate.get("candidate_ref"),
        "source_signals": sorted(candidate.get("source_signals") or []),
        "score": candidate.get("score"),
        "selected": candidate.get("selected"),
        "suppressed_reason": candidate.get("suppressed_reason"),
        "scoring_breakdown": {
            "base_score": breakdown.get("base_score"),
            "type_weight": breakdown.get("type_weight"),
            "signal_bonus": breakdown.get("signal_bonus"),
            "weighted_signals": breakdown.get("weighted_signals") or {},
            "score": breakdown.get("score"),
        },
    }


def _normalize_signals(signals: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "node_id": signals.get("node_id"),
        "node_type": signals.get("node_type"),
        "step_status": signals.get("step_status"),
        "activation_mode": signals.get("activation_mode"),
        "activation_decision": signals.get("activation_decision"),
        "candidate_count": signals.get("candidate_count"),
        "selected_candidate_count": signals.get("selected_candidate_count"),
        "delegate_candidate_count": signals.get("delegate_candidate_count"),
        "selected_non_node_candidate_count": signals.get("selected_non_node_candidate_count"),
        "artifact_count": signals.get("artifact_count"),
        "delegated_run": signals.get("delegated_run"),
        "review_gate_triggered": signals.get("review_gate_triggered"),
        "retry_gate_count": signals.get("retry_gate_count"),
        "next_node_id": signals.get("next_node_id"),
    }


def _normalize_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "activation_tags": sorted(metadata.get("activation_tags") or []),
        "activation_reasons": sorted(metadata.get("activation_reasons") or []),
        "feedback_channels": sorted(metadata.get("feedback_channels") or []),
    }


def _semantic_signature(sample: ActivationTrainingSample) -> str:
    payload = {
        "node_id": sample.node_id,
        "step_status": sample.step_status,
        "activation_mode": sample.activation_mode,
        "activation_decision": sample.activation_decision,
        "candidate_count": sample.candidate_count,
        "selected_candidate_count": sample.selected_candidate_count,
        "candidates": [_normalize_candidate(candidate) for candidate in sample.candidates],
        "reward_hints": sample.reward_hints,
        "signals": _normalize_signals(sample.signals),
        "assembly_block_id": sample.assembly_block_id,
        "assembly_goal": _normalize_whitespace(sample.assembly_goal),
        "metadata": _normalize_metadata(sample.metadata),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _quality_score(sample: ActivationTrainingSample) -> Tuple[int, int, int, int]:
    non_zero_rewards = sum(1 for value in sample.reward_hints.values() if float(value) != 0.0)
    non_null_signals = sum(1 for value in sample.signals.values() if value not in (None, [], {}, ""))
    return (
        sample.selected_candidate_count,
        sample.candidate_count,
        non_zero_rewards,
        non_null_signals,
    )


def _normalize_sample(sample: ActivationTrainingSample) -> ActivationTrainingSample:
    payload = sample.model_dump(mode="json")
    payload["assembly_goal"] = _normalize_whitespace(sample.assembly_goal)
    return ActivationTrainingSample.model_validate(payload)


@dataclass
class CleaningStats:
    input_records: int = 0
    output_records: int = 0
    invalid_json_lines: int = 0
    invalid_schema_lines: int = 0
    duplicate_records_removed: int = 0
    sample_id_collisions: int = 0

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)


def clean_activation_training_data(lines: Iterable[str]) -> tuple[List[ActivationTrainingSample], CleaningStats]:
    stats = CleaningStats()
    deduped: Dict[str, ActivationTrainingSample] = {}
    sample_id_counts: Dict[str, int] = {}

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        stats.input_records += 1

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            stats.invalid_json_lines += 1
            continue

        try:
            sample = _normalize_sample(ActivationTrainingSample.model_validate(payload))
        except Exception:
            stats.invalid_schema_lines += 1
            continue

        sample_id_counts[sample.sample_id] = sample_id_counts.get(sample.sample_id, 0) + 1
        signature = _semantic_signature(sample)
        existing = deduped.get(signature)
        if existing is None or _quality_score(sample) > _quality_score(existing):
            deduped[signature] = sample

    stats.sample_id_collisions = sum(max(0, count - 1) for count in sample_id_counts.values())
    stats.output_records = len(deduped)
    stats.duplicate_records_removed = max(
        0,
        stats.input_records - stats.invalid_json_lines - stats.invalid_schema_lines - stats.output_records,
    )

    cleaned = list(deduped.values())
    return cleaned, stats


def clean_activation_training_file(
    input_path: str | Path,
    output_path: str | Path,
    report_path: str | Path | None = None,
) -> CleaningStats:
    source = Path(input_path)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    cleaned, stats = clean_activation_training_data(source.read_text(encoding="utf-8").splitlines())

    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for sample in cleaned:
            handle.write(json.dumps(sample.model_dump(mode="json"), ensure_ascii=False) + "\n")

    if report_path is not None:
        report_target = Path(report_path)
        report_target.parent.mkdir(parents=True, exist_ok=True)
        report_target.write_text(
            json.dumps(
                {
                    "input_path": str(source),
                    "output_path": str(target),
                    "stats": stats.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return stats
