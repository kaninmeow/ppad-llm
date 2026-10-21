from contextlib import asynccontextmanager
from time import perf_counter
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app.errors import APIError
from app.images import prepare_image
from app.qwen import QwenClient
from app.schemas import DiagnosisResponse
from app.settings import Settings


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
            yield

    app = FastAPI(title="番茄病害初步识别", lifespan=lifespan)

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
            "model": settings.qwen_model,
            "limits": {"max_upload_bytes": settings.max_upload_bytes},
        }

    @app.post("/api/diagnose", response_model=DiagnosisResponse)
    async def diagnose(
        request: Request,
        image: Annotated[UploadFile, File()],
        description: Annotated[str, Form()] = "",
    ):
        started = perf_counter()
        description = description.strip()
        if len(description) > 500:
            raise APIError(422, "INVALID_REQUEST", "补充描述不能超过 500 字。")
        if not settings.configured:
            raise APIError(
                503, "MODEL_NOT_CONFIGURED", "服务尚未配置模型密钥，请联系管理员。"
            )
        try:
            data = await image.read(settings.max_upload_bytes + 1)
        finally:
            await image.close()
        prepared = await run_in_threadpool(prepare_image, data, settings)
        result = await request.app.state.qwen.diagnose(prepared, description)
        return DiagnosisResponse(
            request_id=str(uuid4()),
            model=settings.qwen_model,
            elapsed_ms=int((perf_counter() - started) * 1000),
            result=result,
        )

    return app


app = create_app()
