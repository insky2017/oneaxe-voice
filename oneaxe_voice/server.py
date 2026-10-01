"""Loopback-only HTTP access to the independent GPU worker."""

import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
import ipaddress
import logging
import os
import uuid

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, StrictBool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from . import __version__
from .audio import AudioError
from .backend import BusyError, GPUError, QwenEngine
from .config import Settings
from .engines import EngineRouter
from .modes import validate_mode

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
    engine = engine or EngineRouter(settings)
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

    async def worker_call(method, *args):
        task = asyncio.create_task(asyncio.to_thread(method, *args))
        pending.add(task)
        task.add_done_callback(completed)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Keep the engine lease until an already dispatched CUDA step ends.
            await asyncio.gather(task, return_exceptions=True)
            raise

    class ModeRequest(BaseModel):
        mode: str

    class PolicyRequest(BaseModel):
        auto_unload: StrictBool

    @app.post("/api/dictation/policy")
    async def policy(value: PolicyRequest):
        return await worker_call(engine.set_auto_unload, value.auto_unload)

    @app.post("/api/dictation/unload")
    async def unload():
        try:
            return await worker_call(engine.unload)
        except BusyError as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "2"}) from exc

    @app.post("/api/dictation/prepare")
    async def prepare(value: ModeRequest):
        try:
            validate_mode(value.mode)
            return await worker_call(engine.prepare, value.mode)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except BusyError as exc:
            raise HTTPException(429, str(exc)) from exc
        except GPUError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.websocket("/api/dictation/stream")
    async def streaming(ws: WebSocket):
        # WebSocket upgrades do not pass through HTTP authentication middleware.
        try:
            local = ws.client is not None and ipaddress.ip_address(ws.client.host).is_loopback
        except ValueError:
            local = False
        valid = hmac.compare_digest(ws.headers.get("authorization", ""), "Bearer " + token)
        if not local or not valid or "origin" in ws.headers:
            await ws.close(code=1008)
            return
        await ws.accept()
        session = str(uuid.uuid4())
        finished = False
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 5)
            mode = validate_mode(hello.get("mode"))
            if mode == "vad":
                raise ValueError("稳听使用 WAV 接口")
            await worker_call(engine.begin, mode, session)
            await ws.send_json({"type": "ready", "request_id": session, "mode": mode})
            total = 0
            sequence = 0
            while True:
                message = await asyncio.wait_for(ws.receive(), 30)
                if message["type"] == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data is not None:
                    if not data or len(data) > 64000 or len(data) % 2:
                        raise ValueError("音频块须为不超过 2 秒的单声道 PCM16")
                    total += len(data)
                    if total > 3600 * 32000:
                        raise ValueError("录音超过 60 分钟上限")
                    result = await worker_call(engine.feed, session, data)
                elif message.get("text") == "finish":
                    result = await worker_call(engine.finish, session)
                    finished = True
                elif message.get("text") == "flush":
                    result = await worker_call(engine.flush, session)
                elif message.get("text") == "keepalive":
                    # Idle audio is gated on the desktop after an endpoint.
                    # Keep the lease alive without invoking ASR on silence.
                    await ws.send_json({"type": "keepalive"})
                    continue
                elif message.get("text") == "cancel":
                    break
                else:
                    raise ValueError("未知流式消息")
                sequence += 1
                await ws.send_json({**result, "type": "final" if finished else "partial",
                                    "request_id": session, "sequence": sequence,
                                    "audio_seconds": total / 32000,
                                    "device": f"cuda:{settings.cuda_device}"})
                if finished:
                    break
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except (BusyError, GPUError, ValueError) as exc:
            with suppress(Exception):
                await ws.send_json({"type": "error", "detail": str(exc)})
        except Exception as exc:
            LOGGER.error("stream_failed kind=%s request_id=%s", type(exc).__name__, session)
            with suppress(Exception):
                await ws.send_json({"type": "error", "detail": "流式识别失败，请检查本机服务"})
        finally:
            await asyncio.to_thread(engine.end, session, not finished)
            with suppress(Exception):
                await ws.close()

    @app.post("/api/dictation/warmup")
    async def warmup():
        """Warm up the same single GPU worker without accepting or logging audio."""
        task = asyncio.create_task(asyncio.to_thread(engine.warmup))
        pending.add(task)
        task.add_done_callback(completed)
        try:
            result = await asyncio.shield(task)
        except BusyError as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "2"}) from exc
        except GPUError as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("warmup_failed")
            raise HTTPException(500, "模型预热失败，请检查服务日志") from exc
        return {"model_loaded": result["model_loaded"], "device": result["device"]}

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
