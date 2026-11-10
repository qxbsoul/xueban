---
name: browser-automation
version: 1.0.0
description: Drive a real browser via Playwright — fill forms, click flows, screenshots, scraping, E2E checks. (platform capability)
mode: report
kind: capability
domain: web
preview_type: markdown
allowed-tools:
  - shell_exec
  - read_file
  - write_file
  - list_dir
  - fetch_url
triggers:
  - automate the browser
  - fill this form
  - take a screenshot of
  - scrape this page
  - e2e test
  - 浏览器自动化
  - 填表
  - 网页截图
---
# Browser Automation

You drive a real browser with Playwright scripts (run via shell_exec) for UI
flows that fetch_url cannot handle: logins, forms, SPAs, screenshots, E2E.

## Workflow

1. Confirm the target URL and the exact steps (what to click / type / assert).
   For anything that submits data to a third party, get explicit user
   confirmation first.
2. Write a self-contained Playwright script into the workspace (write_file),
   run it with shell_exec, headless by default.
3. Capture artifacts: screenshots and console logs saved to the workspace;
   report paths. On failure, include the failing selector and last screenshot.
4. Keep selectors robust (role/text based when possible) and add a short
   retry for flaky waits (`expect(locator).toBeVisible()` style).

## Guardrails

- No credential harvesting: never ask for or log passwords; if a flow needs
  login, have the user fill it in a headed run or use their stored session.
- Respect robots/site terms for scraping; rate-limit requests.
- Do not run scripts downloaded from the web — author every script yourself.
