/**
 * knowledge-vaults.ts — /api/knowledge-vaults 路由，2026-08-19。
 *
 * 本地仓库知识库（Obsidian vault 模式），注册表模型移植自 Shadow 的
 * vault_registry.rs + open_knowledge_base 命令族。与 /api/knowledge（Python
 * KnowledgePack RAG 管线）互补：KnowledgePack 管「喂给 Agent 的索引」，
 * 本路由管「本地文件夹即知识库仓库」——注册、浏览、阅读，全部只读用户文件。
 *
 *   GET    /                            列出已注册仓库（含磁盘存活探测）
 *   POST   /        {name?, path}       注册本地文件夹（path 幂等 upsert）
 *   PATCH  /:id     {name}              改显示名
 *   DELETE /:id                          移除注册（绝不删磁盘文件）
 *   GET    /:id                          单个仓库详情
 *   GET    /:id/files                    递归扫描 → 扁平相对路径列表
 *   GET    /:id/file?path=<rel>          读单个文本文件（三重越权守卫）
 *
 * 必须挂在 proxyFallback 之前（index.ts），否则会被转去 Python。
 */

import { Router, type Request, type Response } from 'express';
import { mkdirSync, statSync } from 'node:fs';
import path from 'node:path';
import {
  listVaults,
  getVault,
  removeVault,
  renameVault,
  touchVault,
  upsertVaultByPath,
  type VaultRecord,
} from '../storage/knowledge-vaults';
import { buildVaultLinkGraph, readVaultTextFile, VaultFsError, walkVault } from '../lib/vault-fs';

const router = Router();

/** 列表项 = 注册行 + 现场磁盘探测（不落库）。 */
interface VaultListItem extends VaultRecord {
  /** active = 目录还在磁盘上；missing = 路径已不存在/不可访问。 */
  health: 'active' | 'missing';
}

function toListItem(rec: VaultRecord): VaultListItem {
  let health: 'active' | 'missing' = 'missing';
  try {
    health = statSync(rec.path).isDirectory() ? 'active' : 'missing';
  } catch {
    health = 'missing';
  }
  return { ...rec, health };
}

/** fs-browse.ts 同款：剥掉用户粘贴路径首尾的成对引号与空白。 */
function sanitizePath(raw: string): string {
  let s = raw.trim();
  if (s.length >= 2) {
    const a = s[0];
    const b = s[s.length - 1];
    if ((a === '"' && b === '"') || (a === "'" && b === "'")) s = s.slice(1, -1).trim();
  }
  return s;
}

router.get('/', (_req: Request, res: Response) => {
  const items = listVaults().map(toListItem);
  res.json({ data: { vaults: items } });
});

router.post('/', (req: Request, res: Response) => {
  const body = req.body as { name?: unknown; path?: unknown; create?: unknown };
  const wantCreate = body.create === true;
  const rawPath = typeof body.path === 'string' ? sanitizePath(body.path) : '';
  if (!rawPath) {
    res.status(400).json({ error: { code: 'NO_PATH', message: 'path 必填' } });
    return;
  }
  if (!path.isAbsolute(rawPath)) {
    res.status(400).json({ error: { code: 'NOT_ABSOLUTE', message: 'path 必须是绝对路径' } });
    return;
  }
  const abs = path.resolve(rawPath);
  let existed = true;
  try {
    if (!statSync(abs).isDirectory()) {
      res.status(400).json({ error: { code: 'NOT_A_DIR', message: `${abs} 不是目录` } });
      return;
    }
  } catch {
    existed = false;
  }

  // create:true — 目标不存在时新建一级目录(Shadow「创建新知识库」同款):
  // 父目录必须已存在(不递归建链),末段必须是单层安全目录名。已存在则幂等注册。
  if (!existed) {
    if (!wantCreate) {
      res.status(404).json({
        error: { code: 'NOT_FOUND', message: `路径不存在: ${abs}`, path: abs },
      });
      return;
    }
    const leaf = path.basename(abs);
    if (!leaf || leaf === '.' || leaf === '..' || leaf.startsWith('.')) {
      res.status(400).json({
        error: { code: 'BAD_NAME', message: '目录名不合法(不能为空或以 . 开头)' },
      });
      return;
    }
    const parent = path.dirname(abs);
    try {
      if (!statSync(parent).isDirectory()) {
        res.status(404).json({
          error: { code: 'PARENT_NOT_FOUND', message: `父目录不存在: ${parent}`, path: parent },
        });
        return;
      }
    } catch {
      res.status(404).json({
        error: { code: 'PARENT_NOT_FOUND', message: `父目录不存在: ${parent}`, path: parent },
      });
      return;
    }
    try {
      mkdirSync(abs, { recursive: false });
    } catch (e) {
      res.status(500).json({
        error: { code: 'CREATE_FAILED', message: e instanceof Error ? e.message : String(e) },
      });
      return;
    }
  }

  const name =
    typeof body.name === 'string' && body.name.trim()
      ? body.name.trim()
      : path.basename(abs) || abs;
  const rec = upsertVaultByPath({ name, path: abs });
  res.json({ data: toListItem(rec) });
});

router.patch('/:vaultId', (req: Request, res: Response) => {
  const name = typeof req.body?.name === 'string' ? req.body.name.trim() : '';
  if (!name) {
    res.status(400).json({ error: { code: 'NO_NAME', message: 'name 必填' } });
    return;
  }
  const rec = renameVault(req.params.vaultId, name);
  if (!rec) {
    res.status(404).json({ error: { code: 'VAULT_NOT_FOUND', message: '仓库不存在或已移除' } });
    return;
  }
  res.json({ data: toListItem(rec) });
});

router.delete('/:vaultId', (req: Request, res: Response) => {
  const rec = getVault(req.params.vaultId);
  if (!rec) {
    res.status(404).json({ error: { code: 'VAULT_NOT_FOUND', message: '仓库不存在或已移除' } });
    return;
  }
  // Shadow REG-1：只删注册行。磁盘上的文件夹与其中文件保持原样。
  removeVault(rec.vault_id);
  res.json({
    data: {
      deleted: true,
      vault_id: rec.vault_id,
      // 明示给前端确认弹窗回显：磁盘文件未被触碰。
      disk_untouched: true,
    },
  });
});

router.get('/:vaultId', (req: Request, res: Response) => {
  const rec = getVault(req.params.vaultId);
  if (!rec) {
    res.status(404).json({ error: { code: 'VAULT_NOT_FOUND', message: '仓库不存在或已移除' } });
    return;
  }
  res.json({ data: toListItem(rec) });
});

router.get('/:vaultId/files', (req: Request, res: Response) => {
  const rec = getVault(req.params.vaultId);
  if (!rec) {
    res.status(404).json({ error: { code: 'VAULT_NOT_FOUND', message: '仓库不存在或已移除' } });
    return;
  }
  try {
    if (!statSync(rec.path).isDirectory()) {
      throw new Error('not a dir');
    }
  } catch {
    res.status(410).json({
      error: { code: 'VAULT_PATH_MISSING', message: `仓库路径已不存在: ${rec.path}` },
    });
    return;
  }

  const { files, truncated } = walkVault(rec.path);
  touchVault(rec.vault_id); // 浏览打点（last_opened_at）

  const byType: Record<string, number> = {};
  for (const f of files) byType[f.type] = (byType[f.type] ?? 0) + 1;

  res.json({
    data: {
      vault_id: rec.vault_id,
      path: rec.path,
      files,
      total: files.length,
      truncated,
      by_type: byType,
    },
  });
});

const VAULT_FS_STATUS: Record<string, number> = {
  NOT_FOUND: 404,
  NOT_A_FILE: 404,
  FORBIDDEN: 403,
  UNSUPPORTED_TYPE: 415,
};

/**
 * GET /:vaultId/graph — 文件夹链接图谱(Obsidian 式,挂载即有,无需建包/LLM)。
 * 响应形状对齐 Python /api/knowledge/packs/:id/graph({data:{nodes,links}, meta}),
 * 前端 KnowledgeGraphPage 同一套渲染管线直接可用;rag_backend='vault-links'。
 */
router.get('/:vaultId/graph', (req: Request, res: Response) => {
  const rec = getVault(req.params.vaultId);
  if (!rec) {
    res.status(404).json({ error: { code: 'VAULT_NOT_FOUND', message: '仓库不存在或已移除' } });
    return;
  }
  try {
    if (!statSync(rec.path).isDirectory()) throw new Error('not a dir');
  } catch {
    res.status(410).json({
      error: { code: 'VAULT_PATH_MISSING', message: `仓库路径已不存在: ${rec.path}` },
    });
    return;
  }
  const g = buildVaultLinkGraph(rec.path);
  res.json({
    data: { nodes: g.nodes, links: g.links },
    meta: {
      pack_id: rec.vault_id,
      rag_backend: 'vault-links',
      node_count: g.meta.node_count,
      link_count: g.meta.link_count,
      scanned_files: g.meta.scanned_files,
      truncated: g.meta.truncated,
    },
  });
});

router.get('/:vaultId/file', (req: Request, res: Response) => {
  const rec = getVault(req.params.vaultId);
  if (!rec) {
    res.status(404).json({ error: { code: 'VAULT_NOT_FOUND', message: '仓库不存在或已移除' } });
    return;
  }
  const rel = typeof req.query.path === 'string' ? req.query.path : '';
  try {
    const out = readVaultTextFile(rec.path, rel);
    res.json({ data: { ...out, path: rel, vault_id: rec.vault_id } });
  } catch (e) {
    if (e instanceof VaultFsError) {
      const status = VAULT_FS_STATUS[e.code] ?? 400;
      res.status(status).json({ error: { code: e.code, message: e.message } });
      return;
    }
    res
      .status(500)
      .json({ error: { code: 'READ_FAILED', message: e instanceof Error ? e.message : String(e) } });
  }
});

export { router as knowledgeVaultsRouter };
export default router;
