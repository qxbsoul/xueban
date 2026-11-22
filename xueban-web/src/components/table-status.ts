export type SourceTableStatus = 'structure_validated' | 'content_verified' | 'needs_review' | 'failed';

export interface SourceTableBlockLike {
  markdown?: string;
  text?: string;
  table_status?: SourceTableStatus;
}

export function sourceTableStatus(block: SourceTableBlockLike): SourceTableStatus {
  if (block.table_status) return block.table_status;
  const markdown = block.markdown || block.text || '';
  const match = markdown.match(/data-table-status="(content_verified|structure_validated|needs_review|failed)"/);
  if (match) return match[1] as SourceTableStatus;
  if (/data-table-reliable="true"/.test(markdown)) return 'structure_validated';
  return 'needs_review';
}

export function sourceTableStatusLabel(status: SourceTableStatus): string {
  if (status === 'content_verified') return '内容已验证';
  if (status === 'structure_validated') return '结构已校验 · 内容待核对';
  if (status === 'failed') return '表格校验失败';
  return '表格待校验';
}
