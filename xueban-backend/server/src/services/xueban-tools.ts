import path from 'node:path';
import { mkdirSync } from 'node:fs';
import { builtinExecutors, builtinTools } from '../lib/tools/builtin';
import type { ToolSpec } from '../lib/tool-spec';

/** XUEBAN 员工可调用的只读／生成工具；高风险写入与 shell 仍由专门审批流负责。 */
export const XUEBAN_TOOL_NAMES = [
  'web_search', 'fetch_url', 'read_file', 'list_dir', 'glob_files', 'grep',
  'github_request', 'im_read', 'image_generate',
] as const;

const ALIASES: Record<string, string> = {
  网页检索: 'web_search', 联网搜索: 'web_search', 搜索: 'web_search',
  网页读取: 'fetch_url', 文件读取: 'read_file', 目录读取: 'list_dir',
  文件查找: 'glob_files', 内容搜索: 'grep', GitHub: 'github_request',
  消息读取: 'im_read', 图片生成: 'image_generate',
};

export function canonicalXuebanToolName(value: string): string {
  const trimmed = value.trim();
  const withoutPrefix = trimmed.replace(/^builtin:/, '');
  return ALIASES[withoutPrefix] ?? withoutPrefix;
}

export function xuebanToolsForEmployee(permissions: string[] | undefined): ToolSpec[] {
  const allowed = new Set((permissions ?? []).map(canonicalXuebanToolName));
  const granted = builtinTools.filter(tool => allowed.has(tool.name) && (XUEBAN_TOOL_NAMES as readonly string[]).includes(tool.name));
  return granted;
}

export interface XuebanToolExecutionResult {
  output: unknown;
  isError: boolean;
}

export async function executeXuebanTool(input: {
  companyId: string;
  conversationId?: string;
  name: string;
  input: unknown;
  env?: Readonly<Record<string, string>>;
  signal?: AbortSignal;
}): Promise<XuebanToolExecutionResult> {
  const name = canonicalXuebanToolName(input.name);
  if (!(XUEBAN_TOOL_NAMES as readonly string[]).includes(name)) {
    return { output: { error: `工具 ${name} 未接入 XUEBAN 或需要单独审批，未执行` }, isError: true };
  }
  const executor = builtinExecutors[name];
  if (!executor) return { output: { error: `工具 ${name} 没有执行器，未执行` }, isError: true };
  const workspace = path.resolve(process.cwd(), '.xueban', 'xueban-workspaces', input.companyId);
  mkdirSync(workspace, { recursive: true });
  const result = await executor(input.input, {
    workspace,
    signal: input.signal ?? new AbortController().signal,
    env: input.env,
  });
  const output = result.output as { provider?: string; note?: string } | undefined;
  const stub = output?.provider === 'stub';
  return {
    output: stub ? { ...output, error: output.note ?? '工具服务未配置，未执行' } : result.output,
    isError: Boolean(result.isError || stub),
  };
}
