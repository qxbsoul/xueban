/**
 * fs-browse.ts — 后端目录浏览器,2026-05-30。
 *
 * 为什么需要:浏览器拿不到本地文件夹的真实磁盘绝对路径(安全沙箱),而 CLI 的
 * cwd 需要一个真实路径。Node 后端就跑在用户本机,能读磁盘 —— 所以由后端列目录、
 * 前端逐层点选、回传绝对路径,是纯网页能拿到真路径的唯一方案。
 *
 * 只读、只列目录(不返回文件内容)。单机单用户场景,列目录是预期功能。
 *
 *   GET /api/fs/list?path=<abs>   列出 <path> 下的子目录;path 省略 → 用户 home。
 *                                  path='ROOT' → 列盘符(Windows)或 '/'(POSIX)。
 *   GET /api/fs/home              返回 { home, sep }(前端初始化用)。
 */
import express, { Router, type Request, type Response } from 'express';
import { execFile } from 'node:child_process';
import { mkdirSync, readdirSync, statSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { homedir, platform } from 'node:os';
import path from 'node:path';

const router = Router();
const IS_WIN = platform() === 'win32';

/**
 * 清洗用户粘贴的路径,2026-05-30。Windows 资源管理器「复制为路径」给的是带双引号的
 * `"D:\foo"`,拖拽/PowerShell 也可能带前后空格或单引号 —— 直接 statSync 会 NOT_FOUND。
 * 去掉首尾空白 + 成对的首尾引号。这是用户「粘贴路径读不到」的主因。
 */
function sanitizePath(raw: string): string {
  let s = raw.trim();
  if (s.length >= 2) {
    const a = s[0];
    const b = s[s.length - 1];
    if ((a === '"' && b === '"') || (a === "'" && b === "'")) s = s.slice(1, -1).trim();
  }
  return s;
}

/** Windows 盘符枚举:试 A: … Z:,可 stat 的算存在。POSIX 直接返回 '/'。 */
function listDrives(): Array<{ name: string; path: string; isDir: true }> {
  if (!IS_WIN) return [{ name: '/', path: '/', isDir: true }];
  const out: Array<{ name: string; path: string; isDir: true }> = [];
  for (let c = 65; c <= 90; c++) {
    const drive = `${String.fromCharCode(c)}:\\`;
    try {
      statSync(drive);
      out.push({ name: `${String.fromCharCode(c)}:`, path: drive, isDir: true });
    } catch {
      /* drive not present */
    }
  }
  return out;
}

/** 父目录;到达盘符根(D:\)或 POSIX 根(/)再上 → null(调用方据此回到盘符列表)。 */
function parentOf(abs: string): string | null {
  const parent = path.dirname(abs);
  if (parent === abs) return null; // already at filesystem root
  return parent;
}

router.get('/home', (_req: Request, res: Response) => {
  res.json({ data: { home: homedir(), sep: path.sep, isWindows: IS_WIN } });
});

router.get('/list', (req: Request, res: Response) => {
  const raw = typeof req.query.path === 'string' ? req.query.path : '';

  // Special sentinel → drive/root listing.
  if (raw === 'ROOT') {
    res.json({ data: { path: 'ROOT', parent: null, isRoot: true, entries: listDrives() } });
    return;
  }

  const clean = sanitizePath(raw);
  const target = clean ? path.resolve(clean) : homedir();

  let names: string[];
  try {
    const st = statSync(target);
    if (!st.isDirectory()) {
      res.status(400).json({ error: { code: 'NOT_A_DIR', message: `${target} 不是目录` } });
      return;
    }
    names = readdirSync(target);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    res.status(404).json({ error: { code: 'NOT_FOUND', message: msg, path: target } });
    return;
  }

  const entries: Array<{ name: string; path: string; isDir: boolean }> = [];
  for (const name of names) {
    if (name.startsWith('.')) continue; // hide dotfiles/dirs by default
    const full = path.join(target, name);
    try {
      if (statSync(full).isDirectory()) {
        entries.push({ name, path: full, isDir: true });
      }
    } catch {
      /* unreadable entry (permissions / broken link) — skip */
    }
  }
  entries.sort((a, b) => a.name.localeCompare(b.name));

  // parent: at a Windows drive root (D:\) dirname returns itself → expose ROOT
  // sentinel so the UI can step up to the drive list.
  let parent = parentOf(target);
  if (parent === null && IS_WIN) parent = 'ROOT';

  res.json({ data: { path: target, parent, isRoot: false, entries } });
});

/**
 * GET /api/fs/files?path=<absDir> — 列出某目录下的文件（隐藏目录与 dotfile），2026-06-24。
 *
 * 返回 200 { data: { path, files: Array<{ name, path, size, mtime, ext }> } }
 *      400 { error: { code: 'NOT_A_DIR', ... } }
 *      404 { error: { code: 'NOT_FOUND', ... } }
 */
router.get('/files', (req: Request, res: Response) => {
  const raw = typeof req.query.path === 'string' ? sanitizePath(req.query.path) : '';
  if (!raw) {
    res.status(400).json({ error: { code: 'NO_PATH', message: 'path 必填' } });
    return;
  }
  const target = path.resolve(raw);
  let names: string[];
  try {
    const st = statSync(target);
    if (!st.isDirectory()) {
      res.status(400).json({ error: { code: 'NOT_A_DIR', message: `${target} 不是目录` } });
      return;
    }
    names = readdirSync(target);
  } catch (e) {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: e instanceof Error ? e.message : String(e), path: target } });
    return;
  }
  const files: Array<{ name: string; path: string; size: number; mtime: number; ext: string }> = [];
  for (const name of names) {
    if (name.startsWith('.')) continue;
    const full = path.join(target, name);
    try {
      const st = statSync(full);
      if (st.isFile()) {
        files.push({ name, path: full, size: st.size, mtime: st.mtimeMs, ext: path.extname(name).toLowerCase() });
      }
    } catch { /* skip unreadable */ }
  }
  files.sort((a, b) => a.name.localeCompare(b.name));
  res.json({ data: { path: target, files } });
});

/**
 * GET /api/fs/read?path=<absFile>&root=<absDir> — 读文件内容（带 root 白名单守卫），2026-06-24。
 *
 * 安全要点：
 *   - `root` 必填；`path` 必须在 root 子树内（防路径穿越）。
 *   - 使用 path.resolve + path.relative 判断，不依赖字符串前缀匹配（避免大小写/符号链接欺骗）。
 *   - 文件大小上限 1MB；超出截断前 1MB 并设 truncated:true。
 *
 * 返回 200 { data: { content, size, truncated } }
 *      400 { error: { code: 'NO_ROOT', ... } }         root 未提供
 *      403 { error: { code: 'FORBIDDEN', ... } }       路径越权
 *      404 { error: { code: 'NOT_FOUND', ... } }       文件不存在
 */
const MAX_READ_BYTES = 1024 * 1024; // 1MB

function isInside(child: string, parent: string): boolean {
  const rel = path.relative(parent, child);
  // rel 为空 → child===parent（允许读单个文件直接作为 root 时）
  // rel 以 '..' 开头 → 越出父目录
  // path.isAbsolute(rel) → Windows 不同盘符时 relative 返回绝对路径
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

router.get('/read', (req: Request, res: Response) => {
  const file = typeof req.query.path === 'string' ? sanitizePath(req.query.path) : '';
  const root = typeof req.query.root === 'string' ? sanitizePath(req.query.root) : '';

  // C1: 空 path 必须在 isInside 守卫前就拒绝
  if (!file) {
    res.status(400).json({ error: { code: 'NO_PATH', message: 'path 必填' } });
    return;
  }

  if (!root) {
    res.status(400).json({ error: { code: 'NO_ROOT', message: 'root 必填（限定可读子树）' } });
    return;
  }

  // M3: 绝对路径前置校验（校验原始 file，未 resolve）
  if (!path.isAbsolute(file)) {
    res.status(400).json({ error: { code: 'NOT_ABSOLUTE', message: 'path 必须是绝对路径' } });
    return;
  }

  const absFile = path.resolve(file);
  const absRoot = path.resolve(root);

  // 第一道：resolve 后路径越权检查（防 ../.. 穿越）
  if (!isInside(absFile, absRoot)) {
    res.status(403).json({ error: { code: 'FORBIDDEN', message: '路径越权：仅允许读工作目录内文件' } });
    return;
  }

  // I1: realpathSync 跟踪符号链接真实目标，再过一次 isInside（防 symlink 越权）
  let realFile: string;
  try {
    realFile = realpathSync(absFile);
  } catch {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: '路径不存在' } });
    return;
  }
  if (!isInside(realFile, absRoot)) {
    res.status(403).json({ error: { code: 'FORBIDDEN', message: '符号链接越权' } });
    return;
  }

  let size: number;
  try {
    const st = statSync(realFile);
    if (!st.isFile()) {
      res.status(404).json({ error: { code: 'NOT_FOUND', message: '不是文件' } });
      return;
    }
    size = st.size;
  } catch (e) {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: e instanceof Error ? e.message : String(e) } });
    return;
  }
  if (size > MAX_READ_BYTES) {
    const buf = readFileSync(realFile).subarray(0, MAX_READ_BYTES).toString('utf8');
    res.json({ data: { content: buf, size, truncated: true } });
    return;
  }
  res.json({ data: { content: readFileSync(realFile, 'utf8'), size, truncated: false } });
});

/** Validate a candidate cwd: must be an existing absolute directory. */
router.get('/validate', (req: Request, res: Response) => {
  const raw = typeof req.query.path === 'string' ? sanitizePath(req.query.path) : '';
  if (!raw) {
    res.json({ data: { valid: false, reason: 'empty' } });
    return;
  }
  if (!path.isAbsolute(raw)) {
    res.json({ data: { valid: false, reason: 'not-absolute', path: raw } });
    return;
  }
  try {
    const ok = statSync(raw).isDirectory();
    res.json({ data: { valid: ok, reason: ok ? null : 'not-a-dir', path: path.resolve(raw) } });
  } catch {
    res.json({ data: { valid: false, reason: 'not-found', path: raw } });
  }
});

/**
 * GET /api/fs/pick-native — 弹出操作系统原生文件夹选择对话框,2026-05-30。
 *
 * 浏览器拿不到系统弹窗的真实路径(安全沙箱),但 daemon 跑在用户本机,可以替前端
 * 弹一个真实的 OS 文件夹对话框、把用户选中的绝对路径回传 —— 比粘贴路径更顺手。
 *   - Windows:PowerShell `-STA` + `System.Windows.Forms.FolderBrowserDialog`
 *   - macOS:`osascript`(choose folder)
 *   - Linux:`zenity --file-selection --directory`(装了 zenity 才有)
 * 返回 `{ data: { path } }`(选了)/ `{ data: { cancelled: true } }`(取消)/
 * 503 `{ error: UNSUPPORTED }`(工具缺失)。对话框弹在 daemon 所在机器(=用户本机)。
 */
router.get('/pick-native', (_req: Request, res: Response) => {
  const os = platform();
  let cmd: string;
  let args: string[];
  if (os === 'win32') {
    // 2026-08-20 换现代资源管理器风格选夹器(Win10/11 Vista-style common dialog,
    // 带快速访问/地址栏/搜索),替代旧版 FolderBrowserDialog(树形窗,用户嫌弃);
    // 同日二次迭代:对话框默认偏矮 → ShowDialog 期间用 WinForms Timer 找到
    // 对话框窗口(#32770 + 标题匹配)把尺寸拉满工作区(「到底」),每次打开都是大窗。
    // 原理:WPF OpenFileDialog 本身就是现代对话框;ValidateNames=false +
    // CheckFileExists=false + 假文件名「选中当前文件夹」→ 用户导航到目标目录
    // 点「打开」,GetDirectoryName(FileName) 即所选目录(经典 COM 替代法)。
    // OutputEncoding=UTF8 保证中文路径回传 Node 不乱码。
    // -STA 必须:对话框需单线程单元;-NoProfile 加速且避免配置干扰。
    const ps = `
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;
Add-Type -AssemblyName System.Windows.Forms;
Add-Type -AssemblyName PresentationFramework;
Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public class W32{[DllImport("user32.dll",CharSet=CharSet.Unicode)]public static extern IntPtr FindWindow(string cls,string title);[DllImport("user32.dll")]public static extern bool SetWindowPos(IntPtr h,IntPtr a,int x,int y,int cx,int cy,uint f);}';
$d = New-Object Microsoft.Win32.OpenFileDialog;
$d.Title = 'Xueban - 选择文件夹';
$d.ValidateNames = $false;
$d.CheckFileExists = $false;
$d.CheckPathExists = $true;
$d.FileName = '选中当前文件夹';
$t = New-Object System.Windows.Forms.Timer;
$t.Interval = 40;
$t.Add_Tick({
  $h = [W32]::FindWindow('#32770', $d.Title);
  if ($h -ne [IntPtr]::Zero) {
    $wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea;
    [void][W32]::SetWindowPos($h, [IntPtr]::Zero, $wa.X, $wa.Y, $wa.Width, $wa.Height, 0x0014);
    $t.Stop();
  }
});
$t.Start();
$null = $d.ShowDialog();
$t.Stop();
if ($d.FileName) { [Console]::Out.Write([System.IO.Path]::GetDirectoryName($d.FileName)) }
`;
    cmd = 'powershell.exe';
    args = ['-NoProfile', '-STA', '-Command', ps];
  } else if (os === 'darwin') {
    cmd = 'osascript';
    args = ['-e', 'try', '-e', 'POSIX path of (choose folder with prompt "选择文件夹")', '-e', 'on error', '-e', 'return ""', '-e', 'end try'];
  } else {
    cmd = 'zenity';
    args = ['--file-selection', '--directory', '--title=选择文件夹'];
  }

  // 给用户足够时间操作(最多 5 分钟)。execFile 不走 shell,避免注入。
  execFile(cmd, args, { timeout: 300_000, windowsHide: false }, (err, stdout) => {
    const picked = (stdout || '').trim();
    if (picked) {
      res.json({ data: { path: picked } });
      return;
    }
    if (err && (err as NodeJS.ErrnoException).code === 'ENOENT') {
      res.status(503).json({
        error: { code: 'UNSUPPORTED', message: `本机没有可用的原生文件夹对话框(${cmd} 缺失)` },
      });
      return;
    }
    res.json({ data: { cancelled: true } });
  });
});

/**
 * GET /api/fs/raw?path=<absFile>&root=<absDir> — 原样返回文件字节（带 root 白名单守卫）。
 *
 * Workbench FileViewer 预览图片（<img src>）/ WorkspacePane 预览 .html（iframe）需要直链
 * 拿到字节流，read 接口只回 utf8 文本不行。复用 read 接口同款 root 守卫 + symlink 校验，
 * 按扩展名设 Content-Type，限 8MB。
 */
const MAX_RAW_BYTES = 8 * 1024 * 1024; // 8MB
const RAW_MIME: Record<string, string> = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
  '.webp': 'image/webp', '.bmp': 'image/bmp', '.ico': 'image/x-icon', '.svg': 'image/svg+xml',
  '.avif': 'image/avif', '.pdf': 'application/pdf', '.html': 'text/html; charset=utf-8',
  '.htm': 'text/html; charset=utf-8',
};

router.get('/raw', (req: Request, res: Response) => {
  const file = typeof req.query.path === 'string' ? sanitizePath(req.query.path) : '';
  const root = typeof req.query.root === 'string' ? sanitizePath(req.query.root) : '';

  if (!file) {
    res.status(400).json({ error: { code: 'NO_PATH', message: 'path 必填' } });
    return;
  }
  if (!root) {
    res.status(400).json({ error: { code: 'NO_ROOT', message: 'root 必填（限定可读子树）' } });
    return;
  }
  if (!path.isAbsolute(file)) {
    res.status(400).json({ error: { code: 'NOT_ABSOLUTE', message: 'path 必须是绝对路径' } });
    return;
  }

  const absFile = path.resolve(file);
  const absRoot = path.resolve(root);
  if (!isInside(absFile, absRoot)) {
    res.status(403).json({ error: { code: 'FORBIDDEN', message: '路径越权：仅允许读工作目录内文件' } });
    return;
  }

  let realFile: string;
  try {
    realFile = realpathSync(absFile);
  } catch {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: '路径不存在' } });
    return;
  }
  if (!isInside(realFile, absRoot)) {
    res.status(403).json({ error: { code: 'FORBIDDEN', message: '符号链接越权' } });
    return;
  }

  let size: number;
  try {
    const st = statSync(realFile);
    if (!st.isFile()) {
      res.status(404).json({ error: { code: 'NOT_FOUND', message: '不是文件' } });
      return;
    }
    size = st.size;
  } catch (e) {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: e instanceof Error ? e.message : String(e) } });
    return;
  }
  if (size > MAX_RAW_BYTES) {
    res.status(413).json({ error: { code: 'TOO_LARGE', message: '文件超过 8MB，无法预览' } });
    return;
  }
  const ext = path.extname(realFile).toLowerCase();
  res.setHeader('Content-Type', RAW_MIME[ext] ?? 'application/octet-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.send(readFileSync(realFile));
});

/**
 * POST /api/fs/write — 在工作空间根内写文本文件（新建/覆盖），2026-08-22。
 *
 * 背景：fs 原来只有 list/read/raw 只读接口，工作空间无法落文件（T04 赛道要把
 * 企业资料喂进工作空间）。与 read/raw 同款 root 白名单守卫：
 *   - root 必填；path 可绝对（须在 root 子树内）或相对（相对 root 解析）。
 *   - 任何 '..' 路径段一律 400（拒绝穿越，不做「解析回来还在 root 内」的放行）。
 *   - 父目录不存在时递归创建（创建后也校验 realpath，防 symlink 目录越权）。
 *   - 目标已存在且是 symlink → realpath 复核，越出 root 拒绝。
 *   - 内容上限 1MB（对齐全局 express.json limit）。
 *
 * 入参 { path, content, root }
 * 返回 200 { data: { path, size } }
 *      400 NO_PATH / NO_ROOT / TRAVERSAL / BAD_CONTENT
 *      403 FORBIDDEN   413 TOO_LARGE
 */
const MAX_WRITE_BYTES = 1024 * 1024; // 1MB
const MAX_UPLOAD_BYTES = 8 * 1024 * 1024; // 8MB，对齐 raw 预览上限

/** 路径任一段为 '..' → true（写入侧一律拒绝，不做解析后放行）。 */
function hasTraversal(p: string): boolean {
  return p.split(/[\\/]+/).some((seg) => seg === '..');
}

/**
 * 写目标解析 + 全量守卫。合法时返回绝对路径；不合法时已写好响应、返回 null。
 * 供 /write 与 /upload 共用。
 */
function resolveWriteTarget(
  res: Response,
  rawPath: string,
  rawRoot: string,
): string | null {
  const file = sanitizePath(rawPath);
  const root = sanitizePath(rawRoot);

  if (!file) {
    res.status(400).json({ error: { code: 'NO_PATH', message: 'path 必填' } });
    return null;
  }
  if (!root) {
    res.status(400).json({ error: { code: 'NO_ROOT', message: 'root 必填（限定可写子树）' } });
    return null;
  }
  if (hasTraversal(file)) {
    res.status(400).json({ error: { code: 'TRAVERSAL', message: "路径不允许包含 '..'" } });
    return null;
  }

  const absRoot = path.resolve(root);
  const absFile = path.isAbsolute(file) ? path.resolve(file) : path.resolve(absRoot, file);

  if (!isInside(absFile, absRoot)) {
    res.status(403).json({ error: { code: 'FORBIDDEN', message: '路径越权：仅允许写工作目录内文件' } });
    return null;
  }

  // 目标已存在且是 symlink → 跟踪真实目标再复核（防 symlink 越权覆盖）。
  try {
    if (statSync(absFile).isDirectory()) {
      res.status(400).json({ error: { code: 'IS_A_DIR', message: '目标是目录' } });
      return null;
    }
    if (!isInside(realpathSync(absFile), absRoot)) {
      res.status(403).json({ error: { code: 'FORBIDDEN', message: '符号链接越权' } });
      return null;
    }
  } catch {
    /* 不存在 → 新建，下面校验父目录 */
  }

  // 父目录可能不存在：先递归创建（已在 root 内），再 realpath 复核父链无 symlink 越权。
  try {
    mkdirSync(path.dirname(absFile), { recursive: true });
  } catch (e) {
    res.status(400).json({ error: { code: 'MKDIR_FAILED', message: e instanceof Error ? e.message : String(e) } });
    return null;
  }
  try {
    if (!isInside(realpathSync(path.dirname(absFile)), absRoot)) {
      res.status(403).json({ error: { code: 'FORBIDDEN', message: '符号链接越权' } });
      return null;
    }
  } catch (e) {
    res.status(400).json({ error: { code: 'MKDIR_FAILED', message: e instanceof Error ? e.message : String(e) } });
    return null;
  }

  return absFile;
}

router.post('/write', (req: Request, res: Response) => {
  const body = req.body as { path?: unknown; content?: unknown; root?: unknown } | undefined;
  const rawPath = typeof body?.path === 'string' ? body.path : '';
  const rawRoot = typeof body?.root === 'string' ? body.root : '';
  const content = body?.content;

  if (typeof content !== 'string') {
    res.status(400).json({ error: { code: 'BAD_CONTENT', message: 'content 必填且须为字符串' } });
    return;
  }
  if (Buffer.byteLength(content, 'utf8') > MAX_WRITE_BYTES) {
    res.status(413).json({ error: { code: 'TOO_LARGE', message: '内容超过 1MB' } });
    return;
  }

  const absFile = resolveWriteTarget(res, rawPath, rawRoot);
  if (!absFile) return;

  writeFileSync(absFile, content, 'utf8');
  res.json({ data: { path: absFile, size: statSync(absFile).size } });
});

/** multipart 单文件解析结果。 */
interface MultipartParts {
  fields: Record<string, string>;
  file: { filename: string; data: Buffer } | null;
}

/**
 * 极简 multipart/form-data 解析（本项目不引 multer：server 零新增依赖约定 +
 * 单文件上传场景足够）。按 boundary 切 buffer，逐段拆 header/body；
 * filename*=UTF-8'' 与裸 UTF-8 filename 两种浏览器编码都认。
 */
function parseMultipart(buf: Buffer, boundary: string): MultipartParts {
  const out: MultipartParts = { fields: {}, file: null };
  const marker = Buffer.from(`--${boundary}`);
  let start = buf.indexOf(marker);
  while (start >= 0) {
    let partStart = start + marker.length;
    if (buf[partStart] === 0x2d && buf[partStart + 1] === 0x2d) break; // '--' 结束标记
    if (buf[partStart] === 0x0d && buf[partStart + 1] === 0x0a) partStart += 2; // CRLF
    const next = buf.indexOf(marker, partStart);
    if (next < 0) break;
    let part = buf.subarray(partStart, next);
    if (part.length >= 2 && part[part.length - 2] === 0x0d && part[part.length - 1] === 0x0a) {
      part = part.subarray(0, part.length - 2);
    }
    const headerEnd = part.indexOf('\r\n\r\n');
    if (headerEnd >= 0) {
      const headerText = part.subarray(0, headerEnd).toString('utf8');
      const content = part.subarray(headerEnd + 4);
      const dispo = /content-disposition:\s*form-data;([^\r\n]*)/i.exec(headerText)?.[1] ?? '';
      const name = /(?:^|;)\s*name="([^"]*)"/.exec(dispo)?.[1] ?? '';
      const star = /filename\*=(?:UTF-8|utf-8)''([^;\s]*)/.exec(dispo)?.[1];
      const plain = /(?:^|;)\s*filename="([^"]*)"/.exec(dispo)?.[1];
      let filename: string | undefined;
      if (star) {
        try { filename = decodeURIComponent(star); } catch { filename = star; }
      } else {
        filename = plain;
      }
      if (filename) out.file = { filename, data: content };
      else if (name) out.fields[name] = content.toString('utf8');
    }
    start = next;
  }
  return out;
}

/**
 * POST /api/fs/upload — multipart 上传文件到工作空间根内目录，2026-08-22。
 *
 * 表单字段：dir（目标目录，绝对路径，须在 root 子树内）、root（白名单根）、
 * file（单文件）。文件名只取 basename 语义——含 '/'、'\\'、'..' 一律 400，
 * 不做静默改名。已存在同名文件则覆盖。上限 8MB。
 *
 * 返回 200 { data: { path, name, size } }
 *      400 NO_FILE / NO_ROOT / TRAVERSAL / INVALID_NAME / BAD_MULTIPART
 *      403 FORBIDDEN   413 TOO_LARGE
 */
router.post(
  '/upload',
  express.raw({ type: 'multipart/form-data', limit: MAX_UPLOAD_BYTES + 1024 * 1024 }),
  (req: Request, res: Response) => {
    const ct = String(req.headers['content-type'] ?? '');
    const boundaryMatch = /boundary=(?:"([^"]+)"|([^;\s]+))/i.exec(ct);
    const boundary = boundaryMatch?.[1] ?? boundaryMatch?.[2];
    const buf = req.body as Buffer | undefined;
    if (!boundary || !Buffer.isBuffer(buf)) {
      res.status(400).json({ error: { code: 'BAD_MULTIPART', message: '缺少 multipart boundary 或 body' } });
      return;
    }

    const { fields, file } = parseMultipart(buf, boundary);
    if (!file) {
      res.status(400).json({ error: { code: 'NO_FILE', message: '缺少文件字段 file' } });
      return;
    }
    if (file.data.length > MAX_UPLOAD_BYTES) {
      res.status(413).json({ error: { code: 'TOO_LARGE', message: '文件超过 8MB' } });
      return;
    }

    // 文件名守卫：拒绝任何带路径语义的文件名（防穿越，不静默 basename）。
    if (/[\\/]/.test(file.filename) || file.filename.includes('..') || file.filename.trim() === '') {
      res.status(400).json({ error: { code: 'INVALID_NAME', message: '文件名不合法（含路径分隔符或 ..）' } });
      return;
    }

    const dir = typeof fields.dir === 'string' ? fields.dir : '';
    const root = typeof fields.root === 'string' ? fields.root : '';
    if (!dir) {
      res.status(400).json({ error: { code: 'NO_PATH', message: 'dir 必填' } });
      return;
    }
    if (!root) {
      res.status(400).json({ error: { code: 'NO_ROOT', message: 'root 必填（限定可写子树）' } });
      return;
    }
    if (hasTraversal(dir)) {
      res.status(400).json({ error: { code: 'TRAVERSAL', message: "dir 不允许包含 '..'" } });
      return;
    }
    if (!path.isAbsolute(dir)) {
      res.status(400).json({ error: { code: 'NOT_ABSOLUTE', message: 'dir 必须是绝对路径' } });
      return;
    }

    const absRoot = path.resolve(sanitizePath(root));
    const absDir = path.resolve(sanitizePath(dir));
    if (!isInside(absDir, absRoot)) {
      res.status(403).json({ error: { code: 'FORBIDDEN', message: '路径越权：仅允许写工作目录内' } });
      return;
    }
    try {
      if (!statSync(absDir).isDirectory()) {
        res.status(400).json({ error: { code: 'NOT_A_DIR', message: `${absDir} 不是目录` } });
        return;
      }
      if (!isInside(realpathSync(absDir), absRoot)) {
        res.status(403).json({ error: { code: 'FORBIDDEN', message: '符号链接越权' } });
        return;
      }
    } catch {
      res.status(404).json({ error: { code: 'NOT_FOUND', message: '目标目录不存在' } });
      return;
    }

    const target = path.join(absDir, file.filename);
    // 目标已存在且是 symlink → 复核真实目标仍越权才拒（覆盖普通文件是预期语义）。
    try {
      if (!isInside(realpathSync(target), absRoot)) {
        res.status(403).json({ error: { code: 'FORBIDDEN', message: '符号链接越权' } });
        return;
      }
    } catch {
      /* 不存在 → 新建 */
    }

    writeFileSync(target, file.data);
    res.json({ data: { path: target, name: file.filename, size: file.data.length } });
  },
);

/**
 * GET /api/fs/open-native?path=<absFile> — 用系统默认程序打开文件（Open-in-editor）。
 *
 * Workbench FileViewer 的「在编辑器中打开」按钮 + 二进制兜底用。daemon 跑在用户本机，
 * 调 OS 的「打开」命令交给默认程序（编辑器/看图/PDF 阅读器）。只读打开，不写文件。
 */
router.get('/open-native', (req: Request, res: Response) => {
  const file = typeof req.query.path === 'string' ? sanitizePath(req.query.path) : '';
  if (!file || !path.isAbsolute(file)) {
    res.status(400).json({ error: { code: 'NO_PATH', message: 'path 必填且须为绝对路径' } });
    return;
  }
  const abs = path.resolve(file);
  try {
    if (!statSync(abs).isFile()) {
      res.status(404).json({ error: { code: 'NOT_FOUND', message: '不是文件' } });
      return;
    }
  } catch {
    res.status(404).json({ error: { code: 'NOT_FOUND', message: '路径不存在' } });
    return;
  }

  const os = platform();
  let cmd: string;
  let args: string[];
  if (os === 'win32') {
    cmd = 'cmd.exe';
    args = ['/c', 'start', '', abs];
  } else if (os === 'darwin') {
    cmd = 'open';
    args = [abs];
  } else {
    cmd = 'xdg-open';
    args = [abs];
  }

  execFile(cmd, args, { timeout: 15_000, windowsHide: true }, (err) => {
    if (err && (err as NodeJS.ErrnoException).code === 'ENOENT') {
      res.status(503).json({ error: { code: 'UNSUPPORTED', message: `本机没有可用的打开命令(${cmd} 缺失)` } });
      return;
    }
    res.json({ data: { opened: true } });
  });
});

export { router as fsBrowseRouter };
export default router;
