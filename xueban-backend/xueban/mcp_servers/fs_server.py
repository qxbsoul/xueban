"""xueban-fs MCP Server — 文件系统操作工具。

Story 11.2：暴露五个工具给 LLM Agent：
  read(path, offset?, limit?)           — 带行号的文件读取，支持分页
  write(path, content, overwrite?)      — 写入文件，默认冲突检测
  edit(path, old_string, new_string)    — 精确字符串替换（唯一匹配）
  glob(pattern, base_dir?)              — glob 模式匹配
  grep(pattern, path, context?)         — 正则搜索，带上下文

安全：所有路径必须在 SF_FS_ALLOW_ROOT（默认为启动目录）内。

启动方式：
  python -m xueban.mcp_servers.fs_server
"""
from __future__ import annotations

import asyncio
import glob as glob_module
import json
import os
import re
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

ALLOW_ROOT: Path = Path(os.getenv("SF_FS_ALLOW_ROOT", ".")).resolve()
MAX_FILE_BYTES: int = 128 * 1024  # 128 KB — 超出时分页提示
MAX_GLOB_RESULTS: int = 1000
MAX_GREP_RESULTS: int = 500

# ---------------------------------------------------------------------------
# Server 实例 & 工具定义
# ---------------------------------------------------------------------------

app = Server("xueban-fs")

TOOLS: list[types.Tool] = [
    types.Tool(
        name="read",
        description="读取文件内容（带行号），支持 offset/limit 分页；路径越界返回 permission_denied。",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于 ALLOW_ROOT 的文件路径"},
                "offset": {"type": "integer", "description": "起始行（0-indexed，默认 0）"},
                "limit": {"type": "integer", "description": "最多返回行数（默认全部）"},
            },
            "required": ["path"],
        },
    ),
    types.Tool(
        name="write",
        description="写入文件内容；文件已存在且 overwrite=false 时返回 conflict。",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目标路径（相对于 ALLOW_ROOT）"},
                "content": {"type": "string", "description": "写入内容"},
                "overwrite": {
                    "type": "boolean",
                    "description": "是否覆盖已有文件（默认 false）",
                    "default": False,
                },
            },
            "required": ["path", "content"],
        },
    ),
    types.Tool(
        name="edit",
        description="精确替换文件中唯一出现的 old_string；重复匹配返回 ambiguous，缺失返回 not_found。",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目标文件路径"},
                "old_string": {"type": "string", "description": "要替换的字符串（必须唯一）"},
                "new_string": {"type": "string", "description": "替换后的字符串"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    ),
    types.Tool(
        name="glob",
        description="按 glob 模式匹配文件路径，最多返回 1000 条。",
        inputSchema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 模式，如 **/*.py"},
                "base_dir": {
                    "type": "string",
                    "description": "搜索基准目录（相对 ALLOW_ROOT，默认根目录）",
                },
            },
            "required": ["pattern"],
        },
    ),
    types.Tool(
        name="grep",
        description="在文件或目录中搜索正则模式，返回带上下文的匹配列表，最多 500 条。",
        inputSchema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python 正则表达式"},
                "path": {"type": "string", "description": "搜索的文件或目录路径"},
                "context": {
                    "type": "integer",
                    "description": "每条匹配的上下文行数（默认 3）",
                    "default": 3,
                },
            },
            "required": ["pattern", "path"],
        },
    ),
]


# ---------------------------------------------------------------------------
# MCP 协议处理器
# ---------------------------------------------------------------------------


@app.list_tools()
async def _handle_list_tools() -> list[types.Tool]:
    return TOOLS


@app.call_tool()
async def _handle_call_tool(
    name: str, arguments: dict[str, Any]
) -> list[types.TextContent]:
    dispatch = {
        "read": _read,
        "write": _write,
        "edit": _edit,
        "glob": _glob,
        "grep": _grep,
    }
    handler = dispatch.get(name)
    if handler is None:
        result: dict[str, Any] = {
            "status": "error",
            "error": f"Unknown tool: {name}",
        }
    else:
        result = await handler(arguments)
    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------


def _resolve_path(path: str) -> Path:
    """解析路径并校验不越界；越界抛 PermissionError。"""
    resolved = (ALLOW_ROOT / path).resolve()
    try:
        resolved.relative_to(ALLOW_ROOT)
    except ValueError:
        raise PermissionError(f"Path {path!r} is outside allow root {ALLOW_ROOT}")
    return resolved


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


async def _read(args: dict[str, Any]) -> dict[str, Any]:
    """AC2：带行号的文件读取，128KB 截断，支持 offset/limit 分页。"""
    try:
        p = _resolve_path(args["path"])
    except PermissionError as exc:
        return {"status": "permission_denied", "error": str(exc)}

    if not p.exists():
        return {"status": "not_found", "error": f"{args['path']!r} not found"}
    if p.is_dir():
        return {"status": "error", "error": f"{args['path']!r} is a directory"}

    size = p.stat().st_size
    offset: int = args.get("offset", 0)
    limit: int | None = args.get("limit", None)

    # 大文件 & 未分页：先读字节上限再截断，避免整文件 OOM
    if size > MAX_FILE_BYTES and limit is None and offset == 0:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            partial_text = fh.read(MAX_FILE_BYTES)
        lines = partial_text.splitlines()
        numbered = "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(lines))
        return {
            "status": "truncated",
            "content": numbered,
            "total_lines": len(lines),
            "hint": "File exceeds 128KB. Use offset/limit to paginate.",
        }

    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    total_lines = len(lines)
    selected = lines[offset : offset + limit if limit is not None else None]
    numbered = "\n".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(selected))
    return {
        "status": "success",
        "content": numbered,
        "total_lines": total_lines,
    }


async def _write(args: dict[str, Any]) -> dict[str, Any]:
    """AC3：写入文件；冲突检测；自动创建父目录。"""
    try:
        p = _resolve_path(args["path"])
    except PermissionError as exc:
        return {"status": "permission_denied", "error": str(exc)}

    overwrite: bool = args.get("overwrite", False)
    if p.exists() and not overwrite:
        return {
            "status": "conflict",
            "error": f"{args['path']!r} already exists. Use overwrite=true or edit().",
        }

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"], encoding="utf-8")
    return {"status": "success", "path": args["path"], "bytes_written": len(args["content"].encode())}


async def _edit(args: dict[str, Any]) -> dict[str, Any]:
    """AC3：精确字符串替换（必须唯一匹配）。"""
    try:
        p = _resolve_path(args["path"])
    except PermissionError as exc:
        return {"status": "permission_denied", "error": str(exc)}

    if not p.exists():
        return {"status": "not_found", "error": f"{args['path']!r} not found"}

    old_string: str = args["old_string"]
    new_string: str = args["new_string"]
    text = p.read_text(encoding="utf-8", errors="replace")

    count = text.count(old_string)
    if count == 0:
        return {"status": "not_found", "error": "old_string not found in file"}
    if count > 1:
        # 找出所有匹配的起始行号（支持多行 old_string）
        pos = 0
        match_start_lines: list[int] = []
        while True:
            idx = text.find(old_string, pos)
            if idx == -1:
                break
            match_start_lines.append(text[:idx].count("\n") + 1)
            pos = idx + 1
        return {
            "status": "ambiguous",
            "error": f"old_string matches {count} times; provide more context",
            "matches": match_start_lines,
        }

    new_text = text.replace(old_string, new_string, 1)
    p.write_text(new_text, encoding="utf-8")

    # 找出变更的行号范围
    new_lines = new_text.splitlines()
    changed = [i + 1 for i, (a, b) in enumerate(zip(text.splitlines(), new_lines)) if a != b]

    return {
        "status": "success",
        "path": args["path"],
        "changed_lines": changed,
    }


async def _glob(args: dict[str, Any]) -> dict[str, Any]:
    """AC4：glob 模式匹配，上限 MAX_GLOB_RESULTS 条。"""
    pattern: str = args["pattern"]
    base_dir_rel: str = args.get("base_dir", "")

    # 拒绝绝对路径 pattern（防止沙箱逃逸：Path(base) / "/abs" == Path("/abs")）
    if Path(pattern).is_absolute():
        return {"status": "permission_denied", "error": "Absolute glob patterns are not allowed"}

    try:
        base = _resolve_path(base_dir_rel) if base_dir_rel else ALLOW_ROOT
    except PermissionError as exc:
        return {"status": "permission_denied", "error": str(exc)}

    raw = glob_module.glob(str(base / pattern), recursive=True)
    # 转为相对 ALLOW_ROOT 的路径，并过滤沙箱外结果（符号链接指向外部的情况）
    rel_paths = []
    for f in raw:
        try:
            resolved_f = Path(f).resolve()
            resolved_f.relative_to(ALLOW_ROOT)  # ValueError if outside sandbox
            rel_paths.append(str(Path(f).relative_to(ALLOW_ROOT)))
        except (ValueError, OSError):
            continue
    truncated = len(rel_paths) > MAX_GLOB_RESULTS
    return {
        "status": "success",
        "files": rel_paths[:MAX_GLOB_RESULTS],
        "count": len(rel_paths[:MAX_GLOB_RESULTS]),
        "truncated": truncated,
    }


async def _grep(args: dict[str, Any]) -> dict[str, Any]:
    """AC4：正则搜索，返回带上下文的匹配列表，上限 MAX_GREP_RESULTS 条。"""
    pattern: str = args["pattern"]
    search_path: str = args["path"]
    context_lines: int = args.get("context", 3)

    try:
        p = _resolve_path(search_path)
    except PermissionError as exc:
        return {"status": "permission_denied", "error": str(exc)}

    # 收集要搜索的文件列表
    if p.is_file():
        files = [p]
    elif p.is_dir():
        files = [f for f in p.rglob("*") if f.is_file()]
    else:
        return {"status": "not_found", "error": f"{search_path!r} not found"}

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return {"status": "error", "error": f"Invalid regex: {exc}"}

    matches: list[dict[str, Any]] = []
    for filepath in files:
        try:
            lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for i, line in enumerate(lines):
            if regex.search(line):
                before = lines[max(0, i - context_lines) : i]
                after = lines[i + 1 : i + 1 + context_lines]
                matches.append(
                    {
                        "file": str(filepath.relative_to(ALLOW_ROOT)),
                        "line_number": i + 1,
                        "line_content": line,
                        "context_before": "\n".join(before),
                        "context_after": "\n".join(after),
                    }
                )
                if len(matches) >= MAX_GREP_RESULTS:
                    return {
                        "status": "success",
                        "matches": matches,
                        "truncated": True,
                    }

    return {"status": "success", "matches": matches, "truncated": False}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
