# 示例 Skill — 风格指南

## 配色

- 主色 `#6366f1`（indigo-500） · 辅色 `#8b5cf6`（violet-500）
- 文字 `#0f172a`（slate-900）· 次要文字 `#475569`（slate-600）
- 背景 `#ffffff`，section 浅底用 `#f8fafc`（slate-50）

## 字体与排版

- 字体栈：`system-ui, -apple-system, "Segoe UI", sans-serif`
- 大标题 `text-5xl font-semibold tracking-tight`
- 正文 `text-base leading-relaxed`，行高 1.6 起
- 标题与正文间距：`mt-4`；section 之间 `space-y-16`

## 组件惯例

- Hero 使用 `rounded-2xl` + `bg-gradient-to-br` 渐变 + `p-16`
- 按钮 padding `px-6 py-3`，圆角 `rounded-lg`，hover 用 `transition` 和 `bg-*-50`
- Card 用 `rounded-xl bg-slate-50 p-6 space-y-2`，无投影
- 留白优于装饰：宁可 `py-16` 也不要塞满

LLM 生成网页时请遵循以上 token；颜色必须从 Tailwind 调色板挑选，不要自创 hex。
