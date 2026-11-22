import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

let tmp = '';
let origCwd = '';
let sqlite: typeof import('../storage/sqlite');
let store: typeof import('../storage/learning');
let parser: typeof import('../services/learning-parser');
let agent: typeof import('../services/learning-agent');
let prompts: typeof import('../prompts/learning-parse');

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-table-chain-'));
  process.chdir(tmp);
  sqlite = await import('../storage/sqlite');
  sqlite._resetForTests();
  store = await import('../storage/learning');
  parser = await import('../services/learning-parser');
  agent = await import('../services/learning-agent');
  prompts = await import('../prompts/learning-parse');
  sqlite.getDb();
});

afterAll(() => {
  sqlite._resetForTests();
  process.chdir(origCwd);
  rmSync(tmp, { recursive: true, force: true });
});

describe('B01 表格状态真实数据链路', () => {
  it('PDF 解析 HTML -> 数据库 -> chunk -> 教学提示词', () => {
    const html = readFileSync(path.join(__dirname, 'fixtures', 'b01-page6-table.html'), 'utf8').trim();
    const parsed = parser.parseStructuredDocument(html);
    const table = parsed.blocks.find((block) => block.block_type === 'table');
    expect(table).toBeTruthy();
    expect(table?.status).toBe('needs_review');
    expect(table?.table_status).toBe('structure_validated');

    const stored = store.createSourceBlock({
      owner_id: 'owner-table-chain',
      course_id: 'course-table-chain',
      material_id: 'material-table-chain',
      block_type: 'table',
      ordinal: 1,
      text: table!.text,
      markdown: table!.markdown,
      page: 6,
      line_start: table!.line_start,
      line_end: table!.line_end,
      status: table!.status,
      table_status: table!.table_status,
    });
    expect(stored.table_status).toBe('structure_validated');
    const loaded = store.listSourceBlocks('owner-table-chain', 'course-table-chain')[0];
    expect(loaded.table_status).toBe('structure_validated');

    const chunk = agent.chunkSourceBlocks([loaded], 100000)[0];
    expect(chunk.segments[0].tableStatus).toBe('structure_validated');
    expect(chunk.segments[0].status).toBe('needs_review');
    const prompt = prompts.buildSectionPrompt({
      courseTitle: '测试', chapterTitle: '第八章', sectionTitle: '8.1',
      blocks: [{
        id: loaded.id, type: 'table', text: loaded.markdown, line: loaded.line_start,
        page: loaded.page, status: loaded.status, table_status: loaded.table_status,
      }],
    });
    expect(prompt).toContain('表格未完成内容核验');
    expect(prompt).toContain('不得当作已确认事实');

    const longBlock = { ...loaded, text: 'x'.repeat(60), markdown: 'x'.repeat(60), status: 'needs_review' as const };
    const longSegments = agent.chunkSourceBlocks([longBlock], 20).flatMap((chunk) => chunk.segments);
    expect(longSegments.length).toBeGreaterThan(1);
    for (const segment of longSegments) {
      expect(segment.status).toBe('needs_review');
      expect(segment.tableStatus).toBe('structure_validated');
    }
  });
});