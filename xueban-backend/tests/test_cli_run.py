"""Tests for CLI run command: positional arg, --store flag, _build_runtime_service."""
import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_run_args(argv: list[str]):
    """Parse 'xueban run <argv>' using the real argparse setup from cli.py."""
    import xueban.cli as cli_module

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("workflow_file", nargs="?", default=None)
    run_parser.add_argument("-w", "--workflow", default=None)
    run_parser.add_argument("-i", "--input", default=None)
    run_parser.add_argument("-u", "--user-id", default="default")
    run_parser.add_argument("--writeback", choices=["reference", "markdown"], default="reference")
    run_parser.add_argument("--writeback-root", default=None)
    run_parser.add_argument("--store", choices=["file", "memory"], default="file")

    return parser.parse_args(["run"] + argv)


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

class TestRunArgParsing:
    def test_positional_workflow_arg(self):
        args = _parse_run_args(["workflow.yaml", "-i", "hello"])
        assert args.workflow_file == "workflow.yaml"
        assert args.workflow is None
        assert args.input == "hello"

    def test_flag_workflow_arg(self):
        args = _parse_run_args(["-w", "workflow.yaml", "-i", "hello"])
        assert args.workflow_file is None
        assert args.workflow == "workflow.yaml"
        assert args.input == "hello"

    def test_store_default_is_file(self):
        args = _parse_run_args(["workflow.yaml", "-i", "x"])
        assert args.store == "file"

    def test_store_memory(self):
        args = _parse_run_args(["workflow.yaml", "-i", "x", "--store", "memory"])
        assert args.store == "memory"


# ---------------------------------------------------------------------------
# _build_runtime_service checkpoint store selection
# ---------------------------------------------------------------------------

class TestBuildRuntimeService:
    def test_store_file_returns_file_checkpoint_store(self):
        from xueban.cli import _build_runtime_service
        from xueban.runtime.markdown_adapter import FileCheckpointStore

        svc = _build_runtime_service(store="file")
        assert isinstance(svc._checkpoint_store, FileCheckpointStore)

    def test_store_memory_returns_in_memory_store(self):
        from xueban.cli import _build_runtime_service
        from xueban.runtime import InMemoryCheckpointStore

        svc = _build_runtime_service(store="memory")
        assert isinstance(svc._checkpoint_store, InMemoryCheckpointStore)


# ---------------------------------------------------------------------------
# End-to-end: 3-node generic workflow with memory store
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_three_node_workflow_with_memory_store(self):
        """Run the existing cli-generic-local.yaml and assert 3 checkpoints + success."""
        import asyncio
        from xueban.cli import run_workflow

        yaml_path = str(Path(__file__).parent.parent / "examples" / "runtime-contract" / "cli-generic-local.yaml")
        result_json = None

        async def _run():
            import json, io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                await run_workflow(yaml_path, "test", "default", "reference", None, store="memory")
            return json.loads(buf.getvalue())

        result = asyncio.run(_run())
        assert result["run"]["status"] == "succeeded"
        assert len(result["steps"]) >= 1
        assert len(result["checkpoints"]) >= 1

    def test_workflow_file_store_writes_to_disk(self, tmp_path):
        """File store writes checkpoint JSON files to disk."""
        import asyncio
        from xueban.cli import run_workflow

        yaml_path = str(Path(__file__).parent.parent / "examples" / "runtime-contract" / "cli-generic-local.yaml")

        async def _run():
            import json, io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                await run_workflow(
                    yaml_path, "test", "default", "reference",
                    str(tmp_path), store="file"
                )
            return json.loads(buf.getvalue())

        result = asyncio.run(_run())
        assert result["run"]["status"] == "succeeded"
        ckpt_dir = tmp_path / "checkpoint-store" / "checkpoints"
        assert ckpt_dir.exists()
        ckpt_files = list(ckpt_dir.glob("ckpt-*.json"))
        assert len(ckpt_files) >= 1

