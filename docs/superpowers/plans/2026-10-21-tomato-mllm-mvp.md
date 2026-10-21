# Tomato MLLM MVP Implementation Plan

> **For agentic workers:** Use executing-plans to implement the tasks inline, with verification at each checkpoint. User has requested implementation and selected Qwen; proceed in this session.

**Goal:** Deliver a native WeChat mini program that uploads a tomato photo to a configurable Qwen-VL backend and displays structured preliminary findings.

**Architecture:** FastAPI accepts a multipart image and optional description, verifies and normalizes the photo, then calls Qwen's compatible chat completions API. The native mini program owns selection, upload, result and retry states. No image persistence or database.

**Tech Stack:** Python >=3.11, FastAPI, HTTPX, Pillow, Pydantic Settings, uv, native WeChat JavaScript/WXML/WXSS, pytest, Node test runner.

**Spec:** `docs/superpowers/specs/2026-10-21-tomato-mllm-mvp-design.md`

## Global Constraints

- 原图最大 8 MiB、最多 2500 万像素；支持实际 JPEG/PNG/WebP；分析图最长边 1600 像素。
- 模型请求超时 60 秒；小程序上传请求超时 90 秒。
- All Qwen credentials stay on the backend. No confidence percentages, mock diagnoses in production, automatic paid retries, or photo persistence.
- Four result states: `suspected_disease`, `healthy`, `uncertain`, `not_tomato`.
- No public deployment in this task. No existing Git repository; leave source and documents reviewable in the workspace.

## File Map

- `backend/pyproject.toml`, `.env.example`: reproducible runtime and Qwen configuration.
- `backend/app/{settings,schemas,errors,images,qwen,main}.py`: configuration, typed contract, error contract, image processing, provider, API.
- `backend/tests/test_api.py`: uploaded-image to simulated-provider integration tests.
- `miniprogram/{app.js,app.json,app.wxss,config.js,sitemap.json}`: native application and development API origin.
- `miniprogram/services/diagnosis.js`: upload transport and stable error handling.
- `miniprogram/pages/index/index.{js,json,wxml,wxss}`: MVP interface and page state.
- `tests/miniprogram.test.cjs`: transport and page behavior with WeChat API doubles.
- `project.config.json`: import the repository directly into WeChat developer tools.
- `README.md`: setup, endpoint contract, development networking and verification limits.

## Task 1: Verified Image-to-Qwen API

**Interfaces:** `create_app(settings=None, transport=None) -> FastAPI`; `prepare_image(data: bytes, settings: Settings) -> PreparedImage`; `QwenClient.diagnose(image, description) -> DiagnosisResult`.

- [x] Create the project configuration and integration tests. The successful request must include the actual uploaded image as a JPEG Data URL, a system JSON contract, and the supplied description.
- [x] Implement `Settings`, `DiagnosisResult`, `APIError`, image validation, Qwen HTTP adapter and FastAPI routes.
- [x] Assert file limits, image decoding failures, blank credentials, upstream 401/429/5xx/timeouts, invalid output and result status validation produce stable errors without raw upstream text.
- [x] Install dependencies and run:

```powershell
Set-Location backend
uv sync
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

## Task 2: Native Mini Program Upload and Results

**Consumes:** multipart `POST /api/diagnose` with `image` and `description`; successful JSON `{request_id, model, elapsed_ms, result, notice}`; error JSON `{error: {code, message}}`.

**Produces:** `uploadDiagnosis(options) -> {promise, abort}` and an index page that controls photo selection, input, upload, error, retry and result states.

- [x] Implement the native page, application configuration and upload service. Use `wx.chooseMedia`, `wx.uploadFile`, `wx.previewImage`; selected images stay on device until the user clicks recognize.
- [x] Retain the selected image and description on errors so the user can retry; disable selection and duplicate submission while processing; cancel on unload.
- [x] Validate response shapes before rendering. Show no previous result after selecting a new photo or editing its description.
- [x] Add transport/page tests for success, provider errors, network timeout, malformed response, cancellation and duplicate prevention.
- [x] Run:

```powershell
node --test tests/miniprogram.test.cjs
node --check miniprogram/pages/index/index.js
node --check miniprogram/services/diagnosis.js
```

## Task 3: Setup and Final Verification

- [x] Write concrete PowerShell startup commands and document key/model/base-URL configuration, WeChat import, localhost vs phone networking, HTTPS and domain settings.
- [x] Start Uvicorn and verify `/health`, `/openapi.json` and invalid upload over actual HTTP. Do not make a paid request without configured credentials.
- [x] Re-run all checks once after the final edits; record passed commands and outstanding live-provider / WeChat-device verification in `README.md`.
