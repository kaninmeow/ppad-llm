# 蔬菜观察室 · 六作物病害初步识别

原生微信小程序 + FastAPI 后端，沿用阿里云百炼通义千问多模态模型 `qwen3.8-flash`。明确选择本次作物、选择照片并补充症状后，先完成微信内容安全检测，再分析图片并检测模型生成文本，最后显示初步结果及未经校准的病害可能性。

**历史记录（2026-10-22）：当时真实微信内容安全、HTTPS、公网回调、模拟器和真机全流程尚未联调，本地仅确认已填写模型配置。** 2026-10-24 本次多作物变更未重新检查外部 HTTPS/微信配置，也未进行新的真实模型调用、模拟器/真机联调或六作物准确率验证；自动化测试使用替身。

## 启动与部署

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)、微信开发者工具、真实小程序 AppID 及后端凭据。后端不需要 GPU。

```powershell
Set-Location D:\project\PlantPestAndDiseaseIdentificationSystem\pd-LLM\backend
uv sync
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
```

编辑本地 `backend/.env`，按 `.env.example` 设置 `DASHSCOPE_API_KEY`、现有 `QWEN_BASE_URL` / `QWEN_MODEL`，以及 `WECHAT_APP_ID`、`WECHAT_APP_SECRET`、`WECHAT_CALLBACK_TOKEN`、`WECHAT_ENCODING_AES_KEY`、`PUBLIC_BASE_URL`。Secret 只在后端保存，不要提交或发到聊天中。环境变量优先于 `.env`；修改后重启。百炼地址、模型和密钥须对应同一业务空间/地域，参见[阿里云模型说明](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)。

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8189 --workers 1 --no-access-log
```

本机可检查 [health](http://127.0.0.1:8189/health)、[接口文档](http://127.0.0.1:8189/docs) 和 [OpenAPI](http://127.0.0.1:8189/openapi.json)。`configured` 与 `security_configured` 只表示本地设置存在，不代表上游验证通过。缺少内容安全设置时识别返回 503 `SECURITY_NOT_CONFIGURED`，不会放行。

完整流程须有公网 HTTPS，供微信下载图片和推送加密回调。[部署与真实微信烟测说明](docs/wechat-content-security.md) 包含 Caddy、DNS/80/443、出口 IP 白名单、AES 消息推送和合法域名步骤；[Caddy 示例](deploy/Caddyfile.example) 反代 `127.0.0.1:8189`。**只允许单 worker、单实例**：任务存于进程内，重启丢失，最长保留 35 分钟。关闭访问日志，避免泄露临时图片能力地址及 callback 查询串。

## 打开小程序

1. 微信开发者工具导入根目录 `D:\project\PlantPestAndDiseaseIdentificationSystem\pd-LLM`，代码位于 `miniprogram/`。
2. 根 `project.config.json` 当前为 `touristappid`，`miniprogram/project.config.json` 为真实公开 AppID `wxeec0a1e738ed4126`。操作者须确保**实际导入项目**使用自己有权限且与后端 `WECHAT_APP_ID` 相同的真实 AppID；根目录导入须替换游客 ID。游客登录无法完成真实微信检测流程。本次没有自动改写项目 ID。
3. HTTPS 部署后，由操作者将 `miniprogram/config.js` 的 `API_BASE_URL` 设置为真实 HTTPS 基地址。本次未修改该文件，也未重新验证外部部署状态；在微信后台分别核对 `request` / `uploadFile` 合法域名及隐私声明。
4. 使用同一 AppID 的真实登录测试。纯 HTTP localhost 或关闭域名校验只能做局部开发调试，不能完成微信公网回调和真实审核验证。手机 `127.0.0.1` 指向手机自身。
5. 初始不默认选择作物，先从六个可见按钮中明确选择；选择照片后仅显示占位；点击识别后显示上传、检测、分析进度，完整结果成功后才允许显示和预览照片。违规会清空照片、描述、结果，仅显示固定消息；网络/服务错误保留输入供手动重试。

## 图片与结果

- 单次上传一张 JPEG、PNG 或 WebP，原文件不超过 8 MiB，解码后不超过 2500 万像素，短边至少 32 像素。
- 后端按实际图片内容验证格式，校正 EXIF 方向，将长边缩至最多 1600 像素，转为不附带原始元数据的 JPEG，先经微信图片安全检测再发送给模型。
- 补充描述不超过 500 字。照片和描述在点击识别后提交微信内容安全检测与云端模型分析；服务端使用最长 35 分钟的临时任务存储，终态清除敏感输入。云端服务的数据处理政策由所用服务商规定。
- 模型 HTTP 连接及读写操作的超时为 60 秒，小程序单次网络请求超时最多 90 秒，登录至轮询完成的总体期限为 32 分钟。错误后由用户手动重试，程序不会自动重复调用付费接口。
- 返回的是基于图片的初步识别。无法判断时应补充照片或检查信息，不强制归入已有病种。界面展示模型生成的可见症状和建议，不将其标为专家审核或已检索的事实。
- 疑似病害结果展示当前病害和最多两个其他候选病害的可能性百分比、横条和鉴别依据。数值由同一次模型请求生成，属于**未经准确率校准的模型估计，不是确诊概率**；不同病害可能同时存在，各项不要求合计 100%，程序不会重新归一化数值。没有证据时不凑候选，健康、无法判断、作物不一致和不支持作物的结果不展示病害百分比。

## 作物边界与发布顺序

| crop_id | 名称 |
| --- | --- |
| `tomato` | 番茄 |
| `cucumber` | 黄瓜 |
| `pepper` | 辣椒 |
| `eggplant` | 茄子 |
| `potato` | 马铃薯 |
| `chinese_cabbage` | 大白菜 |

大白菜仅指大白菜/结球白菜，不包括小白菜和甘蓝。用户选择只是分析上下文，不能当作照片已经核实作物的证据。局部照片无法核实时，页面明确显示“按你选择的{作物}分析”和“图片无法独立确认作物”；作物不一致或不受支持时提示重新选择，不展示病害名称、候选百分比或原因列表。更换作物保留本地照片和描述，但清除旧结果、预览许可、错误和进度；选择照片或识别期间锁定作物选择。

先部署保留 v1 并新增 v2 的后端，验证 v1 回归、v2 契约及检测门禁，再发布仅使用 v2 的新客户端。旧客户端仍使用 `POST /api/diagnose` 和 `GET /api/diagnose/{job_id}`，原结果结构及 `not_tomato` 状态不变；新客户端不会降级或自动重发至 v1。新客户端上线后，回退后端仍须保留 v2，或先安排客户端回退。真实样本和上线检查见[多作物验证指南](docs/multi-crop-validation.md)。

## 接口

`POST /api/v2/diagnose` 接收 `multipart/form-data`：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `image` | 文件，必填 | 待识别照片 |
| `crop_id` | 文本，必填 | 上表六个 ID 之一，无默认值；缺失/非法值为 422 `INVALID_REQUEST` |
| `description` | 文本，可选 | 最多 500 字 |
| `login_code` | 文本，必填 | 本次 `wx.login` 获取的非空 code，最多 512 字 |

受理返回 HTTP **202** `{"job_id":"…","job_token":"…","status":"checking","poll_after_ms":1500}`，表示等待检测。客户端使用 `Authorization: Bearer <job_token>` 轮询 `GET /api/v2/diagnose/{job_id}`；进行中仍返回 202（`checking` / `analysing`），只有检测全部通过后的 GET **200** 才包含完整 v2 结果。POST 直接 200 不再被客户端接受。轮询间隔 1–5 秒，退出页面停止等待，不自动重发或付费重试。token 不得进入 URL、日志或 UI。

GET 200 包含 `request_id`、`model`、`elapsed_ms`、`notice` 和结构化 `result`：

| `result` 字段 | 含义 |
| --- | --- |
| `status` | `suspected_disease` / `healthy` / `uncertain` / `crop_mismatch` / `unsupported_crop` |
| `crop` | `{selected, detected, verification, reason}`，严格关系见下表；`selected` 必须等于本次提交的 `crop_id` |
| `disease_name` | 疑似病种名称；其他状态为 `null` |
| `disease_candidates` | 疑似病害时为 1–3 个 `{name, likelihood, reason}`，`likelihood` 是 0–100 整数；首项与 `disease_name` 一致，名称不重复，按可能性降序。其他状态为 `[]` |
| `summary` | 初步识别说明 |
| `observed_symptoms` | 图中可见症状 |
| `possible_causes` | 候选原因，包含 `name` 和 `reason`；`crop_mismatch` / `unsupported_crop` 必须为 `[]` |
| `recommendations` | 下一步建议 |
| `follow_up_questions` | 建议补充的信息 |

| crop.verification | crop.detected | 允许的 status |
| --- | --- | --- |
| `matched` | 等于 `selected` | `suspected_disease` / `healthy` / `uncertain` |
| `unverified` | `null` | `suspected_disease` / `healthy` / `uncertain` |
| `mismatch` | 不同于 `selected` 的受支持 ID | `crop_mismatch` |
| `unsupported` | `other` | `unsupported_crop` |

`reason` 为 1–300 字非空说明，与所有嵌套输出文本一起经过检测后才展示。结果不允许未知字段；任一关系不合法、选项回显与提交不符或病害结构不完整时以固定 `INVALID_MODEL_RESPONSE` 提示结束，不修复数据或自动付费重试。除 `suspected_disease` 外，`disease_name` 均为 `null` 且 `disease_candidates` 为 `[]`。

失败响应统一为 `{"error":{"code":"错误代码","message":"安全说明"}}`。`CONTENT_REJECTED`（422）的消息固定为 `您提交的内容含违规信息。`，前端忽略上游错误文案。缺少配置 `SECURITY_NOT_CONFIGURED`（503）、检测异常 `SECURITY_UNAVAILABLE`（503）、检测超时 `SECURITY_TIMEOUT`（504）均不放行。另有 `LOGIN_FAILED`（401）、`QUEUE_FULL`（429）、`NOT_FOUND`（404）及原有图片/模型错误，详见[完整约定](docs/wechat-content-security.md#接口约定)。接口需要真实微信登录和私有任务凭证；不要记录 code、token、图片能力 URL 或原始上游错误。

## 本地检查

```powershell
Set-Location D:\project\PlantPestAndDiseaseIdentificationSystem\pd-LLM\backend
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
Set-Location ..
node --test tests/miniprogram.test.cjs
node --check miniprogram/pages/index/index.js
node --check miniprogram/services/diagnosis.js
node --check miniprogram/data/crops.js
```

测试覆盖微信登录、异步任务/加密回调、输入与模型输出拦截、任务容量与过期、私有轮询、取消和超时、预览门禁及原有图片/结果结构校验。测试使用模拟微信/模型 HTTP 和微信 API 替身，不消耗模型额度，不等同于真实微信或手机验证。可使用本机开发者工具的 WXML/WXSS 编译器进行模板与样式检查。后端测试依赖有一条 Starlette/HTTPX TestClient 弃用提示。

## 历史模型连通性检查

以下是接入内容安全前的模型兼容性记录，不能视为当前微信异步流程已联调。

2026-10-21 已使用本地配置的真实 API Key 和业务空间地址完成一次 `qwen3.8-flash` 图片调用：上游和后端均返回 HTTP 200，结构化结果校验通过，单次后端耗时约 3.4 秒。输入为纯色联通测试图，模型返回 `uncertain`；此检查验证图片接口兼容性，不代表番茄病害识别准确率或典型响应速度。

2026-10-21 新增病害可能性字段后，又用纯色图检查真实接口：后端返回 HTTP 200，耗时约 3.1 秒，结果为 `uncertain`、`disease_candidates: []`，新结构校验通过。疑似病害及其他候选的展示、数值校验由上述自动化测试验证，尚未用真实病害照片验证。

以上为历史记录。本次未进行六作物真实标注样本评估、微信模拟器或手机真机联调。

## 正式发布前

按[部署和烟测说明](docs/wechat-content-security.md) 配置真实 HTTPS、同一 AppID、接口权限、IP 白名单、安全模式回调、合法域名与用户隐私说明；保留单实例部署及临时凭证保密要求。当前微信身份、队列和每用户并发限制不能替代生产运营所需的额度预算及滥用监控，发布时应按业务需要配置。六作物准确率尚未验证，结果仅供种植参考。

## 目录

```text
backend/                    FastAPI、图片处理、微信检测、千问客户端及测试
miniprogram/                原生微信小程序
tests/miniprogram.test.cjs  小程序网络与状态测试
project.config.json         根目录开发者工具入口（需配置真实 AppID）
deploy/Caddyfile.example     HTTPS 反向代理示例
docs/wechat-content-security.md  部署、契约和真实烟测说明
docs/multi-crop-validation.md    六作物手动验证、样本及指标指南
docs/superpowers/           已确认范围及实施计划
```
