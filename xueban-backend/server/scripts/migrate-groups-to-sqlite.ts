/**
 * migrate-groups-to-sqlite.ts — 后端合一·阶段2（2026-06-13）
 *
 * 把 Python 群存储 .xueban/groups/*.json 幂等迁移进 Node SQLite（groups +
 * group_messages 表，经 groups-store.importGroupRecord）。重跑安全。
 *
 * 安全措施：
 *   - 迁移前自动备份整个 groups/ 目录到 .xueban/groups.bak-<ts>/。
 *   - --dry-run：只读+报数，不写 SQLite、不备份。
 *   - 保留原 group_id + 历史消息；不触发任何 agent 回复。
 *
 * 用法（在 server/ 下）：
 *   npx tsx scripts/migrate-groups-to-sqlite.ts            # 真迁移
 *   npx tsx scripts/migrate-groups-to-sqlite.ts --dry-run  # 预演
 *   SF_GROUPS_DIR=../.xueban/groups npx tsx scripts/...  # 自定义源目录
 *
 * 源目录默认 ../.xueban/groups（根目录的 Python 群）；SQLite 落 server/.xueban。
 */
import fs from 'fs';
import path from 'path';
import { importGroupRecord, getGroup } from '../src/storage/groups-store';

const DRY = process.argv.includes('--dry-run');

function groupsDir(): string {
  if (process.env.SF_GROUPS_DIR) return path.resolve(process.env.SF_GROUPS_DIR);
  // server/ 下运行 → 根目录的 .xueban/groups
  const root = path.resolve(process.cwd(), '..', '.xueban', 'groups');
  if (fs.existsSync(root)) return root;
  // 兜底：server/.xueban/groups
  return path.resolve(process.cwd(), '.xueban', 'groups');
}

function backup(dir: string): string {
  const ts = new Date().toISOString().replace(/[:.]/g, '-');
  const dest = `${dir}.bak-${ts}`;
  fs.cpSync(dir, dest, { recursive: true });
  return dest;
}

function main(): void {
  const dir = groupsDir();
  if (!fs.existsSync(dir)) {
    console.error(`[migrate] 源目录不存在：${dir}`);
    process.exit(1);
  }
  const files = fs.readdirSync(dir).filter((f) => f.endsWith('.json'));
  console.log(`[migrate] 源：${dir}`);
  console.log(`[migrate] 发现 ${files.length} 个群 JSON · ${DRY ? 'DRY-RUN（不写）' : '真迁移'}`);

  if (!DRY) {
    const bak = backup(dir);
    console.log(`[migrate] 已备份 → ${bak}`);
  }

  let ok = 0;
  let skipped = 0;
  let msgTotal = 0;
  const problems: string[] = [];

  for (const file of files) {
    const full = path.join(dir, file);
    let rec: Record<string, unknown>;
    try {
      rec = JSON.parse(fs.readFileSync(full, 'utf-8'));
    } catch (e) {
      problems.push(`${file}: JSON 解析失败 ${e instanceof Error ? e.message : e}`);
      skipped++;
      continue;
    }
    const gid = rec.group_id as string | undefined;
    if (!gid) {
      problems.push(`${file}: 无 group_id`);
      skipped++;
      continue;
    }
    const msgs = Array.isArray(rec.messages) ? rec.messages : [];
    msgTotal += msgs.length;
    if (DRY) {
      console.log(`  · ${gid.slice(0, 8)} kind=${rec.kind ?? 'group'} msgs=${msgs.length} name="${String(rec.name ?? '').slice(0, 20)}"`);
      ok++;
      continue;
    }
    try {
      importGroupRecord(rec as Parameters<typeof importGroupRecord>[0]);
      // 校验回读
      const back = getGroup(gid);
      if (!back) {
        problems.push(`${gid}: 导入后回读为空`);
        skipped++;
        continue;
      }
      if (back.messages.length !== msgs.length) {
        problems.push(`${gid}: 消息数不符 json=${msgs.length} sqlite=${back.messages.length}`);
      }
      ok++;
    } catch (e) {
      problems.push(`${gid}: 导入失败 ${e instanceof Error ? e.message : e}`);
      skipped++;
    }
  }

  console.log('');
  console.log(`[migrate] 完成：导入 ${ok} 群 · 跳过 ${skipped} · 消息共 ${msgTotal} 条`);
  if (problems.length) {
    console.log(`[migrate] ⚠ ${problems.length} 个问题：`);
    for (const p of problems.slice(0, 30)) console.log(`    - ${p}`);
  } else {
    console.log('[migrate] ✓ 无问题');
  }
}

main();
