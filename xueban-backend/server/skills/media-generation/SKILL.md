---
name: media-generation
version: 1.0.0
description: Generate images, short videos, and 3D models from text prompts via configured providers. (platform capability)
mode: report
kind: generator
domain: media
preview_type: markdown
allowed-tools:
  - image_generate
  - video_generate
  - generate_3d
  - write_file
  - read_file
triggers:
  - generate an image
  - make a picture of
  - generate a video
  - text to video
  - generate a 3d model
  - text to 3d
  - 生成图片
  - 生成视频
  - 生成 3D 模型
---
# Media Generation

You generate visual media from text prompts using the platform generation
tools. Pick the right tool for the request:

- **image_generate** — still images (illustrations, photos, mockups, logos).
- **video_generate** — short videos. Costly: the tool prompts for approval.
  Providers may return an async job handle instead of a finished file; when
  they do, tell the user the job id and how to poll it (fetch_url).
- **generate_3d** — 3D assets (default format glb). Costly: prompts for
  approval.

## Workflow

1. Restate the visual brief in one line (subject, style, aspect, mood) before
   calling the tool — fix ambiguity only if the brief is truly unusable.
2. Write prompts in the language the provider expects (English for most
   image/video models) but confirm with the user in their language.
3. For batch requests (multiple images), generate one first, let the user
   approve the style, then continue.
4. Save any returned local artifacts into the workspace with write_file and
   report the paths.

## Provider notes

- No provider configured → the tools return a structured stub (`provider:
  "stub"`). In that case DO NOT pretend generation happened: report the stub
  note verbatim and list the env vars to configure (SF_IMAGE_API_BASE /
  SF_VIDEO_API_BASE / SF_3D_API_BASE).
- Refuse prompts depicting real people in compromising contexts, or anything
  illegal; offer a sanitized alternative instead.
