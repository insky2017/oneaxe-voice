"""Opt-in real-clock V1 acceptance for the already loaded R2T2 or Qwen model.

Never starts services or changes configuration. Model management requires
--allow-model-management and a separate loopback port. Supply distinct
consented WAV/PCM16LE fixtures and token files.
The default suite checks V1 completion, cancellation/disconnect isolation,
permissions, stale bindings, and Qwen legacy PC + mobile V1 coexistence.
Only counters, timings, identities, and hashes are written, never transcripts.
--inspect-inputs validates fixtures locally without contacting any service.
"""

import argparse
import asyncio
from contextlib import suppress
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from e2e_concurrent import audio_frame, pcm, percentiles, sample_gpu, gpu_summary
from e2e_qwen_wait import run_case as legacy_qwen_case


RATE = 16000
AUDIO = {"encoding": "pcm_s16le", "sample_rate": RATE, "channels": 1}
IDENTITY = ("server_instance_id", "model_generation", "session_id")


class CaseFailure(RuntimeError):
    """All reportable reasons are fixed local codes."""


def require(condition, code):
    if not condition:
        raise CaseFailure(code)


def load_audio(path):
    value = path.read_bytes() if path.suffix.casefold() == ".pcm" else pcm(path)
    require(bool(value) and len(value) % 2 == 0, "INVALID_AUDIO_SIZE")
    return value


def audio_info(value):
    return {"sha256": hashlib.sha256(value).hexdigest(), "samples": len(value) // 2,
            "seconds": round(len(value) / (2 * RATE), 6)}


def validated_url(url):
    parts = urlsplit(url)
    require(parts.scheme in {"http", "https"} and bool(parts.hostname), "INVALID_URL")
    require(not parts.username and not parts.password and not parts.query and not parts.fragment
            and parts.path in {"", "/"}, "URL_MUST_NOT_CONTAIN_CREDENTIALS_OR_PATH")
    return url.rstrip("/")


def ws_url(url, path):
    parts = urlsplit(url)
    return urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc, path, "", ""))


def start_message(role, generation, mode):
    value = {"type": "start", "protocol_version": 1, "audio": AUDIO.copy()}
    if role == "pc":
        value["mode"] = mode
    else:
        value.update(expected_server_instance_id=generation[0], expected_model_generation=generation[1])
    return value


def keyword_checks(text, own, foreign):
    normalized = " ".join(text.casefold().split())
    def rows(words):
        return [{"sha256": hashlib.sha256(word.encode()).hexdigest(),
                 "matched": " ".join(word.casefold().split()) in normalized} for word in words]
    required, rejected = rows(own), rows(foreign)
    return {"required": required, "foreign": rejected,
            "passed": all(row["matched"] for row in required) and not any(row["matched"] for row in rejected)}


async def get_status(client):
    response = await client.get("/api/dictation/status")
    require(response.status_code == 200, "STATUS_HTTP_FAILED")
    value = response.json()
    require(isinstance(value, dict), "INVALID_STATUS")
    return value


def require_preserved(value, before):
    require(value.get("model_loaded") is True and all(value.get(key) == before.get(key)
            for key in ("server_instance_id", "model_generation", "worker_pid", "mode")),
            "MODEL_OR_WORKER_CHANGED")


async def wait_status(client, predicate, timeout):
    async def wait():
        while True:
            value = await get_status(client)
            if predicate(value):
                return value
            await asyncio.sleep(.1)
    return await asyncio.wait_for(wait(), timeout)


class Stream:
    def __init__(self, url, token, audio, role, before, caps, args):
        self.url, self.token, self.audio, self.role = url, token, audio, role
        self.before, self.caps, self.args = before, caps, args
        self.ws = None
        self.tasks = []
        self.changed = asyncio.Event()
        self.closed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.seq = self.sent = self.captured = self.received = self.processed = 0
        self.fixed = ""
        self.fixed_times, self.lag, self.schedule_lag, self.flushes = [], [], [], []
        self.pending = ""
        self.limit = self.window = caps["flow"]["window_samples"]
        self.buffer_limit = round(caps["flow"]["client_buffer_max_ms"] * RATE / 1000)
        self.frame_samples = min(2560, caps["audio"]["max_frame_bytes"] // 2)
        self.max_buffered = self.credit_waits = self.events = self.ignored_late_text = 0
        self.terminal = None
        self.expected_code = None
        self.accept_text = True
        self.ending = None
        self.sent_digest = hashlib.sha256()
        self.failure = None

    async def open(self):
        path = "/api/dictation/v1/stream" if self.role == "pc" else "/api/mobile/v1/dictation/stream"
        self.ws = await connect(ws_url(self.url, path), proxy=None, open_timeout=10,
                                close_timeout=3, max_size=2 * 1024 * 1024, max_queue=4,
                                additional_headers={"Authorization": "Bearer " + self.token})
        generation = tuple(self.before[key] for key in IDENTITY[:2])
        await self.ws.send(json.dumps(start_message(self.role, generation, self.before["mode"])))
        ready = json.loads(await asyncio.wait_for(self.ws.recv(), self.args.timeout))
        require(ready.get("type") == "ready", "V1_NOT_READY")
        self.identity = tuple(ready.get(key) for key in IDENTITY)
        require(self.identity[:2] == generation and isinstance(self.identity[2], str)
                and bool(self.identity[2]), "READY_BINDING_MISMATCH")
        require(type(ready.get("seq")) is int and ready["seq"] > 0, "INVALID_READY_SEQUENCE")
        require(ready.get("audio_received_samples") == ready.get("audio_processed_samples") == 0
                and ready.get("audio_send_limit") == self.window, "INVALID_INITIAL_CREDIT")
        require(str(ready.get("device", "")).startswith("cuda:"), "V1_NOT_USING_CUDA")
        self.seq = ready["seq"]

    def task_done(self, task):
        if not task.cancelled() and (error := task.exception()) is not None:
            self.failure = error
        self.changed.set()

    def check(self):
        if self.failure is not None:
            raise self.failure

    async def wait(self, predicate, timeout=None):
        async def until():
            while True:
                self.check()
                if predicate():
                    return
                require(not self.closed.is_set(), "STREAM_ENDED_BEFORE_EXPECTED_PROGRESS")
                self.changed.clear()
                await self.changed.wait()
        await asyncio.wait_for(until(), self.args.timeout if timeout is None else timeout)

    async def control(self, kind):
        async with self.lock:
            value = {"type": kind}
            if kind in {"flush", "finish"}:
                value["after_audio_samples"] = self.sent
            if kind == "flush":
                self.flushes.append({"after_audio_samples": self.sent,
                                     "sent_at_seconds": round(time.monotonic() - self.started, 6),
                                     "processed_barrier_reached": False})
            await self.ws.send(json.dumps(value))

    async def receive(self):
        try:
            async for raw in self.ws:
                if self.role == "mobile" and self.args.mobile_receive_delay:
                    await asyncio.sleep(self.args.mobile_receive_delay)
                item = json.loads(raw)
                require(isinstance(item, dict) and tuple(item.get(key) for key in IDENTITY) == self.identity,
                        "EVENT_BINDING_MISMATCH")
                require(type(item.get("seq")) is int and item["seq"] > self.seq, "EVENT_SEQUENCE_REGRESSED")
                self.seq = item["seq"]
                self.events += 1
                for key, attr in (("audio_received_samples", "received"), ("audio_processed_samples", "processed")):
                    if key in item:
                        require(type(item[key]) is int and getattr(self, attr) <= item[key] <= self.sent,
                                "AUDIO_PROGRESS_REGRESSED_OR_OVERRAN_SENT")
                        setattr(self, attr, item[key])
                require(self.processed <= self.received <= self.sent, "PROCESSED_EXCEEDS_RECEIVED")
                if "audio_send_limit" in item:
                    require(type(item["audio_send_limit"]) is int and item["audio_send_limit"] >= self.limit
                            and item["audio_send_limit"] == self.processed + self.window, "INVALID_CUMULATIVE_CREDIT")
                    self.limit = item["audio_send_limit"]
                if "text" in item:
                    require(isinstance(item["text"], str) and isinstance(item.get("pending", ""), str),
                            "INVALID_TEXT_FIELDS")
                    if self.accept_text:
                        require(item["text"].startswith(self.fixed), "FIXED_TEXT_PREFIX_REGRESSED")
                        if item["text"] != self.fixed:
                            self.fixed_times.append(time.monotonic() - self.started)
                        self.fixed, self.pending = item["text"], item.get("pending", "")
                    else:
                        self.ignored_late_text += 1
                for flush in self.flushes:
                    flush["processed_barrier_reached"] |= self.processed >= flush["after_audio_samples"]
                if item.get("type") in {"final", "error"}:
                    self.terminal = {key: item.get(key) for key in ("type", "reason", "complete")}
                    if item.get("type") == "error":
                        require(self.ending == "error" and item.get("code") == self.expected_code
                                and item.get("complete") is False, "UNEXPECTED_V1_TERMINAL_ERROR")
                        self.terminal["code"] = self.expected_code
                    else:
                        require(self.ending in {"finish", "cancel"}, "UNEXPECTED_FINAL")
                    require(item.get("pending") == "", "TERMINAL_HAS_PENDING_TEXT")
                self.changed.set()
        except ConnectionClosed:
            require(self.ending == "disconnect" or self.terminal is not None, "V1_CLOSED_BEFORE_FINAL")
        finally:
            self.closed.set()
            self.changed.set()
        require(self.ending == "disconnect" or self.terminal is not None, "V1_CLOSED_BEFORE_FINAL")

    async def capture(self, seconds, ending, queue):
        total, next_flush = round(seconds * RATE), self.args.flush_seconds
        for offset in range(0, total, self.frame_samples):
            count = min(self.frame_samples, total - offset)
            deadline = self.started + (offset + count) / RATE
            await asyncio.sleep(max(0, deadline - time.monotonic()))
            self.schedule_lag.append(max(0, time.monotonic() - deadline))
            require(self.captured - self.sent + count <= self.buffer_limit, "CAPTURE_BUFFER_EXCEEDED")
            self.captured += count
            self.max_buffered = max(self.max_buffered, self.captured - self.sent)
            queue.put_nowait(("audio", audio_frame(self.audio, offset, count)))
            if (offset + count) / RATE >= next_flush and next_flush < seconds:
                queue.put_nowait(("flush", None))
                next_flush += self.args.flush_seconds
        if ending == "finish":
            queue.put_nowait(("finish", None))

    async def send(self, queue):
        while True:
            kind, frame = await queue.get()
            if kind != "audio":
                if kind == "finish":
                    self.ending = "finish"
                await self.control(kind)
                if kind == "finish":
                    return
                continue
            count = len(frame) // 2
            if self.sent + count > self.limit:
                self.credit_waits += 1
                await self.wait(lambda: self.sent + count <= self.limit)
            async with self.lock:
                self.sent += count
                self.sent_digest.update(frame)
                await self.ws.send(frame)

    async def monitor(self, seconds):
        while not self.closed.is_set():
            now = time.monotonic() - self.started
            if now <= seconds:
                self.lag.append((now, max(0, min(now, self.captured / RATE) - self.processed / RATE)))
            await asyncio.sleep(.16)

    async def keepalive(self):
        while not self.closed.is_set():
            await asyncio.sleep(10)
            if not self.closed.is_set() and self.ending is None:
                await self.control("keepalive")

    async def interrupt(self, seconds, ending):
        await asyncio.sleep(max(0, self.started + seconds - time.monotonic()))
        self.ending, self.accept_text = ending, False
        for task in self.tasks[:2]:
            task.cancel()
        await asyncio.gather(*self.tasks[:2], return_exceptions=True)
        self.check()
        if ending == "cancel":
            await self.control("cancel")
        else:
            self.ws.transport.abort()

    async def run(self, seconds, ending="finish", gate=None):
        if gate is not None:
            await gate.wait()
        self.started = time.monotonic()
        self.capture_seconds = seconds
        queue = asyncio.Queue(maxsize=math.ceil(self.buffer_limit / self.frame_samples) + 8)
        receiver = asyncio.create_task(self.receive())
        self.tasks = [asyncio.create_task(self.capture(seconds, ending, queue)),
                      asyncio.create_task(self.send(queue)), receiver,
                      asyncio.create_task(self.monitor(seconds)), asyncio.create_task(self.keepalive())]
        if ending in {"cancel", "disconnect"}:
            self.tasks.append(asyncio.create_task(self.interrupt(seconds, ending)))
        for task in self.tasks:
            task.add_done_callback(self.task_done)
        try:
            await self.wait(lambda: self.closed.is_set(), seconds + self.args.timeout + self.args.load_timeout)
            self.check()
            await receiver
            require(self.ws.close_code == (1006 if ending == "disconnect" else 1008 if ending == "managed" else 1000),
                    "WRONG_CLOSE_CODE")
            if ending == "finish":
                require(self.terminal == {"type": "final", "reason": "finished", "complete": True},
                        "FINISH_DID_NOT_REPORT_SUCCESS")
                require(self.captured == self.sent == self.received == self.processed == round(seconds * RATE),
                        "FINISH_DID_NOT_PROCESS_ALL_CAPTURED_AUDIO")
                require(bool(self.fixed_times) and self.fixed_times[0] < seconds, "NO_LIVE_FIXED_TEXT")
                require(all(row["processed_barrier_reached"] for row in self.flushes), "FLUSH_BARRIER_NOT_PROCESSED")
            elif ending == "cancel":
                require(self.terminal == {"type": "final", "reason": "cancelled", "complete": False},
                        "CANCEL_DID_NOT_REPORT_INCOMPLETE_FINAL")
            elif ending == "managed":
                require(self.terminal is not None and self.terminal.get("type") == "error"
                        and self.terminal.get("code") == self.expected_code
                        and self.terminal.get("complete") is False, "MANAGEMENT_ERROR_REPORTED_SUCCESS")
            return self.metrics(ending)
        finally:
            await self.cleanup()

    def metrics(self, ending):
        lag = percentiles([value for _, value in self.lag])
        early = percentiles([value for at, value in self.lag if at < self.capture_seconds / 3])
        late = percentiles([value for at, value in self.lag if at > 2 * self.capture_seconds / 3])
        growth = late.get("p95", 0) - early.get("p95", 0)
        fixed = [at for at in self.fixed_times if at < self.capture_seconds]
        gap = max((b - a for a, b in zip([0] + fixed, fixed + [self.capture_seconds])), default=self.capture_seconds)
        checks = {"client_buffer_bounded": self.max_buffered <= self.buffer_limit,
                  "processed_lag_bounded": lag.get("p95", math.inf) <= self.args.max_processed_lag_seconds,
                  "lag_growth_bounded": growth <= self.args.max_lag_growth_seconds,
                  "capture_clock_bounded": max(self.schedule_lag, default=0) <= self.args.max_processed_lag_seconds}
        if ending == "finish":
            checks.update(live_text_early=any(at < self.capture_seconds / 3 for at in fixed),
                          live_text_late=any(at >= 2 * self.capture_seconds / 3 for at in fixed),
                          fixed_gap_bounded=gap <= self.args.max_fixed_gap_seconds)
        own = self.args.pc_keyword if self.role == "pc" else self.args.mobile_keyword
        foreign = self.args.mobile_keyword if self.role == "pc" else self.args.pc_keyword
        keywords = keyword_checks(self.fixed, own, foreign)
        return {"role": self.role, "ending": ending, "session_id": self.identity[2],
                "server_instance_id": self.identity[0], "model_generation": self.identity[1],
                "capture_seconds": self.capture_seconds, "captured_samples": self.captured,
                "sent_samples": self.sent, "received_samples": self.received, "processed_samples": self.processed,
                "sent_pcm_sha256": self.sent_digest.hexdigest(), "transcript_sha256": hashlib.sha256(self.fixed.encode()).hexdigest(),
                "characters": len(self.fixed), "events": self.events, "last_sequence": self.seq,
                "fixed_updates_seconds": [round(value, 6) for value in self.fixed_times],
                "max_buffered_samples": self.max_buffered, "credit_waits": self.credit_waits,
                "ignored_late_text_events": self.ignored_late_text, "processed_lag_seconds": lag,
                "late_minus_early_p95_seconds": round(growth, 6), "max_fixed_gap_seconds": round(gap, 6),
                "producer_schedule_lag_seconds": percentiles(self.schedule_lag), "flushes": self.flushes,
                "terminal": self.terminal, "close_code": self.ws.close_code,
                "performance_checks": checks, "keyword_checks": keywords,
                "passed": all(checks.values()) and (keywords["passed"] if ending == "finish" else True)}

    async def cleanup(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.ws is not None:
            with suppress(Exception):
                await asyncio.wait_for(self.ws.close(), 5)


async def pair(args, before, caps, tokens, audios, admin, mobile_ending="finish"):
    streams = [Stream(args.url, tokens[0], audios[0], "pc", before, caps, args),
               Stream(args.mobile_url or args.url, tokens[1], audios[1], "mobile", before, caps, args)]
    tasks = []
    try:
        await asyncio.gather(*(stream.open() for stream in streams))
        require(streams[0].identity[2] != streams[1].identity[2], "SESSION_IDENTITIES_NOT_DISTINCT")
        capacity = await rejected_start(args.mobile_url or args.url, tokens[1],
            start_message("mobile", tuple(before[key] for key in IDENTITY[:2]), before["mode"]),
            "CAPACITY_EXCEEDED", args.timeout, close_code=1013)
        seconds = args.seconds if mobile_ending == "finish" else args.isolation_seconds
        mobile_seconds = seconds if mobile_ending == "finish" else args.interrupt_after
        gate = asyncio.Event()
        tasks = [asyncio.create_task(streams[0].run(seconds, gate=gate)),
                 asyncio.create_task(streams[1].run(mobile_seconds, mobile_ending, gate))]
        gate.set()
        isolated = None
        if mobile_ending != "finish":
            await tasks[1]
            current = await wait_status(admin, lambda value: value.get("mobile_slots_available") == 1, args.timeout)
            require_preserved(current, before)
            require(any(row.get("session_id") == streams[0].identity[2] and row.get("kind") == "pc"
                        for row in current.get("active_sessions", [])), "PC_LOST_AFTER_MOBILE_STOP")
            boundary, characters = streams[0].sent, len(streams[0].fixed)
            await streams[0].wait(lambda: streams[0].processed > boundary and len(streams[0].fixed) > characters)
            isolated = {"pc_session_preserved": True, "processed_after_boundary_samples": streams[0].processed,
                        "audio_boundary_samples": boundary, "fixed_characters_before": characters,
                        "fixed_characters_after": len(streams[0].fixed)}
        rows = await asyncio.gather(*tasks)
        after = await wait_status(admin, lambda value: not value.get("busy"), args.timeout)
        require_preserved(after, before)
        return {"scenario": "dual" if mobile_ending == "finish" else "mobile_" + mobile_ending,
                "sessions": rows, "isolation": isolated, "model_preserved": True,
                "second_mobile_rejected": capacity,
                "distinct_transcripts": rows[0]["transcript_sha256"] != rows[1]["transcript_sha256"],
                "passed": all(row["passed"] for row in rows) and
                          rows[0]["transcript_sha256"] != rows[1]["transcript_sha256"]}
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(stream.cleanup() for stream in streams))


async def rejected_start(url, token, hello, expected, timeout, close_code=1008):
    async with connect(ws_url(url, "/api/mobile/v1/dictation/stream"), proxy=None, open_timeout=10,
                       additional_headers={"Authorization": "Bearer " + token}) as ws:
        await ws.send(json.dumps(hello))
        item = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        require(item.get("type") == "error" and item.get("code") == expected
                and item.get("complete") is False and item.get("text", "") == ""
                and item.get("session_id") is None and item.get("seq") == 0, "START_PROTECTION_FAILED")
        await asyncio.wait_for(ws.wait_closed(), timeout)
        require(ws.close_code == close_code, "REJECTED_START_WRONG_CLOSE")
    return {"error_code": expected, "close_code": close_code}


async def guards(args, before, token, admin):
    url = args.mobile_url or args.url
    rows = []
    async with httpx.AsyncClient(base_url=args.url, trust_env=False, timeout=10,
                                 headers={"Authorization": "Bearer " + token}) as mobile:
        for path in ("/api/dictation/status", "/api/devices"):
            response = await mobile.get(path)
            require(response.status_code == 403, "MOBILE_ACCESSED_LOCAL_HTTP")
            rows.append({"method": "GET", "path": path, "status": 403})
    async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=10) as anonymous:
        for headers in ({}, {"Authorization": "Bearer unified-e2e-invalid-token"}):
            response = await anonymous.get("/api/mobile/v1/capabilities", headers=headers)
            require(response.status_code == 401, "INVALID_CREDENTIAL_NOT_DENIED")
            rows.append({"method": "GET", "path": "/api/mobile/v1/capabilities", "status": 401})
    for path in ("/api/dictation/stream", "/api/dictation/v1/stream"):
        try:
            async with connect(ws_url(args.url, path), proxy=None, open_timeout=10,
                               additional_headers={"Authorization": "Bearer " + token}):
                raise CaseFailure("MOBILE_ACCESSED_LOCAL_WEBSOCKET")
        except InvalidStatus as exc:
            require(exc.response.status_code == 403, "WRONG_LOCAL_WEBSOCKET_DENIAL")
            rows.append({"method": "WS", "path": path, "status": 403})
    generation = tuple(before[key] for key in IDENTITY[:2])
    rejected = {}
    for key in ("expected_server_instance_id", "expected_model_generation", "mode", "model_id"):
        hello = start_message("mobile", generation, before["mode"])
        hello[key] = "unified-e2e-stale-or-forbidden"
        rejected[key] = await rejected_start(url, token, hello,
            "MODEL_CHANGED" if key.startswith("expected_") else "INVALID_MESSAGE", args.timeout)
    after = await wait_status(admin, lambda value: not value.get("busy"), args.timeout)
    require_preserved(after, before)
    require(after.get("active_sessions") == [] and after.get("mobile_slots_available") == 1,
            "REJECTED_START_LEAKED_SESSION")
    return {"scenario": "guards", "http_and_ws_denials": rows, "start_rejections": rejected,
            "model_preserved": True, "passed": True}


async def legacy_pair(args, before, caps, tokens, audios, admin):
    require(before["mode"] == "qwen-stream", "LEGACY_CASE_REQUIRES_QWEN")
    require(args.pc_audio.suffix.casefold() != ".pcm", "LEGACY_PC_FIXTURE_MUST_BE_WAV")
    mobile = Stream(args.mobile_url or args.url, tokens[1], audios[1], "mobile", before, caps, args)
    tasks = []
    overlap = 0
    try:
        await mobile.open()
        tasks = [asyncio.create_task(legacy_qwen_case(args.url, tokens[0], args.pc_audio,
                    seconds=args.legacy_seconds, flush_seconds=args.flush_seconds)),
                 asyncio.create_task(mobile.run(args.legacy_seconds))]
        while not all(task.done() for task in tasks):
            for task in tasks:
                if task.done():
                    task.result()
            current = await get_status(admin)
            require_preserved(current, before)
            kinds = {row.get("kind") for row in current.get("active_sessions", [])}
            overlap += int(kinds == {"pc", "mobile"})
            await asyncio.sleep(.5)
        legacy, remote = await asyncio.gather(*tasks)
        require(legacy.get("passed") is True, "LEGACY_QWEN_CASE_FAILED")
        require(overlap >= min(6, math.floor(args.legacy_seconds)), "LEGACY_AND_V1_DID_NOT_OVERLAP")
        after = await wait_status(admin, lambda value: not value.get("busy"), args.timeout)
        require_preserved(after, before)
        return {"scenario": "legacy_pc_mobile_v1", "legacy": legacy, "mobile": remote,
                "overlap_samples": overlap, "overlap_sample_interval_seconds": .5,
                "model_preserved": True, "passed": remote["passed"]}
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await mobile.cleanup()


async def get_capabilities(args, token, before):
    async with httpx.AsyncClient(base_url=args.mobile_url or args.url, trust_env=False, timeout=10,
                                 headers={"Authorization": "Bearer " + token}) as mobile:
        response = await mobile.get("/api/mobile/v1/capabilities")
    require(response.status_code == 200, "CAPABILITIES_HTTP_FAILED")
    caps = response.json()
    require(caps.get("protocol_version") == 1 and caps.get("ready") is True
            and caps.get("stream_supported") is True and caps.get("can_start") is True
            and caps.get("mode") == before["mode"] and caps.get("max_sessions") == 2
            and caps.get("mobile_slots_available") == 1
            and all(caps.get(key) == before[key] for key in IDENTITY[:2]), "CAPABILITIES_NOT_READY_OR_BOUND")
    require(all(caps.get("audio", {}).get(key) == value for key, value in AUDIO.items())
            and type(caps["audio"].get("max_frame_bytes")) is int and caps["audio"]["max_frame_bytes"] >= 2,
            "UNSUPPORTED_AUDIO_CAPABILITIES")
    require(type(caps.get("flow", {}).get("window_samples")) is int
            and caps["flow"]["window_samples"] > 0 and caps["flow"].get("client_buffer_max_ms") == 2000,
            "INVALID_FLOW_CAPABILITIES")
    return {key: caps[key] for key in ("protocol_version", "server_instance_id", "model_generation", "mode",
            "model_state", "ready", "stream_supported", "can_start", "max_sessions", "mobile_slots_available",
            "audio", "flow", "session_max_seconds")}


async def management(args, before, caps, tokens, audios, admin, payload):
    original_mode = before["mode"]
    rows = []

    async def manage_call(path, mode=None):
        current = await get_status(admin)
        require(current.get("pc_busy") is False, "MANAGEMENT_FOUND_PC_RECORDING")
        allowed = mobile.identity[2] if mobile is not None else None
        require(all(row.get("kind") == "mobile" and row.get("session_id") == allowed
                    for row in current.get("active_sessions", [])), "MANAGEMENT_FOUND_UNRELATED_SESSION")
        payload["model_management_calls"] += 1
        response = await admin.post(path, json={"mode": mode} if mode is not None else None,
                                    timeout=args.load_timeout)
        require(response.status_code == 200, "MODEL_MANAGEMENT_HTTP_FAILED")
        return await get_status(admin)

    async def active_mobile(current, capability, action, expected_code):
        nonlocal mobile
        mobile = Stream(args.mobile_url or args.url, tokens[1], audios[1], "mobile", current, capability, args)
        task = None
        try:
            await mobile.open()
            task = asyncio.create_task(mobile.run(args.management_seconds, "managed"))
            await mobile.wait(lambda: bool(mobile.fixed) and mobile.processed >= mobile.frame_samples)
            prefix = mobile.fixed
            mobile.ending, mobile.expected_code = "error", expected_code
            for pending in mobile.tasks[:2]:
                pending.cancel()
            await asyncio.gather(*mobile.tasks[:2], return_exceptions=True)
            mobile.check()
            after = await action()
            metrics = await task
            require(mobile.fixed.startswith(prefix), "MANAGEMENT_DROPPED_FIXED_PREFIX")
            rows.append({"error_code": expected_code, "fixed_prefix_preserved": True,
                         "fixed_characters_before": len(prefix), "mobile": metrics})
            await wait_status(admin, lambda value: not value.get("busy"), args.timeout)
            return after
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await mobile.cleanup()
            mobile = None

    async def finish_mobile(current):
        capability = await get_capabilities(args, tokens[1], current)
        value = Stream(args.mobile_url or args.url, tokens[1], audios[1], "mobile", current, capability, args)
        try:
            await value.open()
            result = await value.run(args.management_seconds)
            require(result["passed"], "NEW_GENERATION_MOBILE_DID_NOT_COMPLETE")
            await wait_status(admin, lambda status: not status.get("busy"), args.timeout)
            return result
        finally:
            await value.cleanup()

    mobile = None
    try:
        target_mode = "r2t2" if original_mode == "qwen-stream" else "qwen-stream"
        switched = await active_mobile(before, caps,
            lambda: manage_call("/api/dictation/prepare", target_mode), "MODEL_CHANGED")
        require(switched.get("mode") == target_mode and switched.get("model_loaded") is True
                and switched.get("model_generation") != before["model_generation"], "SWITCH_DID_NOT_CHANGE_GENERATION")
        new_result = await finish_mobile(switched)
        restored = await manage_call("/api/dictation/prepare", original_mode)
        require(restored.get("mode") == original_mode and restored.get("model_loaded") is True,
                "ORIGINAL_MODE_NOT_RESTORED")
        restored_caps = await get_capabilities(args, tokens[1], restored)
        unloaded = await active_mobile(restored, restored_caps,
            lambda: manage_call("/api/dictation/unload"), "MODEL_NOT_READY")
        require(unloaded.get("model_loaded") is False and unloaded.get("model_generation") is None
                and unloaded.get("worker_pid") is None, "UNLOAD_RETAINED_MODEL")
        reloaded = await manage_call("/api/dictation/prepare", original_mode)
        require(reloaded.get("mode") == original_mode and reloaded.get("model_loaded") is True
                and reloaded.get("model_generation") != restored["model_generation"]
                and reloaded.get("server_instance_id") == before["server_instance_id"], "RELOAD_DID_NOT_CHANGE_GENERATION")
        stale = await rejected_start(args.mobile_url or args.url, tokens[1],
            start_message("mobile", tuple(restored[key] for key in IDENTITY[:2]), original_mode),
            "MODEL_CHANGED", args.timeout)
        final_result = await finish_mobile(reloaded)
        return {"scenario": "isolated_model_management", "terminations": rows,
                "new_mode_mobile": new_result, "reloaded_mobile": final_result, "stale_generation": stale,
                "original_mode_restored": True, "final_model_generation": reloaded["model_generation"], "passed": True}
    finally:
        current = await get_status(admin)
        if current.get("mode") != original_mode or current.get("model_loaded") is not True:
            await manage_call("/api/dictation/prepare", original_mode)


def require_isolated_management_url(args):
    for url in (args.url, args.mobile_url or args.url):
        parts = urlsplit(validated_url(url))
        try:
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = parts.hostname == "localhost"
        require(loopback and parts.port is not None and parts.port != 8097 and parts.scheme == "http",
                "MODEL_MANAGEMENT_REQUIRES_SEPARATE_LOOPBACK_HTTP_PORT")
    require(urlsplit(args.url).netloc == urlsplit(args.mobile_url or args.url).netloc,
            "MODEL_MANAGEMENT_URLS_MUST_MATCH")


async def run(args, payload):
    audios = [load_audio(path) for path in (args.pc_audio, args.mobile_audio)]
    require(audios[0] != audios[1], "AUDIO_FIXTURES_MUST_BE_DISTINCT")
    payload["source_audio"] = {role: audio_info(audio) for role, audio in zip(("pc", "mobile"), audios)}
    tokens = [path.read_text().strip() for path in (args.pc_token_file, args.mobile_token_file)]
    require(all(len(token) >= 32 and "\n" not in token for token in tokens)
            and tokens[0] != tokens[1], "INVALID_OR_SHARED_TOKEN_FILES")
    args.url = validated_url(args.url)
    if args.mobile_url:
        args.mobile_url = validated_url(args.mobile_url)
    async with httpx.AsyncClient(base_url=args.url, trust_env=False, timeout=10,
                                 headers={"Authorization": "Bearer " + tokens[0]}) as admin:
        before = await get_status(admin)
        require(before.get("model_loaded") is True and before.get("mode") in {"r2t2", "qwen-stream"}
                and before.get("busy") is False and before.get("active_sessions") == []
                and before.get("worker_pid") is not None, "CURRENT_MODEL_MUST_BE_READY_AND_IDLE")
        caps = await get_capabilities(args, tokens[1], before)
        payload["model"] = {key: before[key] for key in ("mode", "server_instance_id", "model_generation", "worker_pid")}
        payload["capabilities"] = caps
        selected = {args.scenario} if args.scenario != "all" else {"guards", "dual", "isolation", "legacy"}
        if args.allow_model_management:
            require_isolated_management_url(args)
            selected.add("management")
        gpu_samples, gpu_stop = [], asyncio.Event()
        sampler = asyncio.create_task(sample_gpu(gpu_samples, gpu_stop, time.monotonic(), 1)) if args.sample_gpu else None
        try:
            if "guards" in selected:
                payload["scenarios"].append(await guards(args, before, tokens[1], admin))
            if "dual" in selected:
                payload["scenarios"].append(await pair(args, before, caps, tokens, audios, admin))
            if "isolation" in selected:
                for ending in ("cancel", "disconnect"):
                    payload["scenarios"].append(await pair(args, before, caps, tokens, audios, admin, ending))
            if "legacy" in selected:
                if before["mode"] == "qwen-stream":
                    payload["scenarios"].append(await legacy_pair(args, before, caps, tokens, audios, admin))
                else:
                    require(args.scenario == "all", "LEGACY_CASE_REQUIRES_QWEN")
                    payload["skipped"] = [{"scenario": "legacy", "reason": "CURRENT_MODEL_IS_R2T2"}]
            if "management" in selected:
                payload["scenarios"].append(await management(args, before, caps, tokens, audios, admin, payload))
            if sampler and sampler.done():
                sampler.result()
            payload["passed"] = bool(payload["scenarios"]) and all(row["passed"] for row in payload["scenarios"])
        finally:
            gpu_stop.set()
            if sampler:
                await sampler
                payload["gpu"] = {"summary": gpu_summary(gpu_samples, before["worker_pid"]), "samples": gpu_samples}


async def main(args):
    payload = {"schema_version": 1, "passed": False, "scenarios": [],
               "model_management_calls": 0, "service_management_calls": 0,
               "scope": "real-service-protocol-fixture; microphone/UI/cross-network evidence is separate"}
    try:
        total_timeout = (args.seconds + 2 * args.isolation_seconds + args.legacy_seconds + 8 * args.timeout + 60
                         + (6 * args.load_timeout + 4 * args.management_seconds if args.allow_model_management else 0))
        await asyncio.wait_for(run(args, payload), total_timeout)
    except Exception as exc:
        payload["passed"] = False
        payload["failure"] = {"kind": type(exc).__name__,
                              "code": str(exc) if isinstance(exc, CaseFailure) else "CLIENT_EXCEPTION"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as target:
        os.fchmod(target.fileno(), 0o600)
        json.dump(payload, target, indent=2)
        target.write("\n")
    print(json.dumps({"passed": payload["passed"], "completed_scenarios": [row["scenario"] for row in payload["scenarios"]],
                      "failure": payload.get("failure"), "output": str(args.output)}, indent=2))
    return payload["passed"]


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--mobile-url")
    parser.add_argument("--pc-token-file", type=Path)
    parser.add_argument("--mobile-token-file", type=Path)
    parser.add_argument("--pc-audio", type=Path, required=True)
    parser.add_argument("--mobile-audio", type=Path, required=True, help="16kHz mono PCM16 WAV or raw .pcm")
    parser.add_argument("--pc-keyword", action="append", default=[])
    parser.add_argument("--mobile-keyword", action="append", default=[])
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--isolation-seconds", type=float, default=60)
    parser.add_argument("--interrupt-after", type=float, default=12)
    parser.add_argument("--legacy-seconds", type=float, default=60)
    parser.add_argument("--flush-seconds", type=float, default=20)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--load-timeout", type=float, default=300)
    parser.add_argument("--management-seconds", type=float, default=24)
    parser.add_argument("--allow-model-management", action="store_true",
                        help="Also switch/unload/reload the test model on a separate loopback port and restore its original mode")
    parser.add_argument("--mobile-receive-delay", type=float, default=0,
                        help="Delay reading each mobile event to exercise real socket backpressure")
    parser.add_argument("--max-processed-lag-seconds", type=float, default=2)
    parser.add_argument("--max-lag-growth-seconds", type=float, default=.5)
    parser.add_argument("--max-fixed-gap-seconds", type=float, default=30)
    parser.add_argument("--scenario", choices=("all", "dual", "isolation", "guards", "legacy", "management"), default="all")
    parser.add_argument("--sample-gpu", action="store_true", help="Read-only nvidia-smi sampling during the selected cases")
    parser.add_argument("--inspect-inputs", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.inspect_inputs:
        values = [load_audio(path) for path in (args.pc_audio, args.mobile_audio)]
        require(values[0] != values[1], "AUDIO_FIXTURES_MUST_BE_DISTINCT")
        print(json.dumps({"distinct": True, "source_audio": {role: audio_info(value)
                              for role, value in zip(("pc", "mobile"), values)}}, indent=2))
        return 0
    for name in ("url", "pc_token_file", "mobile_token_file", "output"):
        if getattr(args, name) is None:
            parser.error("--" + name.replace("_", "-") + " is required unless --inspect-inputs")
    if not args.pc_keyword or not args.mobile_keyword or any(not value.strip() for value in args.pc_keyword + args.mobile_keyword):
        parser.error("at least one nonempty distinctive keyword is required for each audio fixture")
    for name in ("seconds", "isolation_seconds", "interrupt_after", "legacy_seconds", "flush_seconds", "timeout",
                 "load_timeout", "management_seconds", "max_processed_lag_seconds", "max_lag_growth_seconds", "max_fixed_gap_seconds"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(name + " must be finite and positive")
    if not args.interrupt_after < args.isolation_seconds:
        parser.error("interrupt-after must occur before the PC isolation session ends")
    if not math.isfinite(args.mobile_receive_delay) or args.mobile_receive_delay < 0:
        parser.error("mobile-receive-delay must be finite and nonnegative")
    if args.scenario == "management" and not args.allow_model_management:
        parser.error("management requires --allow-model-management")
    return 0 if asyncio.run(main(args)) else 1


if __name__ == "__main__":
    raise SystemExit(cli())
