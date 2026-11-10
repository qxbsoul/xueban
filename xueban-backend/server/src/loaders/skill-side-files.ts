/**
 * skill-side-files.ts — load `assets/` + `references/` for a skill (Story 15.12)
 *
 * Pure file-system loader. Given a skill id, walk
 *   .xueban/skills/<id>/assets/
 *   .xueban/skills/<id>/references/
 * recursively, filter by an extension whitelist, apply per-file (100KB) and
 * total (500KB) size limits, and return both a ready-to-inject prompt string
 * and structured file metadata.
 *
 * The composer (Story 15.13) is expected to take the `prompt` field and place
 * it between `skill.system_prompt` and `designSystem.injectionPrompt`.
 *
 * Behaviour contract (Story 15.12 AC1-AC6):
 * - AC1/AC6: recursive walk, assets/ before references/, per-dir alphabetical
 *   stable order. Reference header preserves the relative sub-path
 *   (e.g. `assets/components/button.html`).
 * - AC3: extension whitelist (.md / .html / .css / .json / .txt). Other
 *   extensions are silently skipped (binary / unknown types are not warned).
 * - AC4: single file > 100KB → skip + warn; running total > 500KB → stop
 *   appending further files + warn + set `truncated: true`. Already-injected
 *   sections are NOT rolled back.
 * - AC5: missing skill dir / missing or empty subdirs → empty result, no warn.
 * - Hidden files / dirs (leading `.`) are skipped.
 * - All filesystem errors are caught — this loader NEVER throws.
 */

import fs from 'fs';
import path from 'path';

const ALLOWED_EXTS: ReadonlySet<string> = new Set([
  '.md',
  '.html',
  '.css',
  '.json',
  '.txt',
]);

const MAX_FILE_SIZE = 100 * 1024; // 100 KB per file
const MAX_TOTAL_SIZE = 500 * 1024; // 500 KB total per skill

const DEFAULT_SKILLS_ROOT = path.join(process.cwd(), '.xueban', 'skills');

export type SideFileType = 'md' | 'html' | 'css' | 'json' | 'txt';

export interface SkillSideFile {
  /** path relative to skill root, e.g. "assets/template.html" */
  relPath: string;
  content: string;
  size: number;
  type: SideFileType;
}

export interface SkillSideFilesResult {
  /**
   * Ready-to-inject prompt fragment. Sections joined by `\n\n`. Empty string
   * when no files are loaded so callers can `.filter(Boolean).join('\n\n')`
   * without producing trailing blank lines.
   */
  prompt: string;
  files: SkillSideFile[];
  /** True if any file was dropped because the running total hit MAX_TOTAL_SIZE. */
  truncated: boolean;
}

interface FileEntry {
  relPath: string; // relative to skill root, e.g. "assets/components/button.html"
  absPath: string;
  size: number;
  type: SideFileType;
}

function extToType(ext: string): SideFileType | null {
  switch (ext) {
    case '.md':
      return 'md';
    case '.html':
      return 'html';
    case '.css':
      return 'css';
    case '.json':
      return 'json';
    case '.txt':
      return 'txt';
    default:
      return null;
  }
}

/**
 * Recursively walk `dir`, returning whitelisted files with their relative
 * path expressed relative to the skill root (so subDirPrefix should always
 * start with "assets" or "references").
 *
 * Returns [] if dir is missing / unreadable. Stable per-dir alphabetical order.
 * Hidden entries (leading `.`) are skipped.
 */
function walk(dir: string, subDirPrefix: string): FileEntry[] {
  if (!fs.existsSync(dir)) return [];
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return [];
  }

  // Stable order: alphabetical by name within each directory level.
  entries.sort((a, b) => a.name.localeCompare(b.name));

  const out: FileEntry[] = [];
  for (const entry of entries) {
    if (entry.name.startsWith('.')) continue; // hidden file/dir

    const abs = path.join(dir, entry.name);
    const rel = `${subDirPrefix}/${entry.name}`;

    // 2026-05-11 review P1-5: lstat 拒绝 symlink (OpenDesign buildBatchArchive 模式)。
    // 防止恶意 skill 在 assets/ 放 symlink 指向 /etc/passwd 被 readFileSync 跟随。
    let lst: fs.Stats;
    try {
      lst = fs.lstatSync(abs);
    } catch {
      continue;
    }
    if (lst.isSymbolicLink()) {
      console.warn(`[skill-side-files] reject symlink ${rel}`);
      continue;
    }

    if (entry.isDirectory()) {
      out.push(...walk(abs, rel));
      continue;
    }
    if (!entry.isFile()) continue;

    const ext = path.extname(entry.name).toLowerCase();
    const type = extToType(ext);
    if (!type) continue; // silently skip non-whitelisted types (AC3)

    let size = 0;
    try {
      size = fs.statSync(abs).size;
    } catch {
      continue;
    }
    out.push({ relPath: rel, absPath: abs, size, type });
  }
  return out;
}

/**
 * Load `assets/` + `references/` files from a skill directory and assemble
 * the side-file prompt fragment.
 *
 * @param skillId       directory name under skillsRoot (e.g. "example")
 * @param skillsRoot    optional override (default: <cwd>/.xueban/skills)
 * @returns             { prompt, files, truncated } — prompt is `''` when no
 *                      files load. Never throws; FS errors degrade to warnings.
 */
export function loadSkillSideFiles(
  skillId: string,
  skillsRoot: string = DEFAULT_SKILLS_ROOT,
): SkillSideFilesResult {
  const empty: SkillSideFilesResult = { prompt: '', files: [], truncated: false };

  if (!skillId || typeof skillId !== 'string') return empty;

  // 2026-05-11 review P1-5: skillId path traversal 净化（OpenDesign 模式）。
  // marketplace 时代 skillId 可能来自 user input，必须严格校验。
  if (
    skillId.includes('..') ||
    skillId.includes('/') ||
    skillId.includes('\\') ||
    path.isAbsolute(skillId) ||
    !/^[a-z0-9][a-z0-9_-]{0,63}$/i.test(skillId)
  ) {
    console.warn(`[skill-side-files] reject invalid skillId "${skillId}"`);
    return empty;
  }

  const skillRoot = path.join(skillsRoot, skillId);
  if (!fs.existsSync(skillRoot)) return empty;

  // assets/ before references/ (AC1)
  const candidates: FileEntry[] = [
    ...walk(path.join(skillRoot, 'assets'), 'assets'),
    ...walk(path.join(skillRoot, 'references'), 'references'),
  ];

  if (candidates.length === 0) return empty;

  const sections: string[] = [];
  const accepted: SkillSideFile[] = [];
  let totalSize = 0;
  let truncated = false;

  for (const f of candidates) {
    if (f.size > MAX_FILE_SIZE) {
      console.warn(
        `[skill-side-files] skip ${f.relPath}: size ${f.size} > ${MAX_FILE_SIZE}B`,
      );
      continue;
    }
    if (totalSize + f.size > MAX_TOTAL_SIZE) {
      console.warn(
        `[skill-side-files] truncated at ${f.relPath}: total > ${MAX_TOTAL_SIZE}B`,
      );
      truncated = true;
      break;
    }

    let content: string;
    try {
      content = fs.readFileSync(f.absPath, 'utf-8');
    } catch (err) {
      console.warn(
        `[skill-side-files] read failed ${f.relPath}: ${(err as Error).message}`,
      );
      continue;
    }

    // 2026-05-11 review P1-6: 包 markdown code fence 防止恶意 reference 文件
    // 含 `---` / `## IDENTITY` 突破 layer 边界注入新 prompt 段（OpenDesign 模式）。
    // fence 用文件类型作 code lang，让 Claude 识别这是数据而非指令。
    // strip 内容中的 ``` 终止符防 fence escape。
    const safeContent = content.replace(/```/g, '` ` `');
    sections.push(`## Reference: ${f.relPath}\n\n\`\`\`${f.type}\n${safeContent}\n\`\`\``);
    accepted.push({
      relPath: f.relPath,
      content,
      size: f.size,
      type: f.type,
    });
    totalSize += f.size;
  }

  const prompt = sections.length === 0 ? '' : sections.join('\n\n');
  return { prompt, files: accepted, truncated };
}
