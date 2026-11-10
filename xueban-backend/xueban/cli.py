import argparse
import asyncio
from copy import deepcopy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import yaml

from xueban.highlevel import (
    BUILTIN_AGENT_PRESET_IDS,
    BUILTIN_PRESET_IDS,
    BUILTIN_ROLE_PRESET_IDS,
    TemplateCompiler,
    WorkflowTemplateSpec,
    _slugify,
    build_builtin_preset_bundle,
    export_registry_bundle,
    infer_registry_root,
    import_registry_bundle,
    list_builtin_agent_presets,
    list_builtin_presets,
    list_builtin_role_presets,
    load_spec_file,
    materialize_builtin_preset,
    render_workflow_summary_text,
    save_spec_file,
    scaffold_agent_spec,
    scaffold_role_spec,
    scaffold_skill_spec,
    scaffold_template_spec,
    scaffold_tool_spec,
    SpecRegistry,
    summarize_workflow_definition,
    write_registry_bundle,
)
from xueban.runtime import (
    ChatMessageRequest,
    ChatSessionCreateRequest,
    FileChatSessionStore,
    FileCheckpointStore,
    FileRequestContextStore,
    FileRunStore,
    InMemoryCheckpointStore,
    MarkdownWritebackAdapter,
    ResumeRequest,
    RuntimeRequest,
    RuntimeService,
    WorkflowDefinition,
)

def _default_runtime_root() -> Path:
    override = os.environ.get("XUEBAN_RUNTIME_ROOT") or os.environ.get("AGENTGRAPH_RUNTIME_ROOT")
    if override:
        return Path(override).expanduser()
    codex_memories = Path.home() / ".codex" / "memories"
    if codex_memories.exists():
        return codex_memories / "xueban-runtime"
    return Path.cwd() / "xueban-runtime"


DEFAULT_RUNTIME_ROOT = str(_default_runtime_root())


def _build_runtime_service(
    writeback_mode: str = "reference",
    writeback_root: str | None = None,
    store: str = "file",
) -> RuntimeService:
    if writeback_mode == "markdown":
        root = Path(writeback_root or DEFAULT_RUNTIME_ROOT)
        checkpoint_store = FileCheckpointStore(root / "checkpoint-store")
        run_store = FileRunStore(root / "runs")
        request_context_store = FileRequestContextStore(root / "requests")
        chat_session_store = FileChatSessionStore(root / "chat" / "sessions")
        adapter = MarkdownWritebackAdapter(root, checkpoint_store=checkpoint_store)
        return RuntimeService(
            writeback_adapter=adapter,
            checkpoint_store=checkpoint_store,
            run_store=run_store,
            request_context_store=request_context_store,
            chat_session_store=chat_session_store,
        )
    # Select checkpoint store backend
    if store == "memory":
        checkpoint_store = InMemoryCheckpointStore()
    else:  # "file" (default)
        root = Path(writeback_root or DEFAULT_RUNTIME_ROOT)
        checkpoint_store = FileCheckpointStore(root / "checkpoint-store")
    return RuntimeService(checkpoint_store=checkpoint_store)


def _load_workflow_definition(workflow_path: str) -> WorkflowDefinition:
    path = Path(workflow_path)
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix.lower() in {".yaml", ".yml"}:
            payload = yaml.safe_load(handle)
        else:
            payload = json.load(handle)
    return WorkflowDefinition.model_validate(payload)


def _load_template_definition(template_path: str) -> WorkflowTemplateSpec:
    return load_spec_file(template_path, WorkflowTemplateSpec)


def _parse_input_payload(input_value: str) -> dict:
    try:
        parsed = json.loads(input_value)
    except json.JSONDecodeError:
        return {"message": input_value}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _parse_key_value_pairs(pairs: list[str] | None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for item in pairs or []:
        if "=" not in item:
            raise ValueError(f"--var must use key=value format: {item}")
        key, value = item.split("=", 1)
        payload[key] = value
    return payload


def _parse_assignment_pairs(pairs: list[str] | None) -> Dict[str, Dict[str, Any]]:
    assignments: Dict[str, Dict[str, Any]] = {}
    for item in pairs or []:
        if "=" not in item:
            raise ValueError(f"--assign must use node.field=value format: {item}")
        left, value = item.split("=", 1)
        if "." not in left:
            raise ValueError(f"--assign must use node.field=value format: {item}")
        node_id, field = left.split(".", 1)
        node_id = node_id.strip()
        field = field.strip()
        if not node_id or not field:
            raise ValueError(f"--assign must use node.field=value format: {item}")
        if field == "owned_topics":
            parsed_value: Any = [part.strip() for part in value.split(",") if part.strip()]
        else:
            parsed_value = value
        assignments.setdefault(node_id, {})[field] = parsed_value
    return assignments


def _parse_assignment_payload(assignments_json: str | None, assignment_pairs: list[str] | None) -> Dict[str, Dict[str, Any]]:
    assignments: Dict[str, Dict[str, Any]] = {}
    if assignments_json:
        parsed = json.loads(assignments_json)
        if not isinstance(parsed, dict):
            raise ValueError("--assignments-json must decode to an object")
        for node_id, payload in parsed.items():
            if not isinstance(node_id, str) or not isinstance(payload, dict):
                raise ValueError("--assignments-json must map node ids to assignment objects")
            assignments[node_id] = payload
    for node_id, payload in _parse_assignment_pairs(assignment_pairs).items():
        assignments.setdefault(node_id, {}).update(payload)
    return assignments


def _apply_template_assignments(template: WorkflowTemplateSpec, assignments: Dict[str, Dict[str, Any]]) -> WorkflowTemplateSpec:
    if not assignments:
        return template
    payload = template.model_dump(mode="python", by_alias=True)
    known_ids = {agent["id"] for agent in payload.get("agents", [])}
    unknown = sorted(set(assignments) - known_ids)
    if unknown:
        raise ValueError(f"assignment references unknown template agent ids: {', '.join(unknown)}")
    for agent in payload.get("agents", []):
        agent_id = agent["id"]
        if agent_id in assignments:
            current = deepcopy(agent.get("assignment", {}) or {})
            current.update(assignments[agent_id])
            agent["assignment"] = current
    return WorkflowTemplateSpec.model_validate(payload)


def _build_executor_config_from_args(args) -> dict:
    executor = {
        "kind": args.kind,
        "provider": args.provider,
    }
    if getattr(args, "model", None):
        executor["model"] = args.model
    if getattr(args, "system_prompt", None):
        executor["system_prompt"] = args.system_prompt
    if getattr(args, "parse", None):
        executor["parse"] = args.parse
    if getattr(args, "timeout_seconds", None) is not None:
        executor["timeout_seconds"] = args.timeout_seconds
    if getattr(args, "cli_command", None):
        executor["command"] = args.cli_command
    args_json = getattr(args, "args_json", None)
    if args_json:
        parsed_args = json.loads(args_json)
        if not isinstance(parsed_args, list) or not all(isinstance(item, str) for item in parsed_args):
            raise ValueError("--args-json must decode to a string list")
        executor["args"] = parsed_args
    elif getattr(args, "args_list", None):
        executor["args"] = args.args_list
    if getattr(args, "stdin_mode", None):
        executor["stdin"] = args.stdin_mode
    if getattr(args, "cwd", None):
        executor["cwd"] = args.cwd
    return executor


async def validate_workflow(workflow_path: str) -> int:
    runtime_service = _build_runtime_service()
    workflow = _load_workflow_definition(workflow_path)
    validation = runtime_service.validate_workflow(workflow)
    print(validation.model_dump_json(indent=2))
    return 0 if validation.valid else 1


async def run_workflow(
    workflow_path: str,
    input_value: str,
    user_id: str,
    writeback_mode: str,
    writeback_root: str | None,
    store: str = "file",
):
    runtime_service = _build_runtime_service(
        writeback_mode=writeback_mode,
        writeback_root=writeback_root,
        store=store,
    )
    workflow = _load_workflow_definition(workflow_path)
    request = RuntimeRequest(
        workflow=workflow,
        input=_parse_input_payload(input_value),
        metadata={"source_system": "cli", "user_id": user_id},
    )
    result = await runtime_service.run(request)
    print(result.model_dump_json(indent=2))
    return 0


async def export_workflow_graph(workflow_path: str) -> int:
    runtime_service = _build_runtime_service()
    workflow = _load_workflow_definition(workflow_path)
    graph = runtime_service.export_workflow_graph(workflow)
    print(graph.model_dump_json(indent=2))
    return 0


def export_agentteams_command(workflow_path: str, out: str | None) -> int:
    """Export the AgentTeams executable code package for a workflow (复赛交付件).

    Writes team-cr.yaml / workflow.json / manifest.json / run.sh / README.md
    into --out DIR (default: ./agentteams-export/<workflow_id>.zip in CWD).
    """
    from xueban.runtime.agentteams_package import (
        export_team_package,
        export_team_package_zip,
    )

    workflow = _load_workflow_definition(workflow_path)
    if out and out.endswith("/"):
        target = export_team_package(workflow, out.rstrip("/"))
    elif out:
        target = export_team_package(workflow, out)
    else:
        zip_bytes = export_team_package_zip(workflow)
        target = Path(f"agentteams-export-{workflow.workflow_id}.zip")
        target.write_bytes(zip_bytes)
    print(f"AgentTeams code package written to: {Path(target).resolve()}")
    return 0


def export_sfagent_command(agent_path: str, out: str | None) -> int:
    """Export a .sfagent agent bundle (GOAI M9).

    AGENT_PATH is a JSON/YAML agent spec (id/name/role/capabilities/skills…).
    Writes sfagent.json / identity.yaml / skills.yaml / trajectory.json /
    README.md into --out DIR (default: ./<agent_id>.sfagent.zip in CWD).
    """
    import json as _json

    import yaml

    from xueban.runtime.agent_bundle import build_sfagent_files, export_sfagent_zip

    raw = Path(agent_path).read_text(encoding="utf-8")
    agent = yaml.safe_load(raw) if agent_path.endswith((".yaml", ".yml")) else _json.loads(raw)
    if not isinstance(agent, dict):
        print(f"error: agent spec must be a mapping: {agent_path}", file=sys.stderr)
        return 2
    if out and out.endswith("/"):
        target_dir = Path(out.rstrip("/"))
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, content in build_sfagent_files(agent).items():
            (target_dir / name).write_text(content, encoding="utf-8", newline="\n")
        target = target_dir
    else:
        zip_bytes = export_sfagent_zip(agent)
        target = Path(out or f"{agent.get('id') or agent.get('name') or 'agent'}.sfagent.zip")
        target.write_bytes(zip_bytes)
    print(f".sfagent bundle written to: {Path(target).resolve()}")
    return 0


def validate_plugin_command(plugin_path: str) -> int:
    """Validate a .sfplugin package zip and print the report."""
    import json as _json

    from xueban.runtime.plugin_package import validate_plugin_package

    report = validate_plugin_package(Path(plugin_path).read_bytes())
    print(_json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 2


def import_sfagent_command(zip_path: str) -> int:
    """Import a .sfagent bundle into the agent store (.xueban/agents)."""
    from xueban.runtime.agent_bundle import import_sfagent_zip

    zip_bytes = Path(zip_path).read_bytes()
    agents_dir = Path.cwd() / ".xueban" / "agents"
    try:
        record = import_sfagent_zip(zip_bytes, agents_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"imported agent {record['agent_id']!r} → {record['path']}")
    return 0


async def compile_template_command(
    template_ref: str,
    registry_root: str | None,
    parameters_json: str | None,
    var_pairs: list[str] | None,
    output_path: str | None,
    output_format: str,
    summary_format: str,
) -> int:
    inferred_registry_root = infer_registry_root(template_ref, registry_root)
    registry = SpecRegistry.load_from_root(inferred_registry_root)

    template_path = Path(template_ref)
    if template_path.exists():
        template = _load_template_definition(template_ref)
    else:
        template = registry.get_template(template_ref)

    parameters: Dict[str, Any] = {}
    if parameters_json:
        parsed = json.loads(parameters_json)
        if not isinstance(parsed, dict):
            raise ValueError("--parameters must decode to an object")
        parameters.update(parsed)
    parameters.update(_parse_key_value_pairs(var_pairs))

    workflow = TemplateCompiler(registry).compile(template, parameters=parameters)
    summary = summarize_workflow_definition(workflow)
    if output_format == "yaml":
        rendered = yaml.safe_dump(
            workflow.model_dump(mode="python", by_alias=True),
            sort_keys=False,
            allow_unicode=True,
        )
    else:
        if summary_format == "json":
            rendered = json.dumps(
                {
                    "workflow": workflow.model_dump(mode="json", by_alias=True),
                    "summary": summary,
                },
                ensure_ascii=False,
                indent=2,
            )
        else:
            rendered = json.dumps(workflow.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)

    if output_path:
        Path(output_path).write_text(rendered, encoding="utf-8")
    else:
        print(rendered)
    if summary_format == "text":
        print(render_workflow_summary_text(summary))
    return 0


async def registry_counts_command(registry_root: str) -> int:
    registry = SpecRegistry.load_from_root(registry_root)
    print(json.dumps(registry.counts(), ensure_ascii=False, indent=2))
    return 0


async def registry_list_command(registry_root: str, kind: str) -> int:
    from xueban.highlevel import build_builtin_block_catalog

    registry = SpecRegistry.load_from_root(registry_root)
    items = registry.list_kind(kind)
    # For blocks, always include builtin catalog entries (registry entries take precedence)
    if kind == "blocks":
        builtin = build_builtin_block_catalog()
        existing_ids = {item.block_id for item in items}  # type: ignore[attr-defined]
        for block_id, block in builtin.items():
            if block_id not in existing_ids:
                items.append(block)
    print(
        json.dumps(
            [item.model_dump(mode="json", by_alias=True) for item in items],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


async def assemble_goal_command(
    goal: str,
    *,
    compile: bool = False,
    provider: str | None = None,
    executor_kind: str | None = None,
) -> int:
    """
    Catalog-level activation: given a goal string, return ActivationResult +
    a partial WorkflowAssemblySpec (blocks + links).

    With --compile --provider --executor-kind: go one step further and produce
    a full WorkflowDefinition by auto-binding a default agent to every
    agent-kind block. This is the "one-shot goal → executable workflow" path.

    Exit code 0 if complete=True (all required capabilities covered).
    Exit code 1 if complete=False (prints missing_capabilities).
    """
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
    from xueban.assembly.activation import ActivationSelector, ConnectionResolver

    catalog = build_builtin_block_catalog()
    selector = ActivationSelector()
    resolver = ConnectionResolver()

    activation = selector.select(goal, catalog)
    links = resolver.resolve(activation.candidates)

    if not activation.complete:
        output = {
            "complete": False,
            "missing_capabilities": activation.missing_capabilities,
            "fallback_policy": activation.fallback_policy,
            "candidates": [c.model_dump(mode="json") for c in activation.candidates],
            "assembly": None,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 1

    # Build assembly blocks — bind default agent when compiling
    default_agent_id = "__default_agent__" if compile else None
    assembly_blocks = [
        WorkflowAssemblyBlockSpec(
            id=c.block_id,
            ref=c.block_id,
            agent=default_agent_id if catalog[c.block_id].compile.node_kind == "agent" else None,
        )
        for c in activation.candidates
    ]

    assembly = WorkflowAssemblySpec(
        assembly_id="assembled",
        name=f"assembled: {goal[:60]}",
        goal=goal,
        blocks=assembly_blocks,
        links=links,
    )

    if not compile:
        output = {
            "complete": True,
            "missing_capabilities": [],
            "fallback_policy": activation.fallback_policy,
            "candidates": [c.model_dump(mode="json") for c in activation.candidates],
            "assembly": assembly.model_dump(mode="json", by_alias=True),
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    # --compile mode: create an in-memory registry with a default agent + role
    provider = provider or "claude"
    executor_kind = executor_kind or "cli"
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
    workflow = compiler.compile(assembly)
    print(json.dumps(workflow.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


async def do_goal_command(
    goal: str,
    *,
    provider: str = "codex",
    executor_kind: str = "cli",
    cwd: str | None = None,
    context: str | None = None,
    writeback: str = "reference",
    export_training: bool = False,
) -> int:
    """
    One-shot: assemble → compile → execute a goal end-to-end.

    This is the "give it a task and it runs" command:
      xueban do "analyze the data and generate a report" --cwd /path/to/project --provider codex
    """
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
    from xueban.assembly.activation import ActivationSelector, ConnectionResolver
    from xueban.runtime.contracts import RuntimeRequest
    from xueban.runtime.service import RuntimeService

    catalog = build_builtin_block_catalog()
    selector = ActivationSelector()
    resolver = ConnectionResolver()

    # Step 1: Assemble
    activation = selector.select(goal, catalog)

    if not activation.complete:
        print(json.dumps({
            "status": "failed",
            "reason": "assembly_incomplete",
            "missing_capabilities": activation.missing_capabilities,
            "fallback_policy": activation.fallback_policy,
        }, ensure_ascii=False, indent=2))
        return 1

    links = resolver.resolve(activation.candidates)

    # Step 2: Compile with default agent (pass cwd into executor config)
    executor_config: Dict[str, Any] = {"kind": executor_kind, "provider": provider}
    if cwd:
        executor_config["cwd"] = cwd

    default_role = RoleSpec(role_id="__do_role__", version="0.1", name="Do Worker")
    default_agent = AgentSpec(
        agent_id="__do_agent__",
        version="0.1",
        name="Do Agent",
        role="__do_role__",
        executor=ExecutorProfileSpec(**executor_config),
    )

    assembly_blocks = [
        WorkflowAssemblyBlockSpec(
            id=c.block_id,
            ref=c.block_id,
            agent="__do_agent__" if catalog[c.block_id].compile.node_kind == "agent" else None,
        )
        for c in activation.candidates
    ]

    assembly = WorkflowAssemblySpec(
        assembly_id="do",
        name=f"do: {goal[:60]}",
        goal=goal,
        blocks=assembly_blocks,
        links=links,
    )

    registry = SpecRegistry(
        roles={"__do_role__": default_role},
        agents={"__do_agent__": default_agent},
    )
    workflow = AssemblyCompiler(registry).compile(assembly)

    # Step 3: Execute
    input_payload: Dict[str, Any] = {"goal": goal}
    if context:
        input_payload["context"] = context
    if cwd:
        input_payload["project_path"] = cwd

    request = RuntimeRequest(
        workflow=workflow,
        input=input_payload,
        metadata={
            "source_system": "xueban_do",
            "assembly_goal": workflow.metadata.get("assembly_goal", goal),
            "assembly_block_node_map": workflow.metadata.get("assembly_block_node_map", {}),
        },
    )

    runtime_service = _build_runtime_service(writeback_mode=writeback)
    result = await runtime_service.run(request)

    # Step 4: Output
    output: Dict[str, Any] = {
        "status": "completed",
        "goal": goal,
        "blocks": [c.block_id for c in activation.candidates],
        "steps": len(result.steps),
        "final_output": result.final_output,
    }
    if result.errors:
        output["errors"] = result.errors

    # Step 5: Optional training data export
    if export_training:
        dataset = runtime_service.export_activation_training_dataset(result.run.run_id)
        output["training_samples"] = len(dataset.samples)

    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0


async def registry_get_command(registry_root: str, kind: str, spec_id: str) -> int:
    registry = SpecRegistry.load_from_root(registry_root)
    item = registry.get_kind(kind, spec_id)
    print(json.dumps(item.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2))
    return 0


async def registry_export_command(
    registry_root: str,
    output_root: str,
    kind: str | None,
    spec_id: str | None,
    force: bool,
) -> int:
    written = export_registry_bundle(registry_root, output_root, kind=kind, spec_id=spec_id, force=force)
    print(
        json.dumps(
            {"output_root": str(Path(output_root)), "written": [str(path) for path in written]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


async def registry_import_command(
    registry_root: str,
    source_root: str | None,
    preset: str | None,
    workflow_id: str | None,
    provider: str,
    executor_kind: str,
    kind: str | None,
    spec_id: str | None,
    force: bool,
) -> int:
    if preset:
        result = materialize_builtin_preset(
            preset,
            registry_root,
            workflow_id=workflow_id,
            provider=provider,
            executor_kind=executor_kind,
            force=force,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not source_root:
        raise ValueError("registry import requires --source-root or --preset")
    written = import_registry_bundle(source_root, registry_root, kind=kind, spec_id=spec_id, force=force)
    print(
        json.dumps(
            {"registry_root": str(Path(registry_root)), "written": [str(path) for path in written]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _write_scaffold(path: Path, payload: Dict[str, Any], *, force: bool) -> Path:
    if path.exists() and not force:
        raise ValueError(f"spec file already exists: {path}")
    return save_spec_file(path, payload)


async def init_tool_command(
    registry_root: str,
    tool_id: str,
    kind: str,
    output_path: str | None,
    force: bool,
) -> int:
    payload = scaffold_tool_spec(tool_id, kind=kind)
    path = Path(output_path) if output_path else Path(registry_root) / "tools" / f"{tool_id}.yaml"
    written = _write_scaffold(path, payload, force=force)
    print(json.dumps({"written": str(written), "tool_id": tool_id}, ensure_ascii=False, indent=2))
    return 0


async def init_skill_command(registry_root: str, skill_id: str, output_path: str | None, force: bool) -> int:
    payload = scaffold_skill_spec(skill_id)
    path = Path(output_path) if output_path else Path(registry_root) / "skills" / f"{skill_id}.yaml"
    written = _write_scaffold(path, payload, force=force)
    print(json.dumps({"written": str(written), "skill_id": skill_id}, ensure_ascii=False, indent=2))
    return 0


async def init_role_command(registry_root: str, role_id: str, output_path: str | None, force: bool) -> int:
    payload = scaffold_role_spec(role_id)
    path = Path(output_path) if output_path else Path(registry_root) / "roles" / f"{role_id}.yaml"
    written = _write_scaffold(path, payload, force=force)
    print(json.dumps({"written": str(written), "role_id": role_id}, ensure_ascii=False, indent=2))
    return 0


async def init_role_with_preset_command(
    registry_root: str,
    role_id: str,
    preset: str,
    output_path: str | None,
    force: bool,
) -> int:
    payload = scaffold_role_spec(role_id, preset=preset)
    path = Path(output_path) if output_path else Path(registry_root) / "roles" / f"{role_id}.yaml"
    written = _write_scaffold(path, payload, force=force)
    print(
        json.dumps(
            {"written": str(written), "role_id": role_id, "preset_id": preset},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


async def init_agent_command(
    registry_root: str,
    agent_id: str,
    role: str | None,
    skills: list[str] | None,
    tools: list[str] | None,
    provider: str | None,
    executor_kind: str | None,
    preset: str | None,
    output_path: str | None,
    force: bool,
) -> int:
    payload = scaffold_agent_spec(
        agent_id,
        role=role,
        skills=skills,
        tools=tools,
        provider=provider,
        executor_kind=executor_kind,
        preset=preset,
    )
    path = Path(output_path) if output_path else Path(registry_root) / "agents" / f"{agent_id}.yaml"
    written = _write_scaffold(path, payload, force=force)
    response = {"written": str(written), "agent_id": agent_id}
    if preset:
        response["preset_id"] = preset
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


async def init_template_command(
    registry_root: str,
    template_id: str,
    agent_ref: str,
    agent_node_id: str,
    output_path: str | None,
    force: bool,
) -> int:
    payload = scaffold_template_spec(template_id, agent_ref=agent_ref, agent_node_id=agent_node_id)
    path = Path(output_path) if output_path else Path(registry_root) / "templates" / f"{template_id}.yaml"
    written = _write_scaffold(path, payload, force=force)
    print(json.dumps({"written": str(written), "template_id": template_id}, ensure_ascii=False, indent=2))
    return 0


def _prompt_value(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in {None, ""} else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or (default or "")


def _select_preset_interactively(default: str = "single-reviewer") -> str:
    presets = list_builtin_presets()
    print("可用 preset：")
    for index, preset in enumerate(presets, start=1):
        print(f"{index}. {preset['preset_id']} - {preset['description']}")
    answer = _prompt_value("选择 preset（输入编号或 preset_id）", default)
    if answer.isdigit():
        idx = int(answer) - 1
        if 0 <= idx < len(presets):
            return presets[idx]["preset_id"]
    preset_ids = {preset["preset_id"] for preset in presets}
    if answer not in preset_ids:
        raise ValueError(f"unsupported preset: {answer}")
    return answer


def _recommend_preset_for_task_kind(task_kind: str) -> str:
    normalized = task_kind.strip().lower()
    mapping = {
        "review": "single-reviewer",
        "analysis": "single-reviewer",
        "build": "planner-coder-reviewer",
        "code": "planner-coder-reviewer",
        "delivery": "planner-coder-reviewer",
        "research": "research-review-publish",
        "publish": "research-review-publish",
        "content": "research-review-publish",
    }
    preset = mapping.get(normalized)
    if preset is None:
        raise ValueError(f"unsupported task kind: {task_kind}")
    return preset


def _default_workflow_output(registry_root: str, workflow_id: str) -> Path:
    return Path(registry_root) / "compiled-workflows" / f"{workflow_id}.yaml"


async def init_workflow_command(
    registry_root: str,
    workflow_id: str,
    preset: str | None,
    goal: str,
    provider: str,
    executor_kind: str,
    task_kind: str | None,
    assignments_json: str | None,
    assignment_pairs: list[str] | None,
    output_path: str | None,
    output_format: str,
    force: bool,
) -> int:
    resolved_preset = preset or (_recommend_preset_for_task_kind(task_kind) if task_kind else "single-reviewer")
    workflow_slug = _slugify(workflow_id)
    final_output_path = Path(output_path) if output_path else _default_workflow_output(registry_root, workflow_slug)
    if final_output_path.exists() and not force:
        raise ValueError(f"workflow output already exists: {final_output_path}")
    bundle = build_builtin_preset_bundle(
        resolved_preset,
        workflow_id=workflow_id,
        provider=provider,
        executor_kind=executor_kind,
    )
    assignments = _parse_assignment_payload(assignments_json, assignment_pairs)

    with tempfile.TemporaryDirectory(prefix="xueban-init-workflow-") as temp_root:
        write_registry_bundle(bundle, temp_root, force=True)
        registry = SpecRegistry.load_from_root(temp_root)
        template = registry.get_template(workflow_slug)
        template = _apply_template_assignments(template, assignments)
        workflow = TemplateCompiler(registry).compile(template, parameters={"goal": goal})
        summary = summarize_workflow_definition(workflow)

    result = {
        "preset_id": resolved_preset,
        "workflow_id": workflow_slug,
        "registry_root": str(Path(registry_root)),
    }
    write_registry_bundle(bundle, registry_root, force=force)

    if output_format == "json":
        rendered = json.dumps(workflow.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)
    else:
        rendered = yaml.safe_dump(workflow.model_dump(mode="python", by_alias=True), sort_keys=False, allow_unicode=True)
    final_output_path.parent.mkdir(parents=True, exist_ok=True)
    final_output_path.write_text(rendered, encoding="utf-8")

    print(
        json.dumps(
            {
                "preset_id": resolved_preset,
                "registry_root": str(Path(registry_root)),
                "workflow_output": str(final_output_path),
                "summary": summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


async def scaffold_workflow_command(args) -> int:
    preset = args.pattern or args.preset
    if not preset and args.task_kind:
        preset = _recommend_preset_for_task_kind(args.task_kind)
    if not preset:
        preset = _select_preset_interactively()
    workflow_id = args.workflow_id or _slugify(_prompt_value("Workflow ID", preset))
    goal = args.goal or _prompt_value("任务目标", "Clarify the task and produce a strong result")
    provider = args.provider or _prompt_value("Provider", "claude")
    executor_kind = args.executor_kind or _prompt_value("Executor kind", "cli")
    output_path = args.output or str(_default_workflow_output(args.registry_root, workflow_id))
    return await init_workflow_command(
        args.registry_root,
        workflow_id,
        preset,
        goal,
        provider,
        executor_kind,
        args.task_kind,
        args.assignments_json,
        args.assignment_pairs,
        output_path,
        args.format,
        args.force,
    )


async def list_presets_command() -> int:
    print(json.dumps(list_builtin_presets(), ensure_ascii=False, indent=2))
    return 0


async def list_patterns_command() -> int:
    print(json.dumps(list_builtin_presets(), ensure_ascii=False, indent=2))
    return 0


async def list_role_presets_command() -> int:
    print(json.dumps(list_builtin_role_presets(), ensure_ascii=False, indent=2))
    return 0


async def list_agent_presets_command() -> int:
    print(json.dumps(list_builtin_agent_presets(), ensure_ascii=False, indent=2))
    return 0


async def chat_with_executor(args) -> int:
    runtime_service = _build_runtime_service(writeback_mode="markdown", writeback_root=args.root)
    session = runtime_service.create_chat_session(
        ChatSessionCreateRequest(
            title=args.title,
            executor=_build_executor_config_from_args(args),
            system_prompt=args.system_prompt,
            metadata={"source_system": "cli"},
        )
    )

    if args.message:
        turn = await runtime_service.send_chat_message(
            session.session.session_id,
            ChatMessageRequest(content=args.message, metadata={"source_system": "cli"}),
        )
        print(turn.model_dump_json(indent=2))
        return 0

    print(f"[Xueban chat] session={session.session.session_id} provider={args.provider}")
    print("输入 /exit 结束对话。")
    while True:
        try:
            user_input = input("you> ").strip()
        except EOFError:
            break
        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            break
        turn = await runtime_service.send_chat_message(
            session.session.session_id,
            ChatMessageRequest(content=user_input, metadata={"source_system": "cli"}),
        )
        print(f"assistant> {turn.response_text}")
    return 0


async def list_runs_command(root: str | None) -> int:
    runtime_service = _build_runtime_service(writeback_mode="markdown", writeback_root=root)
    print(json.dumps([item.model_dump(mode="json") for item in runtime_service.list_runs()], ensure_ascii=False, indent=2))
    return 0


async def get_run_command(run_id: str, root: str | None) -> int:
    runtime_service = _build_runtime_service(writeback_mode="markdown", writeback_root=root)
    result = runtime_service.get_run(run_id)
    if result is None:
        raise ValueError(f"run not found: {run_id}")
    print(result.model_dump_json(indent=2))
    return 0


async def get_run_graph_command(run_id: str, root: str | None) -> int:
    runtime_service = _build_runtime_service(writeback_mode="markdown", writeback_root=root)
    result = runtime_service.export_run_graph(run_id)
    if result is None:
        raise ValueError(f"run graph not found: {run_id}")
    print(result.model_dump_json(indent=2))
    return 0


async def get_checkpoint_command(checkpoint_id: str, root: str | None) -> int:
    runtime_service = _build_runtime_service(writeback_mode="markdown", writeback_root=root)
    checkpoint = runtime_service.get_checkpoint(checkpoint_id)
    if checkpoint is None:
        raise ValueError(f"checkpoint not found: {checkpoint_id}")
    print(checkpoint.model_dump_json(indent=2))
    return 0


async def resume_run_command(run_id: str, checkpoint_id: str, root: str | None, metadata: str | None) -> int:
    runtime_service = _build_runtime_service(writeback_mode="markdown", writeback_root=root)
    metadata_payload = _parse_input_payload(metadata) if metadata else {}
    result = await runtime_service.resume(run_id, ResumeRequest(checkpoint_id=checkpoint_id, metadata=metadata_payload))
    print(result.model_dump_json(indent=2))
    return 0


async def list_chat_sessions_command(root: str | None) -> int:
    runtime_service = _build_runtime_service(writeback_mode="markdown", writeback_root=root)
    print(json.dumps([item.model_dump(mode="json") for item in runtime_service.list_chat_sessions()], ensure_ascii=False, indent=2))
    return 0


async def get_chat_session_command(session_id: str, root: str | None) -> int:
    runtime_service = _build_runtime_service(writeback_mode="markdown", writeback_root=root)
    session = runtime_service.get_chat_session(session_id)
    if session is None:
        raise ValueError(f"chat session not found: {session_id}")
    print(session.model_dump_json(indent=2))
    return 0

def main():
    parser = argparse.ArgumentParser(description="Xueban CLI")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    validate_parser = subparsers.add_parser('validate', help='Validate a workflow contract')
    validate_parser.add_argument('-w', '--workflow', required=True, help='Workflow JSON/YAML file')

    run_parser = subparsers.add_parser('run', help='Run a workflow')
    run_parser.add_argument('workflow_file', nargs='?', default=None, help='Workflow JSON/YAML file (positional)')
    run_parser.add_argument('-w', '--workflow', default=None, help='Workflow JSON/YAML file (flag)')
    run_parser.add_argument('-i', '--input', default=None, help='Input text or JSON object')
    run_parser.add_argument('-u', '--user-id', default='default', help='User ID')
    run_parser.add_argument(
        '--writeback',
        choices=['reference', 'markdown'],
        default='reference',
        help='Writeback backend to use during runtime execution',
    )
    run_parser.add_argument(
        '--writeback-root',
        default=None,
        help='Root directory used when --writeback markdown is enabled',
    )
    run_parser.add_argument(
        '--store',
        choices=['file', 'memory'],
        default='file',
        help='Checkpoint store backend (default: file)',
    )

    graph_parser = subparsers.add_parser('graph', help='Export a workflow graph as JSON')
    graph_parser.add_argument('-w', '--workflow', required=True, help='Workflow JSON/YAML file')

    agentteams_parser = subparsers.add_parser(
        'export-agentteams',
        help='Export the AgentTeams executable code package (team CR + run.sh + README)',
    )
    agentteams_parser.add_argument(
        'workflow_file', nargs='?', default=None, help='Workflow JSON/YAML file (positional)'
    )
    agentteams_parser.add_argument(
        '-w', '--workflow', default=None, help='Workflow JSON/YAML file (flag)'
    )
    agentteams_parser.add_argument(
        '--out', default=None,
        help='Output: a directory (trailing / or existing dir) or .zip path; default .zip in CWD',
    )

    sfagent_parser = subparsers.add_parser(
        'export-sfagent',
        help='Export a .sfagent agent bundle (identity + skills + evidence, GOAI M9)',
    )
    sfagent_parser.add_argument(
        'agent_file', help='Agent spec JSON/YAML (id/name/role/capabilities/skills…)'
    )
    sfagent_parser.add_argument(
        '--out', default=None,
        help='Output: a directory (trailing /) or .zip path; default <agent_id>.sfagent.zip',
    )

    sfagent_import_parser = subparsers.add_parser(
        'import-sfagent',
        help='Import a .sfagent bundle into the agent store (.xueban/agents)',
    )
    sfagent_import_parser.add_argument('zip_file', help='Path to a .sfagent.zip bundle')

    plugin_parser = subparsers.add_parser(
        'validate-plugin',
        help='Validate a .sfplugin plugin package (manifest + declared specs)',
    )
    plugin_parser.add_argument('plugin_file', help='Path to a .sfplugin.zip package')

    compile_parser = subparsers.add_parser('compile', help='Compile a high-level template spec into a workflow')
    compile_parser.add_argument('-t', '--template', required=True, help='Template file path or template ID')
    compile_parser.add_argument('--registry-root', default=None, help='Registry root containing tools/skills/roles/agents/templates')
    compile_parser.add_argument('--parameters', default=None, help='Template parameters as JSON object')
    compile_parser.add_argument('--var', action='append', dest='var_pairs', default=None, help='Repeatable key=value template parameter')
    compile_parser.add_argument('--format', choices=['json', 'yaml'], default='json', help='Output format')
    compile_parser.add_argument('-o', '--output', default=None, help='Optional output file path')
    compile_parser.add_argument('--summary', choices=['none', 'json', 'text'], default='none', help='Optional compiled workflow summary output')

    registry_parser = subparsers.add_parser('registry', help='Inspect high-level spec registry')
    registry_subparsers = registry_parser.add_subparsers(dest='registry_command', help='Registry commands')
    registry_counts_parser = registry_subparsers.add_parser('counts', help='Show registry counts')
    registry_counts_parser.add_argument('--registry-root', required=True, help='Registry root containing spec directories')
    registry_list_parser = registry_subparsers.add_parser('list', help='List specs of a given kind')
    registry_list_parser.add_argument('--registry-root', required=True, help='Registry root containing spec directories')
    registry_list_parser.add_argument('--kind', required=True, choices=['tools', 'skills', 'roles', 'agents', 'templates', 'blocks', 'assemblies'], help='Registry kind')
    registry_get_parser = registry_subparsers.add_parser('get', help='Get a specific spec')
    registry_get_parser.add_argument('--registry-root', required=True, help='Registry root containing spec directories')
    registry_get_parser.add_argument('--kind', required=True, choices=['tools', 'skills', 'roles', 'agents', 'templates', 'blocks', 'assemblies'], help='Registry kind')
    registry_get_parser.add_argument('--id', required=True, dest='spec_id', help='Spec ID')
    registry_export_parser = registry_subparsers.add_parser('export', help='Export a registry or a single spec bundle')
    registry_export_parser.add_argument('--registry-root', required=True, help='Source registry root')
    registry_export_parser.add_argument('--output-root', required=True, help='Destination directory')
    registry_export_parser.add_argument('--kind', choices=['tools', 'skills', 'roles', 'agents', 'templates'], default=None, help='Optional single kind to export')
    registry_export_parser.add_argument('--id', dest='spec_id', default=None, help='Optional spec ID when exporting a single spec')
    registry_export_parser.add_argument('--force', action='store_true', help='Overwrite existing files')
    registry_import_parser = registry_subparsers.add_parser('import', help='Import a registry bundle or built-in preset')
    registry_import_parser.add_argument('--registry-root', required=True, help='Target registry root')
    registry_import_parser.add_argument('--source-root', default=None, help='Source registry root to import from')
    registry_import_parser.add_argument('--preset', choices=BUILTIN_PRESET_IDS, default=None, help='Import a built-in preset directly')
    registry_import_parser.add_argument('--workflow-id', default=None, help='Workflow/template id override used with --preset')
    registry_import_parser.add_argument('--provider', default='claude', help='Executor provider used with --preset')
    registry_import_parser.add_argument('--executor-kind', choices=['cli', 'api'], default='cli', help='Executor kind used with --preset')
    registry_import_parser.add_argument('--kind', choices=['tools', 'skills', 'roles', 'agents', 'templates'], default=None, help='Optional single kind to import')
    registry_import_parser.add_argument('--id', dest='spec_id', default=None, help='Optional spec ID when importing a single spec')
    registry_import_parser.add_argument('--force', action='store_true', help='Overwrite existing files')

    presets_parser = subparsers.add_parser('presets', help='Inspect built-in workflow presets')
    presets_subparsers = presets_parser.add_subparsers(dest='presets_command', help='Preset commands')
    presets_subparsers.add_parser('list', help='List built-in presets')
    patterns_parser = subparsers.add_parser('patterns', help='Inspect built-in workflow patterns')
    patterns_subparsers = patterns_parser.add_subparsers(dest='patterns_command', help='Pattern commands')
    patterns_subparsers.add_parser('list', help='List built-in workflow patterns')
    role_presets_parser = subparsers.add_parser('role-presets', help='Inspect built-in role archetypes')
    role_presets_subparsers = role_presets_parser.add_subparsers(dest='role_presets_command', help='Role preset commands')
    role_presets_subparsers.add_parser('list', help='List built-in role presets')
    agent_presets_parser = subparsers.add_parser('agent-presets', help='Inspect built-in agent archetypes')
    agent_presets_subparsers = agent_presets_parser.add_subparsers(dest='agent_presets_command', help='Agent preset commands')
    agent_presets_subparsers.add_parser('list', help='List built-in agent presets')

    init_parser = subparsers.add_parser('init', help='Scaffold high-level spec files')
    init_subparsers = init_parser.add_subparsers(dest='init_kind', help='Scaffold commands')
    init_tool_parser = init_subparsers.add_parser('tool', help='Scaffold a tool spec')
    init_tool_parser.add_argument('--registry-root', required=True, help='Registry root containing spec directories')
    init_tool_parser.add_argument('--id', required=True, dest='tool_id', help='Tool ID')
    init_tool_parser.add_argument('--kind', choices=['cli', 'mcp', 'api', 'builtin'], default='builtin', help='Tool kind')
    init_tool_parser.add_argument('-o', '--output', default=None, help='Optional output file path')
    init_tool_parser.add_argument('--force', action='store_true', help='Overwrite existing file')
    init_skill_parser = init_subparsers.add_parser('skill', help='Scaffold a skill spec')
    init_skill_parser.add_argument('--registry-root', required=True, help='Registry root containing spec directories')
    init_skill_parser.add_argument('--id', required=True, dest='skill_id', help='Skill ID')
    init_skill_parser.add_argument('-o', '--output', default=None, help='Optional output file path')
    init_skill_parser.add_argument('--force', action='store_true', help='Overwrite existing file')
    init_role_parser = init_subparsers.add_parser('role', help='Scaffold a role spec')
    init_role_parser.add_argument('--registry-root', required=True, help='Registry root containing spec directories')
    init_role_parser.add_argument('--id', required=True, dest='role_id', help='Role ID')
    init_role_parser.add_argument('--preset', choices=BUILTIN_ROLE_PRESET_IDS, default=None, help='Optional built-in role archetype')
    init_role_parser.add_argument('-o', '--output', default=None, help='Optional output file path')
    init_role_parser.add_argument('--force', action='store_true', help='Overwrite existing file')
    init_agent_parser = init_subparsers.add_parser('agent', help='Scaffold an agent spec')
    init_agent_parser.add_argument('--registry-root', required=True, help='Registry root containing spec directories')
    init_agent_parser.add_argument('--id', required=True, dest='agent_id', help='Agent ID')
    init_agent_parser.add_argument('--role', default=None, help='Role ID')
    init_agent_parser.add_argument('--preset', choices=BUILTIN_AGENT_PRESET_IDS, default=None, help='Optional built-in agent archetype')
    init_agent_parser.add_argument('--skill', action='append', dest='skills', default=None, help='Repeatable skill ID')
    init_agent_parser.add_argument('--tool', action='append', dest='tools', default=None, help='Repeatable tool ID')
    init_agent_parser.add_argument('--provider', default=None, help='Executor provider')
    init_agent_parser.add_argument('--executor-kind', choices=['cli', 'api'], default=None, help='Executor kind')
    init_agent_parser.add_argument('-o', '--output', default=None, help='Optional output file path')
    init_agent_parser.add_argument('--force', action='store_true', help='Overwrite existing file')
    init_template_parser = init_subparsers.add_parser('template', help='Scaffold a workflow template spec')
    init_template_parser.add_argument('--registry-root', required=True, help='Registry root containing spec directories')
    init_template_parser.add_argument('--id', required=True, dest='template_id', help='Template ID')
    init_template_parser.add_argument('--agent-ref', required=True, help='Referenced agent ID')
    init_template_parser.add_argument('--agent-node-id', default='agent', help='Template-local agent node ID')
    init_template_parser.add_argument('-o', '--output', default=None, help='Optional output file path')
    init_template_parser.add_argument('--force', action='store_true', help='Overwrite existing file')
    init_workflow_parser = init_subparsers.add_parser('workflow', help='Materialize a built-in preset and compile a workflow')
    init_workflow_parser.add_argument('--registry-root', required=True, help='Registry root containing spec directories')
    init_workflow_parser.add_argument('--id', required=True, dest='workflow_id', help='Workflow / template ID')
    init_workflow_parser.add_argument('--preset', choices=BUILTIN_PRESET_IDS, default=None, help='Built-in preset to materialize')
    init_workflow_parser.add_argument('--pattern', choices=BUILTIN_PRESET_IDS, default=None, help='Alias of --preset for pattern-driven workflow creation')
    init_workflow_parser.add_argument('--task-kind', choices=['review', 'analysis', 'build', 'code', 'delivery', 'research', 'publish', 'content'], default=None, help='Optional task intent used to recommend a pattern')
    init_workflow_parser.add_argument('--goal', required=True, help='Goal used for initial template compilation')
    init_workflow_parser.add_argument('--provider', default='claude', help='Executor provider')
    init_workflow_parser.add_argument('--executor-kind', choices=['cli', 'api'], default='cli', help='Executor kind')
    init_workflow_parser.add_argument('--assignments-json', default=None, help='JSON object mapping template agent ids to assignment objects')
    init_workflow_parser.add_argument('--assign', action='append', dest='assignment_pairs', default=None, help='Repeatable node.field=value assignment override')
    init_workflow_parser.add_argument('--format', choices=['json', 'yaml'], default='yaml', help='Compiled workflow output format')
    init_workflow_parser.add_argument('-o', '--output', default=None, help='Optional compiled workflow output path')
    init_workflow_parser.add_argument('--force', action='store_true', help='Overwrite existing files')

    scaffold_parser = subparsers.add_parser('scaffold', help='Guided workflow scaffold wizard')
    scaffold_parser.add_argument('--registry-root', required=True, help='Registry root containing spec directories')
    scaffold_parser.add_argument('--preset', choices=BUILTIN_PRESET_IDS, default=None, help='Optional preset; omit to choose interactively')
    scaffold_parser.add_argument('--pattern', choices=BUILTIN_PRESET_IDS, default=None, help='Alias of --preset for pattern-driven scaffolding')
    scaffold_parser.add_argument('--task-kind', choices=['review', 'analysis', 'build', 'code', 'delivery', 'research', 'publish', 'content'], default=None, help='Optional task intent used to recommend a pattern')
    scaffold_parser.add_argument('--workflow-id', default=None, help='Workflow / template ID')
    scaffold_parser.add_argument('--goal', default=None, help='Goal used for initial template compilation')
    scaffold_parser.add_argument('--provider', default=None, help='Executor provider')
    scaffold_parser.add_argument('--executor-kind', choices=['cli', 'api'], default=None, help='Executor kind')
    scaffold_parser.add_argument('--assignments-json', default=None, help='JSON object mapping template agent ids to assignment objects')
    scaffold_parser.add_argument('--assign', action='append', dest='assignment_pairs', default=None, help='Repeatable node.field=value assignment override')
    scaffold_parser.add_argument('--format', choices=['json', 'yaml'], default='yaml', help='Compiled workflow output format')
    scaffold_parser.add_argument('-o', '--output', default=None, help='Optional compiled workflow output path')
    scaffold_parser.add_argument('--force', action='store_true', help='Overwrite existing files')

    chat_parser = subparsers.add_parser('chat', help='Chat with a configured CLI/API executor')
    chat_parser.add_argument('--kind', choices=['cli', 'api'], required=True, help='Executor kind')
    chat_parser.add_argument('--provider', required=True, help='Executor provider, e.g. claude/codex/openai/anthropic/generic')
    chat_parser.add_argument('--model', default=None, help='Optional model override')
    chat_parser.add_argument('--system-prompt', default=None, help='Optional system prompt')
    chat_parser.add_argument('--message', default=None, help='Optional single-turn message; omit for interactive mode')
    chat_parser.add_argument('--title', default='Xueban Chat Session', help='Chat session title')
    chat_parser.add_argument('--parse', default='text', help='Parse mode for executor output')
    chat_parser.add_argument('--timeout-seconds', type=float, default=None, help='Optional executor timeout')
    chat_parser.add_argument('--command', dest='cli_command', default=None, help='Generic CLI command')
    chat_parser.add_argument('--args-json', default=None, help='JSON-encoded string list for generic CLI args')
    chat_parser.add_argument('--arg', action='append', dest='args_list', default=None, help='Repeatable generic CLI arg')
    chat_parser.add_argument('--stdin-mode', choices=['json', 'text', 'none'], default=None, help='Generic CLI stdin mode')
    chat_parser.add_argument('--cwd', default=None, help='Optional working directory for CLI executor')
    chat_parser.add_argument('--root', default=DEFAULT_RUNTIME_ROOT, help='Persistent runtime root for chat sessions')

    runs_parser = subparsers.add_parser('runs', help='Inspect persisted run records')
    runs_subparsers = runs_parser.add_subparsers(dest='runs_command', help='Run inspection commands')
    runs_list_parser = runs_subparsers.add_parser('list', help='List persisted runs')
    runs_list_parser.add_argument('--root', default=DEFAULT_RUNTIME_ROOT, help='Persistent runtime root')
    runs_get_parser = runs_subparsers.add_parser('get', help='Get a persisted run')
    runs_get_parser.add_argument('--run-id', required=True, help='Run ID')
    runs_get_parser.add_argument('--root', default=DEFAULT_RUNTIME_ROOT, help='Persistent runtime root')
    runs_graph_parser = runs_subparsers.add_parser('graph', help='Export a persisted run graph')
    runs_graph_parser.add_argument('--run-id', required=True, help='Run ID')
    runs_graph_parser.add_argument('--root', default=DEFAULT_RUNTIME_ROOT, help='Persistent runtime root')

    checkpoints_parser = subparsers.add_parser('checkpoints', help='Inspect persisted checkpoints')
    checkpoints_subparsers = checkpoints_parser.add_subparsers(dest='checkpoints_command', help='Checkpoint commands')
    checkpoints_get_parser = checkpoints_subparsers.add_parser('get', help='Get a checkpoint')
    checkpoints_get_parser.add_argument('--checkpoint-id', required=True, help='Checkpoint ID')
    checkpoints_get_parser.add_argument('--root', default=DEFAULT_RUNTIME_ROOT, help='Persistent runtime root')

    sessions_parser = subparsers.add_parser('sessions', help='Inspect persisted chat sessions')
    sessions_subparsers = sessions_parser.add_subparsers(dest='sessions_command', help='Session commands')
    sessions_list_parser = sessions_subparsers.add_parser('list', help='List chat sessions')
    sessions_list_parser.add_argument('--root', default=DEFAULT_RUNTIME_ROOT, help='Persistent runtime root')
    sessions_get_parser = sessions_subparsers.add_parser('get', help='Get a chat session')
    sessions_get_parser.add_argument('--session-id', required=True, help='Chat session ID')
    sessions_get_parser.add_argument('--root', default=DEFAULT_RUNTIME_ROOT, help='Persistent runtime root')

    resume_parser = subparsers.add_parser('resume', help='Resume a persisted run from a checkpoint')
    resume_parser.add_argument('--run-id', required=True, help='Original run ID')
    resume_parser.add_argument('--checkpoint-id', required=True, help='Checkpoint ID')
    resume_parser.add_argument('--root', default=DEFAULT_RUNTIME_ROOT, help='Persistent runtime root')
    resume_parser.add_argument('--metadata', default=None, help='Optional resume metadata as JSON or text')
    
    serve_parser = subparsers.add_parser('serve', help='Start HTTP server')
    serve_parser.add_argument('--port', type=int, default=8000, help='Server port')
    serve_parser.add_argument('--host', default='0.0.0.0', help='Server host')

    mcp_parser = subparsers.add_parser('mcp', help='Start MCP server (stdio mode by default)')
    mcp_parser.add_argument('--stdio', action='store_true', default=True, help='Run in stdio mode (default)')
    mcp_parser.add_argument('--http', action='store_true', default=False, help='Run HTTP debug server instead of stdio')
    mcp_parser.add_argument('--port', type=int, default=3002, help='HTTP debug server port (--http only)')
    mcp_parser.add_argument('--host', default='127.0.0.1', help='HTTP debug server host (--http only)')

    assemble_parser = subparsers.add_parser('assemble', help='Assemble a workflow from a goal using catalog-level activation')
    assemble_parser.add_argument('--goal', required=True, help='Natural-language goal description')
    assemble_parser.add_argument('--compile', action='store_true', default=False, help='Compile to a full WorkflowDefinition (requires --provider and --executor-kind)')
    assemble_parser.add_argument('--provider', default=None, help='Executor provider (e.g. claude, openai, ollama)')
    assemble_parser.add_argument('--executor-kind', choices=['cli', 'api'], default=None, dest='executor_kind', help='Executor kind')

    do_parser = subparsers.add_parser('do', help='One-shot: assemble + compile + execute a goal end-to-end')
    do_parser.add_argument('goal', help='Natural-language goal description')
    do_parser.add_argument('--provider', default='codex', help='Executor provider (default: codex)')
    do_parser.add_argument('--executor-kind', choices=['cli', 'api'], default='cli', dest='executor_kind', help='Executor kind')
    do_parser.add_argument('--cwd', default=None, help='Working directory for executor (e.g. path to target project)')
    do_parser.add_argument('--context', default=None, help='Extra context string to pass to executor')
    do_parser.add_argument('--writeback', choices=['reference', 'markdown'], default='reference')
    do_parser.add_argument('--export-training', action='store_true', default=False, dest='export_training', help='Export training samples after execution')

    args = parser.parse_args()
    
    if args.command == 'validate':
        sys.exit(asyncio.run(validate_workflow(args.workflow)))
    elif args.command == 'compile':
        sys.exit(
            asyncio.run(
                compile_template_command(
                    args.template,
                    args.registry_root,
                    args.parameters,
                    args.var_pairs,
                    args.output,
                    args.format,
                    args.summary,
                )
            )
        )
    elif args.command == 'run':
        workflow_path = args.workflow_file or args.workflow
        if not workflow_path:
            run_parser.error("workflow file required: use 'xueban run workflow.yaml' or '-w workflow.yaml'")
        if not args.input:
            run_parser.error("input required: use '-i \"your input\"'")
        sys.exit(
            asyncio.run(
                run_workflow(
                    workflow_path,
                    args.input,
                    args.user_id,
                    args.writeback,
                    args.writeback_root,
                    store=args.store,
                )
            )
        )
    elif args.command == 'graph':
        sys.exit(asyncio.run(export_workflow_graph(args.workflow)))
    elif args.command == 'export-agentteams':
        workflow_arg = args.workflow_file or args.workflow
        if not workflow_arg:
            parser.error('export-agentteams requires a workflow file (positional or -w)')
        sys.exit(export_agentteams_command(workflow_arg, args.out))
    elif args.command == 'export-sfagent':
        sys.exit(export_sfagent_command(args.agent_file, args.out))
    elif args.command == 'import-sfagent':
        sys.exit(import_sfagent_command(args.zip_file))
    elif args.command == 'validate-plugin':
        sys.exit(validate_plugin_command(args.plugin_file))
    elif args.command == 'registry':
        if args.registry_command == 'counts':
            sys.exit(asyncio.run(registry_counts_command(args.registry_root)))
        elif args.registry_command == 'list':
            sys.exit(asyncio.run(registry_list_command(args.registry_root, args.kind)))
        elif args.registry_command == 'get':
            sys.exit(asyncio.run(registry_get_command(args.registry_root, args.kind, args.spec_id)))
        elif args.registry_command == 'export':
            sys.exit(asyncio.run(registry_export_command(args.registry_root, args.output_root, args.kind, args.spec_id, args.force)))
        elif args.registry_command == 'import':
            sys.exit(
                asyncio.run(
                    registry_import_command(
                        args.registry_root,
                        args.source_root,
                        args.preset,
                        args.workflow_id,
                        args.provider,
                        args.executor_kind,
                        args.kind,
                        args.spec_id,
                        args.force,
                    )
                )
            )
        registry_parser.print_help()
    elif args.command == 'presets':
        if args.presets_command == 'list':
            sys.exit(asyncio.run(list_presets_command()))
        presets_parser.print_help()
    elif args.command == 'patterns':
        if args.patterns_command == 'list':
            sys.exit(asyncio.run(list_patterns_command()))
        patterns_parser.print_help()
    elif args.command == 'role-presets':
        if args.role_presets_command == 'list':
            sys.exit(asyncio.run(list_role_presets_command()))
        role_presets_parser.print_help()
    elif args.command == 'agent-presets':
        if args.agent_presets_command == 'list':
            sys.exit(asyncio.run(list_agent_presets_command()))
        agent_presets_parser.print_help()
    elif args.command == 'init':
        if args.init_kind == 'tool':
            sys.exit(asyncio.run(init_tool_command(args.registry_root, args.tool_id, args.kind, args.output, args.force)))
        elif args.init_kind == 'skill':
            sys.exit(asyncio.run(init_skill_command(args.registry_root, args.skill_id, args.output, args.force)))
        elif args.init_kind == 'role':
            if args.preset:
                sys.exit(asyncio.run(init_role_with_preset_command(args.registry_root, args.role_id, args.preset, args.output, args.force)))
            sys.exit(asyncio.run(init_role_command(args.registry_root, args.role_id, args.output, args.force)))
        elif args.init_kind == 'agent':
            sys.exit(
                asyncio.run(
                    init_agent_command(
                        args.registry_root,
                        args.agent_id,
                        args.role,
                        args.skills,
                        args.tools,
                        args.provider,
                        args.executor_kind,
                        args.preset,
                        args.output,
                        args.force,
                    )
                )
            )
        elif args.init_kind == 'template':
            sys.exit(
                asyncio.run(
                    init_template_command(
                        args.registry_root,
                        args.template_id,
                        args.agent_ref,
                        args.agent_node_id,
                        args.output,
                        args.force,
                    )
                )
            )
        elif args.init_kind == 'workflow':
            sys.exit(
                asyncio.run(
                    init_workflow_command(
                        args.registry_root,
                        args.workflow_id,
                        args.pattern or args.preset,
                        args.goal,
                        args.provider,
                        args.executor_kind,
                        args.task_kind,
                        args.assignments_json,
                        args.assignment_pairs,
                        args.output,
                        args.format,
                        args.force,
                    )
                )
            )
        init_parser.print_help()
    elif args.command == 'scaffold':
        sys.exit(asyncio.run(scaffold_workflow_command(args)))
    elif args.command == 'chat':
        sys.exit(asyncio.run(chat_with_executor(args)))
    elif args.command == 'runs':
        if args.runs_command == 'list':
            sys.exit(asyncio.run(list_runs_command(args.root)))
        elif args.runs_command == 'get':
            sys.exit(asyncio.run(get_run_command(args.run_id, args.root)))
        elif args.runs_command == 'graph':
            sys.exit(asyncio.run(get_run_graph_command(args.run_id, args.root)))
        runs_parser.print_help()
    elif args.command == 'checkpoints':
        if args.checkpoints_command == 'get':
            sys.exit(asyncio.run(get_checkpoint_command(args.checkpoint_id, args.root)))
        checkpoints_parser.print_help()
    elif args.command == 'sessions':
        if args.sessions_command == 'list':
            sys.exit(asyncio.run(list_chat_sessions_command(args.root)))
        elif args.sessions_command == 'get':
            sys.exit(asyncio.run(get_chat_session_command(args.session_id, args.root)))
        sessions_parser.print_help()
    elif args.command == 'resume':
        sys.exit(asyncio.run(resume_run_command(args.run_id, args.checkpoint_id, args.root, args.metadata)))
    elif args.command == 'serve':
        from xueban.server import app
        import uvicorn
        uvicorn.run(app, host=args.host, port=args.port)
    elif args.command == 'mcp':
        from xueban.mcp_server import main as mcp_main
        use_stdio = not args.http
        mcp_main(stdio=use_stdio, host=args.host, port=args.port)
    elif args.command == 'assemble':
        sys.exit(asyncio.run(assemble_goal_command(
            args.goal,
            compile=args.compile,
            provider=args.provider,
            executor_kind=args.executor_kind,
        )))
    elif args.command == 'do':
        sys.exit(asyncio.run(do_goal_command(
            args.goal,
            provider=args.provider,
            executor_kind=args.executor_kind,
            cwd=args.cwd,
            context=args.context,
            writeback=args.writeback,
            export_training=args.export_training,
        )))
    else:
        parser.print_help()

if __name__ == '__main__':
    main()
