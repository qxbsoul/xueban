/**
 * 极简 .env 加载（2026-11-16）。
 *
 * 不引入新依赖；只在进程启动时补齐未设置的变量。
 * 查找顺序：cwd/.env、cwd/xueban-backend/.env、cwd/../.env。
 */
import fs from 'node:fs';
import path from 'node:path';

function parseEnv(file: string): void {
  if (!fs.existsSync(file)) return;
  let text = '';
  try {
    text = fs.readFileSync(file, 'utf-8');
  } catch {
    return;
  }
  text.split(/\r?\n/).forEach((line) => {
    const value = line.trim();
    if (!value || value.startsWith('#')) return;
    const match = value.match(/^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!match) return;
    const key = match[1];
    if (process.env[key] !== undefined) return;
    let content = match[2].trim();
    if ((content.startsWith('"') && content.endsWith('"')) || (content.startsWith("'") && content.endsWith("'"))) {
      content = content.slice(1, -1);
    }
    process.env[key] = content;
  });
}

export function loadEnvFile(): void {
  const candidates = [
    path.join(process.cwd(), '.env'),
    path.join(process.cwd(), 'xueban-backend', '.env'),
    path.join(process.cwd(), '..', '.env'),
  ];
  candidates.forEach(parseEnv);
}

loadEnvFile();
