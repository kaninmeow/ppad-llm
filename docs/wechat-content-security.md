# 微信内容安全部署与联调

代码已接入微信登录、用户描述检测、图片异步检测和模型输出文本检测，新增六作物 v2 客户端。**历史记录（2026-10-22）：当时尚未验证真实微信接入、HTTPS、公网回调、模拟器或手机全流程，本地仅确认已填写模型配置。** 2026-10-24 本次变更没有重新检查当前外部 HTTPS/微信配置，也没有进行新的真实模型调用、设备联调或准确率验证。本地设置标志只表示是否填写，不证明远程服务可用。

## 处理与存储边界

小程序每次识别先调用 `wx.login`，后端用一次性 code 换取 OpenID。后端验证并规范化图片，先检测补充描述，再向微信提交规范化 JPEG 的异步检测；只有微信 v2 检测明确返回 `errcode=0` 和 `result.suggest=pass` 才进入下一步。`review`、`risky` 均拦截，未知或异常结果不放行。图片通过后只调用一次云端模型，模型生成的全部动态文本也须检测通过后才返回。

任务存于单进程内存，最长 35 分钟；总容量 100、未完成任务 20、每个 OpenID 未完成任务 2，每 30 秒清理。终态立即清除照片、描述、OpenID 和媒体能力凭证；结果和安全错误字段保留至任务到期。进程重启丢失任务与结果，用户须重新提交。云端服务商自身的数据处理政策另行适用，不应向用户承诺完全不留存。照片经 EXIF 校正、缩放和移除原始元数据后转 JPEG，原有 8 MiB 等图片限制不变。

**仅运行一个 Uvicorn worker 和一个后端实例。** 不可启用多 worker、负载均衡至多个实例或自动扩容，否则回调、图片下载和轮询可能落在不同进程。需要扩容时须先实现共享任务存储和协调。

## HTTPS 与运行配置

1. 准备真实域名，将 DNS A/AAAA 指向服务器。开放公网 TCP 80/443，保证域名解析、证书申请和续期可用；保持 Caddy 数据目录持久化。已有 HTTP 8189 进程须迁移为下述环回绑定，由 Caddy 对外提供 HTTPS。防火墙不必向公网开放 8189。
2. 在后端本地 `.env` 填写下表。实际 Secret 只在后端保存，不进 Git、前端、截图或日志。进程环境变量优先于 `.env`，更改后重启。
3. 启动一个实例（生产不使用 `--reload`）：

```powershell
Set-Location D:\project\PlantPestAndDiseaseIdentificationSystem\pd-LLM\backend
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8189 --workers 1 --no-access-log
```

4. 复制并修改 [Caddy 示例](../deploy/Caddyfile.example)，将 `api.example.com` 替换为自己的域名。Caddy 默认自动申请 HTTPS 证书，示例不启用 access log：

```caddyfile
api.example.com {
    reverse_proxy 127.0.0.1:8189
}
```

5. 将 `PUBLIC_BASE_URL` 设置为 `https://api.example.com`。它是微信下载临时图片的公网 HTTPS origin，不能填写本机地址、HTTP、用户名密码、查询参数或 fragment；支持一致的反向代理路径前缀，但该示例不使用前缀。
6. HTTPS 部署成功后，由操作者将 `miniprogram/config.js` 的 `API_BASE_URL` 设置为同一个 HTTPS 地址。本次未修改该文件；2026-10-22 曾记录其值为 HTTP 地址，这不能证明当前外部服务配置状态。正式联调前重新核对真实地址，不要把后端密钥复制到此文件。

| 后端变量 | 用途 |
| --- | --- |
| `DASHSCOPE_API_KEY` | 阿里云百炼密钥，沿用现有模型配置 |
| `QWEN_BASE_URL` / `QWEN_MODEL` | 沿用原有业务空间/地域地址与 `qwen3.8-flash`，不因接入内容安全改动 |
| `WECHAT_APP_ID` | 与实际导入微信开发者工具项目相同的真实小程序 AppID |
| `WECHAT_APP_SECRET` | 该小程序的 AppSecret，仅后端 |
| `WECHAT_CALLBACK_TOKEN` | 微信消息推送设置中的自定义 Token，仅后端 |
| `WECHAT_ENCODING_AES_KEY` | 消息推送设置中的 EncodingAESKey，仅后端 |
| `PUBLIC_BASE_URL` | 微信可访问的公网 HTTPS 基地址 |

`/health` 的 `configured` 和 `security_configured` 仅说明本地设置已填写，不证明微信白名单、权限、凭据或回调可用。可在本机使用 HTTP 检查服务启动和接口结构；关闭开发者工具域名校验、游客登录或纯 HTTP localhost 都不能验证完整审核流程。

## 微信后台和开发者工具

- **AppID 必须一致。** 根目录 `project.config.json` 当前是 `touristappid`，`miniprogram/project.config.json` 当前是实际公开 AppID `wxeec0a1e738ed4126`。如果导入根目录，需由操作者把根项目游客 ID 改为自己有权限的真实 AppID；无论从哪个入口导入，开发者工具实际使用的 AppID 必须等于后端 `WECHAT_APP_ID`。本次没有自动改写任一项目 ID。游客 code 无法完成该真实小程序的登录换取 OpenID。
- 在小程序后台配置服务器出口公网 IP 白名单；注意 NAT 出口 IP，不能填写 `127.0.0.1`。确认该 AppID 有所需内容安全接口权限，且 AppSecret 正确。
- 在「开发管理 / 开发设置」配置 `request` 和 `uploadFile` 的服务器合法域名为真实 HTTPS 域名。使用有效证书和平台支持的 TLS，正式验证时恢复合法域名校验。接口 8189 由反向代理接入，前端使用 HTTPS 默认 443。
- 配置消息推送 URL 为 `https://api.example.com/api/wechat/callback`，选择 **安全模式（AES 加密）**。Token、EncodingAESKey 必须与后端本地配置一致，支持 JSON 或 XML 消息格式。完成微信发起的 URL 校验；不以浏览器直接打开无签名 callback 返回成功作为配置通过依据。
- 更新涉及照片、描述、微信登录和云端处理的用户隐私说明及平台声明。页面已提示微信内容安全检测、云端模型分析及短期处理存储。

## 接口约定

`POST /api/v2/diagnose` 接收 multipart `image`、必填 `crop_id`、可选 `description`（最多 500 字）、必填 `login_code`（非空，最多 512 字）。每次提交重新登录；禁止自动重复 POST 或自动重试付费模型。

`crop_id` 无默认值，仅允许 `tomato`（番茄）、`cucumber`（黄瓜）、`pepper`（辣椒）、`eggplant`（茄子）、`potato`（马铃薯）、`chinese_cabbage`（大白菜）。大白菜不含小白菜和甘蓝。缺少或非法值返回 422 `INVALID_REQUEST`；客户端在登录前拒绝非法选择。用户选择是上下文，不能作为已核实的视觉证据。

受理成功仅返回 **HTTP 202**：

```json
{"job_id":"opaque-id","job_token":"private-token","status":"checking","poll_after_ms":1500}
```

**202 表示已受理，尚未检测通过或识别完成。** 小程序不接受旧版 POST 直接 200。`job_id` 和 `job_token` 是有界、非空、URL 安全的随机字符串。客户端仅从已配置基地址构造 `GET /api/v2/diagnose/{job_id}`，请求头为 `Authorization: Bearer <job_token>`，不能跟随响应中的自定义轮询 URL。job token 不进 URL、UI、持久化或日志。

进行中 GET 返回 202 `{"status":"checking"或"analysing","poll_after_ms":1500}`。客户端将间隔限制在 1000–5000 ms，单次网络超时与总体期限分开；从登录开始总等待上限为 32 分钟，退出页面停止请求与定时器并忽略晚到回调。退出或超时只停止客户端等待，后端已受理任务可能继续完成，不能据此立即自动重复付费提交。

只有 GET **200** 才交付 `MultiCropDiagnosisResponse`：`request_id`、`model`、`elapsed_ms`、`notice` 和 `result`。v2 保留原有病害候选结构与长度限制，增加 `result.crop` 的 `selected`、`detected`、`verification` 和 1–300 字非空 `reason`。`selected` 必须等于本次提交值，所有层级均不允许未知字段。

| verification | detected | 允许的 result.status |
| --- | --- | --- |
| `matched` | 与 selected 相同 | `suspected_disease` / `healthy` / `uncertain` |
| `unverified` | `null` | `suspected_disease` / `healthy` / `uncertain` |
| `mismatch` | 另一受支持的作物 ID | `crop_mismatch` |
| `unsupported` | `other` | `unsupported_crop` |

所有非病害状态均为 `disease_name: null` 和 `disease_candidates: []`；`crop_mismatch`、`unsupported_crop` 还必须有 `possible_causes: []`。关系不符、回显与提交不符或嵌套结构非法均以固定 `INVALID_MODEL_RESPONSE` 结束，不修改或自动重试结果。`crop.reason` 同其他所有输出文本一起经内容安全检查后交付。`unverified` 页面突出“按你选择的{作物}分析”和“图片无法独立确认作物”，错配/不支持状态提示重新选择，不展示病害/概率部分。此前前端只显示占位图且禁止预览；成功后才显示照片与完整结果。服务/网络错误保留本地输入供用户手动重试，不能视为检测通过。

错误格式仍为 `{"error":{"code":"…","message":"…"}}`，小程序显示固定安全说明，忽略上游原始错误文案：

| HTTP | code | 行为 |
| --- | --- | --- |
| 401 | `LOGIN_FAILED` | 登录失败，不继续上传/分析 |
| 422 | `INVALID_REQUEST` | 检查作物、图片和描述，手动重试 |
| 502 | `INVALID_MODEL_RESPONSE` | 结果结构或作物关系不合法，不显示结果/照片，不自动重试 |
| 400 | `INVALID_IMAGE` / `UNSUPPORTED_IMAGE` | 检查图片是否有效及格式是否受支持，手动重试 |
| 413 | `IMAGE_TOO_LARGE` | 选择符合大小限制的图片，手动重试 |
| 422 | `CONTENT_REJECTED` | 清空照片、描述、结果和预览许可，仅显示 `您提交的内容含违规信息。`，不展示类别、标签、命中词或原文 |
| 429 | `QUEUE_FULL` | 队列已满，稍后手动重试；客户端也兼容 `TOO_MANY_REQUESTS` |
| 404 | `NOT_FOUND` | 任务不存在、过期或凭证错误，统一失效提示；客户端也兼容 `JOB_NOT_FOUND` |
| 503 | `SECURITY_NOT_CONFIGURED` | 管理员补充内容安全设置 |
| 503 | `SECURITY_UNAVAILABLE` | 检测异常或未知结果，暂不放行 |
| 504 | `SECURITY_TIMEOUT` | 内容安全等待超时，暂不放行 |

原有模型/图片错误仍使用各自稳定 code。HTTP 状态为服务端响应约定；客户端总体 32 分钟期限也采用 `SECURITY_TIMEOUT`，原生单次网络超时为 `NETWORK_TIMEOUT`。

`GET /api/moderation-media/{media_token}.jpg` 是供微信临时下载规范化图片的能力地址，持有者在有效期内可下载。它与 job token 独立，终态或到期失效，**必须按秘密处理**。不要记录请求体、Authorization、媒体完整 URL、callback 签名查询串或上游原始错误；关闭 Uvicorn access log，不给 Caddy 示例添加 access log，其他网关、APM 和 CDN 也要避免保存这些路径与查询串。后端动态响应使用 `Cache-Control: no-store`，不要在代理层覆盖为公开缓存。

## 后端优先发布与兼容

1. 先部署同时提供 v1 和 v2 的后端，保留原 `POST /api/diagnose`、`GET /api/diagnose/{job_id}`、原 `DiagnosisResponse` 与 `not_tomato` 等状态；v1 不新增必填 crop_id。
2. 在隔离测试中验证旧接口回归、v2 必填参数/结构关系、私有轮询及输入/输出检测；准备同 AppID、HTTPS、安全回调与合法域名。
3. 再发布只访问 v2 的六作物客户端，按下节与[多作物验证矩阵](multi-crop-validation.md)实测。客户端不自动降级到 v1，不自动重试付费调用。
4. 新客户端仍在线时，后端回退不得撤掉 v2；需要撤掉 v2 时先安排客户端回退与兼容窗口。单实例重启会使在途任务失效，应提示手动重新提交。此次代码变更没有执行发布或验证线上状态。

## 真实微信烟测（尚未执行）

1. 使用有权限的同一 AppID 配好 HTTPS、后台合法域名、出口 IP 白名单和 AES 消息推送。只检查 health 设置标志和后台验证结果，不在报告中记录 Secret。
2. 在微信开发者工具开启域名校验，用本人账号登录。逐一明确选择六种支持的作物及其正常照片（初始不得默认选择），确认只见占位图且无法预览；点击识别后确认先登录、单次上传 202、轮询 Bearer 头、检测中到分析中、GET 200 后才出现照片/结果/预览。抓包截图隐藏 token、登录 code 和图片能力地址。
3. 用微信官方允许的内容安全测试方式，在受控环境验证描述、图片、模型输出各自的拦截。确认违规界面只有固定消息并清空已提交内容，不回显违规内容；不要为测试触发真实违规传播或修改生产检测逻辑。模型输出拦截可先用隔离测试替身验证，不将模拟结果记为真实微信通过。
4. 验证无权限/失效登录不继续上传；队列满、检测不可用、超时、失效任务分别给出安全提示；网络故障保留输入，但不显示照片。更换作物、照片、清除照片或编辑描述均清除旧结果和预览许可；更换作物保留本地照片/描述并清除进度；相册选择或识别中作物按钮锁定。
5. 上传/轮询/等待期间退出页面，确认无后续 UI 更新或自动重发；重启单实例后旧任务失效，需手动重新提交。此类重新提交可能产生新的模型调用，应由用户主动发起。
6. 手机真机在正常网络下重复正常流程和取消场景，记录日期、AppID 一致性（不记录 Secret）、HTTPS 与回调验证结果。只有这些实测完成后才能宣称完成真实微信联调。六作物诊断准确率还需真实标注样本单独评估，不得把契约/页面测试视为准确率证据。

## 官方资料

- [微信文本内容安全 v2 msgSecCheck](https://developers.weixin.qq.com/miniprogram/dev/server/API/sec-center/sec-check/api_msgseccheck.html)
- [微信图片/音视频异步检测 mediaCheckAsync](https://developers.weixin.qq.com/miniprogram/dev/server/API/sec-center/sec-check/api_mediacheckasync.html)
- [微信登录凭证校验 code2Session](https://developers.weixin.qq.com/miniprogram/dev/server/API/user-login/api_code2session.html)
- [稳定版 access_token](https://developers.weixin.qq.com/miniprogram/dev/server/API/mp-access-token/api_getstableaccesstoken.html)
- [微信消息推送与加解密](https://developers.weixin.qq.com/miniprogram/dev/framework/server-ability/message-push.html)
- [小程序网络、HTTPS 和服务器合法域名](https://developers.weixin.qq.com/miniprogram/dev/framework/ability/network.html)
- [Caddy 自动 HTTPS 前提](https://caddyserver.com/docs/automatic-https)
- [Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
