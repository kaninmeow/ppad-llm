"""Single-event-loop, bounded moderation jobs; only checked results are public."""

import asyncio
import hmac
import secrets
import time
from dataclasses import dataclass
from uuid import uuid4

from app.errors import APIError
from app.images import PreparedImage
from app.qwen import QwenClient
from app.schemas import DiagnosisResponse
from app.settings import Settings
from app.wechat import WeChatClient, security_unavailable

POLL = {"poll_after_ms": 1500}
WORK_TIMEOUT_SECONDS = 180


def not_found() -> APIError:
    return APIError(404, "NOT_FOUND", "请求的资源不存在或已过期。")


def rejected() -> APIError:
    return APIError(422, "CONTENT_REJECTED", "您提交的内容含违规信息。")


def strings(value):
    """Include every string value, including nested candidate and question text."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def numeric(value, expected: int) -> bool:
    # XML leaves are strings; JSON booleans and noncanonical numbers never pass.
    return (type(value) is int and value == expected) or (
        type(value) is str and value == str(expected)
    )


@dataclass(frozen=True)
class Failure:
    """Persist safe fields only: exceptions retain traceback frames and inputs."""

    status_code: int
    code: str
    message: str

    def exception(self) -> APIError:
        return APIError(self.status_code, self.code, self.message)


@dataclass
class Job:
    job_id: str
    token: str
    media_token: str
    created: float
    expires: float
    openid: str
    prepared: PreparedImage | None
    description: str
    status: str = "checking"
    trace: str | None = None
    response: DiagnosisResponse | None = None
    error: Failure | None = None
    task: asyncio.Task | None = None

    @property
    def pending(self) -> bool:
        return self.status in {"checking", "analysing"}


class ModerationJobs:
    def __init__(self, settings: Settings, wechat: WeChatClient, qwen: QwenClient):
        self.settings = settings
        self.wechat = wechat
        self.qwen = qwen
        self.jobs: dict[str, Job] = {}
        self.media: dict[str, Job] = {}
        self.traces: dict[str, Job] = {}
        self.early: dict[str, tuple[float, APIError | None]] = {}
        self.tasks: set[asyncio.Task] = set()
        self.overflow_until = 0.0
        self.closed = False

    def cleanup(self):
        now = time.monotonic()
        for job in list(self.jobs.values()):
            if job.expires <= now:
                self._remove(job)
        for trace, (expires, _) in list(self.early.items()):
            if expires <= now:
                del self.early[trace]

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(30)
            self.cleanup()

    def _clear_inputs(self, job: Job):
        self.media.pop(job.media_token, None)
        job.prepared = None
        job.description = ""
        job.openid = ""
        job.media_token = ""

    def _fail(self, job: Job, error: APIError):
        # A later signed non-pass revokes even a completed result. Rejection sticks.
        if job.error is None or job.error.code != "CONTENT_REJECTED":
            job.error = Failure(error.status_code, error.code, error.message)
        job.status = "failed"
        job.response = None
        self._clear_inputs(job)
        if job.task is not None and job.task is not asyncio.current_task():
            job.task.cancel()

    def _remove(self, job: Job):
        self._fail(job, security_unavailable())
        self.jobs.pop(job.job_id, None)
        if job.trace is not None and self.traces.get(job.trace) is job:
            del self.traces[job.trace]

    def reserve(self, openid: str, prepared: PreparedImage, description: str) -> Job:
        self.cleanup()
        now = time.monotonic()
        if self.closed or now < self.overflow_until:
            raise security_unavailable()
        pending = [job for job in self.jobs.values() if job.pending]
        # Cancelled workers remain counted until their done callbacks release them.
        active = {job.task for job in pending if job.task is not None}
        if (
            len(self.jobs) >= self.settings.moderation_max_jobs
            or len(pending) + len(self.tasks - active)
            >= self.settings.moderation_max_pending
            or sum(job.openid == openid for job in pending)
            >= self.settings.moderation_max_pending_per_user
        ):
            raise APIError(429, "QUEUE_FULL", "当前识别请求较多，请稍后重试。")
        job = Job(
            job_id=str(uuid4()),
            token=secrets.token_urlsafe(32),
            media_token=secrets.token_urlsafe(32),
            created=now,
            expires=now + self.settings.moderation_job_ttl_seconds,
            openid=openid,
            prepared=prepared,
            description=description,
        )
        self.jobs[job.job_id] = job
        self.media[job.media_token] = job
        return job

    async def submit(self, prepared: PreparedImage, description: str, code: str):
        openid = await self.wechat.exchange_code(code)
        job = self.reserve(openid, prepared, description)
        try:
            await self.wechat.check_text(openid, description)
            self._assert_submitting(job)
            trace = await self.wechat.submit_image(
                openid,
                self.settings.public_base_url
                + "/api/moderation-media/"
                + job.media_token
                + ".jpg",
            )
            self._assert_submitting(job)
            if trace in self.traces:
                self._fail(self.traces[trace], security_unavailable())
                raise security_unavailable()
            job.trace = trace
            self.traces[trace] = job
            early = self.early.pop(trace, None)
            if early is not None and early[0] > time.monotonic():
                self._apply(job, early[1])
            return {
                "job_id": job.job_id,
                "job_token": job.token,
                "status": "checking",
                **POLL,
            }
        except (APIError, asyncio.CancelledError):
            # Also release reservations on disconnect/cancellation.
            self._remove(job)
            raise
        except Exception:
            self._remove(job)
            raise security_unavailable() from None

    def _assert_submitting(self, job: Job):
        self.cleanup()
        if self.closed or not job.pending or job.job_id not in self.jobs:
            raise job.error.exception() if job.error else security_unavailable()

    def get(self, job_id: str, authorization: str) -> dict:
        self.cleanup()
        job = self.jobs.get(job_id)
        if job is None or not hmac.compare_digest(
            authorization.encode(), ("Bearer " + job.token).encode()
        ):
            raise not_found()
        if job.error is not None:
            raise job.error.exception()
        if job.response is not None:
            return job.response.model_dump(mode="json")
        return {"status": job.status, **POLL}

    def get_media(self, token: str) -> bytes:
        self.cleanup()
        job = self.media.get(token)
        if job is None or job.prepared is None:
            raise not_found()
        return job.prepared.data

    def event(self, event: dict):
        """Only called with cryptographically verified and decrypted events."""
        self.cleanup()
        if event.get("Event") != "wxa_media_check":
            return
        trace = event.get("trace_id")
        if not isinstance(trace, str) or not 0 < len(trace) <= 128 or not trace.strip():
            raise security_unavailable()
        result = event.get("result")
        error = security_unavailable()
        if (
            event.get("MsgType") == "event"
            and event.get("appid") == self.settings.wechat_app_id
            and numeric(event.get("version"), 2)
            and numeric(event.get("errcode"), 0)
            and isinstance(result, dict)
        ):
            if result.get("suggest") == "pass":
                error = None
            elif result.get("suggest") in ("risky", "review"):
                error = rejected()
        job = self.traces.get(trace)
        if job is not None:
            self._apply(job, error)
        elif trace in self.early:
            expiry, previous = self.early[trace]
            if previous is None or (
                error is not None and error.code == "CONTENT_REJECTED"
            ):
                self.early[trace] = (expiry, error)
        elif len(self.early) < 100:
            self.early[trace] = (
                time.monotonic() + self.settings.moderation_job_ttl_seconds,
                error,
            )
        else:
            # Never evict an adverse verdict then accept a conflicting pass.
            self.overflow_until = max(
                self.overflow_until,
                time.monotonic() + self.settings.moderation_job_ttl_seconds,
            )
            for submitting in list(self.jobs.values()):
                if submitting.trace is None:
                    self._fail(submitting, security_unavailable())

    def _apply(self, job: Job, error: APIError | None):
        if error is not None:
            self._fail(job, error)
        elif job.status == "checking":
            job.status = "analysing"
            job.task = asyncio.create_task(self._work(job))
            self.tasks.add(job.task)
            job.task.add_done_callback(self.tasks.discard)

    async def _work(self, job: Job):
        try:
            if job.status != "analysing" or time.monotonic() >= job.expires:
                self._fail(job, security_unavailable())
                return
            async with asyncio.timeout(WORK_TIMEOUT_SECONDS):
                result = await self.qwen.diagnose(job.prepared, job.description)
                await self.wechat.check_text(
                    job.openid, "\n".join(strings(result.model_dump()))
                )
                if job.status == "analysing" and time.monotonic() < job.expires:
                    job.response = DiagnosisResponse(
                        request_id=job.job_id,
                        model=self.settings.qwen_model,
                        elapsed_ms=int((time.monotonic() - job.created) * 1000),
                        result=result,
                    )
                    job.status = "done"
                else:
                    self._fail(job, security_unavailable())
        except asyncio.CancelledError:
            self._fail(job, security_unavailable())
            raise
        except APIError as exc:
            self._fail(job, exc)
        except Exception:
            self._fail(job, security_unavailable())
        finally:
            self._clear_inputs(job)
            job.task = None

    async def close(self):
        self.closed = True
        for job in list(self.jobs.values()):
            self._remove(job)
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.early.clear()
