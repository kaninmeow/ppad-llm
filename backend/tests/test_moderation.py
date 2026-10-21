import asyncio
import json
import logging
from contextlib import contextmanager
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_api import (
    TEST_KEY,
    api,
    assert_error,
    callback,
    completion,
    image_bytes,
    poll,
    queue,
    result_for,
)
from test_wechat import APPID, envelope, params, settings

from app.main import create_app
from app.moderation import strings


def signed_event(trace="trace-1", **changes):
    event = {
        "MsgType": "event",
        "Event": "wxa_media_check",
        "appid": APPID,
        "version": 2,
        "errcode": 0,
        "trace_id": trace,
        "result": {"suggest": "pass"},
    }
    event.update(changes)
    encrypted = envelope(json.dumps(event).encode())
    return {"params": params(encrypted), "json": {"Encrypt": encrypted}}


@contextmanager
def scenario(
    *,
    early=(),
    text_suggest="pass",
    login=True,
    media_error=False,
    model_wait=0,
    text_wait=0,
    **overrides,
):
    calls = {"model": [], "text": [], "media": [], "all": [], "cancelled": False}
    config = settings(dashscope_api_key=TEST_KEY, **overrides)

    async def handle(request):
        calls["all"].append(request)
        path = request.url.path
        if path == "/sns/jscode2session":
            payload = (
                {"openid": request.url.params["js_code"]}
                if login
                else {"errcode": 40029, "errmsg": "RAW-UPSTREAM-SECRET"}
            )
            return httpx.Response(200, json=payload)
        if path == "/cgi-bin/stable_token":
            return httpx.Response(
                200, json={"access_token": "private-token", "expires_in": 7200}
            )
        if path == "/wxa/msg_sec_check":
            calls["text"].append(json.loads(request.content)["content"])
            if text_wait:
                await asyncio.sleep(text_wait)
            suggest = (
                text_suggest(calls["text"]) if callable(text_suggest) else text_suggest
            )
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "result": {"suggest": suggest},
                    "raw": "RAW-UPSTREAM-SECRET",
                },
            )
        if path == "/wxa/media_check_async":
            calls["media"].append(json.loads(request.content)["media_url"])
            trace = "trace-" + str(len(calls["media"]))
            for changes in early:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as local:
                    response = await local.post(
                        "/api/wechat/callback", **signed_event(trace, **changes)
                    )
                    assert response.status_code == 200
            return httpx.Response(
                200,
                json={"errcode": 0, "trace_id": trace}
                if not media_error
                else {"errcode": -1, "errmsg": "RAW-UPSTREAM-SECRET"},
            )
        calls["model"].append(request)
        try:
            if model_wait:
                await asyncio.sleep(model_wait)
        except asyncio.CancelledError:
            calls["cancelled"] = True
            raise
        return httpx.Response(200, json=completion())

    app = create_app(config, httpx.MockTransport(handle))
    with TestClient(app) as client:
        yield client, calls


def lookup(client, queued, token=None):
    data = queued.json()
    return client.get(
        "/api/diagnose/" + data["job_id"],
        headers={
            "Authorization": "Bearer " + (data["job_token"] if token is None else token)
        },
    )


def state(client, inspect):
    async def run():
        return inspect(client.app.state.moderation)

    return client.portal.call(run)


def assert_inputs_cleared(client):
    assert state(
        client,
        lambda jobs: all(
            job.prepared is None
            and not job.description
            and not job.openid
            and not job.media_token
            for job in jobs.jobs.values()
        ),
    )
    assert state(client, lambda jobs: not jobs.media)


def test_queue_waits_for_authenticated_image_pass():
    with api() as (client, requests):
        queued = queue(client)
        assert queued.status_code == 202
        assert requests == []
        assert set(queued.json()) == {"job_id", "job_token", "status", "poll_after_ms"}
        assert client.get("/api/diagnose/" + queued.json()["job_id"]).status_code == 404
        assert callback(client).status_code == 200
        assert poll(client, queued).status_code == 200
        assert len(requests) == 1


@pytest.mark.parametrize("code", [None, "", "  ", "x" * 513])
def test_missing_empty_or_oversized_login_refused(code):
    with scenario() as (client, calls):
        data = {} if code is None else {"login_code": code}
        response = client.post(
            "/api/diagnose", data=data, files={"image": ("leaf.png", image_bytes())}
        )
        assert_error(response, 422, "INVALID_REQUEST")
        assert calls["all"] == []


def test_forged_login_refused():
    with scenario(login=False) as (client, calls):
        assert_error(queue(client), 401, "LOGIN_FAILED")
        assert len(calls["all"]) == 1
        assert state(client, lambda jobs: not jobs.jobs)


@pytest.mark.parametrize(
    "suggest,code,status",
    [
        ("risky", "CONTENT_REJECTED", 422),
        ("review", "CONTENT_REJECTED", 422),
        ("unknown", "SECURITY_UNAVAILABLE", 503),
    ],
)
def test_input_text_failure_releases_reservation(suggest, code, status):
    with scenario(text_suggest=suggest) as (client, calls):
        for _ in range(3):
            response = queue(client, description="测试描述")
            assert_error(response, status, code)
            if status == 422:
                assert response.json()["error"]["message"] == "您提交的内容含违规信息。"
        assert calls["model"] == calls["media"] == []
        assert state(client, lambda jobs: not jobs.jobs and not jobs.media)


def test_submission_failure_releases_reservation_and_sanitizes():
    with scenario(media_error=True) as (client, calls):
        for _ in range(3):
            assert_error(queue(client), 503, "SECURITY_UNAVAILABLE")
        assert not calls["model"]
        assert state(client, lambda jobs: not jobs.jobs and not jobs.media)


@pytest.mark.parametrize("suggest", ["risky", "review"])
def test_image_rejection_never_calls_model(suggest):
    with scenario() as (client, calls):
        queued = queue(client)
        assert callback(client, result={"suggest": suggest}).text == "success"
        response = poll(client, queued)
        assert_error(response, 422, "CONTENT_REJECTED")
        assert response.json()["error"]["message"] == "您提交的内容含违规信息。"
        assert not calls["model"]
        assert_inputs_cleared(client)


def test_output_text_checks_every_nested_string_before_release():
    with scenario() as (client, calls):
        queued = queue(client, description="叶片描述")
        callback(client)
        response = poll(client, queued)
        assert response.status_code == 200
        assert calls["text"] == ["叶片描述", "\n".join(strings(result_for()))]
        for name in ("疑似早疫病", "可见褐斑，需现场鉴别", "症状出现多久了？"):
            assert name in calls["text"][1]
        assert_inputs_cleared(client)


@pytest.mark.parametrize(
    "suggest,code,status",
    [
        ("review", "CONTENT_REJECTED", 422),
        ("risky", "CONTENT_REJECTED", 422),
        ("unknown", "SECURITY_UNAVAILABLE", 503),
    ],
)
def test_output_failure_never_exposes_result(suggest, code, status):
    with scenario(
        text_suggest=lambda texts: "pass" if len(texts) == 1 else suggest
    ) as (client, calls):
        queued = queue(client, description="input")
        callback(client)
        response = poll(client, queued)
        assert_error(response, status, code)
        assert "疑似早疫病" not in response.text
        assert len(calls["model"]) == 1
        assert_inputs_cleared(client)


@pytest.mark.parametrize(
    "changes",
    [
        {"MsgType": "text"},
        {"appid": "other-app"},
        {"version": 1},
        {"version": True},
        {"version": 2.0},
        {"version": "02"},
        {"errcode": False},
        {"errcode": 0.0},
        {"errcode": -1},
        {"errcode": "00"},
        {"errcode": None},
        {"result": {}},
        {"result": {"suggest": "PASS"}},
        {"result": {"suggest": "unknown"}},
        {"result": []},
    ],
)
def test_signed_malformed_callback_fails_closed(changes):
    with scenario() as (client, calls):
        queued = queue(client)
        assert callback(client, **changes).status_code == 200
        assert_error(poll(client, queued), 503, "SECURITY_UNAVAILABLE")
        callback(client)  # A later pass must not revive a failed job.
        assert not calls["model"]
        assert_inputs_cleared(client)


@pytest.mark.parametrize("tamper", ["signature", "appid"])
def test_bad_transport_signature_or_envelope_appid_cannot_authorize(tamper):
    with scenario() as (client, calls):
        queued = queue(client)
        data = signed_event()
        if tamper == "signature":
            data["params"]["msg_signature"] = "0" * 40
        else:
            encrypted = envelope(
                json.dumps({"Event": "wxa_media_check"}).encode(), appid="other-app"
            )
            data = {"params": params(encrypted), "json": {"Encrypt": encrypted}}
        assert_error(
            client.post("/api/wechat/callback", **data), 503, "SECURITY_UNAVAILABLE"
        )
        assert lookup(client, queued).json()["status"] == "checking"
        assert not calls["model"]
        assert state(client, lambda jobs: not jobs.early)


@pytest.mark.parametrize(
    "early",
    [
        [{}, {}],
        [{"result": {"suggest": "review"}}, {}],
        [{}, {"result": {"suggest": "risky"}}],
    ],
)
def test_early_duplicate_and_conflicting_callbacks(early):
    with scenario(early=early) as (client, calls):
        queued = queue(client)
        assert queued.status_code == 202
        for _ in range(3):
            callback(client)
        response = poll(client, queued)
        rejected = any(event for event in early)
        assert response.status_code == (422 if rejected else 200)
        assert len(calls["model"]) == (0 if rejected else 1)
        assert state(client, lambda jobs: not jobs.early and not jobs.tasks)


def test_conflicting_nonpass_revokes_completed_result():
    with scenario() as (client, calls):
        queued = queue(client)
        callback(client)
        assert poll(client, queued).status_code == 200
        callback(client, result={"suggest": "risky"})
        callback(client)
        assert_error(lookup(client, queued), 422, "CONTENT_REJECTED")
        assert len(calls["model"]) == 1
        assert_inputs_cleared(client)


def test_conflicting_callback_cancels_running_work():
    with scenario(model_wait=30) as (client, calls):
        queued = queue(client)
        callback(client)
        assert lookup(client, queued).json()["status"] == "analysing"
        callback(client, result={"suggest": "review"})
        assert_error(poll(client, queued), 422, "CONTENT_REJECTED")
        assert calls["cancelled"]
        assert not calls["text"]
        assert_inputs_cleared(client)


def test_job_and_media_capabilities_and_no_store():
    with scenario() as (client, calls):
        queued = queue(client, description="private-description")
        details = queued.json()
        assert len(details["job_token"]) >= 43
        assert queued.headers["cache-control"] == "no-store"
        assert client.get("/api/diagnose/" + details["job_id"]).status_code == 404
        assert lookup(client, queued, token="incorrect").status_code == 404
        assert client.get("/api/moderation-media/wrong.jpg").status_code == 404
        media_url = calls["media"][0]
        media_token = media_url.rsplit("/", 1)[1][:-4]
        assert len(media_token) >= 43 and media_token != details["job_token"]
        media = client.get(httpx.URL(media_url).path.removeprefix("/prefix"))
        assert media.status_code == 200
        assert media.headers["cache-control"] == "no-store"
        assert media.headers["content-type"] == "image/jpeg"
        with Image.open(BytesIO(media.content)) as image:
            assert image.format == "JPEG" and image.size == (64, 64)
        response = lookup(client, queued)
        assert response.json() == {"status": "checking", "poll_after_ms": 1500}
        assert details["job_token"] not in response.text
        callback(client)
        assert poll(client, queued).headers["cache-control"] == "no-store"
        assert (
            client.get(httpx.URL(media_url).path.removeprefix("/prefix")).status_code
            == 404
        )


def test_expired_jobs_media_and_early_events_are_removed():
    with scenario() as (client, calls):
        queued = queue(client)
        callback(client, trace="unbound")
        media_path = httpx.URL(calls["media"][0]).path.removeprefix("/prefix")

        def expire(jobs):
            for job in jobs.jobs.values():
                job.expires = 0
            jobs.early["unbound"] = (0, None)

        state(client, expire)
        assert lookup(client, queued).status_code == 404
        assert client.get(media_path).status_code == 404
        assert state(
            client, lambda jobs: not jobs.jobs and not jobs.traces and not jobs.early
        )
        assert not calls["model"]


def test_per_user_and_global_pending_capacity():
    with scenario(moderation_max_pending=3) as (client, calls):
        assert queue(client).status_code == 202
        assert queue(client).status_code == 202
        assert_error(queue(client), 429, "QUEUE_FULL")
        assert queue(client, login_code="other-user").status_code == 202
        assert_error(queue(client, login_code="third-user"), 429, "QUEUE_FULL")
        assert len(calls["media"]) == 3
        assert state(client, lambda jobs: len(jobs.jobs)) == 3


def test_total_capacity_includes_completed_jobs():
    with scenario(moderation_max_jobs=2) as (client, _):
        for trace in ("trace-1", "trace-2"):
            queued = queue(client)
            callback(client, trace=trace)
            assert poll(client, queued).status_code == 200
        assert_error(queue(client), 429, "QUEUE_FULL")


def test_early_cache_is_bounded_and_overflow_fails_closed():
    with scenario() as (client, calls):
        for index in range(100):
            callback(client, trace=f"early-{index}", result={"suggest": "risky"})
        assert state(client, lambda jobs: len(jobs.early)) == 100
        callback(client, trace="overflow")
        callback(client, trace="early-0")
        assert state(client, lambda jobs: len(jobs.early)) == 100
        assert (
            state(client, lambda jobs: jobs.early["early-0"][1].code)
            == "CONTENT_REJECTED"
        )
        assert_error(queue(client), 503, "SECURITY_UNAVAILABLE")
        assert not calls["model"]


def test_timeout_and_shutdown_cancel_workers(monkeypatch):
    monkeypatch.setattr("app.moderation.WORK_TIMEOUT_SECONDS", 0.03)
    with scenario(model_wait=30) as (client, calls):
        queued = queue(client)
        callback(client)
        assert_error(poll(client, queued), 503, "SECURITY_UNAVAILABLE")
        assert calls["cancelled"]
        assert_inputs_cleared(client)
    monkeypatch.setattr("app.moderation.WORK_TIMEOUT_SECONDS", 180)
    with scenario(model_wait=30) as (client, calls):
        queued = queue(client)
        callback(client)
        assert lookup(client, queued).json()["status"] == "analysing"
        jobs = client.app.state.moderation
    assert calls["cancelled"]
    assert not jobs.tasks and not jobs.jobs and not jobs.media


def test_unexpected_worker_failure_is_sanitized():
    with scenario() as (client, _):

        async def fail(*args):
            raise RuntimeError("RAW-UPSTREAM-SECRET " + TEST_KEY)

        client.app.state.qwen.diagnose = fail
        queued = queue(client)
        callback(client)
        assert_error(poll(client, queued), 503, "SECURITY_UNAVAILABLE")
        assert_inputs_cleared(client)


def test_unconfigured_recognition_callback_and_media_never_call_upstream():
    with scenario(wechat_app_secret="") as (client, calls):
        assert_error(queue(client), 503, "SECURITY_UNAVAILABLE")
        assert_error(client.get("/api/diagnose/some-id"), 503, "SECURITY_UNAVAILABLE")
        assert_error(
            client.get("/api/moderation-media/some-token.jpg"),
            503,
            "SECURITY_UNAVAILABLE",
        )
        assert_error(callback(client), 503, "SECURITY_UNAVAILABLE")
        assert_error(client.get("/api/wechat/callback"), 503, "SECURITY_UNAVAILABLE")
        assert client.get("/health").json()["security_configured"] is False
        assert not calls["all"]


def test_callback_challenge_xml_numeric_values_and_unknown_events():
    with scenario() as (client, calls):
        response = client.get(
            "/api/wechat/callback", params={**params(), "echostr": "challenge"}
        )
        assert response.text == "challenge"
        assert callback(client, Event="unknown").text == "success"
        assert state(client, lambda jobs: not jobs.early)
        queued = queue(client)
        raw = (
            "<xml><MsgType>event</MsgType><Event>wxa_media_check</Event>"
            f"<appid>{APPID}</appid><trace_id>trace-1</trace_id>"
            "<version>2</version><errcode>0</errcode>"
            "<result><suggest>pass</suggest></result></xml>"
        ).encode()
        encrypted = envelope(raw)
        assert (
            client.post(
                "/api/wechat/callback",
                params=params(encrypted),
                json={"Encrypt": encrypted},
            ).status_code
            == 200
        )
        assert poll(client, queued).status_code == 200
        assert len(calls["model"]) == 1


def test_access_log_suppresses_media_capabilities(caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logger = logging.getLogger("uvicorn.access")
        logger.info("GET /api/moderation-media/private-capability.jpg 200")
        logger.info("GET /health 200")
    assert "private-capability" not in caplog.text
    assert "/health" in caplog.text


def test_concurrent_requests_reserve_per_user_capacity_atomically():
    with scenario(text_wait=0.03) as (client, calls):

        async def concurrent():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=client.app),
                base_url="http://testserver",
            ) as http:
                return await asyncio.gather(
                    *(
                        http.post(
                            "/api/diagnose",
                            data={"login_code": "same-user", "description": "input"},
                            files={"image": ("leaf.png", image_bytes())},
                        )
                        for _ in range(6)
                    )
                )

        responses = client.portal.call(concurrent)
        assert sorted(response.status_code for response in responses) == [
            202,
            202,
            429,
            429,
            429,
            429,
        ]
        assert len(calls["media"]) == 2 and len(calls["text"]) == 2
        assert not calls["model"]


def test_no_output_exposed_while_text_moderation_pending():
    with scenario(text_wait=0.1) as (client, calls):
        queued = queue(client)
        callback(client)
        response = lookup(client, queued)
        assert response.status_code == 202
        assert response.json() == {"status": "analysing", "poll_after_ms": 1500}
        assert "疑似早疫病" not in response.text
        assert poll(client, queued).status_code == 200
        assert len(calls["model"]) == 1


def test_duplicate_upstream_trace_fails_both_jobs_without_work():
    with api() as (client, calls):
        first = queue(client)
        assert first.status_code == 202
        assert_error(queue(client), 503, "SECURITY_UNAVAILABLE")
        callback(client)
        assert_error(lookup(client, first), 503, "SECURITY_UNAVAILABLE")
        assert calls == []


def test_expiry_cancels_running_work():
    with scenario(model_wait=30) as (client, calls):
        queued = queue(client)
        callback(client)
        assert lookup(client, queued).json()["status"] == "analysing"
        state(
            client, lambda jobs: setattr(next(iter(jobs.jobs.values())), "expires", 0)
        )
        assert lookup(client, queued).status_code == 404
        assert calls["cancelled"]
        assert state(client, lambda jobs: not jobs.tasks and not jobs.jobs)


def test_caller_openid_and_media_url_are_ignored():
    with scenario() as (client, calls):
        response = client.post(
            "/api/diagnose",
            data={
                "login_code": "trusted-login",
                "openid": "attacker",
                "media_url": "https://attacker.example/image.jpg",
            },
            files={"image": ("leaf.png", image_bytes())},
        )
        assert response.status_code == 202
        payload = json.loads(
            next(
                request
                for request in calls["all"]
                if request.url.path == "/wxa/media_check_async"
            ).content
        )
        assert payload["openid"] == "trusted-login"
        assert payload["media_url"].startswith(
            "https://public.example/prefix/api/moderation-media/"
        )


def test_oversized_callback_body_and_bad_trace_are_refused():
    with scenario() as (client, calls):
        assert_error(
            client.post("/api/wechat/callback", content=b"x" * (64 * 1024 + 1)),
            503,
            "SECURITY_UNAVAILABLE",
        )
        for trace in ("", " ", "x" * 129, None, True):
            assert_error(callback(client, trace=trace), 503, "SECURITY_UNAVAILABLE")
        assert not calls["model"]
        assert state(client, lambda jobs: not jobs.early)


def test_cleanup_loop_runs_and_shutdown_cancels_it(monkeypatch):
    with scenario() as (client, _):

        async def exercise():
            jobs = client.app.state.moderation
            intervals = []
            cleanups = []

            async def sleep(seconds):
                intervals.append(seconds)
                if len(intervals) == 2:
                    raise asyncio.CancelledError

            monkeypatch.setattr("app.moderation.asyncio.sleep", sleep)
            monkeypatch.setattr(jobs, "cleanup", lambda: cleanups.append(True))
            with pytest.raises(asyncio.CancelledError):
                await jobs.cleanup_loop()
            return intervals, cleanups

        assert client.portal.call(exercise) == ([30, 30], [True])
        monkeypatch.undo()


def test_terminal_error_does_not_retain_tracebacks_or_unchecked_model_output():
    with scenario(text_suggest="risky") as (client, _):
        queued = queue(client)
        callback(client)
        for _ in range(3):
            assert_error(poll(client, queued), 422, "CONTENT_REJECTED")
        failure = state(client, lambda jobs: next(iter(jobs.jobs.values())).error)
        assert not isinstance(failure, BaseException)
        assert not hasattr(failure, "__traceback__")
        assert vars(failure) == {
            "status_code": 422,
            "code": "CONTENT_REJECTED",
            "message": "您提交的内容含违规信息。",
        }
        assert_inputs_cleared(client)
