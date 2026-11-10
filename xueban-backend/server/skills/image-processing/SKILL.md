---
name: image-processing
version: 1.0.0
description: Edit existing images — watermark removal, restoration, matting, beautification, format conversion. (platform capability)
mode: report
kind: capability
domain: media
preview_type: markdown
allowed-tools:
  - read_file
  - write_file
  - list_dir
  - glob_files
  - shell_exec
triggers:
  - remove watermark
  - restore this photo
  - remove background
  - matting
  - beautify photo
  - upscale image
  - 去水印
  - 修复照片
  - 抠图
  - 美颜
  - 图片放大
---
# Image Processing

You edit raster images that already exist in the workspace: watermark/text
removal, old-photo restoration, background matting, beautification, upscaling
and format conversion.

## Workflow

1. Locate the source image (glob_files / list_dir). Confirm which file if
   several match; never silently pick one.
2. Choose the operation:
   - **watermark / unwanted object removal** — inpainting over the marked area.
   - **restoration** — denoise + scratch removal + colorization if asked.
   - **matting** — background removal; keep alpha when saving PNG/WebP.
   - **beautification** — face retouch; keep it subtle, preserve identity.
   - **upscale** — 2x/4x super-resolution.
3. Run the pipeline as a python imaging script via shell_exec (Pillow /
   opencv / rembg are the expected stack). Write scripts into the workspace
   (write_file) rather than inline one-liners, so the user can re-run them.
4. Save outputs as NEW files (`*_edited.png` etc.) — never overwrite the
   original. Report output paths and dimensions.

## Guardrails

- shell_exec is high-risk: only run imaging commands you wrote yourself in
  this turn; never run user-supplied shell strings verbatim.
- Refuse requests that alter images deceptively (fake documents, deepfakes of
  real people, forged evidence).
- If required python packages are missing, install them with the project
  package manager or report the exact missing package — do not guess APIs.
