import { describe, it, expect } from 'vitest';
import { canonicalIdFromUrl } from './canonical-id';

describe('canonicalIdFromUrl', () => {
  it('extracts repo slug from a GitHub URL (case preserved)', () => {
    expect(canonicalIdFromUrl('https://github.com/bmadcode/BMAD-METHOD')).toBe('BMAD-METHOD');
  });

  it('tolerates a trailing slash', () => {
    expect(canonicalIdFromUrl('https://github.com/bmadcode/BMAD-METHOD/')).toBe('BMAD-METHOD');
  });

  it('strips the .git suffix', () => {
    expect(canonicalIdFromUrl('https://github.com/bmadcode/BMAD-METHOD.git')).toBe('BMAD-METHOD');
  });

  it('falls back to deterministic skill-<sha1> for illegal characters', () => {
    // Spaces & "@" are not in [a-zA-Z0-9_.-]
    const id = canonicalIdFromUrl('https://example.com/weird name@v2');
    expect(id).toMatch(/^skill-[0-9a-f]{8}$/);
    // Same input → same id (deterministic, no Date.now() drift). /review #6
    expect(canonicalIdFromUrl('https://example.com/weird name@v2')).toBe(id);
  });

  it('truncates to 64 chars for very long repo names', () => {
    const longName = 'a'.repeat(100);
    const url = `https://github.com/org/${longName}`;
    const id = canonicalIdFromUrl(url);
    expect(id.length).toBe(64);
  });

  // ─── /review A1 — path traversal guard ──────────────────────────────────
  it('rejects path-traversal "..": URL ending in /.. → fallback', () => {
    const id = canonicalIdFromUrl('https://example.com/safe/../..');
    expect(id).toMatch(/^skill-[0-9a-f]{8}$/);
  });
  it('rejects path-traversal "."', () => {
    const id = canonicalIdFromUrl('https://example.com/.');
    expect(id).toMatch(/^skill-[0-9a-f]{8}$/);
  });
  it('rejects empty URL', () => {
    const id = canonicalIdFromUrl('');
    expect(id).toMatch(/^skill-[0-9a-f]{8}$/);
  });
  it('rejects null-ish coercion safely', () => {
    // @ts-expect-error — runtime guard
    const id = canonicalIdFromUrl(null);
    expect(id).toMatch(/^skill-[0-9a-f]{8}$/);
  });

  // ─── /review A2 — GitHub branch / file tail stripping ───────────────────
  it('strips /tree/<branch> tail and keeps repo slug', () => {
    expect(canonicalIdFromUrl('https://github.com/bmadcode/BMAD-METHOD/tree/main')).toBe('BMAD-METHOD');
    expect(canonicalIdFromUrl('https://github.com/bmadcode/BMAD-METHOD/tree/main/subdir')).toBe('BMAD-METHOD');
  });
  it('strips /blob/<branch>/<file.md> tail', () => {
    expect(canonicalIdFromUrl('https://github.com/bmadcode/BMAD-METHOD/blob/main/README.md')).toBe('BMAD-METHOD');
  });
  it('strips /commit/<sha> tail', () => {
    expect(canonicalIdFromUrl('https://github.com/x/Y/commit/abc123')).toBe('Y');
  });
  it('strips /issues/123 tail (so "issues" never becomes an id)', () => {
    expect(canonicalIdFromUrl('https://github.com/x/Y/issues/42')).toBe('Y');
  });
});
