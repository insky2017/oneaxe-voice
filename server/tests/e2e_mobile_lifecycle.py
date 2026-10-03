"""Real-clock mobile isolation and one reload against the isolated API on 18096.

Requires two supplied PCM16 WAVs and three plain-text bearer-token files. The
third token and --revoke-credential-id must belong to a disposable test device.
The last scenario unloads and prepares r2t2 once; no service is started/stopped.
Reports counts, hashes and protocol identity, never tokens or transcript text.
"""

import argparse
import asyncio
from contextlib import suppress
import hashlib
import ipaddress
import json
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from e2e_concurrent import audio_frame, pcm


class CheckFailure(RuntimeError):
    """Only controlled messages from this script may appear in the report."""


def require(value, message):
    if not value:
        raise CheckFailure(message)


def ws_url(url, path):
    value = urlsplit(url)
    return urlunsplit(("wss" if value.scheme == "https" else "ws", value.netloc, path, "", ""))


def start_message(role, generation):
    value = {"type": "start", "protocol_version": 1,
             "audio": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1}}
    if role == "pc":
        value["mode"] = "r2t2"
    else:
        value.update(expected_server_instance_id=generation[0], expected_model_generation=generation[1])
    return value


class Stream:
    def __init__(self, url, token, audio, role, generation, timeout):
        self.url, self.token, self.audio, self.role = url, token, audio, role
        self.generation, self.timeout = generation, timeout
        self.ws = None
        self.tasks = []
        self.changed = asyncio.Event()
        self.done = asyncio.Event()
        self.lock = asyncio.Lock()
        self.text = ""
        self.sent = self.processed = self.received = self.updates = 0
        self.limit = self.seq = 0
        self.terminal = None
        self.expected = None
        self.accept_text = True
        self.failure = None
        self.forced_disconnect = False

    async def __aenter__(self):
        path = "/api/dictation/v1/stream" if self.role == "pc" else "/api/mobile/v1/dictation/stream"
        self.ws = await connect(ws_url(self.url, path), proxy=None, open_timeout=10,
                                additional_headers={"Authorization": "Bearer " + self.token},
                                max_size=2 * 1024 * 1024)
        try:
            await self.ws.send(json.dumps(start_message(self.role, self.generation)))
            ready = json.loads(await asyncio.wait_for(self.ws.recv(), self.timeout))
            require(ready.get("type") == "ready", "stream did not become ready: " + ready.get("code", "UNKNOWN"))
            self.identity = tuple(ready[k] for k in ("server_instance_id", "model_generation", "session_id"))
            require(self.identity[:2] == self.generation, "stream bound to a different model generation")
            require(ready.get("device", "").startswith("cuda:"), "stream is not using CUDA")
            self.limit, self.seq = ready["audio_send_limit"], ready["seq"]
            self.started = time.monotonic()
            self.tasks = [asyncio.create_task(method()) for method in (self.receive, self.send_audio, self.keepalive)]
            for task in self.tasks:
                task.add_done_callback(self.task_done)
            return self
        except BaseException:
            await self.ws.close()
            raise

    def task_done(self, task):
        if not task.cancelled() and (error := task.exception()) is not None:
            self.failure = error
        self.changed.set()

    def check(self):
        for task in self.tasks:
            if task.done() and not task.cancelled() and (error := task.exception()) is not None:
                raise error
        if self.failure is not None:
            raise self.failure

    async def wait(self, predicate, message):
        async def until():
            while True:
                self.check()
                if predicate():
                    return
                require(not self.done.is_set(), message + ": stream ended")
                self.changed.clear()
                await self.changed.wait()
        try:
            await asyncio.wait_for(until(), self.timeout)
        except asyncio.TimeoutError:
            raise CheckFailure(message + ": timed out") from None

    async def receive(self):
        try:
            async for raw in self.ws:
                item = json.loads(raw)
                require(tuple(item[k] for k in ("server_instance_id", "model_generation", "session_id"))
                        == self.identity, "event belongs to another session or generation")
                if item["seq"] <= self.seq:
                    continue
                self.seq = item["seq"]
                if "audio_send_limit" in item:
                    require(item["audio_send_limit"] >= self.limit, "send credit moved backwards")
                    self.limit = item["audio_send_limit"]
                for key, attr in (("audio_received_samples", "received"), ("audio_processed_samples", "processed")):
                    if key in item:
                        require(getattr(self, attr) <= item[key] <= self.sent, "audio progress is invalid")
                        setattr(self, attr, item[key])
                if self.accept_text and "text" in item:
                    require(item["text"].startswith(self.text), "fixed text regressed")
                    self.updates += int(item["text"] != self.text)
                    self.text = item["text"]
                if item["type"] in {"error", "final"}:
                    self.terminal = {key: item.get(key) for key in ("type", "code", "reason", "complete")}
                    require(self.expected is not None, "unexpected terminal event: " + item.get("code", "FINAL"))
                self.changed.set()
        except ConnectionClosed:
            require(self.valid_close(), "connection ended without a terminal event")
        finally:
            self.done.set()
            self.changed.set()
        require(self.valid_close(), "connection ended without a terminal event")

    def valid_close(self):
        return (self.forced_disconnect or self.terminal is not None
                or self.expected == "cancel" and self.ws.close_code == 1000)

    async def send_audio(self):
        while not self.done.is_set():
            count = 2560
            await asyncio.sleep(max(0, self.started + (self.sent + count) / 16000 - time.monotonic()))
            await self.wait(lambda: self.sent + count <= self.limit, "waiting for audio credit")
            require((time.monotonic() - self.started) * 16000 - self.sent <= 32000,
                    "capture backlog exceeded two seconds")
            try:
                async with self.lock:
                    frame = audio_frame(self.audio, self.sent, count)
                    self.sent += count
                    await self.ws.send(frame)
            except ConnectionClosed:
                require(self.expected is not None, "audio socket closed unexpectedly")
                return

    async def control(self, value):
        async with self.lock:
            await self.ws.send(json.dumps(value))

    async def keepalive(self):
        while not self.done.is_set():
            await asyncio.sleep(10)
            if not self.done.is_set():
                try:
                    await self.control({"type": "keepalive"})
                except ConnectionClosed:
                    require(self.expected is not None, "keepalive socket closed unexpectedly")
                    return

    async def stop_audio(self):
        self.tasks[1].cancel()
        await asyncio.gather(self.tasks[1], return_exceptions=True)
        self.check()

    async def end(self, kind, code=None):
        self.expected = kind
        if kind in {"cancel", "disconnect"}:
            self.accept_text = False
        await self.stop_audio()
        if kind == "disconnect":
            self.forced_disconnect = True
            self.ws.transport.abort()
        elif kind in {"cancel", "finish"}:
            await self.control({"type": kind, **({"after_audio_samples": self.sent} if kind == "finish" else {})})
        await asyncio.wait_for(self.done.wait(), self.timeout)
        self.check()
        if kind == "disconnect":
            require(self.ws.close_code == 1006, "forced disconnect did not end abnormally")
        elif kind == "error":
            require(self.terminal and self.terminal["type"] == "error" and self.terminal["code"] == code,
                    "wrong terminal error after management action")
            require(self.terminal["complete"] is False and self.ws.close_code == 1008,
                    "management error reported success or wrong close code")
        else:
            require(self.ws.close_code == 1000, "normal terminal has wrong close code")
            if kind == "finish":
                require(self.terminal and self.terminal["type"] == "final" and self.terminal["complete"] is True
                        and self.terminal["reason"] == "finished" and self.processed == self.sent,
                        "finish did not process all audio successfully")
            elif self.terminal:
                require(self.terminal["type"] == "final" and self.terminal["complete"] is False
                        and self.terminal["reason"] == "cancelled", "cancel reported success")

    def metrics(self):
        return {"role": self.role, "session_id": self.identity[2], "model_generation": self.identity[1],
                "sent_samples": self.sent, "processed_samples": self.processed, "received_samples": self.received,
                "updates": self.updates, "characters": len(self.text),
                "transcript_sha256": hashlib.sha256(self.text.encode()).hexdigest(),
                "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "terminal": self.terminal, "close_code": self.ws.close_code,
                "forced_disconnect": self.forced_disconnect}

    async def __aexit__(self, *_):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.ws is not None:
            with suppress(Exception):
                await self.ws.close()


async def status(client):
    response = await client.get("/api/dictation/status")
    require(response.status_code == 200, "PC status request failed")
    return response.json()


async def wait_status(client, predicate, timeout):
    async def until():
        while True:
            value = await status(client)
            if predicate(value):
                return value
            await asyncio.sleep(.1)
    return await asyncio.wait_for(until(), timeout)


def preserved(value, initial):
    require(value["model_loaded"] and value["mode"] == "r2t2", "mobile action unloaded or switched the model")
    for key in ("server_instance_id", "model_generation", "worker_pid"):
        require(value[key] == initial[key], "mobile action changed " + key)


async def denied_routes(url, token, credential_id):
    rows = []
    async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=10,
                                 headers={"Authorization": "Bearer " + token}) as client:
        paths = [("GET" if name == "status" else "POST", "/api/dictation/" + name)
                 for name in ("status", "prepare", "warmup", "unload", "policy", "transcribe")]
        paths += [("GET", "/api/devices"), ("POST", "/api/devices"),
                  ("POST", f"/api/devices/{credential_id}/revoke"),
                  ("POST", f"/api/devices/{credential_id}/rotate")]
        for method, path in paths:
            response = await client.request(method, path)
            require(response.status_code == 403, "device credential accessed a local HTTP route")
            rows.append({"method": method, "path": path, "status": response.status_code})
    for path in ("/api/dictation/stream", "/api/dictation/v1/stream"):
        try:
            async with connect(ws_url(url, path), proxy=None, open_timeout=10,
                               additional_headers={"Authorization": "Bearer " + token}):
                raise CheckFailure("device credential accessed a local WebSocket route")
        except InvalidStatus as exc:
            require(exc.response.status_code == 403, "wrong denial status for local WebSocket route")
            rows.append({"method": "WS", "path": path, "status": exc.response.status_code})
    return rows


async def run(args, payload):
    pc_audio, mobile_audio = pcm(args.pc_audio), pcm(args.mobile_audio)
    payload["source_audio_sha256"] = {role: hashlib.sha256(audio).hexdigest()
                                      for role, audio in (("pc", pc_audio), ("mobile", mobile_audio))}
    require(pc_audio != mobile_audio, "PC and mobile require distinct test audio")
    pc_token, mobile_token, revoke_token = [path.read_text().strip() for path in
        (args.pc_token_file, args.mobile_token_file, args.revoke_mobile_token_file)]
    require(all(len(value) >= 32 for value in (pc_token, mobile_token, revoke_token)), "invalid token file")
    require(len({pc_token, mobile_token, revoke_token}) == 3, "test credentials must be distinct")
    mobile_url = args.mobile_url or args.url
    async with httpx.AsyncClient(base_url=args.url, trust_env=False, timeout=10,
                                 headers={"Authorization": "Bearer " + pc_token}) as admin:
        initial = await status(admin)
        require(initial["model_loaded"] and initial["mode"] == "r2t2" and not initial["busy"],
                "isolated model must already be loaded and idle")
        require(initial["worker_pid"] is not None, "loaded model has no worker PID")
        generation = (initial["server_instance_id"], initial["model_generation"])
        payload["initial"] = {key: initial[key] for key in ("server_instance_id", "model_generation", "worker_pid")}
        payload["denied_routes"] = await denied_routes(args.url, mobile_token, args.revoke_credential_id)
        preserved(await status(admin), initial)
        async with Stream(args.url, pc_token, pc_audio, "pc", generation, args.timeout) as pc:
            await pc.wait(lambda: bool(pc.text), "PC did not produce live fixed text")
            for kind in ("cancel", "disconnect", "finish", "revoke"):
                token = revoke_token if kind == "revoke" else mobile_token
                async with Stream(mobile_url, token, mobile_audio, "mobile", generation, args.timeout) as mobile:
                    await asyncio.sleep(args.mobile_seconds)
                    await mobile.wait(lambda: mobile.processed >= 2560, "mobile did not process real audio")
                    if kind == "revoke":
                        mobile.expected = "error"
                        await mobile.stop_audio()
                        response = await admin.post(f"/api/devices/{args.revoke_credential_id}/revoke")
                        require(response.status_code == 200, "test-device revocation failed")
                        await mobile.end("error", "UNAUTHORIZED")
                        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                            response = await client.get(mobile_url + "/api/mobile/v1/capabilities",
                                headers={"Authorization": "Bearer " + revoke_token})
                            require(response.status_code == 401 and response.json().get("code") == "UNAUTHORIZED",
                                    "revoked device still has mobile access")
                    else:
                        await mobile.end(kind)
                    evidence = {"scenario": kind, "mobile": mobile.metrics()}
                after = await wait_status(admin, lambda value: value["mobile_slots_available"] == 1, args.timeout)
                preserved(after, initial)
                require(any(row["session_id"] == pc.identity[2] and row["kind"] == "pc"
                            for row in after["active_sessions"]), "PC session was lost during mobile cleanup")
                characters, boundary = len(pc.text), pc.sent
                await pc.wait(lambda: len(pc.text) > characters and pc.processed > boundary,
                              "PC did not continue fixed text after " + kind)
                preserved(await status(admin), initial)
                evidence["pc_continued"] = {"session_id": pc.identity[2], "characters_before": characters,
                    "characters_after": len(pc.text), "new_audio_boundary_samples": boundary,
                    "processed_after_samples": pc.processed, "worker_pid": initial["worker_pid"],
                    "model_generation": generation[1]}
                payload["scenarios"].append(evidence)
            await pc.end("finish")
            payload["pc_final"] = pc.metrics()
        await wait_status(admin, lambda value: not value["busy"], args.timeout)

        async with Stream(mobile_url, mobile_token, mobile_audio, "mobile", generation, args.timeout) as mobile:
            await asyncio.sleep(args.mobile_seconds)
            await mobile.wait(lambda: mobile.processed >= 2560, "mobile-only stream did not process real audio")
            before_unload = await status(admin)
            require(not before_unload["pc_busy"] and len(before_unload["active_sessions"]) == 1
                    and before_unload["active_sessions"][0]["session_id"] == mobile.identity[2],
                    "unload requires exactly the mobile-only test session")
            mobile.expected = "error"
            await mobile.stop_audio()
            response = await admin.post("/api/dictation/unload", timeout=args.load_timeout)
            require(response.status_code == 200, "mobile-only unload failed")
            await mobile.end("error", "MODEL_NOT_READY")
            unloaded = await wait_status(admin, lambda value: not value["busy"], args.timeout)
            require(not unloaded["model_loaded"] and unloaded["model_generation"] is None
                    and unloaded["worker_pid"] is None, "unload retained the shared model")
            payload["lifecycle"] = {"mobile": mobile.metrics(), "unloaded": True,
                                    "unload_calls": 1, "prepare_calls": 0}
        response = await admin.post("/api/dictation/prepare", json={"mode": "r2t2"}, timeout=args.load_timeout)
        payload["lifecycle"]["prepare_calls"] = 1
        require(response.status_code == 200, "r2t2 reload failed")
        reloaded = await status(admin)
        require(reloaded["model_loaded"] and reloaded["mode"] == "r2t2" and not reloaded["busy"]
                and reloaded["server_instance_id"] == generation[0]
                and reloaded["model_generation"] and reloaded["model_generation"] != generation[1]
                and reloaded["worker_pid"] is not None, "reload did not create a new model generation")
        async with connect(ws_url(mobile_url, "/api/mobile/v1/dictation/stream"), proxy=None, open_timeout=10,
                           additional_headers={"Authorization": "Bearer " + mobile_token}) as ws:
            await ws.send(json.dumps(start_message("mobile", generation)))
            rejected = json.loads(await asyncio.wait_for(ws.recv(), args.timeout))
            require(rejected.get("type") == "error" and rejected.get("code") == "MODEL_CHANGED"
                    and rejected.get("complete") is False, "stale model generation was not rejected")
            await asyncio.wait_for(ws.wait_closed(), args.timeout)
            require(ws.close_code == 1008, "stale model generation has wrong close code")
        require(not (await status(admin))["busy"], "stale start left an active session")
        payload["lifecycle"].update(reloaded_generation=reloaded["model_generation"],
            reloaded_worker_pid=reloaded["worker_pid"], stale_generation_error=rejected["code"])
        payload["passed"] = True


async def main(args):
    payload = {"schema_version": 1, "passed": False, "scenarios": []}
    try:
        await asyncio.wait_for(run(args, payload), 6 * args.timeout + 4 * args.mobile_seconds + 2 * args.load_timeout + 120)
    except Exception as exc:
        payload["failure"] = {"kind": type(exc).__name__}
        if isinstance(exc, CheckFailure):
            payload["failure"]["reason"] = str(exc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"passed": payload["passed"], "output": str(args.output),
                      "completed_scenarios": [row["scenario"] for row in payload["scenarios"]],
                      "lifecycle": payload.get("lifecycle"), "failure": payload.get("failure")}, indent=2))
    return payload["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18096")
    parser.add_argument("--mobile-url")
    parser.add_argument("--pc-token-file", type=Path, required=True)
    parser.add_argument("--mobile-token-file", type=Path, required=True)
    parser.add_argument("--revoke-mobile-token-file", type=Path, required=True,
                        help="Plain token of a disposable device; this run permanently revokes it")
    parser.add_argument("--revoke-credential-id", required=True)
    parser.add_argument("--pc-audio", type=Path, required=True)
    parser.add_argument("--mobile-audio", type=Path, required=True)
    parser.add_argument("--mobile-seconds", type=float, default=8)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--load-timeout", type=float, default=240)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        target = urlsplit(args.url)
        require(target.scheme in {"http", "https"} and ipaddress.ip_address(target.hostname).is_loopback
                and target.port == 18096 and target.path in {"", "/"}
                and not target.username and not target.password and not target.query and not target.fragment,
                "management URL must be a loopback isolated API on port 18096")
        UUID(args.revoke_credential_id)
        require(all(value > 0 for value in (args.mobile_seconds, args.timeout, args.load_timeout)),
                "time limits must be positive")
        args.url = args.url.rstrip("/")
        if args.mobile_url:
            target = urlsplit(args.mobile_url)
            require(target.scheme in {"http", "https"} and bool(target.hostname)
                    and target.path in {"", "/"} and not target.username and not target.password
                    and not target.query and not target.fragment, "invalid mobile API URL")
            args.mobile_url = args.mobile_url.rstrip("/")
    except (ValueError, CheckFailure) as exc:
        parser.error(str(exc))
    raise SystemExit(0 if asyncio.run(main(args)) else 1)
