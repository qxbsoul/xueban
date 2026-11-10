/**
 * wire-contract.test.ts — Wire-contract guard #1 (2026-05-11)
 *
 * Why this exists:
 *   On 2026-05-11 we landed 6 "wire" bugs in a single afternoon. The common
 *   pattern: a localStorage key was WRITTEN on one side of the system and
 *   READ on a different (incompatible) side — or read but never written, or
 *   written but never read. Examples:
 *
 *     - `sf.defaultExecutor`  — UI wrote it, getGenerationSettings() did
 *                                not read it → server defaulted to
 *                                anthropic-direct regardless of UI choice.
 *     - `sf.model`            — UI wrote it; getGenerationSettings() had
 *                                an "env wins" comment and didn't read it.
 *                                User pick → silently ignored.
 *     - `sf.auto_critique`    — UI wrote it but never reached the server.
 *
 *   Each of these is a contract violation: storage keys are an implicit
 *   contract between writer and reader, but TypeScript can't enforce it
 *   because the key is a free string.
 *
 *   This file walks every .ts/.tsx file under `src/` and `server/src/`,
 *   extracts every `setItem('sf...')` / `getItem('sf...')` AND every
 *   `_STORAGE = 'sf...'` constant + every `setStoredString(K)` /
 *   `getStoredString(K)` / `setSetting(K)` / `getSetting(K)` reference,
 *   and pairs them up. Any key with writers but no readers (zombie) or
 *   readers but no writers (phantom) FAILS — unless it's on the ALLOWLIST
 *   below with an explanation.
 *
 * Run:
 *   npx tsx tests/wire-contract.test.ts
 */

import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const ROOT = path.resolve(__dirname, '..');

// ── Scope ────────────────────────────────────────────────────────────────────
// The legacy frontend has been removed. The original ALLOWLIST is preserved in
// tests/wire-contract-allowlist.original.txt; this guard now covers only the
// backend server-side KV contract.
const SCAN_DIRS = [path.join(ROOT, 'server', 'src')];

// Skip pure tests and stubs — they intentionally read/write keys to verify
// the contract, and would otherwise pollute the writer/reader sets.
const SKIP_PATH_FRAGMENTS = [
  // unit-test files
  '.test.ts',
  '.test.tsx',
  '__tests__',
  // generated / stubs
  '_stubs',
  '__fixtures__',
];

// Some keys are intentionally one-sided. Document the reason next to each.
// `writers_ok` = key has only writers (no readers expected — usually consumed
//   out-of-band, e.g. by HTTP header or by a different process).
// `readers_ok` = key has only readers (no writers expected — usually written
//   by a different system, e.g. a UI tool the user opens manually).
const ALLOWLIST: Record<
  string,
  { writers_ok?: true; readers_ok?: true; reason: string }
> = {
  // ── BYOK / API keys — written client-side, read server-side via HTTP
  //    header X-Anthropic-Key etc., NEVER via storage. ────────────────────────
  sf_anthropic_key: { writers_ok: true, reason: 'BYOK; server reads via X-Anthropic-Key header' },
  sf_openai_key: { writers_ok: true, reason: 'BYOK; server reads via X-OpenAI-Key header' },
  sf_deepseek_key: { writers_ok: true, reason: 'BYOK; server reads via X-DeepSeek-Key header' },
  sf_zhipu_key: { writers_ok: true, reason: 'BYOK; server reads via X-Zhipu-Key header' },
  sf_default_provider: { reason: 'BYOK default selector; both sides via getDefaultProvider' },
  'sf.auto_critique': {
    readers_ok: true,
    reason: 'Frontend-owned setting written through PUT /api/settings/:key; dynamic writer is not statically visible',
  },

  // ── Multi-purpose JSON blob — many readers, fewer writers, OK either way. ─
  sf_secrets: { reason: 'JSON blob written by SecretsModal, read by many fetch wrappers' },

  // ── Client-only UI preferences (no server side). ──────────────────────────
  'sf.lastProject': { reason: 'UI: last selected project id' },
  'sf.lastSkill': { reason: 'UI: last selected skill id (writers + readers both exist)' },
  'sf.lastDS': { reason: 'UI: last selected design system' },
  'sf.lastFrame': { reason: 'UI: last selected frame preset' },
  'sf.theme': { reason: 'UI: dark/light theme pref' },
  'sf.notifications': { reason: 'UI: notification toggle blob' },
  'sf.composioKey': { reason: 'UI-only connector key (legacy)' },
  'sf.agentConfig': { reason: 'UI: agent advanced config blob (legacy AdvancedSection)' },
  'sf.customPet': { reason: 'UI: custom pet sprite (PetSettings)' },
  'sf.byokModel': { reason: 'Legacy AgentBackendSection BYOK model dropdown — kept readable' },
  'sf.selectedAgent': { reason: 'AgentBackendSection: which CLI is currently active' },
  // ── Read-only externally-managed values (no in-tree UI writer; defaults
  //    apply when missing). If you intend to add a UI writer for one of these,
  //    remove it from the list. ───────────────────────────────────────────────
  'xueban.user_alias': {
    readers_ok: true,
    reason: 'EditorPage lineage alias — externally set (devtools / future UI); defaults to "anon"',
  },
};

// ── Helpers ──────────────────────────────────────────────────────────────────

interface Hit {
  file: string;
  line: number;
  context: string;
}

interface KeyRecord {
  writers: Hit[];
  readers: Hit[];
}

function* walk(dir: string): Generator<string> {
  if (!fs.existsSync(dir)) return;
  const stack = [dir];
  while (stack.length) {
    const cur = stack.pop()!;
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(cur, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const ent of entries) {
      const full = path.join(cur, ent.name);
      if (ent.isDirectory()) {
        if (ent.name === 'node_modules' || ent.name === 'dist' || ent.name.startsWith('.')) {
          continue;
        }
        stack.push(full);
        continue;
      }
      if (!ent.isFile()) continue;
      if (!/\.(ts|tsx)$/.test(ent.name)) continue;
      if (SKIP_PATH_FRAGMENTS.some((frag) => full.includes(frag))) continue;
      yield full;
    }
  }
}

function rel(p: string): string {
  return path.relative(ROOT, p).replace(/\\/g, '/');
}

// Collect file → content (so we can resolve constant refs).
function readAll(): Map<string, string> {
  const m = new Map<string, string>();
  for (const dir of SCAN_DIRS) {
    for (const f of walk(dir)) {
      try {
        m.set(f, fs.readFileSync(f, 'utf8'));
      } catch {
        // ignore unreadable
      }
    }
  }
  return m;
}

// Build per-file `constantName -> 'sf.xxx'` map. Constants are file-scoped:
// two files can have `const STORAGE_KEY = 'sf_secrets'` and
// `const STORAGE_KEY = 'sf_wizard_state'` without ambiguity, so we resolve
// per-file and only fall back to a cross-file map for imported constants.
//
// Matches:
//   const FOO_STORAGE = 'sf.maxTokens';
//   export const FOO_STORAGE = "sf.lastSkill";
//   const FOO_KEY = 'sf.foo';
//   const LS_KEY = 'sf.pet.x';   (any naming, value is what matters)
const CONST_DECL_RE =
  /\b(?:export\s+)?(?:const|let|var)\s+([A-Z][A-Z0-9_]*)\s*(?::\s*string)?\s*=\s*['"]([^'"]+)['"]/g;

interface ConstantMaps {
  // file -> (constName -> sfKey)  — local to that file
  perFile: Map<string, Map<string, string>>;
  // cross-file fallback: constName -> [(file, sfKey)]
  // Used when a file does not declare CONST locally but references it via import.
  crossFile: Map<string, Array<{ file: string; value: string }>>;
}

function buildConstantMaps(files: Map<string, string>): ConstantMaps {
  const perFile = new Map<string, Map<string, string>>();
  const crossFile = new Map<string, Array<{ file: string; value: string }>>();
  for (const [file, src] of files) {
    const local = new Map<string, string>();
    CONST_DECL_RE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = CONST_DECL_RE.exec(src)) !== null) {
      const name = m[1];
      const val = m[2];
      if (!/^sf[._]|^xueban\.|^XUEBAN_/.test(val)) continue;
      local.set(name, val);
      let arr = crossFile.get(name);
      if (!arr) {
        arr = [];
        crossFile.set(name, arr);
      }
      arr.push({ file, value: val });
    }
    if (local.size > 0) perFile.set(file, local);
  }
  return { perFile, crossFile };
}

interface RawMatch {
  kind: 'writer' | 'reader';
  keyExpr: string; // either literal "'sf.x'" or a bare identifier
  file: string;
  line: number;
  text: string;
}

const PATTERNS: Array<{ kind: 'writer' | 'reader'; re: RegExp }> = [
  // localStorage.setItem('sf.x', ...) — literal
  { kind: 'writer', re: /localStorage\.setItem\s*\(\s*(['"][^'"]+['"]|[A-Z][A-Z0-9_]*)/g },
  // localStorage.getItem('sf.x') — literal or ident
  { kind: 'reader', re: /localStorage\.getItem\s*\(\s*(['"][^'"]+['"]|[A-Z][A-Z0-9_]*)/g },
  // window.localStorage.* variants
  { kind: 'writer', re: /window\.localStorage\.setItem\s*\(\s*(['"][^'"]+['"]|[A-Z][A-Z0-9_]*)/g },
  { kind: 'reader', re: /window\.localStorage\.getItem\s*\(\s*(['"][^'"]+['"]|[A-Z][A-Z0-9_]*)/g },
  // setStoredString(KEY, ...) / getStoredString(KEY)
  { kind: 'writer', re: /setStoredString\s*\(\s*(['"][^'"]+['"]|[A-Z][A-Z0-9_]*)/g },
  { kind: 'reader', re: /getStoredString\s*\(\s*(['"][^'"]+['"]|[A-Z][A-Z0-9_]*)/g },
  // Server-side KV store (server/src/storage/settings.ts).
  { kind: 'writer', re: /setSetting\s*\(\s*(['"][^'"]+['"]|[A-Z][A-Z0-9_]*)/g },
  { kind: 'reader', re: /getSetting\s*\(\s*(['"][^'"]+['"]|[A-Z][A-Z0-9_]*)/g },
];

function lineOf(src: string, idx: number): number {
  let line = 1;
  for (let i = 0; i < idx && i < src.length; i++) {
    if (src.charCodeAt(i) === 10) line++;
  }
  return line;
}

function resolveKey(
  expr: string,
  file: string,
  maps: ConstantMaps,
): string | null {
  if (!expr) return null;
  if (expr.startsWith("'") || expr.startsWith('"')) {
    const v = expr.slice(1, -1);
    return /^sf[._]/.test(v) ? v : null;
  }
  // bare identifier — first try file-local, then unique cross-file value
  const local = maps.perFile.get(file);
  if (local && local.has(expr)) return local.get(expr) ?? null;
  const xs = maps.crossFile.get(expr);
  if (xs && xs.length > 0) {
    // If all cross-file values agree, safe to use. Otherwise null (ambiguous).
    const uniq = Array.from(new Set(xs.map((x) => x.value)));
    if (uniq.length === 1) return uniq[0];
    return null;
  }
  return null;
}

// ── Main ─────────────────────────────────────────────────────────────────────

function main() {
  console.log('\n=== Wire-contract guard ===\n');
  console.log('Scanning server/src/ for sf.* storage key writers + readers…\n');

  const files = readAll();
  console.log(`Files scanned: ${files.size}`);

  const maps = buildConstantMaps(files);
  const totalConsts = Array.from(maps.perFile.values()).reduce((a, m) => a + m.size, 0);
  console.log(
    `Storage-key constants discovered: ${totalConsts} declarations across ${maps.perFile.size} files`,
  );
  const sample = Array.from(maps.crossFile.entries()).slice(0, 6).map(([n, xs]) => {
    const vals = Array.from(new Set(xs.map((x) => x.value)));
    return `${n}=${vals.length === 1 ? vals[0] : `[${vals.join('|')}]`}`;
  });
  if (sample.length > 0) {
    console.log(`  e.g. ${sample.join(', ')}${maps.crossFile.size > 6 ? ', …' : ''}`);
  }

  const records = new Map<string, KeyRecord>();
  const getRec = (key: string): KeyRecord => {
    let r = records.get(key);
    if (!r) {
      r = { writers: [], readers: [] };
      records.set(key, r);
    }
    return r;
  };

  for (const [file, src] of files) {
    for (const { kind, re } of PATTERNS) {
      re.lastIndex = 0;
      let m: RegExpExecArray | null;
      while ((m = re.exec(src)) !== null) {
        const keyExpr = m[1];
        const key = resolveKey(keyExpr, file, maps);
        if (!key) continue;
        const line = lineOf(src, m.index);
        const startOfLine = src.lastIndexOf('\n', m.index) + 1;
        const endOfLine = src.indexOf('\n', m.index);
        const ctx = src
          .slice(startOfLine, endOfLine === -1 ? src.length : endOfLine)
          .trim()
          .slice(0, 100);
        const rec = getRec(key);
        const hit: Hit = { file: rel(file), line, context: ctx };
        if (kind === 'writer') rec.writers.push(hit);
        else rec.readers.push(hit);
      }
    }
  }

  // ── Categorise ────────────────────────────────────────────────────────────
  const zombies: string[] = []; // writers > 0, readers == 0
  const phantoms: string[] = []; // readers > 0, writers == 0
  const balanced: string[] = [];

  for (const [key, rec] of records) {
    if (rec.writers.length > 0 && rec.readers.length === 0) zombies.push(key);
    else if (rec.readers.length > 0 && rec.writers.length === 0) phantoms.push(key);
    else balanced.push(key);
  }

  zombies.sort();
  phantoms.sort();
  balanced.sort();

  // ── Apply allowlist ──────────────────────────────────────────────────────
  let allowlistHits = 0;
  const unallowedZombies = zombies.filter((k) => {
    const a = ALLOWLIST[k];
    if (a?.writers_ok) {
      allowlistHits++;
      return false;
    }
    if (a) {
      allowlistHits++;
      return false;
    }
    return true;
  });
  const unallowedPhantoms = phantoms.filter((k) => {
    const a = ALLOWLIST[k];
    if (a?.readers_ok) {
      allowlistHits++;
      return false;
    }
    if (a) {
      allowlistHits++;
      return false;
    }
    return true;
  });

  // ── Report ───────────────────────────────────────────────────────────────
  console.log(`\nKeys with both writers + readers: ${balanced.length}`);
  for (const k of balanced) {
    const r = records.get(k)!;
    console.log(`  ✓ ${k}  (${r.writers.length}W / ${r.readers.length}R)`);
  }

  if (zombies.length > 0) {
    console.log(`\nZombie keys (W>0, R=0): ${zombies.length}`);
    for (const k of zombies) {
      const a = ALLOWLIST[k];
      const tag = a ? '  [allowlisted]' : '';
      console.log(`  ${a ? '⊘' : '✗'} ${k}${tag}`);
      if (a) console.log(`     reason: ${a.reason}`);
      for (const h of records.get(k)!.writers) {
        console.log(`     W ${h.file}:${h.line}  ${h.context}`);
      }
    }
  }
  if (phantoms.length > 0) {
    console.log(`\nPhantom keys (R>0, W=0): ${phantoms.length}`);
    for (const k of phantoms) {
      const a = ALLOWLIST[k];
      const tag = a ? '  [allowlisted]' : '';
      console.log(`  ${a ? '⊘' : '✗'} ${k}${tag}`);
      if (a) console.log(`     reason: ${a.reason}`);
      for (const h of records.get(k)!.readers) {
        console.log(`     R ${h.file}:${h.line}  ${h.context}`);
      }
    }
  }

  console.log(
    `\nSummary: ${balanced.length} balanced, ${unallowedZombies.length} unallowed zombie, ${unallowedPhantoms.length} unallowed phantom, ${allowlistHits} allowlist hits`,
  );

  if (unallowedZombies.length > 0 || unallowedPhantoms.length > 0) {
    console.log('\nWire-contract violations found.');
    console.log('Fix by either:');
    console.log('  (a) wiring the missing side (writer for phantoms / reader for zombies), or');
    console.log('  (b) adding an entry to ALLOWLIST in tests/wire-contract.test.ts with reason.');
    process.exit(1);
  }
  console.log('\nWire-contract OK.');
  process.exit(0);
}

main();
