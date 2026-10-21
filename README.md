# 番茄病害识别 MVP

原生微信小程序 + FastAPI 后端，直接调用阿里云百炼通义千问多模态模型，默认使用 `qwen3.8-flash`。

已实现的交互范围：选择或拍摄一张照片、补充症状、上传识别、查看疑似病种和可见症状、比较病害可能性、查看下一步建议、失败后重试。结果支持「疑似病害」「未见明显病害」「暂时无法判断」「未识别到番茄」。

## 启动后端

环境要求：Python 3.11 或更新版本、[uv](https://docs.astral.sh/uv/)、微信开发者工具。后端不需要 GPU。

在 PowerShell 中运行：

```powershell
Set-Location D:\project\PlantPestAndDiseaseIdentificationSystem\pd-LLM\backend
uv sync
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
```

编辑 `backend/.env`，填写自己在阿里云百炼创建的 API Key：

```dotenv
DASHSCOPE_API_KEY=
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=qwen3.8-flash
```

`DASHSCOPE_API_KEY` 等号后填入真实密钥；密钥只保留在后端，不要填入小程序、提交到版本库或发到聊天中。进程环境变量优先于 `.env`，修改配置后重启后端。

默认地址对应北京地域。也可以按百炼控制台改成业务空间专属地址，例如 `https://你的业务空间ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。API Key、模型和服务地址需要对应同一地域，模型需在该业务空间可用。参见[阿里云官方模型说明](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)。

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

- 健康检查：<http://127.0.0.1:8000/health>
- 本地接口调试：<http://127.0.0.1:8000/docs>
- OpenAPI：<http://127.0.0.1:8000/openapi.json>

未填写密钥时服务仍可启动，健康检查的 `configured` 为 `false`，识别请求会返回明确的配置错误；不会返回虚构结果。`configured: true` 仅表示已填写密钥，不表示密钥已经通过远程校验。

## 打开微信小程序

1. 在微信开发者工具中导入**项目根目录** `D:\project\PlantPestAndDiseaseIdentificationSystem\pd-LLM`，根目录已有 `project.config.json`，实际小程序代码在 `miniprogram/`。
2. 使用自己的小程序 AppID；当前配置为本地开发用 `touristappid`。工具如不支持游客项目，请使用自己的测试 AppID。本项目不使用云开发。
3. 检查 `miniprogram/config.js` 的 `API_BASE_URL`。同一台电脑上的开发者工具可用默认的 `http://127.0.0.1:8000`。
4. 本地 HTTP 调试需要在开发者工具「详情 → 本地设置」中开启「不校验合法域名、web-view（业务域名）、TLS 版本以及 HTTPS 证书」。此设置仅用于本地开发。
5. 编译后拍照或选图，可补充症状，点击识别。密钥未配置、网络中断或模型调用失败都会在页面显示错误。

### 手机联调

手机上的 `127.0.0.1` 指向手机自身。要连接电脑后端，需要让手机和电脑处于可互通的局域网，将 `API_BASE_URL` 改为电脑的局域网 IP，例如 `http://192.168.1.20:8000`，并以以下方式监听：

```powershell
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

电脑防火墙需要允许该端口；HTTP/IP 地址仅用于允许该配置的开发调试模式。普通真机预览和正式使用应配置可访问的 HTTPS 域名。

## 图片与结果

- 单次上传一张 JPEG、PNG 或 WebP，原文件不超过 8 MiB，解码后不超过 2500 万像素，短边至少 32 像素。
- 后端按实际图片内容验证格式，校正 EXIF 方向，将长边缩至最多 1600 像素，转为不附带原始元数据的 JPEG 后发送给模型。
- 补充描述不超过 500 字。照片和描述在点击识别后发送至云端模型；本程序不建立图片库或保存识别历史。云端服务的数据处理政策由所用服务商规定。
- 模型 HTTP 连接及读写操作的超时为 60 秒，小程序上传请求超时为 90 秒。错误后由用户手动重试，程序不会自动重复调用付费接口。
- 返回的是基于图片的初步识别。无法判断时应补充照片或检查信息，不强制归入已有病种。界面展示模型生成的可见症状和建议，不将其标为专家审核或已检索的事实。
- 疑似病害结果展示当前病害和最多两个其他候选病害的可能性百分比、横条和鉴别依据。数值由同一次模型请求生成，属于**未经准确率校准的模型估计，不是确诊概率**；不同病害可能同时存在，各项不要求合计 100%，程序不会重新归一化数值。没有证据时不凑候选，健康、无法判断和非番茄结果不展示病害百分比。

## 接口

`POST /api/diagnose` 接收 `multipart/form-data`：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `image` | 文件，必填 | 待识别照片 |
| `description` | 文本，可选 | 补充症状，最多 500 字 |

成功响应包含 `request_id`、`model`、`elapsed_ms`、`notice`，以及结构化 `result`：

| `result` 字段 | 含义 |
| --- | --- |
| `status` | `suspected_disease` / `healthy` / `uncertain` / `not_tomato` |
| `disease_name` | 疑似病种名称；其他状态为 `null` |
| `disease_candidates` | 疑似病害时为 1–3 个 `{name, likelihood, reason}`，`likelihood` 是 0–100 整数；首项与 `disease_name` 一致，名称不重复，按可能性降序。其他状态为 `[]` |
| `summary` | 初步识别说明 |
| `observed_symptoms` | 图中可见症状 |
| `possible_causes` | 候选原因，包含 `name` 和 `reason` |
| `recommendations` | 下一步建议 |
| `follow_up_questions` | 建议补充的信息 |

失败响应统一为 `{"error":{"code":"错误代码","message":"可读错误说明"}}`。不会向小程序传递模型服务的原始错误正文或 API Key。

可在 `/docs` 上传照片，也可以在 PowerShell 中调用（`curl.exe` 避免与 PowerShell 别名冲突）：

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/diagnose -F "image=@D:\photos\tomato.jpg" -F "description=下部叶片出现褐色斑点"
```

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
```

已完成以下检查：

- 后端 106 项测试通过，包含候选病害的数值范围、顺序、重复病名和状态一致性校验；Ruff 检查和格式检查通过。
- 小程序 27 项测试通过，包含候选展示、非病害状态不生成百分比、上传逻辑及 WXML 显示条件回归检查；JavaScript 语法和 JSON 配置检查通过。
- 使用本机微信开发者工具自带的 WXML / WXSS 编译器完成模板、全局样式和页面样式编译。
- 启动 Uvicorn 后，通过实际 HTTP 检查健康接口、OpenAPI、缺少图片及缺少密钥的错误响应。

后端自动化测试使用实际编码的图片和模拟上游 HTTP 响应，不消耗模型额度；小程序逻辑测试使用微信 API 替身，不等同于模拟器或真机验证。测试依赖 Starlette 会报告一条 HTTPX TestClient 弃用提示，不影响本次测试结果。

2026-10-21 已使用本地配置的真实 API Key 和业务空间地址完成一次 `qwen3.8-flash` 图片调用：上游和后端均返回 HTTP 200，结构化结果校验通过，单次后端耗时约 3.4 秒。输入为纯色联通测试图，模型返回 `uncertain`；此检查验证图片接口兼容性，不代表番茄病害识别准确率或典型响应速度。

新增病害可能性字段后，又用纯色图检查真实接口：后端返回 HTTP 200，耗时约 3.1 秒，结果为 `uncertain`、`disease_candidates: []`，新结构校验通过。疑似病害及其他候选的展示、数值校验由上述自动化测试验证，尚未用真实病害照片验证。

尚未进行真实番茄病害样本评估，也尚未完成微信模拟器及手机真机联调。

## 正式发布前

本版本面向本地开发。公网部署前需为后端增加访问控制和调用配额限制，配置 HTTPS，并在微信后台配置 `request` / `uploadFile` 合法域名及涉及照片的用户隐私声明。不要将当前无鉴权接口直接公开，以免他人消耗模型额度。

## 目录

```text
backend/                    FastAPI、图片处理、千问客户端及测试
miniprogram/                原生微信小程序
tests/miniprogram.test.cjs   小程序网络与状态测试
project.config.json         微信开发者工具项目入口
docs/superpowers/           已确认范围及实施计划
```
