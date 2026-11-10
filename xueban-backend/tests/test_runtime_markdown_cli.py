import asyncio
import fnmatch
import sys
from pathlib import Path

from xueban.cli import _build_runtime_service
from xueban.runtime import FileCheckpointStore, MarkdownWritebackAdapter, RuntimeRequest, RuntimeService, WorkflowDefinition


CLI_WORKFLOW_PAYLOAD = {
    "workflow_id": "cli-markdown-memory",
    "version": "0.1",
    "name": "CLI Markdown Memory",
    "entrypoint": "collector",
    "defaults": {
        "writeback": {
            "artifact": {"target": "memory", "mode": "inline"},
            "checkpoint": {"target": "memory", "mode": "reference"},
        }
    },
    "nodes": [
        {
            "id": "collector",
            "kind": "agent",
            "type": "tool.cli",
            "config": {
                "role": "collector",
                "executor": {
                    "kind": "cli",
                    "command": sys.executable,
                    "args": [
                        "-c",
                        (
                            "import json,sys;"
                            "payload=json.load(sys.stdin);"
                            "goal=payload['step_input'].get('goal','');"
                            "print(json.dumps({"
                            "'message':'[collector] captured cli result.',"
                            "'summary':f'CLI handled: {goal}',"
                            "'artifact':{'kind':'report','name':'memory-note.md','content':f'# Memory\\n\\n{goal}'}}))"
                        ),
                    ],
                    "stdin": "json",
                    "parse": "json",
                },
            },
        }
    ],
    "edges": [{"from": "collector", "to": "END", "type": "final"}],
}


def _install_fake_path_fs(monkeypatch):
    files = {}
    directories = set()

    def _normalize(path: Path) -> str:
        return str(path).replace("\\", "/")

    def fake_mkdir(self: Path, mode=0o777, parents=False, exist_ok=False):
        current = self
        parts = [current]
        while parents and current.parent != current:
            current = current.parent
            parts.append(current)
        for item in parts:
            directories.add(_normalize(item))

    def fake_write_text(self: Path, data: str, encoding=None):
        directories.add(_normalize(self.parent))
        files[_normalize(self)] = data
        return len(data)

    def fake_read_text(self: Path, encoding=None):
        return files[_normalize(self)]

    def fake_exists(self: Path):
        key = _normalize(self)
        return key in files or key in directories

    def fake_glob(self: Path, pattern: str):
        prefix = _normalize(self).rstrip("/") + "/"
        matches = []
        for key in files:
            if key.startswith(prefix):
                name = key[len(prefix):]
                if "/" not in name and fnmatch.fnmatch(name, pattern):
                    matches.append(Path(key))
        return matches

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    monkeypatch.setattr(Path, "write_text", fake_write_text)
    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "glob", fake_glob)
    return files, directories


def test_markdown_writeback_adapter_persists_memory_note_and_checkpoint(monkeypatch):
    files, _ = _install_fake_path_fs(monkeypatch)
    root = Path("virtual-runtime")
    checkpoint_store = FileCheckpointStore(root / "checkpoint-store")
    adapter = MarkdownWritebackAdapter(root, checkpoint_store=checkpoint_store)
    service = RuntimeService(writeback_adapter=adapter, checkpoint_store=checkpoint_store)

    result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=WorkflowDefinition.model_validate(CLI_WORKFLOW_PAYLOAD),
                input={"goal": "markdown-memory-bridge"},
                metadata={"source_system": "pytest-markdown"},
            )
        )
    )

    memory_note = root / "memory" / "memory-note.md"
    assert result.run.status == "succeeded"
    assert str(memory_note).replace("\\", "/") in files
    assert "markdown-memory-bridge" in files[str(memory_note).replace("\\", "/")]

    checkpoint_keys = [key for key in files if "/checkpoint-store/checkpoints/" in key]
    assert checkpoint_keys
    assert any('"target":"memory"' in files[key].replace(" ", "") for key in checkpoint_keys)
    assert str(root / "runs" / f"{result.run.run_id}.json").replace("\\", "/") in files


def test_cli_builds_markdown_runtime_service(monkeypatch):
    _install_fake_path_fs(monkeypatch)
    service = _build_runtime_service(writeback_mode="markdown", writeback_root="virtual-root")
    assert isinstance(service, RuntimeService)
    assert isinstance(service._writeback_adapter, MarkdownWritebackAdapter)
    assert isinstance(service._checkpoint_store, FileCheckpointStore)

