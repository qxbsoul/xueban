"""Story 11.2 — xueban-fs MCP Server 测试。

覆盖范围：
- AC1  工具列表（read / write / edit / glob / grep）
- AC2  read() 文件读取与分页，路径安全
- AC3  write() 写入 / edit() 精确替换
- AC4  glob() / grep() 搜索
- AC5  McpClient 集成测试（完整流程）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# AC1 — 工具列表
# ---------------------------------------------------------------------------


class TestFsToolList:
    def test_server_exposes_five_tools(self):
        from xueban.mcp_servers import fs_server

        names = {t.name for t in fs_server.TOOLS}
        assert names == {"read", "write", "edit", "glob", "grep"}

    def test_all_tools_have_input_schema(self):
        from xueban.mcp_servers import fs_server

        for tool in fs_server.TOOLS:
            assert tool.inputSchema, f"{tool.name} missing inputSchema"

    def test_read_path_required(self):
        from xueban.mcp_servers import fs_server

        read_tool = next(t for t in fs_server.TOOLS if t.name == "read")
        assert "path" in read_tool.inputSchema.get("required", [])

    def test_write_path_content_required(self):
        from xueban.mcp_servers import fs_server

        write_tool = next(t for t in fs_server.TOOLS if t.name == "write")
        required = write_tool.inputSchema.get("required", [])
        assert "path" in required
        assert "content" in required

    def test_edit_path_old_new_required(self):
        from xueban.mcp_servers import fs_server

        edit_tool = next(t for t in fs_server.TOOLS if t.name == "edit")
        required = edit_tool.inputSchema.get("required", [])
        assert "path" in required
        assert "old_string" in required
        assert "new_string" in required


# ---------------------------------------------------------------------------
# Fixtures — 临时目录作为 ALLOW_ROOT
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_root(tmp_path: Path):
    """为每个测试提供隔离的 ALLOW_ROOT 临时目录。"""
    with patch.dict(os.environ, {"SF_FS_ALLOW_ROOT": str(tmp_path)}):
        # 重新加载 fs_server 使 ALLOW_ROOT 生效
        import importlib
        from xueban.mcp_servers import fs_server
        importlib.reload(fs_server)
        yield tmp_path, fs_server
    # 测试结束后恢复原始模块
    from xueban.mcp_servers import fs_server as _fs
    importlib.reload(_fs)


# ---------------------------------------------------------------------------
# AC2 — read() 文件读取
# ---------------------------------------------------------------------------


class TestReadTool:
    @pytest.mark.asyncio
    async def test_read_success_with_line_numbers(self, tmp_root):
        tmp_path, fs_server = tmp_root
        (tmp_path / "hello.txt").write_text("line one\nline two\nline three\n")

        result = await fs_server._read({"path": "hello.txt"})
        assert result["status"] == "success"
        assert "1\tline one" in result["content"]
        assert "2\tline two" in result["content"]
        assert "3\tline three" in result["content"]

    @pytest.mark.asyncio
    async def test_read_not_found(self, tmp_root):
        _, fs_server = tmp_root
        result = await fs_server._read({"path": "nonexistent.txt"})
        assert result["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_read_path_traversal_blocked(self, tmp_root):
        _, fs_server = tmp_root
        result = await fs_server._read({"path": "../../etc/passwd"})
        assert result["status"] == "permission_denied"

    @pytest.mark.asyncio
    async def test_read_with_offset_and_limit(self, tmp_root):
        tmp_path, fs_server = tmp_root
        (tmp_path / "data.txt").write_text("a\nb\nc\nd\ne\n")

        result = await fs_server._read({"path": "data.txt", "offset": 1, "limit": 2})
        assert result["status"] == "success"
        # Lines 2-3 (0-indexed offset 1 → lines b, c)
        assert "2\tb" in result["content"]
        assert "3\tc" in result["content"]
        assert "a" not in result["content"]
        assert "d" not in result["content"]

    @pytest.mark.asyncio
    async def test_read_large_file_truncated(self, tmp_root):
        tmp_path, fs_server = tmp_root
        big = "x" * 135_000  # > 128KB (128*1024 = 131072)
        (tmp_path / "big.txt").write_text(big)

        result = await fs_server._read({"path": "big.txt"})
        assert result["status"] == "truncated"
        assert "paginate" in result.get("hint", "").lower() or "offset" in result.get("hint", "")


# ---------------------------------------------------------------------------
# AC3 — write() / edit()
# ---------------------------------------------------------------------------


class TestWriteTool:
    @pytest.mark.asyncio
    async def test_write_new_file_success(self, tmp_root):
        tmp_path, fs_server = tmp_root
        result = await fs_server._write({"path": "new.txt", "content": "hello world"})
        assert result["status"] == "success"
        assert (tmp_path / "new.txt").read_text() == "hello world"

    @pytest.mark.asyncio
    async def test_write_existing_file_conflict(self, tmp_root):
        tmp_path, fs_server = tmp_root
        (tmp_path / "existing.txt").write_text("old content")

        result = await fs_server._write({"path": "existing.txt", "content": "new"})
        assert result["status"] == "conflict"

    @pytest.mark.asyncio
    async def test_write_overwrite_flag(self, tmp_root):
        tmp_path, fs_server = tmp_root
        (tmp_path / "existing.txt").write_text("old")

        result = await fs_server._write(
            {"path": "existing.txt", "content": "new", "overwrite": True}
        )
        assert result["status"] == "success"
        assert (tmp_path / "existing.txt").read_text() == "new"

    @pytest.mark.asyncio
    async def test_write_creates_parent_dirs(self, tmp_root):
        tmp_path, fs_server = tmp_root
        result = await fs_server._write({"path": "sub/dir/file.txt", "content": "hi"})
        assert result["status"] == "success"
        assert (tmp_path / "sub" / "dir" / "file.txt").exists()

    @pytest.mark.asyncio
    async def test_write_path_traversal_blocked(self, tmp_root):
        _, fs_server = tmp_root
        result = await fs_server._write({"path": "../../evil.txt", "content": "x"})
        assert result["status"] == "permission_denied"


class TestEditTool:
    @pytest.mark.asyncio
    async def test_edit_success_unique_match(self, tmp_root):
        tmp_path, fs_server = tmp_root
        (tmp_path / "code.py").write_text("def hello():\n    pass\n")

        result = await fs_server._edit(
            {"path": "code.py", "old_string": "def hello():", "new_string": "def greet():"}
        )
        assert result["status"] == "success"
        assert (tmp_path / "code.py").read_text() == "def greet():\n    pass\n"

    @pytest.mark.asyncio
    async def test_edit_not_found(self, tmp_root):
        tmp_path, fs_server = tmp_root
        (tmp_path / "code.py").write_text("def hello():\n    pass\n")

        result = await fs_server._edit(
            {"path": "code.py", "old_string": "def missing():", "new_string": "x"}
        )
        assert result["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_edit_ambiguous_multiple_matches(self, tmp_root):
        tmp_path, fs_server = tmp_root
        (tmp_path / "code.py").write_text("foo\nfoo\nbar\n")

        result = await fs_server._edit(
            {"path": "code.py", "old_string": "foo", "new_string": "baz"}
        )
        assert result["status"] == "ambiguous"
        assert "matches" in result

    @pytest.mark.asyncio
    async def test_edit_path_traversal_blocked(self, tmp_root):
        _, fs_server = tmp_root
        result = await fs_server._edit(
            {"path": "../../evil.py", "old_string": "x", "new_string": "y"}
        )
        assert result["status"] == "permission_denied"


# ---------------------------------------------------------------------------
# AC4 — glob() / grep()
# ---------------------------------------------------------------------------


class TestGlobTool:
    @pytest.mark.asyncio
    async def test_glob_finds_files(self, tmp_root):
        tmp_path, fs_server = tmp_root
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("y")
        (tmp_path / "c.txt").write_text("z")

        result = await fs_server._glob({"pattern": "**/*.py"})
        assert result["status"] == "success"
        files = result["files"]
        assert any("a.py" in f for f in files)
        assert any("b.py" in f for f in files)
        assert all("c.txt" not in f for f in files)

    @pytest.mark.asyncio
    async def test_glob_truncated_at_limit(self, tmp_root):
        tmp_path, fs_server = tmp_root
        # Create more files than MAX_GLOB_RESULTS
        for i in range(10):
            (tmp_path / f"f{i}.txt").write_text("")

        old_max = fs_server.MAX_GLOB_RESULTS
        fs_server.MAX_GLOB_RESULTS = 5
        try:
            result = await fs_server._glob({"pattern": "**/*.txt"})
        finally:
            fs_server.MAX_GLOB_RESULTS = old_max

        assert result.get("truncated") is True
        assert len(result["files"]) == 5


class TestGrepTool:
    @pytest.mark.asyncio
    async def test_grep_finds_matches(self, tmp_root):
        tmp_path, fs_server = tmp_root
        (tmp_path / "code.py").write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n")

        result = await fs_server._grep({"pattern": "def ", "path": "code.py"})
        assert result["status"] == "success"
        assert len(result["matches"]) == 2
        assert result["matches"][0]["file"].endswith("code.py")

    @pytest.mark.asyncio
    async def test_grep_with_context(self, tmp_root):
        tmp_path, fs_server = tmp_root
        (tmp_path / "code.py").write_text("line1\ntarget\nline3\n")

        result = await fs_server._grep({"pattern": "target", "path": "code.py", "context": 1})
        assert result["status"] == "success"
        match = result["matches"][0]
        assert "line1" in match.get("context_before", "")
        assert "line3" in match.get("context_after", "")

    @pytest.mark.asyncio
    async def test_grep_no_matches(self, tmp_root):
        tmp_path, fs_server = tmp_root
        (tmp_path / "code.py").write_text("nothing here\n")

        result = await fs_server._grep({"pattern": "xyz_not_present", "path": "code.py"})
        assert result["status"] == "success"
        assert result["matches"] == []


# ---------------------------------------------------------------------------
# AC5 — McpClient 集成测试
# ---------------------------------------------------------------------------


class TestFsMcpClientIntegration:
    @pytest.mark.asyncio
    async def test_full_flow_write_read_edit_grep_glob(self, tmp_path):
        """
        完整流程：write → read → edit → grep → glob，全程通过 McpClient 调用。
        使用 tmp_path 作为 SF_FS_ALLOW_ROOT，确保服务端和客户端在相同根目录下操作。
        """
        from xueban.runtime.mcp.client import McpClient
        from xueban.runtime.mcp.transport import McpTransportConfig

        env_cmd = [
            sys.executable, "-c",
            f"import os; os.environ['SF_FS_ALLOW_ROOT']=r'{tmp_path}'; "
            f"import asyncio; "
            f"from xueban.mcp_servers.fs_server import main; "
            f"asyncio.run(main())",
        ]
        cfg = McpTransportConfig(kind="stdio", command=env_cmd)
        client = McpClient(cfg)

        try:
            await client.connect()

            # write
            r = await client.call_tool("write", {"path": "test.txt", "content": "hello world"})
            res = json.loads(r.content[0].text)
            assert res["status"] == "success", f"write failed: {res}"

            # read
            r = await client.call_tool("read", {"path": "test.txt"})
            res = json.loads(r.content[0].text)
            assert res["status"] == "success"
            assert "hello world" in res["content"]

            # edit
            r = await client.call_tool(
                "edit", {"path": "test.txt", "old_string": "hello", "new_string": "goodbye"}
            )
            res = json.loads(r.content[0].text)
            assert res["status"] == "success", f"edit failed: {res}"

            # grep
            r = await client.call_tool("grep", {"pattern": "goodbye", "path": "test.txt"})
            res = json.loads(r.content[0].text)
            assert res["status"] == "success"
            assert len(res["matches"]) >= 1

            # glob
            r = await client.call_tool("glob", {"pattern": "*.txt"})
            res = json.loads(r.content[0].text)
            assert res["status"] == "success"
            assert any("test.txt" in f for f in res["files"])

        finally:
            await client.close()
