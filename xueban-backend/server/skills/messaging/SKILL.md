---
name: messaging
version: 1.0.0
description: Chat and email — list conversations, read history, send messages and emails through configured bridges. (platform capability)
mode: report
kind: capability
domain: comms
preview_type: markdown
allowed-tools:
  - im_read
  - im_send
  - send_email
triggers:
  - read my chats
  - send a message to
  - send an email
  - check this conversation
  - 发消息
  - 发邮件
  - 看看聊天记录
---
# Messaging & Email

You read chat/messaging history and send messages or emails through the
platform bridges:

- **im_read** — `list_conversations` for the chat list, `read_messages` for
  one conversation's history (needs `conversation_id`).
- **im_send** — send text to one conversation. Side-effecting: the tool
  prompts for approval.
- **send_email** — email with optional base64 attachments. Side-effecting:
  prompts for approval.

## Workflow

1. For "check X" requests: im_read first, summarize findings with times and
   participants; quote only what answers the question.
2. For sends: draft the exact text/subject and show it to the user BEFORE the
   tool call. Keep the user's voice and language; never add claims they did
   not approve.
3. Emails: plain text by default; use html only for real formatting needs;
   attach only files the user named.

## Guardrails

- No provider configured → tools return a stub and NOTHING is sent. Report
  the stub note and the env vars to configure (SF_IM_API_BASE /
  SF_MAIL_API_BASE or RESEND_API_KEY + SF_MAIL_FROM) — never claim delivery.
- Never send to recipients the user did not name; re-confirm when the address
  list is ambiguous.
- Private content you read stays in this conversation; do not echo unrelated
  history into a reply.
