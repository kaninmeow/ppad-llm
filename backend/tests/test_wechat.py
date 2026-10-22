import asyncio
import base64
import hashlib
import json
import logging
import struct
import time
import traceback

import httpx
import pytest
from pydantic import ValidationError

from app.errors import APIError
from app.settings import Settings
from app.wechat import WeChatClient
from app.wechat_callback import WeChatCallback

APPID = "wxba5fad812f8e6fb9"
SECRET = "private-app-secret"
TOKEN = "AAAAA"
KEY = bytes(range(32))
PASS = {"errcode": 0, "result": {"suggest": "pass"}}


def settings(**overrides):
    values = dict(
        wechat_app_id=APPID,
        wechat_app_secret=SECRET,
        wechat_callback_token=TOKEN,
        wechat_encoding_aes_key=base64.b64encode(KEY).decode().rstrip("="),
        public_base_url="https://public.example/prefix/",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def assert_safe(exc, code="SECURITY_UNAVAILABLE", status=503):
    assert isinstance(exc.value, APIError)
    assert exc.value.code == code
    assert exc.value.status_code == status
    assert SECRET not in str(exc.value)
    assert "RAW-SENSITIVE" not in exc.value.message


def run_client(handler, operation):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await operation(WeChatClient(settings(), http))

    return asyncio.run(run())


def test_settings_configuration_and_defaults():
    config = settings()
    assert config.security_configured
    assert config.public_base_url == "https://public.example/prefix"
    assert config.wechat_request_timeout == 10
    assert config.moderation_job_ttl_seconds == 2100
    assert config.moderation_max_jobs == 100
    assert config.moderation_max_pending == 20
    assert config.moderation_max_pending_per_user == 2
    assert not Settings(_env_file=None).security_configured
    assert not settings(wechat_app_secret="").security_configured
    assert not settings(public_base_url="http://example.com").security_configured
    assert SECRET not in repr(config)


@pytest.mark.parametrize("key", ["invalid", "A" * 42, "!" * 43])
def test_settings_reject_invalid_aes_key(key):
    with pytest.raises(ValidationError):
        settings(wechat_encoding_aes_key=key)


def test_settings_validation_error_and_traceback_hide_invalid_secret():
    invalid_secret = "SENSITIVE_INVALID_AES_KEY_NEVER_LOG"
    with pytest.raises(ValidationError) as exc:
        settings(wechat_encoding_aes_key=invalid_secret)
    assert invalid_secret not in str(exc.value)
    assert invalid_secret not in "".join(traceback.format_exception(exc.value))
    assert "WECHAT_ENCODING_AES_KEY must encode 32 bytes" in str(exc.value)


def test_login_token_cache_text_and_media_contract(caplog):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "api.weixin.qq.com"
        assert request.url.scheme == "https"
        assert request.extensions["timeout"]["read"] == 10
        if request.url.path == "/sns/jscode2session":
            assert request.url.params["secret"] == SECRET
            assert request.url.params["js_code"] == "login-code"
            return httpx.Response(200, json={"openid": "openid_valid-123"})
        body = json.loads(request.content)
        if request.url.path == "/cgi-bin/stable_token":
            assert body == {
                "grant_type": "client_credential",
                "appid": APPID,
                "secret": SECRET,
                "force_refresh": False,
            }
            return httpx.Response(
                200, json={"access_token": "private-token", "expires_in": 7200}
            )
        assert request.url.params["access_token"] == "private-token"
        assert body["openid"] == "openid_valid-123"
        assert body["scene"] == body["version"] == 2
        if request.url.path == "/wxa/msg_sec_check":
            assert body["content"] == "叶片发黄"
            return httpx.Response(200, json=PASS)
        assert request.url.path == "/wxa/media_check_async"
        assert body["media_type"] == 2
        assert body["media_url"] == "https://public.example/image"
        return httpx.Response(200, json={"errcode": 0, "trace_id": "trace-123"})

    async def operation(client):
        openid = await client.exchange_code("login-code")
        await client.check_text(openid, "叶片发黄")
        assert (
            await client.submit_image(openid, "https://public.example/image")
            == "trace-123"
        )

    with caplog.at_level(logging.DEBUG):
        run_client(handler, operation)
    assert len(requests) == 4
    assert SECRET not in caplog.text
    assert "private-token" not in caplog.text
    assert "login-code" not in caplog.text


@pytest.mark.parametrize("suggest", ["review", "risky"])
def test_text_rejection(suggest):
    def handler(request):
        if request.url.path == "/cgi-bin/stable_token":
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 7200}
            )
        return httpx.Response(
            200,
            json={
                "errcode": 0,
                "result": {"suggest": suggest},
                "keyword": "RAW-SENSITIVE",
            },
        )

    with pytest.raises(APIError) as exc:
        run_client(handler, lambda c: c.check_text("openid", "description"))
    assert_safe(exc, "CONTENT_REJECTED", 422)
    assert exc.value.message == "您提交的内容含违规信息。"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"errcode": False, "result": {"suggest": "pass"}},
        {"errcode": "0", "result": {"suggest": "pass"}},
        {"result": {"suggest": "pass"}},
        {"errcode": 0},
        {"errcode": 0, "result": []},
        {"errcode": 0, "result": {"suggest": "unknown"}},
        {"errcode": -1, "errmsg": "RAW-SENSITIVE"},
    ],
)
def test_text_malformed_results_fail_closed(payload):
    def handler(request):
        if request.url.path == "/cgi-bin/stable_token":
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 7200}
            )
        return httpx.Response(200, json=payload)

    with pytest.raises(APIError) as exc:
        run_client(handler, lambda c: c.check_text("openid", "description"))
    assert_safe(exc)


def test_text_unicode_chunks_overlap_and_check_all():
    chunks = []
    content = "".join(chr(0x4E00 + i) for i in range(6100))

    def handler(request):
        if request.url.path == "/cgi-bin/stable_token":
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 7200}
            )
        chunks.append(json.loads(request.content)["content"])
        return httpx.Response(200, json=PASS)

    run_client(handler, lambda c: c.check_text("openid", content))
    assert len(chunks) == 3
    assert all(0 < len(chunk) <= 2500 for chunk in chunks)
    assert chunks == [content[:2500], content[2400:4900], content[4800:]]


@pytest.mark.parametrize("errcode", [40001, 40014, 42001])
def test_invalid_token_recovers_only_once(errcode):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/cgi-bin/stable_token":
            return httpx.Response(
                200, json={"access_token": str(len(calls)), "expires_in": 7200}
            )
        return httpx.Response(200, json={"errcode": errcode, "errmsg": "RAW-SENSITIVE"})

    with pytest.raises(APIError) as exc:
        run_client(handler, lambda c: c.check_text("openid", "description"))
    assert_safe(exc)
    assert calls == ["/cgi-bin/stable_token", "/wxa/msg_sec_check"] * 2


def test_invalid_token_recovery_success():
    protected = 0

    def handler(request):
        nonlocal protected
        if request.url.path == "/cgi-bin/stable_token":
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 7200}
            )
        protected += 1
        return httpx.Response(200, json={"errcode": 42001} if protected == 1 else PASS)

    run_client(handler, lambda c: c.check_text("openid", "description"))
    assert protected == 2


@pytest.mark.parametrize("failure", ["timeout", "json", "http", "unknown"])
def test_unknown_outcomes_never_retry(failure):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/cgi-bin/stable_token":
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 7200}
            )
        if failure == "timeout":
            raise httpx.ReadTimeout("RAW-SENSITIVE", request=request)
        if failure == "json":
            return httpx.Response(200, text="RAW-SENSITIVE")
        return httpx.Response(500 if failure == "http" else 200, json={"errcode": -1})

    with pytest.raises(APIError) as exc:
        run_client(
            handler, lambda c: c.submit_image("openid", "https://public.example/image")
        )
    assert_safe(exc)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("login", {"errcode": 40029, "errmsg": "RAW-SENSITIVE"}),
        ("login", {"openid": "bad space"}),
        ("login", {"openid": True}),
        ("login", {"errcode": False, "openid": "openid"}),
        ("token", {"access_token": True, "expires_in": 7200}),
        ("token", {"access_token": "token", "expires_in": False}),
        ("media", {"errcode": 0, "trace_id": True}),
        ("media", {"errcode": 0, "trace_id": "bad\ntrace"}),
        ("media", {"errcode": False, "trace_id": "trace"}),
        ("media", {"trace_id": "trace"}),
    ],
)
def test_malformed_identity_token_trace_are_sanitized(endpoint, payload):
    def handler(request):
        if endpoint == "login" or endpoint == "token":
            return httpx.Response(200, json=payload)
        if request.url.path == "/cgi-bin/stable_token":
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 7200}
            )
        return httpx.Response(200, json=payload)

    with pytest.raises(APIError) as exc:
        run_client(
            handler,
            lambda c: (
                c.exchange_code("code")
                if endpoint == "login"
                else c.submit_image("openid", "https://public.example/image")
            ),
        )
    assert_safe(
        exc,
        "LOGIN_FAILED" if endpoint == "login" else "SECURITY_UNAVAILABLE",
        401 if endpoint == "login" else 503,
    )


def test_concurrent_token_fetch_is_single_and_expiry_uses_monotonic(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.wechat.time.monotonic", lambda: clock[0])
    count = 0

    async def handler(request):
        nonlocal count
        if request.url.path == "/cgi-bin/stable_token":
            count += 1
            await asyncio.sleep(0)
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 120}
            )
        return httpx.Response(200, json=PASS)

    async def operation(client):
        await asyncio.gather(*(client.check_text("openid", "text") for _ in range(8)))
        assert count == 1
        clock[0] += 121
        await client.check_text("openid", "text")

    run_client(handler, operation)
    assert count == 2


def envelope(payload, *, appid=APPID, padding=None, length=None):
    # Independently construct the documented wire layout, without production helpers.
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = (
        b"0123456789abcdef"
        + struct.pack("!I", len(payload) if length is None else length)
        + payload
        + appid.encode()
    )
    pad = 32 - len(raw) % 32
    raw += bytes([pad]) * pad
    if padding is not None:
        raw = raw[:-1] + bytes([padding])
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(KEY[:16])).encryptor()
    return base64.b64encode(encryptor.update(raw) + encryptor.finalize()).decode()


def params(encrypted=None, *, timestamp=None):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    values = [TOKEN, timestamp, "nonce"]
    if encrypted is not None:
        values.append(encrypted)
    return {
        "timestamp": timestamp,
        "nonce": "nonce",
        **({"encrypt_type": "aes"} if encrypted is not None else {}),
        "msg_signature" if encrypted is not None else "signature": hashlib.sha1(
            "".join(sorted(values)).encode()
        ).hexdigest(),
    }


def test_callback_plain_and_encrypted_url_verification():
    callback = WeChatCallback(settings())
    assert callback.verify_url({**params(), "echostr": "challenge"}) == "challenge"
    encrypted = envelope(b"encrypted-challenge")
    assert (
        callback.verify_url({**params(encrypted), "echostr": encrypted})
        == "encrypted-challenge"
    )
    encrypted_query = {**params(encrypted), "echostr": encrypted}
    encrypted_query.pop("encrypt_type")
    assert callback.verify_url(encrypted_query) == "encrypted-challenge"


@pytest.mark.parametrize("outer_format", ["json", "xml"])
@pytest.mark.parametrize("inner_format", ["json", "xml"])
def test_encrypted_callback_json_and_xml(outer_format, inner_format):
    event = {
        "Event": "wxa_media_check",
        "trace_id": "trace-123",
        "result": {"suggest": "pass"},
    }
    raw = (
        json.dumps(event).encode()
        if inner_format == "json"
        else (
            b"<xml><Event>wxa_media_check</Event><trace_id>trace-123</trace_id>"
            b"<result><suggest>pass</suggest></result></xml>"
        )
    )
    encrypted = envelope(raw)
    body = (
        json.dumps({"Encrypt": encrypted, "AppId": APPID}).encode()
        if outer_format == "json"
        else (
            f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt>"
            f"<AppId>{APPID}</AppId></xml>"
        ).encode()
    )
    decoded = WeChatCallback(settings()).decode_event(params(encrypted), body)
    assert decoded == event
    assert decoded.is_xml is (inner_format == "xml")


def test_official_callback_ciphertext(monkeypatch):
    monkeypatch.setattr("app.wechat_callback.time.time", lambda: 1714112445)
    encrypted = (
        "+qdx1OKCy+5JPCBFWw70tm0fJGb2Jmeia4FCB7kao+/Q5c/ohsOzQHi8khUOb05JCpj0J"
        "B4RvQMkUyus8TPxLKJGQqcvZqzDpVzazhZv6JsXUnnR8XGT740XgXZUXQ7vJVnAG+tE8"
        "NUd4yFyjPy7GgiaviNrlCTj+l5kdfMuFUPpRSrfMZuMcp3Fn2Pede2IuQrKEYwKSqFIZo"
        "NqJ4M8EajAsjLY2km32IIjdf8YL/P50F7mStwntrA2cPDrM1kb6mOcfBgRtWygb3VIYnS"
        "eOBrebufAlr7F9mFUPAJGj04="
    )
    query = {
        "timestamp": "1714112445",
        "nonce": "415670741",
        "encrypt_type": "aes",
        "msg_signature": "046e02f8204d34f8ba5fa3b1db94908f3df2e9b3",
    }
    event = WeChatCallback(settings(wechat_encoding_aes_key="A" * 43)).decode_event(
        query, json.dumps({"Encrypt": encrypted}).encode()
    )
    assert event["debug_str"] == "hello world"
    assert event["Event"] == "debug_demo"


def test_callback_xml_preserves_nested_collections_without_verdict_interpretation():
    raw = (
        b"<xml><version>2</version><errcode>0</errcode><detail>"
        b"<item><suggest>pass</suggest></item><item><suggest>review</suggest></item>"
        b"</detail></xml>"
    )
    encrypted = envelope(raw)
    event = WeChatCallback(settings()).decode_event(
        params(encrypted), json.dumps({"Encrypt": encrypted}).encode()
    )
    assert event == {
        "version": "2",
        "errcode": "0",
        "detail": {"item": [{"suggest": "pass"}, {"suggest": "review"}]},
    }


@pytest.mark.parametrize(
    "case",
    [
        "signature",
        "past",
        "future",
        "timestamp",
        "padding",
        "appid",
        "outer_appid",
        "length",
        "plaintext",
        "oversized",
        "doctype",
        "entity",
        "declaration",
        "malformed",
        "duplicate_json",
        "duplicate_xml",
        "inner_declaration",
    ],
)
def test_callback_invalid_data_fail_closed(case):
    raw = b'{"Event":"wxa_media_check"}'
    if case == "inner_declaration":
        raw = b'<?xml version="1.0"?><xml><Event>x</Event></xml>'
    encrypted = envelope(
        raw,
        appid="other-app" if case == "appid" else APPID,
        padding=0 if case == "padding" else None,
        length=999999 if case == "length" else None,
    )
    query = params(encrypted)
    body = json.dumps(
        {"Encrypt": encrypted, "AppId": "wrong" if case == "outer_appid" else APPID}
    ).encode()
    if case == "signature":
        query["msg_signature"] = "0" * 40
    if case in {"past", "future"}:
        query = params(
            encrypted, timestamp=int(time.time()) + (-601 if case == "past" else 601)
        )
    if case == "timestamp":
        query["timestamp"] = "bad-time"
    if case == "plaintext":
        query.pop("encrypt_type")
    if case == "oversized":
        body = b"x" * 65537
    if case in {"doctype", "entity", "declaration"}:
        body = {
            "doctype": b"<!DOCTYPE xml><xml/>",
            "entity": b'<!DOCTYPE xml [<!ENTITY a "x">]><xml>&a;</xml>',
            "declaration": b'<?xml version="1.0"?><xml/>',
        }[case]
    if case == "malformed":
        body = b"[]"
    if case == "duplicate_json":
        body = (
            '{"Encrypt":"' + encrypted + '","Encrypt":"' + encrypted + '"}'
        ).encode()
    if case == "duplicate_xml":
        body = (
            f"<xml><Encrypt>{encrypted}</Encrypt><Encrypt>{encrypted}</Encrypt></xml>"
        ).encode()
    with pytest.raises(APIError) as exc:
        WeChatCallback(settings()).decode_event(query, body)
    assert_safe(exc)


@pytest.mark.parametrize("outcome", ["timeout", "redirect", "invalid_json"])
def test_login_transport_errors_sanitized_and_not_retried(outcome):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if outcome == "timeout":
            raise httpx.ReadTimeout("RAW-SENSITIVE", request=request)
        if outcome == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.example"})
        return httpx.Response(200, text="RAW-SENSITIVE")

    with pytest.raises(APIError) as exc:
        run_client(handler, lambda c: c.exchange_code("code"))
    assert_safe(exc, "LOGIN_FAILED", 401)
    assert count == 1


@pytest.mark.parametrize(
    "encrypted",
    ["!bad-base64!", "AA==", "A" * 65537],
    ids=["invalid-base64", "short", "oversized"],
)
def test_callback_signed_malformed_ciphertext_is_sanitized(encrypted):
    with pytest.raises(APIError) as exc:
        WeChatCallback(settings()).decode_event(
            params(encrypted), json.dumps({"Encrypt": encrypted}).encode()
        )
    assert_safe(exc)


@pytest.mark.parametrize("kind", ["signature", "stale", "unknown_mode", "oversized"])
def test_url_verification_rejects_invalid_challenges(kind):
    query = {**params(), "echostr": "challenge"}
    if kind == "signature":
        query["signature"] = "0" * 40
    elif kind == "stale":
        query = {**params(timestamp=int(time.time()) - 601), "echostr": "challenge"}
    elif kind == "unknown_mode":
        query["encrypt_type"] = "unknown"
    else:
        query["echostr"] = "x" * 65537
    with pytest.raises(APIError) as exc:
        WeChatCallback(settings()).verify_url(query)
    assert_safe(exc)


def test_whitespace_chunks_still_require_explicit_pass():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/cgi-bin/stable_token":
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 7200}
            )
        return httpx.Response(200, json={})

    with pytest.raises(APIError) as exc:
        run_client(handler, lambda c: c.check_text("openid", " " * 2500))
    assert_safe(exc)
    assert calls == ["/cgi-bin/stable_token", "/wxa/msg_sec_check"]
