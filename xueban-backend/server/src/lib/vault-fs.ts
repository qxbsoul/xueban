/**
 * vault-fs.ts — 本地仓库（vault）文件扫描 + 安全读取，2026-08-19。
 *
 * 移植自 Shadow（Tauri 版）的 knowledge_base.rs index_all / vault_registry.rs
 * collect_vault_files，并复用 fs-browse.ts 已有的越权守卫形状：
 *
 *   walkVault(root)
 *     递归扫描（迭代栈，不跟随符号链接），跳过 dot 目录（.git / .obsidian /
 *     .shadow…）、node_modules、__pycache__，dot 文件与 Thumbs.db /
 *     desktop.ini 也不收；只收知识型文本扩展 + pdf + 图片。返回扁平相对路径
 *     列表（'/' 分隔 —— 前端据此自建文件树，同 Shadow Explorer.buildTree）。
 *     带 files / visited 双上限：巨树截断标 truncated，不拖死请求。
 *
 *   readVaultTextFile(root, rel)
 *     相对路径 → path.resolve + isInside + realpath 三重检查（fs-browse /read
 *     同款，防 ../ 穿越与 symlink 越权），只读白名单文本扩展，1MB 截断。
 *     pdf / 图片不走这里 —— 前端用 /api/fs/raw?root=<vault> 直链预览。
 */

import { readdirSync, readFileSync, realpathSync, statSync } from 'node:fs';
import path from 'node:path';

/** 知识型文本扩展（Shadow SUPPORTED_EXTENSIONS 的文本子集）。 */
const VAULT_TEXT_EXTENSIONS = new Set([
  'md', 'markdown', 'txt', 'json', 'yaml', 'yml', 'toml', 'csv',
  'xml', 'rst', 'org', 'tex', 'log', 'html', 'htm',
]);

/** 图片扩展：走 /api/fs/raw 直链预览（<img src>），不做文本读取。 */
const VAULT_IMAGE_EXTENSIONS = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'avif', 'ico',
]);

const VAULT_PDF_EXTENSIONS = new Set(['pdf']);

/** 目录级忽略：dot 目录（.git/.obsidian/.shadow…）之外的显式名单。 */
const VAULT_IGNORED_DIRS = new Set(['node_modules', '__pycache__']);

/** 文件级忽略（不带 dot 前缀的系统垃圾文件）。 */
const VAULT_IGNORED_FILES = new Set(['Thumbs.db', 'desktop.ini']);

/** 默认上限：防止把巨型 monorepo 扫进一个 HTTP 响应里。 */
const MAX_WALK_FILES = 20_000;
const MAX_WALK_VISITED = 200_000;
const MAX_READ_BYTES = 1024 * 1024; // 1MB（对齐 fs-browse /read）

export type VaultFileType = 'doc' | 'pdf' | 'image';

export interface VaultFileRecord {
  /** 文件名（不含目录）。 */
  name: string;
  /** 相对仓库根的路径，一律 '/' 分隔（Windows 也转 '/'，前端按 '/' 切树）。 */
  path: string;
  type: VaultFileType;
  size: number;
  /** mtime，epoch 毫秒。 */
  mtime: number;
}

export interface WalkVaultOptions {
  maxFiles?: number;
  maxVisited?: number;
}

export interface WalkVaultResult {
  files: VaultFileRecord[];
  truncated: boolean;
}

export function vaultFileTypeOf(ext: string): VaultFileType | null {
  const e = ext.toLowerCase();
  if (VAULT_TEXT_EXTENSIONS.has(e)) return 'doc';
  if (VAULT_IMAGE_EXTENSIONS.has(e)) return 'image';
  if (VAULT_PDF_EXTENSIONS.has(e)) return 'pdf';
  return null;
}

export function isVaultTextExtension(ext: string): boolean {
  return VAULT_TEXT_EXTENSIONS.has(ext.toLowerCase());
}

/**
 * 递归扫描 vault 目录。迭代 DFS（显式栈）避免深目录递归溢出；
 * readdirSync({withFileTypes}) 拿 Dirent，符号链接一律不跟随（防环，
 * 对齐 Shadow walkdir 默认行为）。
 */
export function walkVault(root: string, opts: WalkVaultOptions = {}): WalkVaultResult {
  const maxFiles = opts.maxFiles ?? MAX_WALK_FILES;
  const maxVisited = opts.maxVisited ?? MAX_WALK_VISITED;
  const files: VaultFileRecord[] = [];
  let visited = 0;
  let truncated = false;

  // 栈元素带相对路径前缀，省去逐层 path.relative 的开销。
  const stack: Array<{ abs: string; rel: string }> = [{ abs: root, rel: '' }];
  while (stack.length > 0) {
    if (visited >= maxVisited) {
      truncated = true;
      break;
    }
    const { abs, rel } = stack.pop()!;
    let entries;
    try {
      entries = readdirSync(abs, { withFileTypes: true });
    } catch {
      continue; // 无权限 / 已被并发删除 —— 跳过该目录
    }
    visited += 1;
    for (const ent of entries) {
      const name = ent.name;
      if (ent.isDirectory()) {
        if (name.startsWith('.') || VAULT_IGNORED_DIRS.has(name)) continue;
        stack.push({ abs: path.join(abs, name), rel: rel ? `${rel}/${name}` : name });
        continue;
      }
      // 符号链接 / 其他特殊类型（FIFO…）不跟随不收录。
      if (!ent.isFile()) continue;
      if (name.startsWith('.') || VAULT_IGNORED_FILES.has(name)) continue;
      const ext = path.extname(name).slice(1);
      const type = vaultFileTypeOf(ext);
      if (type === null) continue;
      if (files.length >= maxFiles) {
        truncated = true;
        break;
      }
      try {
        const st = statSync(path.join(abs, name));
        files.push({
          name,
          path: rel ? `${rel}/${name}` : name,
          type,
          size: st.size,
          mtime: st.mtimeMs,
        });
      } catch {
        /* stat 失败（并发删除/权限）—— 跳过该文件 */
      }
    }
    if (truncated) break;
  }

  files.sort((a, b) => a.path.localeCompare(b.path));
  return { files, truncated };
}

// ─── 安全读取 ────────────────────────────────────────────────────────────────

export type VaultFsErrorCode =
  | 'NOT_FOUND'
  | 'NOT_A_FILE'
  | 'FORBIDDEN'
  | 'UNSUPPORTED_TYPE';

export class VaultFsError extends Error {
  constructor(public readonly code: VaultFsErrorCode, message: string) {
    super(message);
    this.name = 'VaultFsError';
  }
}

/** fs-browse.ts 同款：resolve 后 rel 不越出 parent 即视为在内。 */
function isInside(child: string, parent: string): boolean {
  const rel = path.relative(parent, child);
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

export interface ReadVaultOptions {
  maxBytes?: number;
}

export interface VaultTextContent {
  content: string;
  size: number;
  truncated: boolean;
}

/**
 * 读 vault 内一个文本文件。rel 必须是相对路径且 resolve 后仍在 root 子树内；
 * realpath 双检防符号链接越权。非白名单扩展（pdf/图片/二进制）拒绝。
 */
export function readVaultTextFile(
  root: string,
  rel: string,
  opts: ReadVaultOptions = {},
): VaultTextContent {
  const maxBytes = opts.maxBytes ?? MAX_READ_BYTES;

  // C1: 绝对路径直接拒（fs-browse /read 的 NOT_ABSOLUTE 前置同款，
  // 但 vault 语义下更严：连「root 恰好等于该绝对路径」都不放行）。
  if (!rel || path.isAbsolute(rel)) {
    throw new VaultFsError('FORBIDDEN', '仅允许仓库内相对路径');
  }
  // 显式拒 '..' 段（isInside 之后的兜底，先把最常见的穿越挡在 resolve 前）。
  const segs = rel.split(/[\\/]+/);
  if (segs.includes('..')) {
    throw new VaultFsError('FORBIDDEN', '路径不得包含 ..');
  }

  let realRoot: string;
  try {
    realRoot = realpathSync(path.resolve(root));
  } catch (e) {
    throw new VaultFsError('NOT_FOUND', `仓库根不存在: ${e instanceof Error ? e.message : String(e)}`);
  }

  // Windows 用户可能手输反斜杠相对路径;统一成 '/' 再 resolve(POSIX 也认)。
  const normRel = rel.replace(/\\/g, '/');
  const target = path.resolve(realRoot, normRel);
  if (!isInside(target, realRoot)) {
    throw new VaultFsError('FORBIDDEN', '路径越权：仅允许读仓库内文件');
  }

  let realFile: string;
  try {
    realFile = realpathSync(target);
  } catch {
    throw new VaultFsError('NOT_FOUND', '文件不存在');
  }
  if (!isInside(realFile, realRoot)) {
    throw new VaultFsError('FORBIDDEN', '符号链接越权');
  }

  let size: number;
  try {
    const st = statSync(realFile);
    if (!st.isFile()) {
      throw new VaultFsError('NOT_A_FILE', '不是文件');
    }
    size = st.size;
  } catch (e) {
    if (e instanceof VaultFsError) throw e;
    throw new VaultFsError('NOT_FOUND', e instanceof Error ? e.message : String(e));
  }

  if (!isVaultTextExtension(path.extname(realFile).slice(1))) {
    throw new VaultFsError('UNSUPPORTED_TYPE', '该类型不支持文本预览');
  }

  if (size > maxBytes) {
    const buf = readFileSync(realFile).subarray(0, maxBytes).toString('utf8');
    return { content: buf, size, truncated: true };
  }
  return { content: readFileSync(realFile, 'utf8'), size, truncated: false };
}

// ─── 文件夹链接图谱(Obsidian 式,无需 LLM)──────────────────────────────────

/**
 * buildVaultLinkGraph — 从 vault 文档内容即时构建「笔记链接图谱」。
 *
 * Obsidian 的 graph view 本质是文档间的链接图([[双链]] + Markdown 相对链接),
 * 不依赖任何索引/LLM —— 因此文件夹源挂载即有图谱,不需要先建知识包:
 *   - 节点 = 仓库内全部文档文件(md/txt/json/…)+ 未解析双链的 ghost 节点
 *   - 边   = [[WikiLink]](relation='wiki')与 [text](rel.md)(relation='ref')
 *   - http(s)/锚点/外部链接忽略;相对链接越出仓库根忽略
 * 输出形状对齐前端 graphAnalysis.GraphData(节点带 type:'file'/val=度数,
 * 边端点为字符串路径 id),KnowledgeGraphPage 可直接渲染。
 */
export interface VaultGraphFileNode {
  id: string;
  name: string;
  type: 'file';
  val: number;
  path: string;
  raw_type: string;
}
export interface VaultGraphLink {
  source: string;
  target: string;
  relation: string;
}
export interface VaultGraphResult {
  nodes: Array<VaultGraphFileNode | { id: string; name: string; type: 'file'; val: number; raw_type: 'ghost' }>;
  links: VaultGraphLink[];
  meta: {
    node_count: number;
    link_count: number;
    scanned_files: number;
    truncated: boolean;
  };
}

const GRAPH_MAX_FILES = 2000;
const GRAPH_MAX_FILE_BYTES = 256 * 1024;
const GRAPH_MAX_GHOSTS = 200;

function stemOf(name: string): string {
  const i = name.lastIndexOf('.');
  return i > 0 ? name.slice(0, i) : name;
}

export function buildVaultLinkGraph(root: string): VaultGraphResult {
  const { files, truncated: walkTruncated } = walkVault(root);
  const docs = files.filter((f) => f.type === 'doc').slice(0, GRAPH_MAX_FILES);

  // 索引:精确相对路径 → 文件;文件名 stem → 相对路径(首个命中,同名歧义取先)
  const byPath = new Map<string, string>();
  const byStem = new Map<string, string>();
  for (const f of docs) {
    byPath.set(f.path, f.path);
    const stem = stemOf(f.name);
    if (!byStem.has(stem)) byStem.set(stem, f.path);
  }

  const degrees = new Map<string, number>();
  const linkSet = new Set<string>();
  const links: VaultGraphLink[] = [];
  const ghosts = new Map<string, true>();

  const addLink = (src: string, tgt: string, relation: string) => {
    const key = `${src}\u0000${tgt}\u0000${relation}`;
    if (linkSet.has(key)) return;
    linkSet.add(key);
    links.push({ source: src, target: tgt, relation });
    degrees.set(src, (degrees.get(src) ?? 0) + 1);
    degrees.set(tgt, (degrees.get(tgt) ?? 0) + 1);
  };

  const WIKI_RE = /\[\[([^\]|#]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]/g;
  const MD_RE = /\[[^\]]*\]\(([^)\s]+)\)/g;

  for (const f of docs) {
    let text: string;
    try {
      const st = statSync(path.join(root, f.path.split('/').join(path.sep)));
      if (st.size > GRAPH_MAX_FILE_BYTES) continue;
      text = readFileSync(path.join(root, f.path.split('/').join(path.sep)), 'utf-8');
    } catch {
      continue;
    }

    // 1) [[WikiLink]](可带 #锚点 与 |别名)
    for (const m of text.matchAll(WIKI_RE)) {
      const raw = m[1].trim();
      if (!raw) continue;
      const target =
        byPath.get(raw) ??
        byPath.get(`${raw}.md`) ??
        byPath.get(`${raw}.markdown`) ??
        byStem.get(stemOf(raw.split('/').pop() ?? raw));
      if (target) {
        addLink(f.path, target, 'wiki');
      } else if (ghosts.size < GRAPH_MAX_GHOSTS) {
        ghosts.set(raw, true);
      }
    }

    // 2) [text](href) — 仅仓库内相对路径(http/锚点/data 一律忽略)
    for (const m of text.matchAll(MD_RE)) {
      const href = m[1].trim();
      if (!href || /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(href) || href.startsWith('#')) continue;
      const clean = href.split('#')[0];
      if (!clean) continue;
      const dir = f.path.includes('/') ? f.path.slice(0, f.path.lastIndexOf('/')) : '';
      const parts = `${dir}/${clean}`.split('/');
      const stack: string[] = [];
      for (const p of parts) {
        if (!p || p === '.') continue;
        if (p === '..') stack.pop();
        else stack.push(p);
      }
      const rel = stack.join('/');
      const finalSeg = rel.split('/').pop() ?? rel;
      const target =
        byPath.get(rel) ??
        byPath.get(`${rel}.md`) ??
        byPath.get(`${rel}.markdown`) ??
        byStem.get(stemOf(finalSeg));
      if (target) addLink(f.path, target, 'ref');
    }
  }

  const nodes: VaultGraphResult['nodes'] = docs.map((f) => ({
    id: f.path,
    name: stemOf(f.name),
    type: 'file' as const,
    val: (degrees.get(f.path) ?? 0) + 1,
    path: f.path,
    raw_type: f.name.endsWith('.md') || f.name.endsWith('.markdown') ? 'note' : 'doc',
  }));
  for (const [name] of ghosts) {
    nodes.push({ id: `ghost:${name}`, name, type: 'file' as const, val: 1, raw_type: 'ghost' as const });
  }

  return {
    nodes,
    links,
    meta: {
      node_count: nodes.length,
      link_count: links.length,
      scanned_files: docs.length,
      truncated: walkTruncated || files.length > GRAPH_MAX_FILES,
    },
  };
}
