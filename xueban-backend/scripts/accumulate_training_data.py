"""
scripts/accumulate_training_data.py

Batch-generate ActivationTrainingSamples by:
1. Assembling workflows from a set of goal prompts
2. Compiling them with a specified provider (codex/claude)
3. Executing them via `xueban run`
4. Exporting training data after each run

Usage:
    python scripts/accumulate_training_data.py --provider codex --count 10
    python scripts/accumulate_training_data.py --provider claude --goals-file goals.txt
    python scripts/accumulate_training_data.py --dry-run  # just assemble, don't execute

The script writes accumulated samples to scripts/data/training/activation_samples.jsonl,
one ActivationTrainingSample per line.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import List

# Add project root to path
ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "scripts" / "data" / "training"
sys.path.insert(0, str(ROOT))

from xueban.assembly.activation import ActivationSelector, ConnectionResolver
from xueban.highlevel import (
    AgentSpec,
    AssemblyCompiler,
    ExecutorProfileSpec,
    RoleSpec,
    SpecRegistry,
    WorkflowAssemblyBlockSpec,
    WorkflowAssemblySpec,
    build_builtin_block_catalog,
)
from xueban.runtime.contracts import RuntimeRequest, WorkflowDefinition

# ---------------------------------------------------------------------------
# Default goal corpus — diverse goals to exercise different block combinations
# ---------------------------------------------------------------------------

DEFAULT_GOALS: List[str] = [
    # plan only
    "plan a strategy for learning Python",
    "规划一个学习计划",
    "create a detailed plan for building a REST API",
    # plan + execute
    "plan and execute a code review checklist",
    "规划并执行数据清洗任务",
    "plan and run a security audit",
    "plan and execute performance benchmarking",
    # plan + review
    "plan the migration and review the results",
    "规划方案并审查可行性",
    # plan + execute + review
    "plan the task, execute it, and review the output",
    "规划、执行并检查任务完成情况",
    "plan a documentation update, execute it, and review quality",
    # execute only
    "execute the data pipeline",
    "运行测试套件",
    # review only
    "review the pull request changes",
    "审查代码质量",
    # plan + artifact
    "plan and output a report artifact",
    # plan + execute + artifact
    "plan the analysis, execute it, and save the artifact",
    "规划分析流程，执行并保存产物",
    # plan + execute + checkpoint
    "plan the long task, execute with checkpoints",
    # delegate
    "plan and delegate the subtask to a specialist",
    "规划并分派子任务",
    # mixed with parallel
    "plan and execute tasks in parallel",
    "并行执行多个任务并汇聚结果",
]


def _build_workflow(
    goal: str,
    provider: str,
    executor_kind: str,
) -> WorkflowDefinition | None:
    """Assemble + compile a goal into a WorkflowDefinition. Returns None if incomplete."""
    catalog = build_builtin_block_catalog()
    selector = ActivationSelector()
    resolver = ConnectionResolver()

    activation = selector.select(goal, catalog)
    if not activation.complete:
        return None

    links = resolver.resolve(activation.candidates)

    assembly_blocks = [
        WorkflowAssemblyBlockSpec(
            id=c.block_id,
            ref=c.block_id,
            agent="__default_agent__" if catalog[c.block_id].compile.node_kind == "agent" else None,
        )
        for c in activation.candidates
    ]

    assembly = WorkflowAssemblySpec(
        assembly_id=f"accum-{int(time.time())}",
        name=f"accumulated: {goal[:60]}",
        goal=goal,
        blocks=assembly_blocks,
        links=links,
    )

    default_role = RoleSpec(role_id="__default_role__", version="0.1", name="Default Worker")
    default_agent = AgentSpec(
        agent_id="__default_agent__",
        version="0.1",
        name="Default Agent",
        role="__default_role__",
        executor=ExecutorProfileSpec(kind=executor_kind, provider=provider),
    )
    registry = SpecRegistry(
        roles={"__default_role__": default_role},
        agents={"__default_agent__": default_agent},
    )

    compiler = AssemblyCompiler(registry)
    return compiler.compile(assembly)


async def _run_and_export(
    workflow: WorkflowDefinition,
    goal: str,
    writeback_mode: str = "reference",
) -> List[dict]:
    """Execute a workflow and export training samples."""
    from xueban.runtime.service import RuntimeService

    service = RuntimeService()
    # Pass assembly metadata into request.metadata so it flows into run.metadata
    # (RuntimeService._execute merges request.metadata into RunRecord.metadata)
    request = RuntimeRequest(
        workflow=workflow,
        input={"goal": goal, "context": "training data accumulation run"},
        metadata={
            "source_system": "accumulate_script",
            "assembly_goal": workflow.metadata.get("assembly_goal", goal),
            "assembly_block_node_map": workflow.metadata.get("assembly_block_node_map", {}),
        },
    )

    result = await service.run(request)
    dataset = service.export_activation_training_dataset(result.run.run_id)
    return [s.model_dump(mode="json") for s in dataset.samples]


def main():
    parser = argparse.ArgumentParser(description="Accumulate ActivationTrainingSamples")
    parser.add_argument("--provider", default="codex", choices=["codex", "claude", "generic"])
    parser.add_argument("--executor-kind", default="cli", choices=["cli", "api"])
    parser.add_argument("--goals-file", help="File with one goal per line (overrides defaults)")
    parser.add_argument("--count", type=int, help="Limit number of goals to run")
    parser.add_argument("--output", default=str(DATA_DIR / "activation_samples.jsonl"))
    parser.add_argument("--dry-run", action="store_true", help="Assemble only, don't execute")
    args = parser.parse_args()

    # Load goals
    if args.goals_file:
        goals = Path(args.goals_file).read_text(encoding="utf-8").strip().splitlines()
        goals = [g.strip() for g in goals if g.strip()]
    else:
        goals = DEFAULT_GOALS

    if args.count:
        goals = goals[:args.count]

    print(f"Goals: {len(goals)} | Provider: {args.provider} | Dry run: {args.dry_run}")

    # Ensure output directory exists
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_samples = 0
    successful_runs = 0
    failed_assembles = 0

    for i, goal in enumerate(goals):
        print(f"\n[{i+1}/{len(goals)}] Goal: {goal}")

        workflow = _build_workflow(goal, args.provider, args.executor_kind)
        if workflow is None:
            print(f"  -> SKIP: assembly incomplete (OOD goal)")
            failed_assembles += 1
            continue

        blocks = [n.id for n in workflow.nodes]
        print(f"  -> Assembled: {' -> '.join(blocks)}")

        if args.dry_run:
            # In dry-run mode, write the workflow definition for inspection
            print(f"  -> DRY RUN: {len(workflow.nodes)} nodes")
            continue

        try:
            samples = asyncio.run(_run_and_export(workflow, goal))
            with open(output_path, "a", encoding="utf-8") as f:
                for sample in samples:
                    f.write(json.dumps(sample, ensure_ascii=False) + "\n")
            total_samples += len(samples)
            successful_runs += 1
            print(f"  -> OK: {len(samples)} samples exported")
        except Exception as e:
            print(f"  -> ERROR: {e}")

    print(f"\n{'='*60}")
    print(f"Runs: {successful_runs} OK, {failed_assembles} skipped")
    print(f"Samples: {total_samples} total -> {args.output}")
    if not args.dry_run and total_samples > 0:
        print(f"\nTo train ActivationBandit:")
        print(f"  from xueban.assembly.learner import ActivationBandit")
        print(f"  bandit = ActivationBandit()")
        print(f"  bandit.train(dataset)  # needs {max(0, 50 - total_samples)} more for min_samples=50")


if __name__ == "__main__":
    main()
