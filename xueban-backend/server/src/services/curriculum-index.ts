export interface CurriculumChunk {
  id: string;
  page: number;
  section: string;
  text: string;
  tokens: string[];
}

export interface CurriculumIndex {
  version: number;
  hash: string;
  pages: number;
  chunks: CurriculumChunk[];
  terms: Record<string, number>;
  avgLength: number;
}

export interface CurriculumHit {
  chunk: CurriculumChunk;
  score: number;
  titleScore: number;
  keywordScore: number;
  semanticScore: number;
}

const INDEX_VERSION = 1;

function tokenize(value: string): string[] {
  const normalized = String(value || '').toLowerCase().replace(/[^\u4e00-\u9fff\w]+/g, ' ').trim();
  const tokens: string[] = [];
  normalized.split(/\s+/).filter(Boolean).forEach((word) => {
    if (/^[\u4e00-\u9fff]+$/.test(word)) {
      for (let index = 0; index < word.length; index += 1) {
        tokens.push(word[index]);
        if (index + 1 < word.length) tokens.push(word.slice(index, index + 2));
      }
    } else {
      tokens.push(word);
    }
  });
  return tokens;
}

export function buildCurriculumIndex(hash: string, pages: Array<{ page: number; text: string }>): CurriculumIndex {
  const chunks: CurriculumChunk[] = [];
  let section = '';
  pages.forEach(({ page, text }) => {
    const paragraphs = String(text || '').split(/\n{2,}|\n(?=\s*(?:第[一二三四五六七八九十百零\d]+[章节]|\d+(?:\.\d+)+)\s)/).map((item) => item.trim()).filter(Boolean);
    paragraphs.forEach((paragraph, index) => {
      const heading = paragraph.split('\n')[0];
      if (/^第[一二三四五六七八九十百零\d]+[章节]|^\d+(?:\.\d+)+/.test(heading)) section = heading;
      chunks.push({ id: `p${page}-${index}`, page, section, text: paragraph, tokens: tokenize(`${section} ${paragraph}`) });
    });
  });
  const terms: Record<string, number> = {};
  chunks.forEach((chunk) => chunk.tokens.forEach((token) => { terms[token] = (terms[token] || 0) + 1; }));
  const avgLength = chunks.length ? chunks.reduce((sum, chunk) => sum + chunk.tokens.length, 0) / chunks.length : 0;
  return { version: INDEX_VERSION, hash, pages: pages.length, chunks, terms, avgLength };
}

export function searchCurriculum(index: CurriculumIndex, query: string, limit = 6): CurriculumHit[] {
  const queryTokens = tokenize(query);
  if (!queryTokens.length || !index.chunks.length) return [];
  const total = index.chunks.length;
  const scores = index.chunks.map((chunk) => {
    const termCounts: Record<string, number> = {};
    chunk.tokens.forEach((token) => { termCounts[token] = (termCounts[token] || 0) + 1; });
    let keyword = 0;
    queryTokens.forEach((token) => {
      const df = index.terms[token] || 0;
      if (!df) return;
      const idf = Math.log(1 + (total - df + 0.5) / (df + 0.5));
      const tf = termCounts[token] || 0;
      keyword += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * chunk.tokens.length / Math.max(index.avgLength, 1)));
    });
    const title = queryTokens.filter((token) => token.length > 1 && chunk.section.includes(token)).length * 3;
    const semantic = queryTokens.filter((token) => chunk.text.includes(token)).length / Math.max(queryTokens.length, 1);
    return { chunk, keywordScore: keyword, titleScore: title, semanticScore: semantic, score: keyword + title + semantic * 2 };
  });
  return scores.sort((left, right) => right.score - left.score).slice(0, limit).filter((hit) => hit.score > 0);
}

export function curriculumContext(index: CurriculumIndex, query: string, budget = 4000): string {
  const hits = searchCurriculum(index, query, 6);
  const lines: string[] = [];
  let used = 0;
  hits.forEach((hit) => {
    const block = `[课纲第 ${hit.chunk.page} 页 · ${hit.chunk.section || '课程要求'}]\n${hit.chunk.text}`;
    if (used + block.length > budget) return;
    lines.push(block);
    used += block.length;
  });
  return lines.join('\n\n');
}
