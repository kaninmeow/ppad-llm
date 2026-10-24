# Six-crop diagnosis design

User approved the design on 2026-10-24: users select a crop, and one existing Qwen multimodal call verifies the crop and analyses visible disease evidence. This document records that approved design and its implementation contracts.

## Scope

- Support exactly tomato / 番茄, cucumber / 黄瓜, pepper / 辣椒, eggplant / 茄子, potato / 马铃薯, chinese_cabbage / 大白菜. The cabbage category means 大白菜, not 小白菜 or 甘蓝.
- One uploaded photo, one main crop per submission. No automatic crop-selection mode, training, detector, RAG, history or new storage service in this version.
- Preserve the existing Qwen model/endpoint/key, image limits and no automatic paid retries. Do not edit actual .env, public project AppIDs, or miniprogram/config.js. Do not deploy, push or merge as part of implementation.
- Work on feat/multi-crop-diagnosis in the existing clean checkout. Local branches isolate this work from main. Do not spawn nested subagents.

## Interaction

The page starts with no selected crop. Six visible crop buttons precede photo and description input. Recognition requires a valid crop and photo. Selecting another crop retains local photo/description but clears results, photo approval, errors and stale progress. Crop selection, photo selection and description edits are locked while selecting a photo or processing a submission. Other existing cancellation and late-callback protections remain.

The same model call receives the image, server-owned crop context and user description as data. The model can agree, be unable to independently verify the crop from a close-up, identify a different supported crop, or identify an unsupported subject. Partial photos may still be analysed conditionally using the user's crop selection; this is visibly labelled and never presented as independently verified identification.

Results show selected crop, verification state and its reason. A mismatched or unsupported subject shows no disease conclusion or candidate probabilities. Healthy means only 未见明显病害. Existing 1–3 ranked candidates and independent estimated likelihoods remain uncalibrated model estimates.

## Versioned API and compatibility

Keep POST /api/diagnose, GET /api/diagnose/{job_id}, QwenClient.diagnose(image, description), DiagnosisResult and DiagnosisResponse backward compatible, including not_tomato. Existing clients still receive the original schema.

Add POST /api/v2/diagnose with existing multipart image, description, login_code plus required crop_id, validated against the six IDs before upstream work. Missing/unknown crop IDs return 422 INVALID_REQUEST safely. Add GET /api/v2/diagnose/{job_id} using the same Bearer job capability. Admission and pending envelopes are unchanged; final v2 responses retain the existing response envelope but use MultiCropDiagnosisResult.

Jobs record an immutable result API version and per-job crop context. Looking up a v1 job on the v2 endpoint, or vice versa, returns the existing indistinguishable 404 NOT_FOUND. Media and encrypted callback routes stay shared. No global mutable current crop or prompt.

## V2 result contract

MultiCropDiagnosisResult retains disease_name, disease_candidates, summary, observed_symptoms, possible_causes, recommendations and follow_up_questions with existing limits/invariants. Its status is suspected_disease, healthy, uncertain, crop_mismatch or unsupported_crop. It adds:

```json
{
  "crop": {
    "selected": "cucumber",
    "detected": null,
    "verification": "unverified",
    "reason": "局部照片不足以独立核实作物，按用户选择的黄瓜进行分析。"
  }
}
```

selected is one supported ID and must equal the submitted crop_id. detected is a supported ID, "other", or null. reason is bounded nonblank text. Enforce these exact relationships in backend and frontend:

| verification | detected | permitted status |
| --- | --- | --- |
| matched | equals selected | suspected_disease / healthy / uncertain |
| unverified | null | suspected_disease / healthy / uncertain |
| mismatch | a different supported ID | crop_mismatch |
| unsupported | other | unsupported_crop |

All non-disease statuses have null disease_name and empty candidates. crop_mismatch and unsupported_crop also have empty possible_causes. Invalid relationships, unexpected fields or an echoed selected crop different from the submitted crop fail with existing sanitized INVALID_MODEL_RESPONSE; do not silently repair or retry a paid call.

## Architecture and moderation

backend/app/crops.py owns immutable crop metadata and per-crop prompt context. Frontend public crop options mirror these six IDs/names in a small module. Catalogue agreement is checked during review/verification; adding a crop is a deliberate backend + client release.

QwenClient adds diagnose_crop(image, description, crop_id), sharing the existing HTTP request, error handling and strict parsing rather than duplicating transport. V1 keeps its original prompt/schema; v2 uses a shared general prompt plus whitelisted crop context and the v2 schema. Crop context is not evidence or a source of guaranteed disease knowledge.

The existing coordinator performs description moderation, normalized image async moderation, the one model call, and moderation of every nested output string including crop reasons. Both API versions use the same bounded queues, deadlines, cancellation, independent capabilities, terminal input cleanup and single-worker/instance assumptions. No unchecked preview or output is introduced. Content rejection still clears photo/description/results and displays only 您提交的内容含违规信息。

## Validation and rollout

Automated tests cover all six selected crops, all verification states and inconsistent combinations, required/unknown crop rejection before upstream calls, mixed concurrent jobs, version-isolated polling, new output-string moderation/rejection, old v1 compatibility, and existing lifecycle/security regressions. Frontend tests cover selection requirements/locking/reset, crop submission, strict v2 schema/context validation, mismatch/unsupported/unverified display, existing probabilities, polling/cancellation and safety gates. Run backend pytest + Ruff, Node tests + JS/JSON checks and available native WXML/WXSS compilers.

Document API versions, supported crop boundaries, staged backend-first deployment and new-client release. Keep old interfaces available. No live Qwen or WeChat call, device check, six-crop accuracy assessment or platform approval is implied by mocked tests. Real samples must be labelled/checked per crop; report disease candidate hit rates, abstention, errors and latency separately. Prompt ablations are a later study, not a completed experiment.
