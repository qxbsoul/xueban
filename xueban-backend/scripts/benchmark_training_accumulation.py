"""
Repeatable benchmark harness for training data accumulation flows.

This script focuses on the local parts of the accumulation pipeline:
1. workflow assembly
2. runtime execution with a local generic CLI stub
3. activation training dataset export

It intentionally avoids external model/network latency so the numbers are
stable enough to use as a local regression baseline.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import tracemalloc
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Dict, List

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scripts.accumulate_training_data import DEFAULT_GOALS, _build_workflow
from xueban.runtime.contracts import RuntimeRequest, WorkflowDefinition
from xueban.runtime.service import RuntimeService


TARGET_GOAL = "plan and execute performance benchmarking"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "training"


def _now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


def _stats(values: List[float]) -> Dict[str, float | None]:
    if not values:
        return {
            "count": 0,
            "mean": None,
            "min": None,
            "max": None,
            "stddev": None,
            "p95": None,
        }
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return {
        "count": len(values),
        "mean": round(mean(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "stddev": round(pstdev(values), 3) if len(values) > 1 else 0.0,
        "p95": round(ordered[p95_index], 3),
    }


def _safe_text(value: str) -> str:
    return value.encode("utf-8", "replace").decode("utf-8")


def _peak_kib() -> float:
    _, peak = tracemalloc.get_traced_memory()
    return round(peak / 1024, 3)


def _ensure_output_dir(raw_path: str) -> Path:
    preferred = Path(raw_path)
    try:
        if preferred.exists() and not preferred.is_dir():
            raise OSError(f"output path is not a directory: {preferred}")
        probe = preferred / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return preferred
    except OSError:
        for fallback in (ROOT / "data" / "training", ROOT):
            try:
                probe = fallback / ".write-test"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                return fallback
            except OSError:
                continue
        raise


def _patch_workflow_for_local_generic(workflow: WorkflowDefinition) -> WorkflowDefinition:
    payload = deepcopy(workflow.model_dump(mode="python", by_alias=True))
    for node in payload["nodes"]:
        if node.get("type") != "agent.execute":
            continue
        node.setdefault("config", {})["executor"] = {
            "kind": "cli",
            "provider": "generic",
            "command": sys.executable,
            "args": [
                "-c",
                (
                    "import json,sys;"
                    "payload=json.load(sys.stdin);"
                    "node=payload.get('node',{}).get('id','agent');"
                    "goal=payload.get('step_input',{}).get('goal','');"
                    "print(json.dumps({"
                    "'message':f'[{node}] local benchmark ok',"
                    "'summary':goal,"
                    "'artifact':{'kind':'report','name':f'{node}.md','content':'# benchmark ok'}"
                    "}, ensure_ascii=False))"
                ),
            ],
            "stdin": "json",
            "parse": "json",
        }
    return WorkflowDefinition.model_validate(payload)


def benchmark_assembly(goal: str, iterations: int, provider: str, executor_kind: str) -> Dict[str, Any]:
    assembly_times: List[float] = []
    peak_kib_values: List[float] = []
    node_count = None
    edge_count = None

    for _ in range(iterations):
        tracemalloc.start()
        started = time.perf_counter()
        workflow = _build_workflow(goal, provider, executor_kind)
        elapsed_ms = _ms(started)
        peak_kib_values.append(_peak_kib())
        tracemalloc.stop()

        if workflow is None:
            raise ValueError("workflow assembly returned None")

        assembly_times.append(elapsed_ms)
        node_count = len(workflow.nodes)
        edge_count = len(workflow.edges)

    return {
        "goal": goal,
        "provider": provider,
        "executor_kind": executor_kind,
        "workflow": {
            "node_count": node_count,
            "edge_count": edge_count,
            "entrypoint": workflow.entrypoint,
            "nodes": [node.id for node in workflow.nodes],
        },
        "assembly_latency": _stats(assembly_times),
        "assembly_peak_kib": _stats(peak_kib_values),
    }


async def _run_local_once(goal: str, provider: str, executor_kind: str) -> Dict[str, Any]:
    assembly_started = time.perf_counter()
    workflow = _build_workflow(goal, provider, executor_kind)
    assembly_ms = _ms(assembly_started)
    if workflow is None:
        raise ValueError("workflow assembly returned None")

    patched = _patch_workflow_for_local_generic(workflow)
    service = RuntimeService()
    request = RuntimeRequest(
        workflow=patched,
        input={"goal": _safe_text(goal), "context": "training data accumulation run"},
        metadata={"source_system": "benchmark_script"},
    )

    run_started = time.perf_counter()
    result = await service.run(request)
    run_ms = _ms(run_started)

    export_started = time.perf_counter()
    dataset = service.export_activation_training_dataset(result.run.run_id)
    export_ms = _ms(export_started)

    return {
        "assembly_ms": assembly_ms,
        "run_ms": run_ms,
        "export_ms": export_ms,
        "total_ms": round(assembly_ms + run_ms + export_ms, 3),
        "steps": len(result.steps),
        "artifacts": len(result.artifacts),
        "checkpoints": len(result.checkpoints),
        "samples": len(dataset.samples) if dataset else 0,
        "final_message": result.final_output.get("message"),
        "nodes": [node.id for node in patched.nodes],
    }


def benchmark_local_runtime(goal: str, iterations: int, provider: str, executor_kind: str) -> Dict[str, Any]:
    runs: List[Dict[str, Any]] = []
    peak_kib_values: List[float] = []

    for _ in range(iterations):
        tracemalloc.start()
        run = asyncio.run(_run_local_once(goal, provider, executor_kind))
        peak_kib_values.append(_peak_kib())
        tracemalloc.stop()
        runs.append(run)

    return {
        "goal": goal,
        "iterations": iterations,
        "shape": {
            "nodes": runs[0]["nodes"] if runs else [],
            "steps": runs[0]["steps"] if runs else 0,
            "artifacts": runs[0]["artifacts"] if runs else 0,
            "checkpoints": runs[0]["checkpoints"] if runs else 0,
            "samples": runs[0]["samples"] if runs else 0,
        },
        "assembly_latency": _stats([item["assembly_ms"] for item in runs]),
        "run_latency": _stats([item["run_ms"] for item in runs]),
        "export_latency": _stats([item["export_ms"] for item in runs]),
        "total_latency": _stats([item["total_ms"] for item in runs]),
        "runtime_peak_kib": _stats(peak_kib_values),
        "last_message": runs[-1]["final_message"] if runs else None,
    }


async def _run_corpus_once(goal: str, provider: str, executor_kind: str) -> Dict[str, Any]:
    started = time.perf_counter()
    workflow = _build_workflow(goal, provider, executor_kind)
    assembly_ms = _ms(started)
    if workflow is None:
        raise ValueError("workflow assembly returned None")

    patched = _patch_workflow_for_local_generic(workflow)
    service = RuntimeService()
    request = RuntimeRequest(
        workflow=patched,
        input={"goal": _safe_text(goal), "context": "training data accumulation run"},
        metadata={"source_system": "benchmark_script"},
    )
    run_started = time.perf_counter()
    result = await service.run(request)
    run_ms = _ms(run_started)
    return {
        "goal": goal,
        "assembly_ms": assembly_ms,
        "run_ms": run_ms,
        "total_ms": round(assembly_ms + run_ms, 3),
        "steps": len(result.steps),
        "artifacts": len(result.artifacts),
        "checkpoints": len(result.checkpoints),
    }


def benchmark_corpus(goals: List[str], provider: str, executor_kind: str) -> Dict[str, Any]:
    successes: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []

    for goal in goals:
        try:
            successes.append(asyncio.run(_run_corpus_once(goal, provider, executor_kind)))
        except Exception as exc:
            failures.append({"goal": goal, "error": str(exc)})

    return {
        "goal_count": len(goals),
        "success_count": len(successes),
        "failure_count": len(failures),
        "total_latency": _stats([item["total_ms"] for item in successes]),
        "assembly_latency": _stats([item["assembly_ms"] for item in successes]),
        "run_latency": _stats([item["run_ms"] for item in successes]),
        "slowest_successes": sorted(successes, key=lambda item: item["total_ms"], reverse=True)[:5],
        "failures": failures,
    }


def build_report_markdown(report: Dict[str, Any]) -> str:
    target_assembly = report["target_assembly"]
    target_runtime = report["target_runtime"]
    corpus = report["corpus"]
    concerns: List[str] = []

    if corpus["failure_count"] > 0:
        concerns.append(f"默认目标集有 {corpus['failure_count']} 个失败样本，训练积累无法全量稳定跑通。")
    if target_runtime["total_latency"]["mean"] and target_runtime["total_latency"]["mean"] > 250:
        concerns.append("目标链路本地闭环均值已超过 250ms，适合作为后续回归门槛。")
    if not concerns:
        concerns.append("目标链路本地 benchmark 表现稳定，当前更大的风险来自功能性失败而不是纯耗时。")

    failure_lines = "\n".join(
        f"- `{item['goal']}`: {item['error']}" for item in corpus["failures"]
    ) or "- 无"

    slowest_lines = "\n".join(
        f"- `{item['goal']}`: total={item['total_ms']}ms, steps={item['steps']}, checkpoints={item['checkpoints']}"
        for item in corpus["slowest_successes"]
    ) or "- 无"

    concern_lines = "\n".join(f"- {item}" for item in concerns)

    return f"""# Training Accumulation Benchmark Report

## Scope

- Target goal: `{report["goal"]}`
- Context: `training data accumulation run`
- Timestamp (UTC): `{report["timestamp_utc"]}`
- Provider profile used for assembly: `{report["provider"]}/{report["executor_kind"]}`
- Runtime benchmark mode: local generic CLI stub (network-free baseline)

## Target Goal Benchmark

### Assembly

- Nodes: `{", ".join(target_assembly["workflow"]["nodes"])}`
- Mean: `{target_assembly["assembly_latency"]["mean"]} ms`
- P95: `{target_assembly["assembly_latency"]["p95"]} ms`
- Min/Max: `{target_assembly["assembly_latency"]["min"]} / {target_assembly["assembly_latency"]["max"]} ms`
- Peak alloc mean: `{target_assembly["assembly_peak_kib"]["mean"]} KiB`

### End-to-End Local Runtime

- Steps / Artifacts / Checkpoints / Samples: `{target_runtime["shape"]["steps"]} / {target_runtime["shape"]["artifacts"]} / {target_runtime["shape"]["checkpoints"]} / {target_runtime["shape"]["samples"]}`
- Assembly mean: `{target_runtime["assembly_latency"]["mean"]} ms`
- Run mean: `{target_runtime["run_latency"]["mean"]} ms`
- Export mean: `{target_runtime["export_latency"]["mean"]} ms`
- Total mean: `{target_runtime["total_latency"]["mean"]} ms`
- Total P95: `{target_runtime["total_latency"]["p95"]} ms`
- Runtime peak alloc mean: `{target_runtime["runtime_peak_kib"]["mean"]} KiB`

## Corpus Smoke Benchmark

- Goal count: `{corpus["goal_count"]}`
- Success / Failure: `{corpus["success_count"]} / {corpus["failure_count"]}`
- Total mean: `{corpus["total_latency"]["mean"]} ms`
- Assembly mean: `{corpus["assembly_latency"]["mean"]} ms`
- Run mean: `{corpus["run_latency"]["mean"]} ms`

### Slowest Successful Goals

{slowest_lines}

### Failures

{failure_lines}

## Key Findings

{concern_lines}

## Notes

- This benchmark isolates local runtime overhead and intentionally excludes external model/API latency.
- The numbers are appropriate for regression tracking inside this repo, not for user-facing end-to-end SLA claims.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark training accumulation flows")
    parser.add_argument("--goal", default=TARGET_GOAL, help="Goal to benchmark")
    parser.add_argument("--provider", default="codex", help="Provider profile used during assembly")
    parser.add_argument("--executor-kind", default="cli", choices=["cli", "api"])
    parser.add_argument("--assembly-iterations", type=int, default=50)
    parser.add_argument("--runtime-iterations", type=int, default=20)
    parser.add_argument("--skip-corpus", action="store_true", help="Skip default-goal smoke benchmark")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    output_dir = None
    output_error = None
    try:
        output_dir = _ensure_output_dir(args.output_dir)
    except OSError as exc:
        output_error = str(exc)

    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "goal": args.goal,
        "provider": args.provider,
        "executor_kind": args.executor_kind,
        "target_assembly": benchmark_assembly(
            goal=args.goal,
            iterations=args.assembly_iterations,
            provider=args.provider,
            executor_kind=args.executor_kind,
        ),
        "target_runtime": benchmark_local_runtime(
            goal=args.goal,
            iterations=args.runtime_iterations,
            provider=args.provider,
            executor_kind=args.executor_kind,
        ),
        "corpus": (
            {"goal_count": 0, "success_count": 0, "failure_count": 0, "total_latency": _stats([]), "assembly_latency": _stats([]), "run_latency": _stats([]), "slowest_successes": [], "failures": []}
            if args.skip_corpus
            else benchmark_corpus(DEFAULT_GOALS, args.provider, args.executor_kind)
        ),
    }

    slug = _now_slug()
    json_path = None
    md_path = None
    markdown = build_report_markdown(report)

    if output_dir is not None:
        try:
            json_path = output_dir / f"{slug}-training-accumulation-benchmark.json"
            md_path = output_dir / f"{slug}-training-accumulation-benchmark.md"
            json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            md_path.write_text(markdown, encoding="utf-8")
        except OSError as exc:
            output_error = str(exc)
            json_path = None
            md_path = None

    print(
        json.dumps(
            {
                "json_report": str(json_path) if json_path else None,
                "markdown_report": str(md_path) if md_path else None,
                "output_error": output_error,
                "report": report,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
