"""Real-clock PC/mobile protocol and GPU validation against an isolated API.

Uses two explicitly supplied test WAVs. Logs timing/counts/hashes, never tokens
or transcript text. Does not start, stop, or reload any service or model.
"""

import argparse
import asyncio
from contextlib import suppress
import csv
import hashlib
import json
from pathlib import Path
import time
import wave

import httpx
from websockets.asyncio.client import connect


def pcm(path):
    with wave.open(str(path), "rb") as source:
        assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (16000, 1, 2)
        value = source.readframes(source.getnframes())
        assert value, "test audio is empty"
        return value


def percentiles(values):
    values = sorted(values)
    if not values:
        return {}
    return {f"p{p}": round(values[min(len(values) - 1, int((len(values) - 1) * p / 100))], 4)
            for p in (50, 95, 99)}


def audio_frame(audio, sample, count):
    pos, remaining, pieces = sample * 2 % len(audio), count * 2, []
    while remaining:
        piece = audio[pos:pos + remaining]
        pieces.append(piece)
        remaining -= len(piece)
        pos = 0
    return b"".join(pieces)


async def supervised(pipeline, watchers, timeout):
    async def wait():
        done, _ = await asyncio.wait([pipeline, *watchers], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        assert pipeline in done, "monitor task stopped unexpectedly"
        return await pipeline
    return await asyncio.wait_for(wait(), timeout)


async def gpu_query(fields, kind):
    process = await asyncio.create_subprocess_exec(
        "nvidia-smi", f"--query-{kind}=" + ",".join(fields), "--format=csv,noheader,nounits",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, f"nvidia-smi {kind} query failed: exit {process.returncode}"
        return [dict(zip(fields, (value.strip() for value in row)))
                for row in csv.reader(output.decode().splitlines()) if row]
    except BaseException:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        raise


def number(value, integer=False):
    try:
        return int(value) if integer else float(value)
    except (ValueError, TypeError):
        return None


async def sample_gpu(samples, stop, started, interval):
    deadline = time.monotonic()
    while not stop.is_set():
        gpus, processes = await asyncio.gather(
            gpu_query(["index", "uuid", "utilization.gpu", "memory.used", "memory.total", "power.draw"], "gpu"),
            gpu_query(["gpu_uuid", "pid", "used_gpu_memory"], "compute-apps"))
        assert gpus, "nvidia-smi returned no GPUs"
        samples.append({
            "at_seconds": round(time.monotonic() - started, 4),
            "gpus": [{"index": number(row["index"], True), "uuid": row["uuid"],
                      "utilization_percent": number(row["utilization.gpu"]),
                      "memory_used_mib": number(row["memory.used"]),
                      "memory_total_mib": number(row["memory.total"]),
                      "power_watts": number(row["power.draw"])} for row in gpus],
            "processes": [{"gpu_uuid": row["gpu_uuid"], "pid": number(row["pid"], True),
                           "memory_used_mib": number(row["used_gpu_memory"])} for row in processes],
        })
        deadline += interval
        try:
            await asyncio.wait_for(stop.wait(), max(0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            pass


def gpu_summary(samples, worker_pid):
    def peak(rows, key):
        values = [row[key] for row in rows if row.get(key) is not None]
        return max(values) if values else None
    gpus = [row for sample in samples for row in sample["gpus"]]
    processes = [row for sample in samples for row in sample["processes"]]
    return {"sample_count": len(samples), "worker_pid": worker_pid,
            "gpu_peaks": [{"uuid": uuid,
                           **{key: peak([row for row in gpus if row["uuid"] == uuid], key)
                              for key in ("utilization_percent", "memory_used_mib", "power_watts")}}
                          for uuid in sorted({row["uuid"] for row in gpus})],
            "process_memory_peaks": [{"gpu_uuid": uuid, "pid": pid,
                                      "memory_used_mib": peak([row for row in processes
                                          if row["gpu_uuid"] == uuid and row["pid"] == pid], "memory_used_mib")}
                                     for uuid, pid in sorted({(row["gpu_uuid"], row["pid"])
                                                             for row in processes if row["pid"] is not None})]}


def keyword_evidence(text, keywords):
    normalized = " ".join(text.casefold().split())
    return [{"sha256": hashlib.sha256(keyword.encode()).hexdigest(),
             "matched": " ".join(keyword.casefold().split()) in normalized} for keyword in keywords]


def isolation_evidence(results, pc_keywords, mobile_keywords):
    evidence = {}
    for (metrics, text), own, foreign in zip(results, (pc_keywords, mobile_keywords),
                                            (mobile_keywords, pc_keywords)):
        role = metrics["role"]
        own_matches, foreign_matches = keyword_evidence(text, own), keyword_evidence(text, foreign)
        language_ok = (any("\u4e00" <= char <= "\u9fff" for char in text) if role == "pc"
                       else any(char.isascii() and char.isalpha() for char in text))
        evidence[role] = {"required_keywords": own_matches, "other_audio_keywords": foreign_matches,
                          "expected_language": "zh" if role == "pc" else "en",
                          "language_present": language_ok,
                          "passed": language_ok and all(item["matched"] for item in own_matches)
                                    and not any(item["matched"] for item in foreign_matches)}
    evidence["distinct_transcripts"] = results[0][1] != results[1][1]
    evidence["distinct_sessions"] = results[0][0]["session_id"] != results[1][0]["session_id"]
    evidence["passed"] = (evidence["pc"]["passed"] and evidence["mobile"]["passed"] and
                          evidence["distinct_transcripts"] and evidence["distinct_sessions"])
    return evidence


def performance_evidence(metrics, args):
    early, late = metrics["lag_early"].get("p95"), metrics["lag_late"].get("p95")
    growth = late - early if early is not None and late is not None else None
    checks = {"processed_lag_p95": metrics["processed_lag_seconds"].get("p95", float("inf"))
                                      <= args.max_processed_lag_seconds,
              "late_lag_growth": growth is not None and growth <= args.max_lag_growth_seconds,
              "capture_stop_lag": metrics["capture_stop_lag_seconds"] <= args.max_processed_lag_seconds,
              "live_text_early": any(t < metrics["capture_seconds"] / 3 for t in metrics["fixed_updates_seconds"]),
              "live_text_late": any(metrics["capture_seconds"] * 2 / 3 <= t < metrics["capture_seconds"]
                                    for t in metrics["fixed_updates_seconds"]),
              "fixed_text_gap": metrics["max_fixed_interval_seconds"] <= args.max_fixed_gap_seconds,
              "all_audio_sent": metrics["sent_samples"] == metrics["expected_samples"]}
    if not metrics["cancel_requested"]:
        checks["final_processed_all_audio"] = metrics["processed_samples"] == metrics["sent_samples"]
        checks["complete_final"] = metrics.get("complete") is True
    else:
        checks["cancelled_without_success"] = (metrics.get("complete") is False and
                                                metrics.get("final_reason") == "cancelled" and
                                                isinstance(metrics.get("final_at_seconds"), (int, float)))
    return {"passed": all(checks.values()), "checks": checks,
            "late_minus_early_p95_seconds": round(growth, 4) if growth is not None else None}


async def session(url, token, audio, seconds, role, generation, *, cancel_after=None):
    ws_url = url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    path = "/api/dictation/v1/stream" if role == "pc" else "/api/mobile/v1/dictation/stream"
    headers = {"Authorization": "Bearer " + token}
    counters = dict(role=role, frames=0, max_queue=0, credit_waits=0, updates=0)
    fixed, fixed_times, lag, inference = "", [], [], []
    async with connect(ws_url + path, additional_headers=headers, proxy=None,
                       open_timeout=10, max_size=2 * 1024 * 1024) as ws:
        hello = dict(type="start", protocol_version=1,
                     audio=dict(encoding="pcm_s16le", sample_rate=16000, channels=1))
        if role == "pc":
            hello["mode"] = "r2t2"
        else:
            hello.update(expected_server_instance_id=generation[0], expected_model_generation=generation[1])
        await ws.send(json.dumps(hello))
        ready = json.loads(await asyncio.wait_for(ws.recv(), 240))
        assert ready["type"] == "ready", ready.get("code", ready.get("type"))
        identity = tuple(ready[k] for k in ("server_instance_id", "model_generation", "session_id"))
        assert identity[:2] == generation, "model changed while binding"
        limit, seq, sent, processed, received = ready["audio_send_limit"], ready["seq"], 0, 0, 0
        started = time.monotonic()
        queue = asyncio.Queue(maxsize=16)
        produced = 0
        credit = asyncio.Event()
        completed = asyncio.Event()
        duration = min(seconds, cancel_after) if cancel_after is not None else seconds
        send_lock = asyncio.Lock()

        async def control(value):
            async with send_lock:
                await ws.send(json.dumps(value))

        async def produce():
            nonlocal produced
            total = int(duration * 16000)
            for sample in range(0, total, 2560):
                count = min(2560, total - sample)
                await asyncio.sleep(max(0, started + (sample + count) / 16000 - time.monotonic()))
                assert produced - sent + count <= 32000, "unsent audio exceeded two-second buffer"
                queue.put_nowait(audio_frame(audio, sample, count))
                produced += count
                counters["max_queue"] = max(counters["max_queue"], queue.qsize())
                counters["max_buffered_samples"] = max(counters.get("max_buffered_samples", 0), produced - sent)
            await asyncio.sleep(max(0, started + duration - time.monotonic()))
            counters["capture_stop_lag_seconds"] = round(max(0, duration - processed / 16000), 4)
            counters["processed_at_capture_stop_samples"] = processed
            await queue.put(None)

        async def send():
            nonlocal sent
            while True:
                chunk = await queue.get()
                if chunk is None:
                    control = {"type": "cancel"} if cancel_after is not None else {
                        "type": "finish", "after_audio_samples": sent}
                    counters["stop_at_seconds"] = time.monotonic() - started
                    async with send_lock:
                        await ws.send(json.dumps(control))
                    return
                while sent + len(chunk) // 2 > limit:
                    counters["credit_waits"] += 1
                    credit.clear()
                    if sent + len(chunk) // 2 <= limit:
                        break
                    await asyncio.wait_for(credit.wait(), 10)
                async with send_lock:
                    sent += len(chunk) // 2
                    await ws.send(chunk)
                counters["frames"] += 1

        async def receive():
            nonlocal fixed, limit, seq, processed, received
            async for raw in ws:
                item = json.loads(raw)
                assert tuple(item[k] for k in ("server_instance_id", "model_generation", "session_id")) == identity
                if item["seq"] <= seq:
                    counters["old_events_ignored"] = counters.get("old_events_ignored", 0) + 1
                    continue
                seq = item["seq"]
                if "audio_send_limit" in item:
                    assert item["audio_send_limit"] >= limit
                    limit = item["audio_send_limit"]
                    credit.set()
                now = time.monotonic() - started
                if "audio_received_samples" in item:
                    assert received <= item["audio_received_samples"] <= sent, "invalid received progress"
                    received = item["audio_received_samples"]
                if "audio_processed_samples" in item:
                    assert processed <= item["audio_processed_samples"] <= sent, "invalid processed progress"
                    processed = item["audio_processed_samples"]
                if "inference_ms" in item:
                    inference.append(item["inference_ms"])
                if "text" in item:
                    assert item["text"].startswith(fixed), "fixed text regressed"
                    if item["text"] != fixed:
                        fixed_times.append(now)
                        counters["updates"] += 1
                    fixed = item["text"]
                if item["type"] == "error":
                    raise AssertionError("server error: " + item["code"])
                if item["type"] == "final":
                    counters["final_at_seconds"] = now
                    counters["complete"] = item.get("complete")
                    counters["final_reason"] = item.get("reason")
                    if cancel_after is None:
                        assert item.get("complete") is True
                    else:
                        assert item.get("complete") is False and item.get("reason") == "cancelled"
                    completed.set()
                    return
            raise AssertionError("connection ended before final")

        async def keepalive():
            while True:
                await asyncio.sleep(10)
                if not completed.is_set():
                    await control({"type": "keepalive"})

        async def measure_lag():
            while True:
                now = time.monotonic() - started
                if now <= duration:
                    lag.append((now, max(0, now - processed / 16000)))
                await asyncio.sleep(.16)

        tasks = [asyncio.create_task(fn()) for fn in (produce, send, receive)]
        ping = asyncio.create_task(keepalive())
        monitor = asyncio.create_task(measure_lag())
        pipeline = asyncio.gather(*tasks)
        try:
            await supervised(pipeline, [ping, monitor], duration + 90)
        finally:
            for task in tasks + [ping, monitor]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(pipeline, ping, monitor, return_exceptions=True)
        assert fixed and fixed_times[0] < duration, "no live text before capture stopped"
        counters.update(session_id=identity[2], model_generation=identity[1], sent_samples=sent,
                        expected_samples=int(duration * 16000), processed_samples=processed,
                        received_samples=received, capture_seconds=duration,
                        cancel_requested=cancel_after is not None,
                        processed_live_ratio=round(counters["processed_at_capture_stop_samples"] / (duration * 16000), 6),
                        first_fixed_seconds=round(fixed_times[0], 4), characters=len(fixed),
                        transcript_sha256=hashlib.sha256(fixed.encode()).hexdigest(),
                        fixed_updates_seconds=[round(t, 3) for t in fixed_times],
                        processed_lag_seconds=percentiles([v for _, v in lag]),
                        lag_early=percentiles([v for t, v in lag if t < duration / 3]),
                        lag_late=percentiles([v for t, v in lag if t > duration * 2 / 3]),
                        inference_ms=percentiles(inference),
                        finish_latency_seconds=round(counters.get("final_at_seconds", counters["stop_at_seconds"])
                                                     - counters["stop_at_seconds"], 4),
                        max_fixed_interval_seconds=round(max(b - a for a, b in zip(
                            [0] + [t for t in fixed_times if t <= duration],
                            [t for t in fixed_times if t <= duration] + [duration])), 4))
        return counters, fixed


async def main(args):
    gpu_samples, stop_gpu, sampler, pipeline = [], asyncio.Event(), None, None
    sessions = []
    payload = dict(passed=False, seconds=args.seconds, sessions=[],
                   start_order=args.start_order,
                   thresholds={"processed_lag_p95_seconds": args.max_processed_lag_seconds,
                               "late_lag_p95_growth_seconds": args.max_lag_growth_seconds,
                               "max_fixed_gap_seconds": args.max_fixed_gap_seconds},
                   gpu={"interval_seconds": args.gpu_sample_seconds, "samples": gpu_samples})
    worker_pid = None
    try:
        pc_audio, mobile_audio = pcm(args.pc_audio), pcm(args.mobile_audio)
        pc_hash, mobile_hash = (hashlib.sha256(value).hexdigest() for value in (pc_audio, mobile_audio))
        assert pc_hash != mobile_hash, "test audio must contain distinct Chinese and English speech"
        payload["source_audio_sha256"] = {"pc": pc_hash, "mobile": mobile_hash}
        pc_token = args.pc_token_file.read_text().strip()
        mobile_token = args.mobile_token_file.read_text().strip()
        async with httpx.AsyncClient(base_url=args.url, trust_env=False,
                                     headers={"Authorization": "Bearer " + pc_token}) as client:
            response = await client.get("/api/dictation/status")
            response.raise_for_status()
            before = response.json()
            assert before["model_loaded"] and before["mode"] == "r2t2" and not before["busy"]
            generation = (before["server_instance_id"], before["model_generation"])
            worker_pid = before["worker_pid"]
            payload["generation"] = generation
            sampler = asyncio.create_task(sample_gpu(gpu_samples, stop_gpu, time.monotonic(), args.gpu_sample_seconds))
            async def launch(role):
                if args.start_order != "simultaneous" and args.start_order != role + "-first":
                    await asyncio.sleep(.2)
                return await session(args.url if role == "pc" else args.mobile_url or args.url,
                                     pc_token if role == "pc" else mobile_token,
                                     pc_audio if role == "pc" else mobile_audio, args.seconds, role, generation,
                                     cancel_after=args.cancel_mobile_after if role == "mobile" else None)
            sessions = [asyncio.create_task(launch(role)) for role in ("pc", "mobile")]
            pipeline = asyncio.gather(*sessions)
            results = await supervised(pipeline, [sampler], args.seconds + 340)
            payload["sessions"] = [metrics for metrics, _ in results]
            response = await client.get("/api/dictation/status")
            response.raise_for_status()
            after = response.json()
            payload["model_preserved"] = after["model_generation"] == before["model_generation"] and after["model_loaded"]
            payload["worker_preserved"] = after["worker_pid"] == worker_pid
        payload["isolation"] = isolation_evidence(results, args.pc_keyword, args.mobile_keyword)
        payload["performance"] = {metrics["role"]: performance_evidence(metrics, args) for metrics, _ in results}
        payload["cancel_isolated"] = (args.cancel_mobile_after is None or
                                      results[0][0].get("complete") is True and
                                      results[1][0].get("complete") is not True)
        payload["passed"] = (payload["isolation"]["passed"] and payload["model_preserved"] and
                              payload["worker_preserved"] and payload["cancel_isolated"] and
                              all(value["passed"] for value in payload["performance"].values()))
    except Exception as exc:
        payload["failure"] = {"kind": type(exc).__name__, "reason": str(exc)}
        payload["passed"] = False
    finally:
        if pipeline and not pipeline.done():
            pipeline.cancel()
        for task in sessions:
            if not task.done():
                task.cancel()
        stop_gpu.set()
        if pipeline:
            await asyncio.gather(pipeline, return_exceptions=True)
        await asyncio.gather(*sessions, return_exceptions=True)
        if sampler:
            result = await asyncio.gather(sampler, return_exceptions=True)
            if isinstance(result[0], BaseException):
                payload["failure"] = {"kind": type(result[0]).__name__, "reason": str(result[0])}
                payload["passed"] = False
        payload["gpu"]["summary"] = gpu_summary(gpu_samples, worker_pid)
        if not gpu_samples:
            payload["passed"] = False
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"passed": payload["passed"], "output": str(args.output),
                      "failure": payload.get("failure"),
                      "performance": payload.get("performance"),
                      "isolation_passed": payload.get("isolation", {}).get("passed"),
                      "gpu": payload["gpu"]["summary"],
                      "sessions": [{k: row[k] for k in ("role", "updates", "first_fixed_seconds", "processed_lag_seconds")}
                                   for row in payload["sessions"]]}, indent=2))
    return payload["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--mobile-url")
    parser.add_argument("--pc-token-file", type=Path, required=True)
    parser.add_argument("--mobile-token-file", type=Path, required=True)
    parser.add_argument("--pc-audio", type=Path, required=True)
    parser.add_argument("--mobile-audio", type=Path, required=True)
    parser.add_argument("--pc-keyword", action="append", required=True,
                        help="Distinct phrase expected in the Chinese PC audio; repeat for multiple phrases")
    parser.add_argument("--mobile-keyword", action="append", required=True,
                        help="Distinct phrase expected in the English mobile audio; repeat for multiple phrases")
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--cancel-mobile-after", type=float)
    parser.add_argument("--start-order", choices=("simultaneous", "pc-first", "mobile-first"),
                        default="simultaneous", help="Delay the second client by 0.2 seconds")
    parser.add_argument("--gpu-sample-seconds", type=float, choices=(.5, 1), default=1)
    parser.add_argument("--max-processed-lag-seconds", type=float, default=2)
    parser.add_argument("--max-lag-growth-seconds", type=float, default=.5)
    parser.add_argument("--max-fixed-gap-seconds", type=float, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.seconds <= 0 or args.cancel_mobile_after is not None and not 0 < args.cancel_mobile_after < args.seconds:
        parser.error("seconds must be positive; cancellation must occur strictly within the PC session")
    if any(value <= 0 for value in (args.max_processed_lag_seconds, args.max_lag_growth_seconds,
                                    args.max_fixed_gap_seconds)):
        parser.error("performance thresholds must be positive")
    if any(not value.strip() for value in args.pc_keyword + args.mobile_keyword):
        parser.error("keywords must not be empty")
    raise SystemExit(0 if asyncio.run(main(args)) else 1)
