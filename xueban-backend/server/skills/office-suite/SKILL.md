---
name: office-suite
version: 1.0.0
description: Create and edit Word / Excel / PowerPoint documents, analyze table data, beautify document layouts. (platform capability)
mode: report
kind: generator
domain: content
preview_type: markdown
allowed-tools:
  - read_file
  - write_file
  - edit_file
  - glob_files
  - list_dir
  - shell_exec
triggers:
  - make a ppt
  - create a spreadsheet
  - write a word document
  - edit this xlsx
  - analyze this table
  - beautify this document
  - 做个 PPT
  - 生成 excel
  - 写个 word
  - 表格分析
  - 文档美化
---
# Office Suite

You produce and edit Office deliverables: `.docx` (Word), `.xlsx` (Excel),
`.pptx` (PowerPoint) — plus CSV/table analysis and document layout polish.

## Toolchain

Drive python libraries through shell_exec scripts written into the workspace:
- `.docx` → python-docx
- `.xlsx` → openpyxl (formulas, styles, charts)
- `.pptx` → python-pptx
- table analysis → pandas; charts → matplotlib (embed as images)

Write each script to a file (write_file) and run it with shell_exec. Never
paste multi-line python as shell one-liners.

## Workflow

1. Restate deliverable type, audience, and outline; for long documents get
   the outline approved before generating.
2. Generate to a new file in the workspace; never overwrite a source file —
   copy-then-edit for "edit this document" requests (read the source first
   and preserve its structure/styles).
3. Recompute formula-heavy spreadsheets after writing and sanity-check row
   counts / totals against the source data.
4. Report the output path and a 3-5 bullet summary of the structure.

## Quality bar

- Tables: align numeric columns right, format thousands separators, freeze
  header rows in xlsx.
- Slides: one idea per slide, ≤6 bullets, ≤12 words per bullet.
- Documents: real heading styles (not bold paragraphs) so navigation works.
- Chinese documents: 宋体/黑体/等线 pairing; never invent statutory wording —
  quote legal text verbatim from provided sources.
