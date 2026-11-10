/**
 * memory-entries.ts — Story 16.1 — River Memory CRUD persistence
 *
 * Storage: $CWD/.xueban/memory-entries.json
 * Write pattern: atomic tmp + rename (same as settings.ts)
 */

import fs from 'fs';
import path from 'path';
import { randomUUID } from 'crypto';

export type MemoryScope = 'user' | 'project' | 'session';

export interface MemoryEntry {
  id: string;
  scope: MemoryScope;
  title: string;
  content: string;
  created_at: string;
  updated_at: string;
}

export interface MemoryStore {
  enabled: boolean;
  entries: MemoryEntry[];
}

const TITLE_MAX = 120;
const CONTENT_MAX = 4000;
const UUID_RE = /^[0-9a-f-]{36}$/;

function storagePaths() {
  const dir = path.join(process.cwd(), '.xueban');
  return { dir, file: path.join(dir, 'memory-entries.json') };
}

function load(): MemoryStore {
  const { file } = storagePaths();
  if (!fs.existsSync(file)) return { enabled: true, entries: [] };
  let raw: string;
  try {
    raw = fs.readFileSync(file, 'utf-8');
  } catch {
    return { enabled: true, entries: [] };
  }
  try {
    const parsed = JSON.parse(raw) as Partial<MemoryStore>;
    return {
      enabled: typeof parsed.enabled === 'boolean' ? parsed.enabled : true,
      entries: Array.isArray(parsed.entries) ? (parsed.entries as MemoryEntry[]) : [],
    };
  } catch {
    return { enabled: true, entries: [] };
  }
}

function persist(store: MemoryStore): void {
  const { dir, file } = storagePaths();
  fs.mkdirSync(dir, { recursive: true });
  const tmp = `${file}.tmp.${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(store, null, 2), 'utf-8');
  fs.renameSync(tmp, file);
}

export function isValidId(id: string): boolean {
  return UUID_RE.test(id);
}

export function listEntries(scope?: MemoryScope): MemoryEntry[] {
  const { entries } = load();
  const list = scope ? entries.filter((e) => e.scope === scope) : entries;
  return list.slice().sort(
    (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
  );
}

export function createEntry(input: Pick<MemoryEntry, 'scope' | 'title' | 'content'>): MemoryEntry {
  if (!['user', 'project', 'session'].includes(input.scope)) {
    throw new Error('INVALID_SCOPE');
  }
  if (!input.title || input.title.trim().length === 0) throw new Error('INVALID_TITLE');
  if (input.title.length > TITLE_MAX) throw new Error('TITLE_TOO_LONG');
  if ((input.content ?? '').length > CONTENT_MAX) throw new Error('CONTENT_TOO_LONG');

  const now = new Date().toISOString();
  const entry: MemoryEntry = {
    id: randomUUID(),
    scope: input.scope,
    title: input.title.trim(),
    content: input.content ?? '',
    created_at: now,
    updated_at: now,
  };
  const store = load();
  store.entries.push(entry);
  persist(store);
  return entry;
}

export function updateEntry(
  id: string,
  patch: Partial<Pick<MemoryEntry, 'scope' | 'title' | 'content'>>,
): MemoryEntry | null {
  const store = load();
  const idx = store.entries.findIndex((e) => e.id === id);
  if (idx === -1) return null;

  const entry = { ...store.entries[idx] };
  if (patch.scope !== undefined) {
    if (!['user', 'project', 'session'].includes(patch.scope)) throw new Error('INVALID_SCOPE');
    entry.scope = patch.scope;
  }
  if (patch.title !== undefined) {
    if (!patch.title || patch.title.trim().length === 0) throw new Error('INVALID_TITLE');
    if (patch.title.length > TITLE_MAX) throw new Error('TITLE_TOO_LONG');
    entry.title = patch.title.trim();
  }
  if (patch.content !== undefined) {
    if (patch.content.length > CONTENT_MAX) throw new Error('CONTENT_TOO_LONG');
    entry.content = patch.content;
  }
  entry.updated_at = new Date().toISOString();
  store.entries[idx] = entry;
  persist(store);
  return entry;
}

export function deleteEntry(id: string): boolean {
  const store = load();
  const before = store.entries.length;
  store.entries = store.entries.filter((e) => e.id !== id);
  if (store.entries.length === before) return false;
  persist(store);
  return true;
}

export function getSettings(): { enabled: boolean } {
  const { enabled } = load();
  return { enabled };
}

export function updateSettings(settings: { enabled: boolean }): { enabled: boolean } {
  const store = load();
  store.enabled = settings.enabled;
  persist(store);
  return { enabled: store.enabled };
}
