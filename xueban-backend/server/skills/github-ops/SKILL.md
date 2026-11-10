---
name: github-ops
version: 1.0.0
description: Operate GitHub — browse repos, triage issues and PRs, comment, open issues, manage releases. (platform capability)
mode: report
kind: capability
domain: engineering
preview_type: markdown
allowed-tools:
  - github_request
  - github_write
  - read_file
  - write_file
triggers:
  - check this github issue
  - look at the PR
  - open a github issue
  - comment on the issue
  - github 上那个 issue
  - 看看 PR
  - 提个 issue
---
# GitHub Operations

You work with GitHub through the REST passthrough tools:

- **github_request** — GET only (repos, issues, PRs, search, releases). Works
  unauthenticated (rate-limited); GITHUB_TOKEN raises the quota.
- **github_write** — POST/PUT/PATCH/DELETE. Requires GITHUB_TOKEN and prompts
  for approval on every call.

## Workflow

1. Parse the owner/repo/ref from the user's words or URL. Confirm the repo
   when ambiguous (fork vs upstream).
2. Reads: fetch with github_request paths like
   `/repos/{owner}/{repo}/issues/{n}`,
   `/repos/{owner}/{repo}/pulls/{n}/files`,
   `/search/issues?q=repo:{owner}/{repo}+is:issue+...`. Paginate with
   `?per_page=` / `page=` when the user needs "all".
3. Writes: show the exact payload (title/body/labels) before calling
   github_write. Keep issue/PR bodies in markdown; link related numbers.
4. Surface rate-limit 403s honestly (the tool adds a note) — suggest
   GITHUB_TOKEN when unset.

## Guardrails

- Never force-push, delete repos/branches, or change settings via the API.
- Never leak the GITHUB_TOKEN value into output or files.
- Reviews: comment on what you verified; do not approve/merge on the user's
  behalf unless explicitly asked in this turn.
