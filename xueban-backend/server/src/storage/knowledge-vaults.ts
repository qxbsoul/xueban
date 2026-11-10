/**
 * knowledge-vaults.ts — 本地仓库注册表（vault registry），2026-08-19。
 *
 * 移植自 Shadow（Tauri 版）vault_registry.rs 的注册表模型：用户把任意本地
 * 文件夹注册为知识库仓库（Obsidian vault 模式），注册表只存「路径 → 名称」
 * 映射。铁律（Shadow REG-1）：remove 只删注册行，磁盘上的用户文件永不触碰。
 *
 * `path` 上 UNIQUE —— 同一路径重复注册 = 幂等 upsert（改名/复活 status），
 * 与 Shadow 的 `INSERT ... ON CONFLICT(path) DO UPDATE` 一致。
 * `status` 是注册级标记（active/archived）；「路径还在不在磁盘上」不落库，
 * 由路由层 list 时现场 statSync 探测（missing 探测值）。
 *
 * Schema（additive，DDL 见 sqlite.ts getDb()）：knowledge_vaults 表。
 */

import { randomUUID } from 'crypto';
import { getDb } from './sqlite';

export interface VaultRecord {
  vault_id: string;
  name: string;
  path: string;
  created_at: string;
  last_opened_at: string | null;
  status: string;
}

interface VaultRow {
  vault_id: string;
  name: string;
  path: string;
  created_at: string;
  last_opened_at: string | null;
  status: string;
}

function rowToRecord(r: VaultRow): VaultRecord {
  return {
    vault_id: r.vault_id,
    name: r.name,
    path: r.path,
    created_at: r.created_at,
    last_opened_at: r.last_opened_at,
    status: r.status,
  };
}

export function listVaults(): VaultRecord[] {
  const rows = getDb()
    .prepare(`SELECT * FROM knowledge_vaults ORDER BY created_at DESC`)
    .all() as VaultRow[];
  return rows.map(rowToRecord);
}

export function getVault(id: string): VaultRecord | null {
  const row = getDb()
    .prepare(`SELECT * FROM knowledge_vaults WHERE vault_id = ?`)
    .get(id) as VaultRow | undefined;
  return row ? rowToRecord(row) : null;
}

export function getVaultByPath(p: string): VaultRecord | null {
  const row = getDb()
    .prepare(`SELECT * FROM knowledge_vaults WHERE path = ?`)
    .get(p) as VaultRow | undefined;
  return row ? rowToRecord(row) : null;
}

/**
 * 幂等注册：同 path 已存在 → 更新 name 并复活为 active（保留原 id /
 * created_at / last_opened_at）；不存在 → 插入新行。返回最终落库的记录。
 */
export function upsertVaultByPath(input: { name: string; path: string }): VaultRecord {
  const db = getDb();
  const now = new Date().toISOString();
  const existing = getVaultByPath(input.path);
  if (existing) {
    db.prepare(`UPDATE knowledge_vaults SET name = ?, status = 'active' WHERE vault_id = ?`)
      .run(input.name, existing.vault_id);
    return getVault(existing.vault_id)!;
  }
  const id = randomUUID();
  db.prepare(
    `INSERT INTO knowledge_vaults (vault_id, name, path, created_at, status)
     VALUES (?, ?, ?, ?, 'active')`,
  ).run(id, input.name, input.path, now);
  return getVault(id)!;
}

export function renameVault(id: string, name: string): VaultRecord | null {
  const db = getDb();
  const res = db.prepare(`UPDATE knowledge_vaults SET name = ? WHERE vault_id = ?`).run(name, id);
  if (res.changes === 0) return null;
  return getVault(id);
}

/** 只删注册行。磁盘文件 / 仓库内容永不删除（Shadow REG-1）。 */
export function removeVault(id: string): boolean {
  const res = getDb()
    .prepare(`DELETE FROM knowledge_vaults WHERE vault_id = ?`)
    .run(id);
  return res.changes > 0;
}

/** 浏览仓库（列文件）时打点，供列表按最近使用排序 / 展示。 */
export function touchVault(id: string): void {
  getDb()
    .prepare(`UPDATE knowledge_vaults SET last_opened_at = ? WHERE vault_id = ?`)
    .run(new Date().toISOString(), id);
}
