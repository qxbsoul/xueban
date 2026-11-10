import { getDb } from './sqlite';

export function linkKnowledgePack(companyId: string, packId: string): void {
  getDb().prepare(`
    INSERT INTO xueban_knowledge_packs (company_id, pack_id, created_at)
    VALUES (?, ?, ?)
    ON CONFLICT(company_id, pack_id) DO NOTHING
  `).run(companyId, packId, new Date().toISOString());
}

export function listKnowledgePackIds(companyId: string): string[] {
  return (getDb().prepare(`
    SELECT pack_id FROM xueban_knowledge_packs WHERE company_id = ? ORDER BY created_at, pack_id
  `).all(companyId) as Array<{ pack_id: string }>).map(row => row.pack_id);
}

export function companyOwnsKnowledgePack(companyId: string, packId: string): boolean {
  return Boolean(getDb().prepare(`
    SELECT 1 FROM xueban_knowledge_packs WHERE company_id = ? AND pack_id = ?
  `).get(companyId, packId));
}

export function unlinkKnowledgePack(companyId: string, packId: string): boolean {
  return getDb().prepare(`
    DELETE FROM xueban_knowledge_packs WHERE company_id = ? AND pack_id = ?
  `).run(companyId, packId).changes > 0;
}
