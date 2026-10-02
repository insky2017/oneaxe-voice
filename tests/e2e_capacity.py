"""Real-clock capacity measurements against an already-ready isolated API.

Run: python tests/e2e_capacity.py --manifest manifest.json --url
http://127.0.0.1:18098 --seconds 120 --output result.json
Compare: python tests/e2e_capacity.py --compare-baseline dual.json
--compare-candidate four.json --output comparison.json

Manifest: {"streams": [{"name": "pc", "role": "pc", "token_file":
"pc.token", "audio_file": "public-test.wav", "offset": 0,
"source_key": "zh", "baseline_key": "zh0", "keywords": ["phrase"]}]}
An optional admin_token_file is needed when the manifest has no PC stream.
Paths are relative to the manifest. Offsets are seconds into looping test WAVs.
Reports never contain tokens, audio paths, keywords, or transcript text.
No service/model lifecycle operation is performed.
"""

import argparse
import asyncio
from collections import deque
from contextlib import suppress
import hashlib
import json
import math
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect

try:
    from .e2e_concurrent import audio_frame, gpu_summary, pcm, percentiles, sample_gpu, supervised
except ImportError:
    from e2e_concurrent import audio_frame, gpu_summary, pcm, percentiles, sample_gpu, supervised


SAMPLE_RATE = 16000
FRAME_SAMPLES = 2560
TIMINGS = ("inference_ms", "queue_ms", "prepare_ms", "generate_ms", "apply_ms")
OPTIONAL_TIMINGS = ("api_queue_ms", "rpc_ms")
STEP_KINDS = ("audio", "flush", "finish")


class EvidenceError(Exception):
    """Only fixed codes are serialized; arbitrary exception messages stay private."""


def require(condition, code):
    if not condition:
        raise EvidenceError(code)


def failure(exc):
    return {"kind": type(exc).__name__,
            "code": str(exc) if isinstance(exc, EvidenceError) else "CLIENT_OPERATION_FAILED"}


def validate_url(value):
    parsed = urlsplit(value)
    require(parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.port in {18096, 18098, 18099}
            and not parsed.username and not parsed.password and not parsed.query
            and not parsed.fragment and parsed.path in {"", "/"}, "ISOLATED_LOOPBACK_URL_REQUIRED")
    return value.rstrip("/")


def load_manifest(path):
    raw = json.loads(path.read_text())
    require(isinstance(raw, dict) and isinstance(raw.get("streams"), list), "INVALID_MANIFEST")
    require(len(raw["streams"]) in {1, 2, 3, 4, 6, 8}, "UNSUPPORTED_STREAM_COUNT")
    streams, names, token_paths, tokens = [], set(), set(), set()
    for index, row in enumerate(raw["streams"]):
        require(isinstance(row, dict), "INVALID_STREAM")
        role, name = row.get("role"), row.get("name", f"stream-{index + 1}")
        require(role in {"pc", "mobile"} and isinstance(name, str) and name and name not in names,
                "INVALID_STREAM_ROLE_OR_NAME")
        require(isinstance(row.get("token_file"), str) and isinstance(row.get("audio_file"), str),
                "STREAM_FILES_REQUIRED")
        token_path = (path.parent / row["token_file"]).resolve()
        require(token_path not in token_paths, "INDEPENDENT_TOKEN_FILES_REQUIRED")
        token = token_path.read_text().strip()
        require(bool(token) and token not in tokens, "INDEPENDENT_TOKENS_REQUIRED")
        audio = pcm((path.parent / row["audio_file"]).resolve())
        offset = row.get("offset", 0)
        require(isinstance(offset, (int, float)) and not isinstance(offset, bool)
                and math.isfinite(offset) and offset >= 0, "INVALID_AUDIO_OFFSET")
        offset_samples = round(offset * SAMPLE_RATE)
        require(offset_samples < len(audio) // 2, "AUDIO_OFFSET_OUT_OF_RANGE")
        keywords = row.get("keywords", [])
        require(isinstance(keywords, list) and all(isinstance(v, str) and v.strip() for v in keywords),
                "INVALID_KEYWORDS")
        audio_hash = hashlib.sha256(audio).hexdigest()
        source_key = row.get("source_key", audio_hash)
        baseline_key = row.get("baseline_key")
        require(isinstance(source_key, str) and source_key
                and (baseline_key is None or isinstance(baseline_key, str) and baseline_key),
                "INVALID_SOURCE_KEY")
        streams.append({"name": name, "role": role, "token": token, "audio": audio,
                        "audio_sha256": audio_hash, "offset_samples": offset_samples,
                        "source_key": source_key, "baseline_key": baseline_key, "keywords": keywords})
        names.add(name)
        token_paths.add(token_path)
        tokens.add(token)
    pc = [row for row in streams if row["role"] == "pc"]
    require(len(pc) <= 1, "ONE_PC_STREAM_ALLOWED")
    if raw.get("admin_token_file"):
        require(isinstance(raw["admin_token_file"], str), "INVALID_ADMIN_TOKEN_FILE")
        admin = (path.parent / raw["admin_token_file"]).read_text().strip()
    else:
        require(bool(pc), "ADMIN_TOKEN_FILE_REQUIRED")
        admin = pc[0]["token"]
    require(bool(admin), "EMPTY_ADMIN_TOKEN")
    return streams, admin


def lag_summary(samples, seconds):
    first = percentiles([v for t, v in samples if t < seconds / 3])
    last = percentiles([v for t, v in samples if t >= seconds * 2 / 3])
    growth = last.get("p95", 0) - first.get("p95", 0) if first and last else None
    return {"processed_lag_seconds": percentiles([v for _, v in samples]),
            "lag_early": first, "lag_late": last,
            "late_minus_early_p95_seconds": round(growth, 4) if growth is not None else None,
            "lag_by_minute": [{"minute": minute + 1, "sample_count": len(values),
                               **percentiles(values)}
                              for minute in range(math.ceil(seconds / 60))
                              if (values := [v for t, v in samples if minute * 60 <= t < (minute + 1) * 60])],
            "lag_samples": [{"at_seconds": round(t, 4), "lag_seconds": round(v, 4)} for t, v in samples]}


def timing_summary(events):
    return {kind: {"event_count": len(rows),
                   **{key: percentiles([row[key] for row in rows if key in row])
                      for key in TIMINGS + OPTIONAL_TIMINGS}}
            for kind in STEP_KINDS if (rows := [row for row in events if row.get("step_kind") == kind])}


def keyword_matches(text, words):
    normalized = " ".join(text.casefold().split())
    return [" ".join(word.casefold().split()) in normalized for word in words]


class Stream:
    def __init__(self, spec, url, generation, args):
        self.spec, self.url, self.generation, self.args = spec, url, generation, args
        self.ws, self.identity, self.started = None, None, None
        self.tasks, self.events, self.lag, self.fixed_times = [], [], [], []
        self.fixed, self.seq, self.limit = "", 0, 0
        self.produced = self.sent = self.processed = self.received = 0
        self.credit, self.completed, self.send_lock = asyncio.Event(), asyncio.Event(), asyncio.Lock()
        self.flush_pending, self.flush_latencies = deque(), []
        self.queue = asyncio.Queue(maxsize=32)
        self.metrics = {key: spec[key] for key in
                        ("name", "role", "source_key", "baseline_key", "audio_sha256", "offset_samples")}
        self.metrics.update(frames=0, updates=0, credit_waits=0, credit_wait_seconds=0,
                            max_queue=0, max_buffered_samples=0, flow_control_errors=0,
                            fixed_prefix_errors=0, telemetry_missing_events=0, flush_sent=0,
                            capture_seconds=args.seconds, expected_samples=round(args.seconds * SAMPLE_RATE),
                            complete=False, cleanup={"cancel_attempted": False, "socket_closed": False})

    async def bind(self):
        path = "/api/dictation/v1/stream" if self.spec["role"] == "pc" else "/api/mobile/v1/dictation/stream"
        self.ws = await connect(self.url.replace("http://", "ws://", 1) + path,
                                additional_headers={"Authorization": "Bearer " + self.spec["token"]},
                                proxy=None, open_timeout=10, max_size=2 * 1024 * 1024)
        hello = {"type": "start", "protocol_version": 1,
                 "audio": {"encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE, "channels": 1}}
        if self.spec["role"] == "pc":
            hello["mode"] = "r2t2"
        else:
            hello.update(expected_server_instance_id=self.generation[0], expected_model_generation=self.generation[1])
        await self.ws.send(json.dumps(hello))
        ready = json.loads(await asyncio.wait_for(self.ws.recv(), 240))
        if ready.get("type") == "error":
            raise EvidenceError("SERVER_" + safe_code(ready.get("code")))
        require(ready.get("type") == "ready", "READY_REQUIRED")
        self.identity = tuple(ready[key] for key in ("server_instance_id", "model_generation", "session_id"))
        require(self.identity[:2] == self.generation, "MODEL_CHANGED_WHILE_BINDING")
        self.seq, self.limit = ready["seq"], ready["audio_send_limit"]
        self.metrics.update(session_id=self.identity[2], model_generation=self.identity[1])

    async def control(self, value):
        async with self.send_lock:
            await self.ws.send(json.dumps(value))

    async def produce(self):
        total = self.metrics["expected_samples"]
        next_flush = self.args.flush_seconds * SAMPLE_RATE if self.args.flush_seconds else math.inf
        for sample in range(0, total, FRAME_SAMPLES):
            count = min(FRAME_SAMPLES, total - sample)
            await asyncio.sleep(max(0, self.started + (sample + count) / SAMPLE_RATE - time.monotonic()))
            require(self.produced - self.sent + count <= 32000, "CAPTURE_BUFFER_EXCEEDED")
            self.queue.put_nowait(("audio", audio_frame(self.spec["audio"],
                                                       self.spec["offset_samples"] + sample, count)))
            self.produced += count
            if self.produced >= next_flush and self.produced < total:
                self.queue.put_nowait(("flush", self.produced))
                next_flush += self.args.flush_seconds * SAMPLE_RATE
            self.metrics["max_queue"] = max(self.metrics["max_queue"], self.queue.qsize())
            self.metrics["max_buffered_samples"] = max(self.metrics["max_buffered_samples"],
                                                       self.produced - self.sent)
        self.metrics["capture_stop_lag_seconds"] = round(max(0, self.args.seconds - self.processed / SAMPLE_RATE), 4)
        self.metrics["processed_at_capture_stop_samples"] = self.processed
        self.queue.put_nowait(("finish", total))

    async def send(self):
        while True:
            kind, value = await self.queue.get()
            if kind != "audio":
                require(value == self.sent, "CLIENT_AUDIO_BARRIER_MISMATCH")
                now = time.monotonic() - self.started
                if kind == "flush":
                    self.flush_pending.append(now)
                    self.metrics["flush_sent"] += 1
                else:
                    self.metrics["finish_sent_at_seconds"] = round(now, 4)
                await self.control({"type": kind, "after_audio_samples": self.sent})
                if kind == "finish":
                    return
                continue
            count = len(value) // 2
            if self.sent + count > self.limit:
                self.metrics["credit_waits"] += 1
                waiting = time.monotonic()
                while self.sent + count > self.limit:
                    self.credit.clear()
                    if self.sent + count <= self.limit:
                        break
                    await asyncio.wait_for(self.credit.wait(), 10)
                self.metrics["credit_wait_seconds"] += time.monotonic() - waiting
            async with self.send_lock:
                self.sent += count
                await self.ws.send(value)
            self.metrics["frames"] += 1

    def absorb(self, item, now):
        require(tuple(item.get(key) for key in ("server_instance_id", "model_generation", "session_id"))
                == self.identity, "EVENT_IDENTITY_MISMATCH")
        require(type(item.get("seq")) is int and item["seq"] > self.seq, "EVENT_SEQUENCE_REGRESSED")
        self.seq = item["seq"]
        if "audio_send_limit" in item:
            require(type(item["audio_send_limit"]) is int and item["audio_send_limit"] >= self.limit,
                    "SEND_CREDIT_REGRESSED")
            self.limit = item["audio_send_limit"]
            self.credit.set()
        for key, attr in (("audio_received_samples", "received"), ("audio_processed_samples", "processed")):
            if key in item:
                require(type(item[key]) is int and getattr(self, attr) <= item[key] <= self.sent,
                        "AUDIO_PROGRESS_INVALID")
                setattr(self, attr, item[key])
        if "text" in item:
            require(isinstance(item["text"], str), "INVALID_FIXED_TEXT")
            if not item["text"].startswith(self.fixed):
                self.metrics["fixed_prefix_errors"] += 1
                raise EvidenceError("FIXED_PREFIX_REGRESSED")
            if item["text"] != self.fixed:
                self.fixed_times.append(now)
                self.metrics["updates"] += 1
            self.fixed = item["text"]
        if item.get("type") == "error":
            if item.get("code") == "FLOW_CONTROL_EXCEEDED":
                self.metrics["flow_control_errors"] += 1
            raise EvidenceError("SERVER_" + safe_code(item.get("code")))
        if item.get("type") in {"partial", "final"}:
            kind = item.get("step_kind")
            row = {"at_seconds": round(now, 4), "step_kind": kind,
                   "processed_samples": self.processed, "received_samples": self.received}
            missing = kind not in STEP_KINDS
            for key in TIMINGS:
                value = item.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                    row[key] = value
                else:
                    missing = True
            for key in OPTIONAL_TIMINGS:
                value = item.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                    row[key] = value
            self.metrics["telemetry_missing_events"] += int(missing)
            if kind in STEP_KINDS:
                self.events.append(row)
            if kind == "flush":
                require(bool(self.flush_pending), "UNEXPECTED_FLUSH_EVENT")
                self.flush_latencies.append(now - self.flush_pending.popleft())
        if item.get("type") == "final":
            self.metrics.update(final_at_seconds=round(now, 4), complete=item.get("complete") is True,
                                final_reason=safe_code(item.get("reason")))
            self.completed.set()

    async def receive(self):
        async for raw in self.ws:
            self.absorb(json.loads(raw), time.monotonic() - self.started)
            if self.completed.is_set():
                return
        raise EvidenceError("CONNECTION_ENDED_BEFORE_FINAL")

    async def measure_lag(self):
        while True:
            now = time.monotonic() - self.started
            if 0 <= now < self.args.seconds:
                self.lag.append((now, max(0, now - self.processed / SAMPLE_RATE)))
            await asyncio.sleep(.16)

    async def keepalive(self):
        while True:
            await asyncio.sleep(10)
            if not self.completed.is_set():
                await self.control({"type": "keepalive"})

    async def run(self, started):
        self.started = started
        await asyncio.sleep(max(0, started - time.monotonic()))
        self.tasks = [asyncio.create_task(fn()) for fn in (self.produce, self.send, self.receive,
                                                          self.measure_lag, self.keepalive)]
        pipeline = asyncio.gather(*self.tasks[:3])
        try:
            await supervised(pipeline, self.tasks[3:], self.args.seconds + 90)
        except BaseException as exc:
            self.metrics["failure"] = failure(exc)
            raise
        finally:
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(pipeline, *self.tasks, return_exceptions=True)

    async def cleanup(self):
        if self.ws is None:
            return
        if not self.completed.is_set():
            self.metrics["cleanup"]["cancel_attempted"] = True
            try:
                await asyncio.wait_for(self.control({"type": "cancel"}), 3)
            except Exception as exc:
                self.metrics["cleanup"]["cancel_failure"] = failure(exc)
        try:
            await asyncio.wait_for(self.ws.close(), 5)
            self.metrics["cleanup"]["socket_closed"] = True
        except Exception as exc:
            self.metrics["cleanup"]["close_failure"] = failure(exc)

    def report(self, specs):
        result = dict(self.metrics)
        result.update(produced_samples=self.produced, sent_samples=self.sent,
                      received_samples=self.received, processed_samples=self.processed,
                      characters=len(self.fixed), transcript_sha256=hashlib.sha256(self.fixed.encode()).hexdigest(),
                      first_fixed_seconds=round(self.fixed_times[0], 4) if self.fixed_times else None,
                      fixed_updates_seconds=[round(v, 4) for v in self.fixed_times],
                      steps=timing_summary(self.events), step_events=self.events,
                      flush_completed=len(self.flush_latencies),
                      flush_latency_seconds=percentiles(self.flush_latencies),
                      flush_latencies_seconds=[round(v, 4) for v in self.flush_latencies],
                      finish_latency_seconds=round(result["final_at_seconds"] - result["finish_sent_at_seconds"], 4)
                      if "final_at_seconds" in result and "finish_sent_at_seconds" in result else None,
                      **lag_summary(self.lag, self.args.seconds))
        live = [v for v in self.fixed_times if v < self.args.seconds]
        result["max_fixed_interval_seconds"] = round(max(b - a for a, b in zip(
            [0] + live, live + [self.args.seconds])), 4)
        expected = {"audio": result["frames"], "flush": result["flush_sent"], "finish": 1}
        observed = {kind: result["steps"].get(kind, {}).get("event_count", 0) for kind in STEP_KINDS}
        count_checks = {kind: observed[kind] == expected[kind] for kind in STEP_KINDS}
        result["rpc_event_counts"] = {"expected": expected, "observed": observed, "checks": count_checks,
                                      "passed": all(count_checks.values())}
        result["telemetry_complete"] = (result["telemetry_missing_events"] == 0
                                        and observed["audio"] > 0 and result["rpc_event_counts"]["passed"])
        foreign = [word for spec in specs if spec["audio_sha256"] != self.spec["audio_sha256"]
                   for word in spec["keywords"]]
        own_matches, foreign_matches = keyword_matches(self.fixed, self.spec["keywords"]), keyword_matches(self.fixed, foreign)
        result["keyword_isolation"] = {"own_checked": bool(own_matches), "own_matches": own_matches,
                                       "foreign_checked": bool(foreign_matches), "foreign_matches": foreign_matches,
                                       "passed": all(own_matches) and not any(foreign_matches)}
        correctness = {"all_audio_sent": self.sent == result["expected_samples"],
                       "all_audio_received": self.received == self.sent,
                       "all_audio_processed": self.processed == self.sent,
                       "complete_final": result["complete"], "no_client_or_server_error": "failure" not in result,
                       "fixed_prefix_preserved": result["fixed_prefix_errors"] == 0,
                       "no_flow_control_error": result["flow_control_errors"] == 0,
                       "flush_complete": result["flush_completed"] == result["flush_sent"],
                       "keyword_checks": result["keyword_isolation"]["passed"]}
        result["correctness"] = {"passed": all(correctness.values()), "checks": correctness}
        realtime = {"lag_p95": result["processed_lag_seconds"].get("p95", math.inf) <= self.args.max_lag_seconds,
                    "capture_stop_lag": result.get("capture_stop_lag_seconds", math.inf) <= self.args.max_lag_seconds,
                    "live_text_early": any(v < self.args.seconds / 3 for v in live),
                    "live_text_late": any(v >= self.args.seconds * 2 / 3 for v in live),
                    "fixed_text_gap": result["max_fixed_interval_seconds"] <= self.args.max_fixed_gap_seconds}
        result["absolute_realtime"] = {"passed": all(realtime.values()), "checks": realtime}
        return result


def safe_code(value):
    if isinstance(value, str) and value and len(value) <= 80 and all(c.isascii() and (c.isalnum() or c == "_") for c in value):
        return value
    return "UNKNOWN"


async def get_status(client):
    response = await client.get("/api/dictation/status")
    response.raise_for_status()
    return response.json()


async def run_experiment(args):
    payload = {"schema_version": 1, "passed": False, "capture_seconds": args.seconds,
               "flush_seconds": args.flush_seconds, "frame_samples": FRAME_SAMPLES,
               "sessions": [], "gpu": {"interval_seconds": args.gpu_sample_seconds, "samples": []}}
    streams, specs, runners, bind_tasks, sampler, client, pipeline = [], [], [], [], None, None, None
    stop_gpu, worker_pid = asyncio.Event(), None
    try:
        url = validate_url(args.url)
        specs, admin = load_manifest(args.manifest)
        payload["stream_count"] = len(specs)
        client = httpx.AsyncClient(base_url=url, trust_env=False, timeout=15,
                                  headers={"Authorization": "Bearer " + admin})
        before = await get_status(client)
        require(before.get("model_loaded") and before.get("mode") == "r2t2" and not before.get("busy")
                and not before.get("active_sessions"), "READY_IDLE_R2T2_REQUIRED")
        generation = (before["server_instance_id"], before["model_generation"])
        worker_pid = before.get("worker_pid")
        payload["generation"] = list(generation)
        payload["model_identity"] = {"model": before.get("model"), "mode": before.get("mode"),
                                     "device": before.get("device")}
        payload["service_capacity"] = before.get("max_sessions")
        streams = [Stream(spec, url, generation, args) for spec in specs]
        bind_tasks = [asyncio.create_task(stream.bind()) for stream in streams]
        await asyncio.wait_for(asyncio.gather(*bind_tasks), 250)
        require(len({stream.identity[2] for stream in streams}) == len(streams), "SESSION_IDS_NOT_DISTINCT")
        started = time.monotonic() + .25
        payload["all_ready_before_capture"] = True
        sampler = asyncio.create_task(sample_gpu(payload["gpu"]["samples"], stop_gpu, started, args.gpu_sample_seconds))
        runners = [asyncio.create_task(stream.run(started)) for stream in streams]
        pipeline = asyncio.gather(*runners)
        await supervised(pipeline, [sampler], args.seconds + 95)
    except BaseException as exc:
        payload["failure"] = failure(exc)
    finally:
        for task in bind_tasks + runners:
            if not task.done():
                task.cancel()
        task_results = await asyncio.gather(*bind_tasks, *runners, return_exceptions=True)
        if pipeline:
            await asyncio.gather(pipeline, return_exceptions=True)
        for stream, bind_result in zip(streams, task_results[:len(bind_tasks)]):
            if isinstance(bind_result, BaseException):
                stream.metrics.setdefault("failure", failure(bind_result))
        await asyncio.gather(*(stream.cleanup() for stream in streams), return_exceptions=True)
        stop_gpu.set()
        if sampler:
            result = (await asyncio.gather(sampler, return_exceptions=True))[0]
            if isinstance(result, BaseException):
                payload["gpu"]["failure"] = failure(result)
        payload["gpu"]["summary"] = gpu_summary(payload["gpu"]["samples"], worker_pid)
        if client:
            try:
                owned = {stream.identity[2] for stream in streams if stream.identity}
                deadline = time.monotonic() + 15
                while True:
                    after = await get_status(client)
                    active = {row["session_id"] for row in after.get("active_sessions", [])}
                    if not active.intersection(owned) or time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(.2)
                payload["cleanup"] = {"owned_sessions_remaining": len(active.intersection(owned)),
                                      "active_sessions_remaining": len(active), "passed": not active}
                payload["model_preserved"] = (after.get("model_loaded") is True and
                    (after.get("server_instance_id"), after.get("model_generation")) == tuple(payload.get("generation", ())))
                payload["worker_preserved"] = after.get("worker_pid") == worker_pid
            except Exception as exc:
                payload["cleanup"] = {"passed": False, "failure": failure(exc)}
            await client.aclose()
        payload["sessions"] = [stream.report(specs) for stream in streams]
        payload["same_audio_pairs_not_disambiguated_by_keywords"] = sum(
            first["audio_sha256"] == second["audio_sha256"]
            for index, first in enumerate(specs) for second in specs[index + 1:])
        payload["correctness"] = {"passed": "failure" not in payload and bool(streams)
                                  and all(row["correctness"]["passed"] for row in payload["sessions"])
                                  and payload.get("cleanup", {}).get("passed", False)
                                  and payload.get("model_preserved", False) and payload.get("worker_preserved", False)}
        payload["absolute_realtime"] = {"passed": bool(streams) and all(row["absolute_realtime"]["passed"] for row in payload["sessions"])}
        payload["telemetry_complete"] = bool(streams) and all(row["telemetry_complete"] for row in payload["sessions"])
        payload["measurement_valid"] = ("failure" not in payload["gpu"]
                                        and bool(payload["gpu"]["samples"]) and payload["telemetry_complete"])
        payload["passed"] = payload["correctness"]["passed"] and payload["absolute_realtime"]["passed"]
    return payload


def compare_results(baseline, candidate):
    baselines = baseline if isinstance(baseline, list) else [baseline]
    result = {"schema_version": 1, "passed": False, "sessions": [],
              "thresholds": {"audio_p95_ratio": 1.1, "audio_p99_ratio": 1.2,
                             "lag_p95_increase_seconds": .1, "late_minus_early_p95_seconds": .1,
                             "first_fixed": {"minimum_allowance_seconds": .3, "relative_allowance": .2},
                             "flush_and_finish": {"minimum_allowance_seconds": .1, "relative_allowance": .2}}}
    requirements = {"dual_baselines": bool(baselines) and all(base.get("stream_count") == 2 for base in baselines),
                    "baselines_passed": all(base.get("passed") is True for base in baselines),
                    "same_model": bool(candidate.get("model_identity", {}).get("model"))
                                  and all(base.get("model_identity") == candidate.get("model_identity") for base in baselines),
                    "same_duration": all(base.get("capture_seconds") == candidate.get("capture_seconds") for base in baselines),
                    "same_flush_schedule": all(base.get("flush_seconds") == candidate.get("flush_seconds") for base in baselines),
                    "baseline_telemetry_complete": all(base.get("telemetry_complete") is True for base in baselines),
                    "candidate_telemetry_complete": candidate.get("telemetry_complete") is True}
    result["generation_observation"] = {"baseline_generations": [base.get("generation") for base in baselines],
                                        "candidate_generation": candidate.get("generation"),
                                        "all_equal": all(base.get("generation") == candidate.get("generation") for base in baselines)}
    baseline_sessions = [{**row, "baseline_index": index}
                         for index, base in enumerate(baselines) for row in base.get("sessions", [])]
    for row in candidate.get("sessions", []):
        matching = [base for base in baseline_sessions
                    if isinstance(row.get("audio_sha256"), str) and len(row["audio_sha256"]) == 64
                    and type(row.get("offset_samples")) is int
                    and base.get("audio_sha256") == row.get("audio_sha256")
                    and base.get("offset_samples") == row.get("offset_samples")
                    and base.get("source_key") == row.get("source_key")
                    and (row.get("baseline_key") is None or base.get("baseline_key") == row["baseline_key"])]
        matching.sort(key=lambda base: (base.get("name") != row.get("name"), base.get("role") != row.get("role")))
        report = {"name": row.get("name"), "source_key": row.get("source_key"),
                  "matched_baseline": bool(matching), "passed": False}
        if matching:
            base = matching[0]
            report["baseline_name"] = base.get("name")
            report["baseline_index"] = base["baseline_index"]
            audio = row.get("steps", {}).get("audio", {}).get("inference_ms", {})
            base_audio = base.get("steps", {}).get("audio", {}).get("inference_ms", {})
            measures = {"audio_p95_ratio": ratio(audio.get("p95"), base_audio.get("p95")),
                        "audio_p99_ratio": ratio(audio.get("p99"), base_audio.get("p99")),
                        "lag_p95_increase_seconds": difference(row.get("processed_lag_seconds", {}).get("p95"),
                                                                base.get("processed_lag_seconds", {}).get("p95")),
                        "late_minus_early_p95_seconds": row.get("late_minus_early_p95_seconds")}
            report["measures"] = measures
            report["checks"] = {key: value is not None and value <= result["thresholds"][key] + 1e-9
                                for key, value in measures.items()}
            report["paired_tail_observations"] = {"first_fixed_delta_seconds": difference(row.get("first_fixed_seconds"), base.get("first_fixed_seconds")),
                "finish_latency_delta_seconds": difference(row.get("finish_latency_seconds"), base.get("finish_latency_seconds")),
                "flush_p95_delta_seconds": difference(row.get("flush_latency_seconds", {}).get("p95"), base.get("flush_latency_seconds", {}).get("p95"))}
            no_flush = candidate.get("flush_seconds") == 0 or (
                isinstance(candidate.get("flush_seconds"), (int, float))
                and candidate.get("capture_seconds", 0) <= candidate["flush_seconds"])
            report["tail_latency_checks"] = {
                "first_fixed": latency_check(row.get("first_fixed_seconds"), base.get("first_fixed_seconds"), .3),
                "finish": latency_check(row.get("finish_latency_seconds"), base.get("finish_latency_seconds"), .1),
                "flush_p95": latency_check(row.get("flush_latency_seconds", {}).get("p95"),
                                           base.get("flush_latency_seconds", {}).get("p95"), .1,
                                           allow_unmeasured=no_flush)}
            report["checks"].update({key: value["passed"] for key, value in report["tail_latency_checks"].items()})
            report["passed"] = all(report["checks"].values())
        result["sessions"].append(report)
    result["comparability"] = {"passed": all(requirements.values()), "checks": requirements}
    result["relative_speed"] = {"passed": result["comparability"]["passed"] and bool(result["sessions"])
                               and all(row["passed"] for row in result["sessions"]),
        "worst_stream_measures": {key: max(values) if values else None for key in result["thresholds"]
                                  if (values := [row.get("measures", {}).get(key) for row in result["sessions"]
                                                 if row.get("measures", {}).get(key) is not None])}}
    result["correctness"] = candidate.get("correctness", {"passed": False})
    result["absolute_realtime"] = candidate.get("absolute_realtime", {"passed": False})
    result["passed"] = all(result[key].get("passed") is True for key in ("relative_speed", "correctness", "absolute_realtime"))
    return result


def difference(value, baseline):
    return round(value - baseline, 6) if isinstance(value, (int, float)) and isinstance(baseline, (int, float)) else None


def ratio(value, baseline):
    return round(value / baseline, 6) if isinstance(value, (int, float)) and isinstance(baseline, (int, float)) and baseline > 0 else None


def latency_check(value, baseline, minimum_allowance, *, allow_unmeasured=False):
    measured = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
                   for v in (value, baseline))
    limit = baseline + max(minimum_allowance, baseline * .2) if measured else None
    return {"candidate_seconds": value, "baseline_seconds": baseline,
            "limit_seconds": round(limit, 6) if limit is not None else None, "measured": measured,
            "passed": value <= limit + 1e-9 if measured else allow_unmeasured and value is None and baseline is None}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--flush-seconds", type=float, default=24, help="0 disables periodic flush; default covers the 16s rolling window")
    parser.add_argument("--gpu-sample-seconds", type=float, choices=(.5, 1), default=1)
    parser.add_argument("--max-lag-seconds", type=float, default=2)
    parser.add_argument("--max-fixed-gap-seconds", type=float, default=30)
    parser.add_argument("--compare-baseline", type=Path, action="append",
                        help="Repeat for dual baselines covering other source audio")
    parser.add_argument("--compare-candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.compare_baseline or args.compare_candidate:
        if not args.compare_baseline or not args.compare_candidate or args.manifest or args.url:
            parser.error("comparison requires both result paths and cannot include manifest/url")
    else:
        if not args.manifest or not args.url:
            parser.error("experiment requires --manifest and --url")
        try:
            validate_url(args.url)
        except (EvidenceError, ValueError):
            parser.error("--url must be an HTTP loopback address on 18096, 18098, or 18099")
    if any(not math.isfinite(value) or value <= 0 for value in (args.seconds, args.max_lag_seconds, args.max_fixed_gap_seconds)):
        parser.error("duration and realtime thresholds must be finite and positive")
    if not math.isfinite(args.flush_seconds) or args.flush_seconds < 0 or 0 < args.flush_seconds < .16:
        parser.error("flush interval must be 0 or at least 0.16 seconds")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.compare_baseline:
        try:
            result = compare_results([json.loads(path.read_text()) for path in args.compare_baseline],
                                     json.loads(args.compare_candidate.read_text()))
        except Exception as exc:
            result = {"passed": False, "failure": failure(exc)}
    else:
        result = asyncio.run(run_experiment(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"passed": result["passed"], "output": str(args.output),
                      "failure": result.get("failure"), "correctness": result.get("correctness"),
                      "absolute_realtime": result.get("absolute_realtime"),
                      "relative_speed": result.get("relative_speed")}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
