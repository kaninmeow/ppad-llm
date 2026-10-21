import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app.errors import APIError
from app.images import prepare_image
from app.moderation import ModerationJobs
from app.qwen import QwenClient
from app.settings import Settings
from app.wechat import WeChatClient, security_unavailable
from app.wechat_callback import WeChatCallback


class _HideCapabilityAccess(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn includes the full request target (including capability tokens).
        return not any(
            route in record.getMessage()
            for route in ("/api/moderation-media/", "/api/wechat/callback")
        )


logging.getLogger("uvicorn.access").addFilter(_HideCapabilityAccess())


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=False
        ) as client:
            app.state.qwen = QwenClient(settings, client)
            app.state.wechat = WeChatClient(settings, client)
            app.state.moderation = ModerationJobs(
                settings, app.state.wechat, app.state.qwen
            )
            app.state.callback = WeChatCallback(settings)
            cleanup = asyncio.create_task(app.state.moderation.cleanup_loop())
            try:
                yield
            finally:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
                await app.state.moderation.close()

    app = FastAPI(title="番茄病害初步识别", lifespan=lifespan)

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def require_security():
        if not settings.security_configured:
            raise security_unavailable()

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "请求参数无效，请上传图片，补充描述最多 500 字。",
                }
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {"code": "INVALID_REQUEST", "message": "请求格式或路径无效。"}
            },
        )

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "configured": settings.configured,
            "security_configured": settings.security_configured,
            "model": settings.qwen_model,
            "limits": {"max_upload_bytes": settings.max_upload_bytes},
        }

    @app.post("/api/diagnose", status_code=202)
    async def diagnose(
        request: Request,
        image: Annotated[UploadFile, File()],
        login_code: Annotated[str, Form(min_length=1, max_length=512)],
        description: Annotated[str, Form()] = "",
    ):
        description = description.strip()
        if not login_code.strip():
            raise APIError(422, "INVALID_REQUEST", "请先完成微信登录。")
        if len(description) > 500:
            raise APIError(422, "INVALID_REQUEST", "补充描述不能超过 500 字。")
        if not settings.configured:
            raise APIError(
                503, "MODEL_NOT_CONFIGURED", "服务尚未配置模型密钥，请联系管理员。"
            )
        require_security()
        try:
            data = await image.read(settings.max_upload_bytes + 1)
        finally:
            await image.close()
        prepared = await run_in_threadpool(prepare_image, data, settings)
        return await request.app.state.moderation.submit(
            prepared, description, login_code
        )

    @app.get("/api/diagnose/{job_id}")
    async def diagnosis_job(request: Request, job_id: str):
        require_security()
        result = request.app.state.moderation.get(
            job_id, request.headers.get("authorization", "")
        )
        return JSONResponse(result, status_code=202 if "status" in result else 200)

    @app.get("/api/moderation-media/{media_token}.jpg")
    async def moderation_media(request: Request, media_token: str):
        require_security()
        return Response(
            request.app.state.moderation.get_media(media_token), media_type="image/jpeg"
        )

    @app.get("/api/wechat/callback")
    async def verify_callback(request: Request):
        require_security()
        return PlainTextResponse(
            request.app.state.callback.verify_url(request.query_params)
        )

    @app.post("/api/wechat/callback")
    async def moderation_callback(request: Request):
        require_security()
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 64 * 1024:
                raise security_unavailable()
            body.extend(chunk)
        event = request.app.state.callback.decode_event(
            request.query_params, bytes(body)
        )
        request.app.state.moderation.event(event)
        return PlainTextResponse("success")

    return app


app = create_app()
