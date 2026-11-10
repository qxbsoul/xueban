---
name: app-deploy
version: 1.0.0
description: Deploy and publish web apps and mini-programs — build, package, ship to the configured hosting target. (platform workflow)
mode: report
kind: workflow
domain: ops
preview_type: markdown
allowed-tools:
  - shell_exec
  - read_file
  - write_file
  - glob_files
  - fetch_url
triggers:
  - deploy this site
  - publish the app
  - ship it
  - 部署
  - 发布
  - 上线
---
# App Deploy

You take a finished web app / static site / mini-program project through
build → package → publish on the hosting target configured for this
environment (CLI or HTTP bridge).

## Workflow

1. **Pre-flight**: confirm the project entry (package.json / manifest), the
   target (staging vs production), and that tests/build pass locally. Never
   deploy a red build.
2. **Build** via shell_exec (`npm run build` / project equivalent). Surface
   build errors verbatim; fix and rebuild rather than shipping partial output.
3. **Package**: static sites → build dir; mini-programs → the platform upload
   package; services → container image when a Dockerfile exists.
4. **Publish** through the configured target CLI/bridge (e.g. `sites deploy`
   style CLI, `docker push` + release hook, or platform upload tool). Record
   the release URL / version id.
5. **Verify**: fetch_url the published URL and check it responds with the new
   content; report URL + version to the user.

## Guardrails

- Production publishes are side-effecting: state the target and get a yes
  before the publish step.
- Never commit secrets into build output or images; scrub .env files.
- Rollback plan first: know the previous version id before replacing it, and
  offer the rollback command in your report.
- No target configured → report exactly which CLI/bridge is missing instead
  of improvising a host.
