# WeChat Content Security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Gate tomato diagnosis and result display on genuine WeChat content moderation.

**Architecture:** A server-side WeChat client and encrypted callback verifier feed bounded ephemeral diagnosis jobs. The mini program logs in, uploads and polls; only fully checked results retain the existing response schema.

**Tech Stack:** Python 3.11+, FastAPI, HTTPX, Pillow, cryptography; native WeChat JS/WXML/WXSS and Node tests.

**Spec:** `docs/superpowers/specs/2026-10-21-wechat-content-security-design.md`

**Execution status:** Code tasks are complete. Backend 252 tests plus Ruff, frontend 39 tests, and native WXML/WXSS checks passed. Real HTTPS, WeChat callback, and device integration remain unconfigured and unverified; no live deployment or approval is implied.

## Global Constraints

- Preserve the user's uncommitted `miniprogram/config.js`; do not edit actual `.env`, expose secrets, make paid model calls, deploy or push.
- Only explicit v2 `errcode=0` and `result.suggest=pass` permit content release. `review` and `risky` use HTTP 422 `CONTENT_REJECTED` with exact message `您提交的内容含违规信息。`. Unknown/error results fail closed using `SECURITY_UNAVAILABLE`.
- Check input description, normalized image and all model-generated text. No unsafe content, labels, keywords, raw upstream errors or credentials in public errors/logs.
- Retain the existing diagnosis schema, Qwen model configuration and image limits. No automatic paid-model retry.
- Use a bounded process-local store, one Uvicorn worker/instance. Jobs live 35 minutes, total capacity 100, pending capacity 20, pending per OpenID 2, cleanup every 30 seconds. Clear sensitive inputs on terminal state.
- No subagents spawned by implementers or reviewers. Work in the current `fix/wechat-content-security` branch. Explicitly stage owned files only.

## Files and boundaries

`wechat.py` owns outgoing WeChat requests and access token caching; `wechat_callback.py` owns callback authenticity and decryption. `moderation.py` owns ephemeral jobs and model orchestration; `main.py` only validates HTTP contracts and delegates. Frontend transport owns login/upload/poll/cancellation, the page owns UI states. Deployment docs explain missing external setup.

### Task 1: WeChat service and secure callback primitives

**Files:** Modify `backend/app/settings.py`, `backend/pyproject.toml`, `backend/uv.lock`, `backend/.env.example`. Create `backend/app/wechat.py`, `backend/app/wechat_callback.py`, `backend/tests/test_wechat.py`.

**Interfaces:**

```python
class WeChatClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient): ...
    async def exchange_code(self, code: str) -> str: ... # validated OpenID
    async def check_text(self, openid: str, content: str) -> None: ... # raises APIError
    async def submit_image(self, openid: str, media_url: str) -> str: ... # trace_id

class WeChatCallback:
    def __init__(self, settings: Settings): ...
    def verify_url(self, params: Mapping[str, str]) -> str: ... # echostr, including encrypted challenge
    def decode_event(self, params: Mapping[str, str], body: bytes) -> dict: ...
```

- [x] Add focused failing tests for successful login/token caching/text pass/media trace, text rejection, malformed response fail-closed, expired-token recovery, encrypted JSON/XML callback, bad signatures/padding/AppID/timestamp and non-encrypted POST rejection. Generate independent AES test envelopes with deterministic payloads, assert no raw sensitive errors.
- [x] Run `uv run pytest tests/test_wechat.py -q` to see failures before implementation.
- [x] Implement settings: `wechat_app_id: str`, `wechat_app_secret: SecretStr`, `wechat_callback_token: SecretStr`, `wechat_encoding_aes_key: SecretStr`, `public_base_url: str`, `wechat_request_timeout: float=10`, `moderation_job_ttl_seconds: int=2100`, `moderation_max_jobs: int=100`, `moderation_max_pending: int=20`, `moderation_max_pending_per_user: int=2`. Set defaults to empty for external config; `security_configured` requires valid credentials and HTTPS public base; validate nonempty AES key decodes to 32 bytes. Use HTTPS API host fixed to `api.weixin.qq.com` and scene=2/version=2, media_type=2. Public base may include a path prefix, strip trailing slash. Add `cryptography>=44,<48` and lock from public PyPI.
- [x] Stable token cache uses an async lock and monotonic expiry, POST `/cgi-bin/stable_token`, `force_refresh:false`; protected calls may retry once only on explicit invalid-token errors (40001/40014/42001), never on timeout or unknown outcome. Login GET `/sns/jscode2session` must never log URL containing AppSecret. Map login problems to sanitized `LOGIN_FAILED`, other failures to sanitized `SECURITY_UNAVAILABLE`; no server-supplied raw errors. `check_text` splits overlong strings to <=2500 Unicode characters with >=100 overlap, checks all nonempty chunks and releases only on explicit pass.
- [x] Callback follows official AES256 CBC protocol (Base64 key+`=`, IV first16 key bytes, random16 + network uint32 body length + body + AppID, PKCS7 block32). Compare SHA1 signatures with constant-time compare. Reject stale timestamps outside 600 seconds, overly large bodies (>64KiB), unsafe XML declarations/DOCTYPE/entities, malformed payload/AppID. GET supports ordinary URL challenge signature and encrypted challenge; POST must use `encrypt_type=aes` and `msg_signature`. Validate encrypted outer envelope AppID when supplied and decrypted suffix AppID always. Event content is JSON or XML dict, no schema-specific verdict interpretation in this primitive.
- [x] Update `.env.example` with blank backend-only variables and one-worker note; run `uv run pytest tests/test_wechat.py -q`, `uv run ruff check .`, `uv run ruff format --check .`; format own files if necessary. Existing tests still work because main API has not changed.
- [x] Review diff and commit explicitly owned files. Write report to supplied task report path with command outputs, commit and interface notes.

### Task 2: Async moderation jobs and diagnosis API

**Files:** Create `backend/app/moderation.py`, `backend/tests/test_moderation.py`; modify `backend/app/main.py`, `backend/tests/test_api.py` and relevant schema definitions only if needed.

**Consumes:** Task 1's `WeChatClient(settings, client)` and `WeChatCallback(settings)` signatures from its brief; `QwenClient.diagnose(prepared, description)` and current `DiagnosisResponse`. `Settings.security_configured` and `moderation_*` capacities/TTL are available.

**Produces:**

```text
POST /api/diagnose multipart(image, description='', login_code required nonblank<=512)
202 {job_id,job_token,status:"checking",poll_after_ms:1500}
GET /api/diagnose/{job_id} Authorization: Bearer <job_token>
202 {status:"checking"|"analysing",poll_after_ms:1500} OR 200 original DiagnosisResponse OR existing error envelope
GET /api/moderation-media/{media_token}.jpg -> JPEG, Cache-Control:no-store
GET/POST /api/wechat/callback -> challenge / "success"
```

- [x] Write integration tests using WeChat/Qwen HTTP mocks and signed encrypted callbacks. Prove queue acceptance does not call Qwen; empty/missing/forged login is refused; input/text rejection stops early; callback image rejection prevents Qwen; successful flow checks output before returning existing response; output rejection returns no result; early callbacks and duplicates yield one Qwen call; bad signatures/AppID/version/errcode never permit execution; tokenless/wrong token job lookup and wrong media token return404; expired jobs/media disappear and capacity is bounded; API failure paths sanitize upstream content. Retain all existing image/Qwen/schema coverage while adapting test helper to new authenticated asynchronous contract. For example:

```python
queued = client.post('/api/diagnose', data={'login_code': 'fresh-code'}, files=files)
assert queued.status_code == 202
assert model_calls == []
assert client.get('/api/diagnose/' + queued.json()['job_id']).status_code == 404
```

- [x] Run selected new tests to confirm pre-implementation failures.
- [x] Implement bounded in-memory job coordinator with >=32 random bytes independently for job token and media token. All registry state changes occur on the one event loop without intervening awaits; reserve capacity atomically after login, release reservation on submission failure; at most two pending jobs per OpenID. Store only prepared JPEG, description, OpenID, request metadata, state and final response/error; clear inputs in all terminal paths. Avoid unbounded completed tasks, early-event cache and worker sets. Early callback cache max100 items with expiry <=job TTL, matching only cryptographically verified events; prefer retaining non-pass verdict if conflicting. Retry-safe duplicate callbacks never start Qwen twice. New signed conflicting non-pass callback must not expose an unchecked result.
- [x] Validate callback event MsgType/event/AppID/version=2, trace_id (bounded nonempty string), top-level numeric errcode=0 and exact pass. Unknown version or result fails closed; risky/review use fixed rejection; download errors use service error. Unknown event kinds may be acknowledged without storing. On pass, schedule one lifespan-owned task; wrap total model/output moderation work in a bounded timeout (<=180s), catch APIError and unexpected failures with fixed safe error, cancel on shutdown. Iterate all string values in `result.model_dump()` (including candidate names/reasons and follow-up questions) joined by newlines for text checking; do not leak unchecked results via job APIs.
- [x] Integrate lifespan client, coordinator, cleanup coroutine every30s and cancellation. Enforce configuration on recognition before any upstream request, retain model-not-configured error semantics. `/health` adds `security_configured`; status still only means settings present, not remote verified. Callback and media routes fail closed when unconfigured. Read uploads with existing size/image validation; do not accept caller OpenID or arbitrary media URL. Gate all `/api/diagnose` paths; no legacy moderation bypass. HTTP error schema stays unchanged. POST202 includes private job token once; poll responses never include media URL/tokens/inputs. Set no-store on dynamic content responses.
- [x] Run `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`; resolve actual failures, inspect diff, commit owned files and write task report.

### Task 3: Mini program flow, deployment instructions and final integration

**Files:** Modify `miniprogram/services/diagnosis.js`, `miniprogram/pages/index/index.js`, `miniprogram/pages/index/index.wxml`, `miniprogram/pages/index/index.wxss` as needed, `tests/miniprogram.test.cjs`, `README.md`. Create `deploy/Caddyfile.example`, `docs/wechat-content-security.md`. Do not modify `miniprogram/config.js`.

**Consumes:** Exact HTTP contract in Task 2. `uploadDiagnosis({filePath,description,onProgress,onStage}) -> {promise,abort}` remains page entry. `onStage` receives `checking` or `analysing`; promise resolves only full original200response.

- [x] Add failing Node tests using mocked wx.login, wx.uploadFile, wx.request and controlled timers: login code in form; login failure uploads nothing; queued202 polls Bearer private job token; repeated202 waits; valid200 resolves; malformed202/status/token/url rejects; no caller server-origin redirect; abort during login/upload/poll/delay stops all subsequent work and ignores late callbacks; timeout deadline32min; stale updates prevented; rejection displays exact fixed message even if upstream error contains harmful content. Update existing hardcoded localhost assertions to derive API base from existing config, preserving user change.
- [x] Implement login-before-upload and bounded polling at server delay clamped1000..5000ms; separate each network timeout from overall32min deadline, support cancellation of in-flight requests and timers; no automatic repost or paid retry. `job_id` and token must be opaque nonempty bounded URL-safe strings; build poll URL only from configured base and encoded job id. Fixed messages for all security/login/job codes, including `SECURITY_NOT_CONFIGURED`, `SECURITY_UNAVAILABLE`, `SECURITY_TIMEOUT`, `LOGIN_FAILED`, `CONTENT_REJECTED`, `TOO_MANY_REQUESTS`, `JOB_NOT_FOUND`. Never use unsafe upstream message for content-rejected responses.
- [x] Page tracks moderation stage and `photoApproved` false for each new photo/recognition, shows selected-photo placeholder before pass and gates `wx.previewImage`; successful final response sets approved. Changing/clearing photo clears approval; description editing clears results. Rejection clears photo, description, result and approval, sets a rejection state whose panel only renders the fixed violation message (no title, retained-content hint, labels or raw reason). Network/service errors retain input for retry but do not mark it approved. Busy progress distinguishes uploading/checking/analysing. Update consent to mention WeChat content safety and cloud model, short-term processing storage; no implementation details in primary UI.
- [x] Document missing real HTTPS setup, one-worker/single-instance requirement, restart behavior,35min data TTL, temporary media capability confidentiality, input/model moderation, successful versus accepted async response, response/error contracts and real WeChat smoke-test steps. Include official current endpoint docs. Add Caddy example using `api.example.com { reverse_proxy 127.0.0.1:8189 }`, no mandatory access log. Explain DNS80/443, existing FastAPI8189 process binding, `PUBLIC_BASE_URL`, WeChat IP whitelist, same real AppID for frontend/backend, message push `/api/wechat/callback` in AES safe mode JSON orXML, token/AES from backend-only local env, `request`/`uploadFile` HTTPS legal domains, update existing HTTP `API_BASE_URL` after HTTPS deployed. Clearly mark no actual WeChat integration verified yet. Remove obsolete README claims that request200direct, no content security, no server retention, anonymous API, localHTTP sufficient for full flow, old exact passing counts; keep model-accuracy limitations and earlier real model test history with date.
- [x] Run `node --test tests/miniprogram.test.cjs`, JS syntax checks for changedJS, parse all projectJSON, and compile WXML/WXSS using local WeChat compilers if available. Run backend full pytest/Ruff again only if backend changed. Confirm original user config diff unchanged. Commit explicitly owned files and write report with validation evidence and deployment limitations.
