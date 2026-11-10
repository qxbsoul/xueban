/**
 * skill-reader/parse-workflow.ts — collect verbatim workflow files.
 *
 * PR-A's contract is strictly verbatim collection — we read every
 * `<skillDir>/workflows/*.yaml|yml` (plus `.md` workflow specs that some
 * skills use) and surface the raw bytes. We do **not**:
 *   - parse the workflow into a DAG (PR-C does that via LLM)
 *   - interpret step ordering or dependencies
 *   - infer modes (sequential / parallel / dag)
 *
 * BMAD's workflows live under sub-skill directories (e.g.
 * `src/bmm-skills/.../bmad-prfaq/workflows/*`) rather than a top-level
 * `workflows/`. For PR-A we restrict the scan to the **root-level**
 * `workflows/` directory — recursive scanning is PR-C's responsibility if
 * it decides to flatten sub-skills. Keeping PR-A's scope tight prevents
 * surprise file inclusion (e.g. `.github/workflows/*.yaml` CI configs).
 *
 * Missing `workflows/` dir → returns `[]` without throwing.
 */

import fs from 'fs';
import path from 'path';
import matter from 'gray-matter';
import type { WorkflowFile } from './types';

const WORKFLOW_EXTS = new Set(['.yaml', '.yml', '.md']);

/** POSIX-normalize a path so the content hash is stable across Windows / Linux. */
function toPosix(p: string): string {
  return p.replace(/\\/g, '/');
}

/**
 * Recursive walk of `<skillDir>/workflows/` so multi-step workflows that
 * organize files into sub-folders (e.g. `workflows/research/step-1.md`) are
 * picked up. Hidden entries and `node_modules` are skipped. Symlinks are
 * skipped (security — same convention as `probe.ts`).
 */
function walkWorkflows(
  rootAbs: string,
  rel: string,
  acc: WorkflowFile[],
  depth: number,
): void {
  if (depth > 6) return; // matches probe.ts MAX_DEPTH spirit

  const abs = path.join(rootAbs, rel);
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(abs, { withFileTypes: true });
  } catch {
    return;
  }
  // Sort here so within-directory order is deterministic too.
  entries.sort((a, b) => a.name.localeCompare(b.name));

  for (const e of entries) {
    if (e.name.startsWith('.')) continue;
    if (e.name === 'node_modules') continue;

    const entryRel = rel ? `${rel}/${e.name}` : e.name;
    const entryAbs = path.join(abs, e.name);

    let lst: fs.Stats;
    try {
      lst = fs.lstatSync(entryAbs);
    } catch {
      continue;
    }
    if (lst.isSymbolicLink()) continue;

    if (e.isDirectory()) {
      walkWorkflows(rootAbs, entryRel, acc, depth + 1);
      continue;
    }
    if (!e.isFile()) continue;

    const ext = path.extname(e.name).toLowerCase();
    if (!WORKFLOW_EXTS.has(ext)) continue;

    let raw: string;
    try {
      raw = fs.readFileSync(entryAbs, 'utf-8');
    } catch {
      continue;
    }

    let frontmatter: Record<string, unknown> | null = null;
    if (ext === '.md') {
      try {
        const parsed = matter(raw);
        if (parsed.data && Object.keys(parsed.data).length > 0) {
          frontmatter = parsed.data as Record<string, unknown>;
        }
      } catch {
        // malformed frontmatter → keep raw, drop frontmatter
      }
    }

    acc.push({
      path: toPosix(`workflows/${entryRel}`),
      raw,
      frontmatter,
    });
  }
}

/**
 * Public entry: collect all workflow files from `<skillDir>/workflows/`.
 *
 * Recursive within `workflows/` only; does not scan `.github/workflows/`
 * or sub-skill `workflows/` directories elsewhere in the tree.
 */
export async function parseWorkflows(skillDir: string): Promise<WorkflowFile[]> {
  const workflowsDir = path.join(skillDir, 'workflows');
  if (!fs.existsSync(workflowsDir)) return [];

  const acc: WorkflowFile[] = [];
  walkWorkflows(workflowsDir, '', acc, 0);
  acc.sort((a, b) => a.path.localeCompare(b.path));
  return acc;
}
