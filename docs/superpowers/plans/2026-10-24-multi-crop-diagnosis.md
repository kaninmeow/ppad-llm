# Six-Crop Diagnosis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add user-selected six-crop diagnosis while preserving the released tomato API and the complete moderation chain.

**Architecture:** A whitelisted crop registry, v2 strict result schema and version-tagged existing jobs reuse the one-call model transport and moderation coordinator. The native mini program selects crops and consumes only the v2 result; old clients retain v1.

**Tech Stack:** Existing FastAPI/Pydantic/HTTPX and native WeChat JS/WXML/WXSS; pytest and Node test runner. No dependencies added.

**Spec:** docs/superpowers/specs/2026-10-24-multi-crop-diagnosis-design.md

## Global Constraints

- Exactly tomato/番茄, cucumber/黄瓜, pepper/辣椒, eggplant/茄子, potato/马铃薯, chinese_cabbage/大白菜; 大白菜 excludes 小白菜 and 甘蓝.
- Preserve actual .env, miniprogram/config.js, model settings, AppIDs and existing image limits. No live upstream/paid calls, deploy, push or merge. Work on feat/multi-crop-diagnosis in this checkout; no implementer/reviewer subagents.
- Preserve v1 POST /api/diagnose, GET /api/diagnose/{job_id}, QwenClient.diagnose(image, description) and original result schema/statuses. Add v2 without breaking v1.
- Both versions retain input text + normalized image + all nested output text moderation, fixed rejection 您提交的内容含违规信息。, no unchecked preview, private Bearer polling, cancellation/time limits, single worker/instance and existing bounded storage. No automatic paid retry.
- V2 crop.selected must equal submitted crop_id; enforce all crop/status/disease relationships in the spec in backend and frontend. User crop selection is context, not verified visual evidence. Clearly label unverified conditional analysis.
- Keep tests meaningful: mock external HTTP/platform boundaries but exercise production schemas/routes/transport/page. Preserve all original regression coverage; do not simply delete failing tests.

## Files and task boundaries

Task 1 owns backend crop/schema/model/jobs/API changes and backend tests. Task 2 consumes those exact contracts and owns frontend, frontend tests and user documentation. No parallel implementers.

### Task 1: Versioned six-crop backend with legacy compatibility

**Files:** Create backend/app/crops.py and backend/tests/test_crops.py. Modify backend/app/schemas.py, backend/app/qwen.py, backend/app/moderation.py, backend/app/main.py. Extend backend/tests/test_api.py or backend/tests/test_moderation.py only as needed for existing helper reuse; preserve old tests/semantics. Do not change settings, dependencies or WeChat primitives.

**Consumes:** Existing WeChatClient, QwenClient.diagnose, PreparedImage, APIError, ModerationJobs.submit/get and original schema.

**Produces exact interfaces:**

```python
# crops.py
CropId = Literal["tomato", "cucumber", "pepper", "eggplant", "potato", "chinese_cabbage"]
# Immutable CROPS metadata keyed by these IDs; each crop exposes name, aliases,
# photo_tip and prompt_hint. Choose clear immutable representation locally.

# schemas.py
class CropAssessment(StrictModel):
    selected: CropId
    detected: CropId | Literal["other"] | None
    verification: Literal["matched", "unverified", "mismatch", "unsupported"]
    reason: ShortText

class MultiCropDiagnosisResult(...):
    # All original disease fields/limits plus crop: CropAssessment.
    # status: suspected_disease|healthy|uncertain|crop_mismatch|unsupported_crop
    ...

class MultiCropDiagnosisResponse(...):
    # Same envelope fields, result: MultiCropDiagnosisResult.
    ...

# qwen.py
async def diagnose_crop(self, image: PreparedImage, description: str,
                        crop_id: CropId) -> MultiCropDiagnosisResult: ...

# moderation.py -- optional kwargs keep old callers working
async def submit(self, prepared, description, code, *, crop_id=None): ...
def get(self, job_id, authorization, *, version=1) -> dict: ...
```

- [ ] Write failing tests in test_crops.py exercising real schemas and mocked HTTP model/API boundaries. A representative valid fixture is below; add parametrized variations for all six IDs and status relationships. Invalid cross-field data must raise ValidationError, and selected echo different from request must produce safe INVALID_MODEL_RESPONSE. Mismatch/unsupported must have no disease fields/possible causes.

```python
def matched_crop(crop_id="tomato"):
    return {"selected": crop_id, "detected": crop_id,
            "verification": "matched", "reason": "图片作物与所选作物相符。"}

def test_unknown_v2_crop_is_rejected_before_upstream():
    with api() as (client, requests):
        response = client.post("/api/v2/diagnose",
            files={"image": ("leaf.png", image_bytes(), "image/png")},
            data={"login_code": "fresh-code", "crop_id": "unknown"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"
        assert requests == []
```

- [ ] Run focused new tests red before implementation. Cover missing crop, invalid IDs and injection strings; accepted jobs wait for signed image callbacks; all six prompt contexts; one model call per job; strict JSON/disease rules; unverified conditional outcomes; supported mismatch and unsupported result; same/different version job lookup; concurrent v1/v2 and different selected crops; new crop.reason included in output moderation and blocked on rejection. Existing v1 tests remain unmodified unless helper extension is strictly necessary.
- [ ] Implement immutable crops.py metadata with the six exact IDs/names, aliases, simple photo tips and concise context. Hints constrain the task and ask for evidence, not assert diagnosis accuracy or hardcoded medical knowledge. No arbitrary user crop string becomes prompt instructions.
- [ ] Implement the strict v2 schema and wrappers. Share common disease field validation when helpful while preserving DiagnosisResult JSON schema/behavior. Enforce matched=selected, unverified=null, mismatch=different supported ID + crop_mismatch, unsupported=other + unsupported_crop. Only normal statuses allow matched/unverified. Existing disease invariants always apply; mismatch/unsupported possible_causes must be empty.
- [ ] Refactor the existing Qwen HTTP/parsing into a shared private helper and add diagnose_crop. Leave v1 SYSTEM_PROMPT and diagnose call contract compatible. V2 prompt includes schema, single selected crop context, conditional analysis rules, one main crop, no invented symptoms or drug doses, and treats description/image text as untrusted data. Validate returned selected crop equals requested before returning. Keep response_format=json_object, current model/settings, existing timeouts/errors and no retries.
- [ ] Extend Job with fixed API result version and per-job crop context; keep legacy defaults. Pass crop context from submit/reserve to the one worker. V1 worker uses diagnose + DiagnosisResponse; v2 uses diagnose_crop + MultiCropDiagnosisResponse. Clear selected crop input on terminal together with other inputs, retaining a separate immutable version for polling. Every new output string passes existing strings()/text moderation. Existing callback/capacity/cancellation behavior remains.
- [ ] Add required crop_id form POST /api/v2/diagnose and private GET /api/v2/diagnose/{job_id}. Same202 pending/admission envelopes and existing error/no-store behavior. Share input validation/preparation with v1. Version-mismatched lookup is404 NOT_FOUND, including after completion. Invalid crops fail before login/model requests. No new media/callback route or bypass.
- [ ] Run uv run pytest -q, uv run ruff check ., uv run ruff format --check . from backend. Fix real failures, inspect diff, commit explicitly owned files only. Report red/green commands, counts, commit, interface details and remaining external validation to task-1-report.md in supplied scratch directory.

### Task 2: Six-crop mini program, strict client validation and release guide

**Files:** Create miniprogram/data/crops.js. Modify miniprogram/services/diagnosis.js, miniprogram/pages/index/index.js/index.wxml/index.wxss/index.json, miniprogram/app.json, miniprogram/sitemap.json, tests/miniprogram.test.cjs, README.md and docs/wechat-content-security.md. Create docs/multi-crop-validation.md. Do not change miniprogram/config.js, project AppIDs or backend.

**Consumes:** Task 1's POST /api/v2/diagnose multipart crop_id + existing form fields, GET /api/v2/diagnose/{job_id} Bearer polling, unchanged202 envelopes and exact CropAssessment/v2 schema above. Six public options match backend registry IDs and Chinese names. Legacy backend API remains available; new client uses only v2.

**Produces:**

```javascript
// data/crops.js: public [{id, name}] for the six IDs, no default selection.
// services/diagnosis.js
uploadDiagnosis({ filePath, cropId, description, onProgress, onStage })
// returns existing {promise, abort}; cropId required and whitelisted.
// page
// selectedCropId: '', selectedCropName: '', cropOptions: six options
// onCropSelect reads event.currentTarget.dataset.cropId, validates catalogue,
// locks while busy/choosing and clears stale result/approval/progress on change.
```

- [ ] Extend meaningful Node wx/platform tests red first. Upgrade v2 fixtures with explicit selected crop and assessment, preserve existing probability/result safety, lifecycle, deadline, cancellation, schema and error regression coverage. Test all six options, no initial selection, missing/invalid crop stops before login, crop_id form value and v2 paths, strict echo matching, all assessment/status combinations, no disease results for mismatch/unsupported, clear conditional label, select/reset/lock behavior and late callbacks. Add INVALID_REQUEST fixed wording if needed, never raw errors.
- [ ] Implement the six public crop options and service cropId requirement. Capture requested crop for full operation; never let later caller/page edits change the in-flight value. Use /api/v2/diagnose for POST and poll paths. Preserve all timeout/cancellation/admission/capability guards, no redirects/repost/retries. Validate crop IDs, enums/nullability and cross-field relationships against submitted crop, along with original disease validation before resolving.
- [ ] Add six visible buttons in a 3-column selector before photo input; use dataset crop ID and wx:key=id, with dynamic disabled/selection bindings. No picker default-selection trap. Recognition requires selected crop + image; selection locks while busy/choosing. Crop changes retain local photo/description but clear result, approval, rejection/error and progress. Existing photo/description changes and lifecycle protections remain. Rejecting content clears photo/description/result/approval, retaining only safe fixed crop option if desired; rejection panel still only exact fixed message.
- [ ] Update general headings/navigation to 蔬菜观察室 and use neutral existing CSS leaf shapes in place of tomato-specific decorative art. Preserve visual style and layout. On checked results show selected crop, verification reason, supported detected crop when relevant. Unverified prominently says 按你选择的{作物}分析 and 图片无法独立确认作物. Mismatch/unsupported show reselect guidance and no disease/probability section. All dynamic crop.reason text appears only after checked final result; previews still require final success.
- [ ] Update README and deployment guide with six crop boundaries, new required parameter/result states, legacy v1 compatibility and backend-first then new-client rollout. Keep existing HTTPS, single-worker, credentials, moderation, TTL/privacy and no paid retries guidance. Do not assert current external HTTPS/WeChat configuration state from an old snapshot; preserve dated history and say this change has not performed new live model/device/accuracy validation. Add multi-crop-validation.md with per-crop manual check matrix (healthy, disease evidence, unclear close-up, mismatch, unsupported, safety, cancellation), initial labelled sample/metrics guidance and later prompt comparison; no fabricated results or promised accuracy.
- [ ] Run node --test tests/miniprogram.test.cjs, production JS syntax and all project JSON parse checks. Compile WXML and app/page WXSS separately with available local WeChat compilers, from miniprogram directory, writing artifacts to TEMP. Backend unchanged, do not rerun its suite. Compare six options with backend CROPS and confirm config/AppIDs untouched. Inspect diff, commit owned files only and report commands/output/commit/limits to task-2-report.md.

## Final integration gate

After approved task gates, review the whole branch once. Parent verifies current backend pytest/Ruff and frontend Node/native compilation evidence; rerun suites only for changed code or unresolved doubts. No implementation claims imply actual six-crop diagnostic accuracy, live platform connectivity or completed rollout. Keep local feature branch for review.
