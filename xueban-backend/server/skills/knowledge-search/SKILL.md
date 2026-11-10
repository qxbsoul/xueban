---
name: knowledge-search
version: 1.0.0
description: Retrieve knowledge from the workspace knowledge base and the web — cite sources, compare answers. (platform capability)
mode: report
kind: capability
domain: research
preview_type: markdown
allowed-tools:
  - read_file
  - grep
  - glob_files
  - list_dir
  - web_search
  - fetch_url
triggers:
  - search the knowledge base
  - what do we know about
  - look this up
  - find references on
  - 查一下资料
  - 知识库里有没有
  - 检索
---
# Knowledge Search

You retrieve and synthesize information from two layers, and always say which
layer an answer came from:

1. **Workspace knowledge** — documents in the workspace: glob_files for
   candidates, grep for keywords, read_file for the relevant sections.
2. **Web** — web_search (recency/source filters when the question is
   time-bound; `source: "wechat"` for 微信公众号 content), then fetch_url to
   open the top hits and read past the snippet.

## Workflow

1. Restate the question as 2-4 searchable phrasings (EN + 中文 when useful).
2. Workspace first (it is cheaper and more authoritative for project facts);
   fall back to web for current events / external facts.
3. Answer with citations: file paths for workspace sources, URLs for web
   sources. Mark anything uncertain as unverified.
4. When sources conflict, present both and say which is more recent or
   better-sourced — do not silently pick one.

## Guardrails

- web_search stub ("no provider configured") means NO real search happened —
  say so; never answer such a turn from memory as if it were searched.
- WeChat 公众号 search needs SERPAPI_API_KEY; note the fallback when the tool
  reports one.
