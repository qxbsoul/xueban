/**
 * skill-prefs.ts — per-skill enable/disable persistence.
 *
 * Skills management (OpenDesign parity): users can toggle a skill off without
 * deleting it. Disabled skills are still listed by GET /api/skills (with
 * `enabled: false`) so the management panel can render the toggle, but they are
 * filtered out of GET /api/skills/installed so they no longer show up in the
 * @skill picker.
 *
 * Storage: `.xueban/skill-prefs.json` with shape `{ disabled: string[] }`.
 * Reads are tolerant (missing file / parse error → empty Set). Writes are
 * atomic (stage to .tmp then rename) to avoid a half-written file if the
 * process dies mid-write. No third-party deps — matches the simple JSON
 * persistence style used by skill-ingest's .installed.json.
 */

import fs from 'fs';
import path from 'path';

interface SkillPrefs {
  disabled: string[];
}

function prefsFile(): string {
  return path.resolve(process.cwd(), '.xueban', 'skill-prefs.json');
}

function readPrefs(): SkillPrefs {
  const file = prefsFile();
  if (!fs.existsSync(file)) return { disabled: [] };
  try {
    const raw = fs.readFileSync(file, 'utf-8');
    const parsed = JSON.parse(raw);
    if (parsed && Array.isArray(parsed.disabled)) {
      return { disabled: parsed.disabled.filter((x: unknown): x is string => typeof x === 'string') };
    }
    return { disabled: [] };
  } catch {
    return { disabled: [] };
  }
}

/** Set of skill ids the user has explicitly disabled. Empty on missing/corrupt file. */
export function getDisabledSkills(): Set<string> {
  return new Set(readPrefs().disabled);
}

/** A skill is enabled unless it has been explicitly disabled. */
export function isSkillEnabled(skillId: string): boolean {
  return !getDisabledSkills().has(skillId);
}

/**
 * Toggle a skill on/off. enabled=false adds the id to `disabled`;
 * enabled=true removes it. Read → modify → atomic write (.tmp + rename).
 * Creates the `.xueban` directory if it doesn't exist.
 */
export function setSkillEnabled(skillId: string, enabled: boolean): void {
  const prefs = readPrefs();
  const set = new Set(prefs.disabled);
  if (enabled) {
    set.delete(skillId);
  } else {
    set.add(skillId);
  }
  const next: SkillPrefs = { disabled: Array.from(set) };

  const file = prefsFile();
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = file + '.tmp.' + process.pid;
  fs.writeFileSync(tmp, JSON.stringify(next, null, 2), 'utf-8');
  fs.renameSync(tmp, file);
}
