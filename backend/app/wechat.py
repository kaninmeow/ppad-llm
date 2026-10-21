"""WeChat server APIs with strict moderation verdicts and bounded token recovery."""

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping

import httpx

from app.errors import APIError
from app.settings import Settings

_HOST = "https://api.weixin.qq.com"
_INVALID_TOKENS = {40001, 40014, 42001}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_WECHAT_URL = re.compile(r"https://api\.weixin\.qq\.com/[^\s\"\']+")


class _RedactWeChatQuery(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTPX logs request URLs at INFO, including AppSecret on login and tokens.
        message = record.getMessage()
        record.msg = _WECHAT_URL.sub(
            lambda match: (
                match[0].split("?", 1)[0] + ("?<redacted>" if "?" in match[0] else "")
            ),
            message,
        )
        record.args = ()
        return True


logging.getLogger("httpx").addFilter(_RedactWeChatQuery())


def security_unavailable() -> APIError:
    return APIError(503, "SECURITY_UNAVAILABLE", "内容安全服务暂不可用，请稍后重试。")


def _login_failed() -> APIError:
    return APIError(401, "LOGIN_FAILED", "微信登录失败，请重新登录后重试。")


def _valid_string(value: object, limit: int = 4096) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and value.isascii()
        and all(33 <= ord(char) <= 126 for char in value)
    )


def _errcode(data: Mapping) -> int | None:
    value = data.get("errcode")
    return value if type(value) is int else None


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate object key")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise ValueError("Invalid JSON constant")


def strict_json(raw: bytes) -> dict:
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("Expected object")
    return value


class WeChatClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self._token: str | None = None
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = await self.client.request(
                method,
                _HOST + path,
                timeout=self.settings.wechat_request_timeout,
                follow_redirects=False,
                **kwargs,
            )
            if response.status_code != 200 or len(response.content) > 1024 * 1024:
                raise ValueError("Invalid response")
            return strict_json(response.content)
        except (httpx.HTTPError, ValueError, RecursionError):
            raise security_unavailable() from None

    async def exchange_code(self, code: str) -> str:
        try:
            if not self.settings.security_configured or not _valid_string(code, 512):
                raise _login_failed()
            data = await self._request(
                "GET",
                "/sns/jscode2session",
                params={
                    "appid": self.settings.wechat_app_id,
                    "secret": self.settings.wechat_app_secret.get_secret_value(),
                    "js_code": code,
                    "grant_type": "authorization_code",
                },
            )
            if "errcode" in data and _errcode(data) != 0:
                raise _login_failed()
            openid = data.get("openid")
            if not isinstance(openid, str) or not _IDENTIFIER.fullmatch(openid):
                raise _login_failed()
            return openid
        except APIError:
            raise _login_failed() from None

    async def _access_token(self, rejected: str | None = None) -> str:
        if not self.settings.security_configured:
            raise security_unavailable()
        async with self._token_lock:
            # Another coroutine may already have replaced a rejected token.
            if rejected is not None and self._token == rejected:
                self._token = None
                self._expires_at = 0
            if self._token is not None and time.monotonic() < self._expires_at:
                return self._token
            data = await self._request(
                "POST",
                "/cgi-bin/stable_token",
                json={
                    "grant_type": "client_credential",
                    "appid": self.settings.wechat_app_id,
                    "secret": self.settings.wechat_app_secret.get_secret_value(),
                    "force_refresh": False,
                },
            )
            token, expires = data.get("access_token"), data.get("expires_in")
            if (
                ("errcode" in data and _errcode(data) != 0)
                or not _valid_string(token)
                or type(expires) not in {int, float}
                or not 0 < expires <= 7200
            ):
                raise security_unavailable()
            self._token = token
            self._expires_at = time.monotonic() + expires - min(60, expires * 0.1)
            return token

    async def _protected(self, path: str, payload: dict) -> dict:
        token = await self._access_token()
        for attempt in range(2):
            data = await self._request(
                "POST", path, params={"access_token": token}, json=payload
            )
            code = _errcode(data)
            if attempt == 0 and code in _INVALID_TOKENS:
                token = await self._access_token(rejected=token)
                continue
            if code != 0:
                raise security_unavailable()
            return data
        raise security_unavailable()  # Defensive: both attempts always return or raise.

    async def check_text(self, openid: str, content: str) -> None:
        if not isinstance(openid, str) or not _IDENTIFIER.fullmatch(openid):
            raise security_unavailable()
        if not isinstance(content, str):
            raise security_unavailable()
        # 100 Unicode-character overlap preserves context across API boundaries.
        start = 0
        while start < len(content):
            chunk = content[start : start + 2500]
            if chunk:
                data = await self._protected(
                    "/wxa/msg_sec_check",
                    {"openid": openid, "content": chunk, "scene": 2, "version": 2},
                )
                result = data.get("result")
                if not isinstance(result, dict):
                    raise security_unavailable()
                suggest = result.get("suggest")
                if suggest in ("review", "risky"):
                    raise APIError(422, "CONTENT_REJECTED", "您提交的内容含违规信息。")
                if suggest != "pass":
                    raise security_unavailable()
            if start + 2500 >= len(content):
                break
            start += 2400

    async def submit_image(self, openid: str, media_url: str) -> str:
        if not isinstance(openid, str) or not _IDENTIFIER.fullmatch(openid):
            raise security_unavailable()
        if not isinstance(media_url, str) or not media_url.startswith("https://"):
            raise security_unavailable()
        data = await self._protected(
            "/wxa/media_check_async",
            {
                "openid": openid,
                "media_url": media_url,
                "media_type": 2,
                "scene": 2,
                "version": 2,
            },
        )
        trace = data.get("trace_id")
        if not isinstance(trace, str) or not _IDENTIFIER.fullmatch(trace):
            raise security_unavailable()
        return trace
