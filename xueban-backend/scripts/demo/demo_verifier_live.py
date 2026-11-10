"""Live demo: PDCA loop with a real LLM-as-Verifier (Zhipu GLM, BYOK).

Runs the cybernetic control.loop with sensor.type=verifier so every iteration's
convergence decision is scored by a real LLM call (ApiExecutor provider=zhipu).
Requires: ZHIPU_API_KEY in env (free-tier glm-4-flash works); optional
SF_VERIFIER_PROVIDER/SF_VERIFIER_API_KEY_ENV overrides.

Usage:
    python scripts/demo/demo_verifier_live.py [--k 2] [--budget 4]
Writes a markdown evidence file under docs/competition/run-evidence/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition  # noqa: E402


def build_workflow(*, budget: int, k: int, provider: str, model: str) -> WorkflowDefinition:
    verifier_cfg: dict = {"provider": provider, "K": k, "temperature": 0}
    if model:
        verifier_cfg["model"] = model
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "demo-verifier-live",
            "version": "0.1",
            "name": "PDCA Live Verifier Demo",
            "entrypoint": "loop",
            "nodes": [
                {
                    "id": "loop",
                    "kind": "node",
                    "type": "control.loop",
                    "config": {
                        "kind": "until_converge",
                        "iteration_budget": budget,
                        "setpoint": {"metric": "quality_score", "op": ">=", "value": 0.85},
                        "controller": {
                            "type": "setpoint_nudge",
                            "field": "effort",
                            "rate": 3,
                            "init": {"effort": 0},
                        },
                        "sensor": {"type": "verifier", "verifier": verifier_cfg},
                        "subgraph": ["writer"],
                    },
                },
                {
                    "id": "writer",
                    "kind": "agent",
                    "type": "agent.execute",
                    "config": {
                        "role": "doer",
                        "message_template": "[doer] 产出质量分 0.9（effort={effort}）",
                        "emit": {"quality_score": 0.9},
                        "set_state": {"quality_score": 0.9},
                    },
                },
            ],
            "edges": [{"from": "loop", "to": "END", "type": "final"}],
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=1, help="verifier sampling repetitions")
    parser.add_argument("--budget", type=int, default=4, help="loop iteration budget")
    args = parser.parse_args()

    provider = os.environ.get("SF_VERIFIER_PROVIDER", "zhipu")
    model = os.environ.get("ZHIPU_MODEL", "glm-4-flash")
    if not os.environ.get(os.environ.get("SF_VERIFIER_API_KEY_ENV", "ZHIPU_API_KEY")):
        print("missing ZHIPU_API_KEY (or SF_VERIFIER_API_KEY_ENV target) in env", file=sys.stderr)
        return 2

    result = asyncio.run(
        RuntimeService().run(
            RuntimeRequest(
                workflow=build_workflow(budget=args.budget, k=args.k, provider=provider, model=model),
                input={"goal": "produce a high-quality deliverable"},
            )
        )
    )
    out = result.final_output
    rec = out.get("loop_record", {})
    iters = rec.get("iterations", [])

    print("=== PDCA live verifier demo ===")
    print(f"provider={provider} model={model} K={args.k}")
    print(f"converged={out.get('loop_converged')} iterations={out.get('loop_iterations')} status={out.get('loop_status')}")
    for it in iters:
        ev = it.get("evidence", {})
        sensor = ev.get("sensor", {}) or {}
        print(
            f"  iter {it.get('index')}: value={sensor.get('value')} "
            f"verifier_score={sensor.get('verifier_score')} conf={sensor.get('verifier_confidence')} "
            f"converged={sensor.get('converged')} reasoning={str(sensor.get('verifier_reasoning'))[:80]}"
        )

    evidence_dir = REPO / "docs" / "competition" / "run-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    path = evidence_dir / f"{stamp}-verifier-live.md"
    path.write_text(
        f"""# LLM-as-Verifier Live Run（{stamp}）

- provider: `{provider}` / model: `{model}` / K: {args.k}（真实 API 调用）
- workflow: `demo-verifier-live`（control.loop + sensor.type=verifier）
- 结果: converged={out.get('loop_converged')} iterations={out.get('loop_iterations')} status={out.get('loop_status')}

## Iterations

```json
{json.dumps(
    [
        {
            "index": it.get("index"),
            "sensor": it.get("evidence", {}).get("sensor"),
        }
        for it in iters
    ],
    ensure_ascii=False,
    indent=2,
)}
```
""",
        encoding="utf-8",
    )
    print(f"evidence written: {path}")
    return 0 if out.get("loop_converged") else 1


if __name__ == "__main__":
    sys.exit(main())
