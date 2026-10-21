# 微信内容安全接入

## 目标

修复图片识别缺少微信内容安全检查的问题，覆盖图片、用户描述、模型输出。仅微信检测明确通过的内容可进入下一步或展示。用户目前没有可用的 HTTPS 域名及消息推送配置，代码和部署说明先准备好，真实微信联调需要部署后完成。

## 方案

沿用 FastAPI 和原生微信小程序，使用微信当前的 v2 `msgSecCheck`、`mediaCheckAsync`。旧同步图片检测版本已停止更新；微信云开发也是可选方案，但会改变现有部署方式。选择现有后端接收异步回调，保持现有项目结构。

1. 小程序每次提交前 `wx.login`，将临时 code 与图片、描述上传；后端通过 `code2Session` 获取 OpenID。
2. 后端验证、规范化图片，检测用户描述；创建带独立随机访问凭证的短期任务，通过带独立随机凭证的图片地址提交微信图片异步检测。提交成功只返回 HTTP 202，不代表检测通过。
3. 微信消息推送采用安全模式，校验签名、解密、验证 AppID 和事件格式。图片只有 `errcode=0`、`version=2`、`result.suggest=pass` 才能触发模型。`review`、`risky` 拦截；异常、未知结果和超时均不放行。
4. 模型仍用 `qwen3.8-flash`。所有动态文本字段经文本检测后才返回原有结构化结果和病害可能性。
5. 小程序轮询任务，展示检测中、识别中状态。检测前不渲染照片；违规清空图片、描述及结果，只提示“您提交的内容含违规信息。”，不暴露类别、命中词或原文。

## 接口与边界

- `POST /api/diagnose`：multipart `image`、`description`、必填 `login_code`；成功 202 `{job_id, job_token, status:"checking", poll_after_ms:1500}`。
- `GET /api/diagnose/{job_id}`：`Authorization: Bearer <job_token>`；进行中 202 `{status:"checking"|"analysing",poll_after_ms:1500}`；完成 200 原有 `DiagnosisResponse`；失败返回现有 error 结构。凭证错误统一 404。
- `GET /api/moderation-media/{media_token}.jpg`：供微信下载规范化 JPEG，随机凭证至少 256 位，无目录列表，`Cache-Control: no-store`；任务终止或到期后不可获取。
- `GET /api/wechat/callback`：微信服务器配置校验；`POST`：仅接收安全模式加密事件，JSON/XML 都支持，响应 `success`。
- 固定违规错误 `CONTENT_REJECTED`，HTTP 422，消息“您提交的内容含违规信息。”。
- 未配置微信服务返回 503 `SECURITY_NOT_CONFIGURED`；微信不可用、格式异常返回 `SECURITY_UNAVAILABLE`；超过期限返回 `SECURITY_TIMEOUT`，不伪装成违规。
- 微信公网地址必须 HTTPS，无凭证、query、fragment；Token、AppSecret、AES key 为后端 SecretStr 配置，不写入前端或日志。

## MVP 运行约束

采用有容量限制的进程内短期任务表，单 Uvicorn worker、单实例运行；不引入数据库和新基础设施。重启后任务不可恢复，用户重新提交；重启不会放行未检测内容。任务最长保留 35 分钟，最多保留 100 个任务、同时最多 20 个未完成任务、每个 OpenID 最多 2 个未完成任务。终态立即释放图片、描述和 OpenID，结果到期清理；每 30 秒清理过期任务。公开媒体凭证与查询凭证独立。

微信回调可在 30 分钟内到达；前端最长轮询 32 分钟，页面卸载取消网络与定时器，停止更新。后端模型任务有总体超时、异常拦截和生命周期取消。签名有效的先到回调通过有界短期缓存处理，重复回调不会重复触发付费模型。程序不自动重试付费模型。输出文本按不超过 2500 字的片段检查并保留片段重叠；任何一段未通过均不展示。

## 验证

使用模拟微信和模型 HTTP 上游、真实编码图片、加密回调向量测试：未认证无法提交；描述不通过不调用图片或模型；异步受理不触发模型；签名/解密/AppID/版本错误不放行；提前、重复回调安全；图片不通过不调用模型；模型输出不通过不返回；媒体与任务凭证隔离；超时清理与容量限制；前端登录、轮询、取消、违规提示与预览门禁。保留已有图片校验和结果结构测试。

## 部署与限制

补充 Caddy HTTPS 反向代理示例、配置表、微信后台的 request/uploadFile 合法域名、消息推送安全模式、IP 白名单及隐私处理说明。保留用户已修改的 `miniprogram/config.js`，上线时由用户配置 HTTPS 地址。无真实密钥及公网回调条件时，只能声明自动化检查完成，不能声明微信联调或审核通过。

## 官方资料

- [文本内容安全识别](https://developers.weixin.qq.com/miniprogram/dev/server/API/sec-center/sec-check/api_msgseccheck.html)
- [多媒体内容安全识别](https://developers.weixin.qq.com/miniprogram/dev/server/API/sec-center/sec-check/api_mediacheckasync.html)
- [小程序登录凭证校验](https://developers.weixin.qq.com/miniprogram/dev/server/API/user-login/api_code2session.html)
- [稳定版接口调用凭据](https://developers.weixin.qq.com/miniprogram/dev/server/API/mp-access-token/api_getstableaccesstoken.html)
- [消息推送与加解密](https://developers.weixin.qq.com/miniprogram/dev/framework/server-ability/message-push.html)
