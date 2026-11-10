/**
 * generate-skill-manifest.ts — Skill manifest 双格式生成器
 *
 * 功能:
 *   1. 扫描 server/skills/ 目录下的所有 SKILL.md 文件
 *   2. 解析 frontmatter 元数据
 *   3. 生成 JSON manifest（skills-manifest.json）
 *   4. 支持双格式：SKILL.md（人类可读）+ JSON（程序可读）
 *
 * 对标设计文档：Skill manifest 双格式支持（项目任务 #9）
 */

import fs from 'fs';
import path from 'path';
import matter from 'gray-matter';

interface SkillManifestEntry {
  id: string;
  name: string;
  description: string;
  version: string;
  mode: 'blueprint' | 'prototype' | 'report';
  kind: 'workflow' | 'capability' | 'generator';
  domain?: string;
  source: 'builtin' | 'user';
  triggers: string[];
  allowedTools: string[];
  path: string;
  lastModified: string;
}

interface SkillManifest {
  version: string;
  generatedAt: string;
  skills: SkillManifestEntry[];
  totalCount: number;
  builtinCount: number;
  userCount: number;
}

const SKILLS_ROOT = path.join(__dirname, '..', '..', 'server', 'skills');
const OUTPUT_PATH = path.join(__dirname, '..', '..', 'server', 'skills-manifest.json');

/**
 * 递归扫描目录，查找所有 SKILL.md 文件
 */
function findSkillFiles(dir: string, baseDir: string = dir): string[] {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  const skillFiles: string[] = [];

  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    const relativePath = path.join(baseDir, entry.name);

    if (entry.isDirectory()) {
      // 跳过隐藏目录和 node_modules
      if (entry.name.startsWith('.') || entry.name === 'node_modules') {
        continue;
      }
      skillFiles.push(...findSkillFiles(fullPath, relativePath));
    } else if (entry.name === 'SKILL.md') {
      skillFiles.push(relativePath);
    }
  }

  return skillFiles;
}

/**
 * 判断 skill 是 builtin 还是 user
 */
function determineSource(skillPath: string): 'builtin' | 'user' {
  // 转换为 POSIX 风格路径进行统一判断
  const normalizedPath = skillPath.replace(/\\/g, '/');

  // 在 server/skills/ 下的为 builtin
  if (normalizedPath.includes('server/skills/')) {
    return 'builtin';
  }
  return 'user';
}

/**
 * 解析 SKILL.md 文件，提取 manifest entry
 */
function parseSkillFile(skillPath: string): SkillManifestEntry | null {
  // skillPath 是相对于 process.cwd() 的路径
  const fullPath = path.resolve(process.cwd(), skillPath);

  if (!fs.existsSync(fullPath)) {
    console.warn(`⚠️  File not found: ${fullPath}`);
    return null;
  }

  const stats = fs.statSync(fullPath);
  const content = fs.readFileSync(fullPath, 'utf-8');
  const { data } = matter(content);

  // 验证必需字段
  if (!data.name || !data.description) {
    console.warn(`⚠️  Skipping ${skillPath}: missing name or description`);
    return null;
  }

  // 从路径提取 skill id（取 SKILL.md 所在目录的父目录名）
  const parts = skillPath.split(path.sep);
  const skillIndex = parts.indexOf('SKILL.md');
  const id = skillIndex > 0 ? parts[skillIndex - 1] : path.basename(path.dirname(skillPath));

  return {
    id,
    name: data.name,
    description: data.description,
    version: data.version || '1.0.0',
    mode: data.mode || 'blueprint',
    kind: data.kind || 'workflow',
    domain: data.domain,
    source: determineSource(skillPath),
    triggers: Array.isArray(data.triggers) ? data.triggers : [],
    allowedTools: Array.isArray(data.allowedTools) ? data.allowedTools : [],
    path: skillPath,
    lastModified: stats.mtime.toISOString(),
  };
}

/**
 * 生成完整 manifest
 */
export function generateSkillManifest(): SkillManifest {
  console.log('🔍 Scanning skills directory...');

  const skillFiles = findSkillFiles(SKILLS_ROOT);
  console.log(`📁 Found ${skillFiles.length} SKILL.md files`);

  const skills: SkillManifestEntry[] = [];
  let builtinCount = 0;
  let userCount = 0;

  for (const skillPath of skillFiles) {
    const entry = parseSkillFile(skillPath);
    if (entry) {
      skills.push(entry);
      if (entry.source === 'builtin') {
        builtinCount++;
      } else {
        userCount++;
      }
    }
  }

  const manifest: SkillManifest = {
    version: '1.0.0',
    generatedAt: new Date().toISOString(),
    skills,
    totalCount: skills.length,
    builtinCount,
    userCount,
  };

  // 写入 JSON 文件
  fs.writeFileSync(OUTPUT_PATH, JSON.stringify(manifest, null, 2), 'utf-8');
  console.log(`✅ Generated ${OUTPUT_PATH}`);
  console.log(`   Total: ${manifest.totalCount} skills (${builtinCount} builtin + ${userCount} user)`);

  return manifest;
}

/**
 * CLI 入口：直接运行此文件生成 manifest
 */
if (require.main === module) {
  try {
    generateSkillManifest();
    process.exit(0);
  } catch (error) {
    console.error('❌ Failed to generate manifest:', error);
    process.exit(1);
  }
}
