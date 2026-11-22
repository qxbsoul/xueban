import { describe, expect, it } from 'vitest';
import { sourceTableStatus, sourceTableStatusLabel } from '../../../../xueban-web/src/components/table-status';

describe('表格状态展示', () => {
  it('未知状态默认待校验', () => {
    expect(sourceTableStatus({ markdown: '<table><tbody><tr><td>1</td></tr></tbody></table>' })).toBe('needs_review');
  });
  it('结构校验但内容未核验', () => {
    const status = sourceTableStatus({ markdown: '<table data-table-reliable="true" data-table-status="structure_validated"></table>' });
    expect(status).toBe('structure_validated');
    expect(sourceTableStatusLabel(status)).toContain('内容待核对');
  });
  it('内容已验证', () => {
    const status = sourceTableStatus({ markdown: '<table data-table-status="content_verified"></table>' });
    expect(status).toBe('content_verified');
    expect(sourceTableStatusLabel(status)).toBe('内容已验证');
  });
  it('失败状态不会被视为正常表格', () => {
    const status = sourceTableStatus({ markdown: '<table data-table-status="failed"></table>' });
    expect(status).toBe('failed');
    expect(sourceTableStatusLabel(status)).toBe('表格校验失败');
  });
});