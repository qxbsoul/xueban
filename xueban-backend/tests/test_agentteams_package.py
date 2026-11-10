"""AgentTeams offline code package (复赛必交「可执行代码包」) tests.

- translate_dag_to_team: DAG→Team-spec mapping (workers / loop tasks / parallel flags / skills)
- export_team_package(+zip): package files exist, CR matches live render, run.sh executable content
- CLI subcommand + HTTP endpoint round-trip
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from xueban.runtime import WorkflowDefinition
from xueban.runtime.agentteams_bridge import render_team_cr
from xueban.runtime.agentteams_package import (
    build_package_files,
    export_team_package,
    export_team_package_zip,
    translate_dag_to_team,
)

PDCA_WORKFLOW = {
    "workflow_id": "pdca",
    "version": "0.1",
    "name": "PDCA 闭环",
    "entrypoint": "loop",
    "nodes": [
        {
            "id": "loop", "kind": "node", "type": "control.loop",
            "config": {
                "kind": "until_converge",
                "iteration_budget": 10,
                "setpoint": {"metric": "quality", "op": ">=", "value": 0.95},
                "subgraph": ["planner", "executor", "checker"],
            },
        },
        {"id": "planner", "kind": "agent", "type": "agent.execute",
         "config": {"role": "P", "skills": ["planning"]}},
        {"id": "executor", "kind": "agent", "type": "agent.execute",
         "config": {"role": "C", "skills": ["coding"]}},
        {"id": "checker", "kind": "agent", "type": "agent.execute",
         "config": {"role": "V", "skills": ["review", "planning"]}},
    ],
    "edges": [
        {"from": "loop", "to": "END", "type": "final"},
        {"from": "planner", "to": "executor", "type": "default"},
        {"from": "executor", "to": "checker", "type": "default"},
    ],
}

PARALLEL_WORKFLOW = {
    "workflow_id": "par",
    "version": "0.1",
    "name": "Parallel research",
    "entrypoint": "fan",
    "nodes": [
        {"id": "fan", "kind": "node", "type": "control.parallel",
         "config": {"branches": ["a", "b"], "barrier": "join"}},
        {"id": "a", "kind": "agent", "type": "agent.execute", "config": {"role": "researcher"}},
        {"id": "b", "kind": "agent", "type": "agent.execute", "config": {"role": "writer"}},
        {"id": "join", "kind": "node", "type": "control.barrier",
         "config": {"source_parallel": "fan"}},
    ],
    "edges": [{"from": "join", "to": "END", "type": "final"}],
}


def test_translate_maps_agents_to_workers_and_loop_to_task():
    spec = translate_dag_to_team(WorkflowDefinition.model_validate(PDCA_WORKFLOW))
    assert spec["provider"] == "pdca"
    roles = {w["role"]: w["id"] for w in spec["workers"]}
    assert roles == {"P": "planner", "C": "executor", "V": "checker"}
    # skills 取并集且去重
    assert spec["skills"] == ["coding", "planning", "review"]
    # control.loop → task，不成为 worker
    assert len(spec["tasks"]) == 1
    task = spec["tasks"][0]
    assert task["success_criteria"] == {"metric": "quality", "op": ">=", "value": 0.95}
    assert task["iteration_budget"] == 10
    assert task["body"] == ["planner", "executor", "checker"]
    assert task["id"] not in [w["id"] for w in spec["workers"]]


def test_translate_flags_parallel_branch_workers():
    spec = translate_dag_to_team(WorkflowDefinition.model_validate(PARALLEL_WORKFLOW))
    by_id = {w["id"]: w for w in spec["workers"]}
    assert by_id["a"]["parallel"] is True
    assert by_id["b"]["parallel"] is True


def test_package_files_and_cr_consistency():
    workflow = WorkflowDefinition.model_validate(PDCA_WORKFLOW)
    files = build_package_files(workflow)
    assert set(files) == {"team-cr.yaml", "workflow.json", "manifest.json", "run.sh", "README.md"}
    # 导出的 CR 必须与 live 派发用的 render_team_cr 一致（离线包=live spec）
    spec = translate_dag_to_team(workflow)
    assert files["team-cr.yaml"] == render_team_cr(spec)
    assert "agentteams.alibaba.com/v1" in files["team-cr.yaml"]
    manifest = json.loads(files["manifest.json"])
    assert manifest["team"]["workers"][0]["id"] == "planner"
    assert "kubectl apply" in files["run.sh"]
    assert "pdca" in files["README.md"]


def test_export_to_dir(tmp_path: Path):
    workflow = WorkflowDefinition.model_validate(PDCA_WORKFLOW)
    out = export_team_package(workflow, tmp_path / "pkg")
    assert sorted(p.name for p in out.iterdir()) == [
        "README.md", "manifest.json", "run.sh", "team-cr.yaml", "workflow.json",
    ]
    assert json.loads((out / "workflow.json").read_text(encoding="utf-8"))["workflow_id"] == "pdca"


def test_export_zip_round_trip():
    workflow = WorkflowDefinition.model_validate(PDCA_WORKFLOW)
    raw = export_team_package_zip(workflow)
    with zipfile.ZipFile(__import__("io").BytesIO(raw)) as zf:
        names = sorted(zf.namelist())
        assert "team-cr.yaml" in names and "run.sh" in names
        assert "kind: Team" in zf.read("team-cr.yaml").decode("utf-8")


def test_http_endpoint_returns_zip():
    from fastapi.testclient import TestClient

    from xueban.server import app

    client = TestClient(app)
    resp = client.post("/workflow/export/agentteams", json=PDCA_WORKFLOW)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]
    with zipfile.ZipFile(__import__("io").BytesIO(resp.content)) as zf:
        assert "team-cr.yaml" in zf.namelist()


def test_cli_export_command(tmp_path: Path, capsys, monkeypatch):
    import sys

    from xueban.cli import main

    wf_file = tmp_path / "wf.json"
    wf_file.write_text(json.dumps(PDCA_WORKFLOW), encoding="utf-8")
    out_zip = tmp_path / "pkg.zip"
    monkeypatch.setattr(
        sys, "argv",
        ["xueban", "export-agentteams", str(wf_file), "--out", str(out_zip)],
    )
    try:
        main()
    except SystemExit as exit_code:
        assert exit_code.code in (0, None)
    assert out_zip.exists()
    captured = capsys.readouterr().out
    assert "code package written" in captured
