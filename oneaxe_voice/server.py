"""Loopback-only HTTP access to the independent GPU worker."""

import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
import ipaddress
import logging
import os
import uuid

from fastapi import FastAPI, File, HTTPException, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from . import __version__
from .audio import AudioError
from .backend import BusyError, GPUError, QwenEngine
from .config import Settings

LOGGER = logging.getLogger("uvicorn.error")


class LocalAccess:
    """Authenticate before multipart parsing and bound both sized and streamed bodies."""

    def __init__(self, app, token: str, max_body_bytes: int) -> None:
        """Wrap the ASGI application with a dedicated local capability."""
        self.app = app
        self.expected = ("Bearer " + token).encode("ascii")
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send) -> None:
        """Enforce the local API boundary without buffering uploaded audio twice."""
        if scope["type"] != "http" or scope["path"] == "/health":
            await self.app(scope, receive, send)
            return
        try:
            local = ipaddress.ip_address(scope["client"][0]).is_loopback
        except (ValueError, TypeError):
            local = False
        if not local:
            await JSONResponse({"detail": "仅允许本机客户端"}, 403)(scope, receive, send)
            return
        headers = dict(scope["headers"])
        if not hmac.compare_digest(headers.get(b"authorization", b""), self.expected):
            await JSONResponse({"detail": "本机访问令牌无效"}, 401)(scope, receive, send)
            return
        try:
            length = int(headers.get(b"content-length", b"0"))
            if length < 0:
                raise ValueError
        except ValueError:
            await JSONResponse({"detail": "Content-Length 无效"}, 400)(scope, receive, send)
            return
        if length > self.max_body_bytes:
            await JSONResponse({"detail": "请求体过大"}, 413)(scope, receive, send)
            return
        received = 0

        async def bounded_receive():
            """Apply the same upload limit when Content-Length is absent."""
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise StarletteHTTPException(413, "请求体过大")
            return message

        await self.app(scope, bounded_receive, send)


def create_app(settings: Settings | None = None, engine=None) -> FastAPI:
    """Build one application without loading a model or touching VPlus."""
    settings = settings or Settings.from_env()
    token = settings.token_path.read_text().strip()
    if len(token) < 32:
        raise ValueError("请先运行 oneaxe-voice init 生成本机令牌")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    engine = engine or QwenEngine(settings)
    pending: set[asyncio.Task] = set()

    async def idle_loop() -> None:
        """Periodically release this service's weights after the idle timeout."""
        while True:
            await asyncio.sleep(5)
            await asyncio.to_thread(engine.unload_if_idle)

    @asynccontextmanager
    async def lifespan(app):
        """Keep worker jobs alive until completion, including during shutdown."""
        reaper = asyncio.create_task(idle_loop())
        try:
            yield
        finally:
            reaper.cancel()
            with suppress(asyncio.CancelledError):
                await reaper
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.to_thread(engine.unload_if_idle, force=True)

    app = FastAPI(
        title="OneAxe Voice", version=__version__, lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.add_middleware(LocalAccess, token=token, max_body_bytes=settings.max_body_bytes)
    app.state.engine = engine

    @app.get("/health")
    async def health():
        """Report HTTP service liveness without implying model readiness."""
        return {"ok": True, "service": "oneaxe-voice", "version": __version__}

    @app.get("/api/dictation/status")
    async def status():
        """Report the actual model and worker state without starting inference."""
        return engine.status()

    def completed(task: asyncio.Task) -> None:
        """Release task references and consume errors even after client cancellation."""
        pending.discard(task)
        if not task.cancelled():
            task.exception()

    @app.post("/api/dictation/transcribe")
    async def transcribe(file: UploadFile = File(...)):
        """Return text for one short recording, keeping CUDA work single-file."""
        try:
            data = await file.read(settings.max_audio_bytes + 1)
        finally:
            await file.close()
        if len(data) > settings.max_audio_bytes:
            raise HTTPException(413, "音频文件超过 12 MiB")
        request_id = str(uuid.uuid4())
        task = asyncio.create_task(asyncio.to_thread(engine.transcribe, data, request_id))
        pending.add(task)
        task.add_done_callback(completed)
        try:
            # A disconnected caller must not delete audio still used by a CUDA worker.
            result = await asyncio.shield(task)
        except BusyError as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "2"}) from exc
        except AudioError as exc:
            raise HTTPException(422, str(exc)) from exc
        except GPUError as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("dictation_failed request_id=%s", request_id)
            raise HTTPException(500, "识别失败，请查看 OneAxe Voice 服务日志") from exc
        LOGGER.info(
            "dictation_done request_id=%s seconds=%s device=%s timing_ms=%s",
            request_id, result["audio_seconds"], result.get("device"), result["timing_ms"],
        )
        return result

    return app
