"""One PC V1 lane to accompany a real Android dictation acceptance run.

Requires httpx and websockets >= 13. Never prints credentials or transcript text.
It reads the local PC status and does not manage the model or service.
"""

import argparse
import asyncio
from contextlib import suppress
import hashlib
import json
from pathlib import Path
import time
import wave

import httpx
from websockets.asyncio.client import connect


RATE = 16000
FRAME_SAMPLES = 2560  # 160 ms; six or seven audio messages per second.
BUFFER_SAMPLES = RATE * 2


def load_pcm(path):
    with wave.open(str(path), "rb") as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (RATE, 1, 2):
            raise ValueError("WAV must be 16 kHz, mono, PCM16")
        audio = source.readframes(source.getnframes())
    if not audio:
        raise ValueError("WAV contains no audio")
    return audio


def frame_at(audio, first_sample, count):
    offset = first_sample * 2 % len(audio)
    needed = count * 2
    pieces = []
    while needed:
        piece = audio[offset:offset + needed]
        pieces.append(piece)
        needed -= len(piece)
        offset = 0
    return b"".join(pieces)


def percentile(values, percentage):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[int((len(ordered) - 1) * percentage / 100)], 3)


async def status(client):
    response = await client.get("/api/dictation/status")
    response.raise_for_status()
    return response.json()


async def run(args):
    audio = load_pcm(args.wav)
    token = args.pc_token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("PC token file is empty")
    base = args.url.rstrip("/")
    if base != "http://127.0.0.1:8097":
        raise ValueError("PC companion is restricted to the local PC endpoint")

    async with httpx.AsyncClient(base_url=base, trust_env=False,
                                 headers={"Authorization": "Bearer " + token},
                                 timeout=10) as client:
        before = await status(client)
        if not (before.get("model_loaded") and before.get("mode") == "r2t2"
                and before.get("model_generation") and before.get("server_instance_id")):
            raise RuntimeError("PC model is not ready in r2t2 mode")
        if before.get("pc_busy"):
            raise RuntimeError("PC dictation is already active")
        generation = (before["server_instance_id"], before["model_generation"])
        ws_url = "ws://127.0.0.1:8097/api/dictation/v1/stream"
        async with connect(ws_url, additional_headers={"Authorization": "Bearer " + token},
                           proxy=None, open_timeout=10, max_size=2 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "start", "protocol_version": 1, "mode": "r2t2",
                                      "audio": {"encoding": "pcm_s16le", "sample_rate": RATE,
                                                "channels": 1}}))
            ready = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if ready.get("type") != "ready":
                raise RuntimeError("PC stream rejected: " + ready.get("code", "UNKNOWN"))
            identity = tuple(ready[key] for key in (
                "server_instance_id", "model_generation", "session_id"))
            if identity[:2] != generation:
                raise RuntimeError("model generation changed before PC ready")

            deadline = time.monotonic()
            total = round(args.seconds * RATE)
            produced = sent = received = processed = 0
            limit = ready["audio_send_limit"]
            seq = ready["seq"]
            fixed = ""
            queue = asyncio.Queue(maxsize=13)
            credit = asyncio.Event()
            done = asyncio.Event()
            send_lock = asyncio.Lock()
            samples = []
            fixed_times = []
            metrics = {"frames": 0, "flow_waits": 0, "max_unsent_samples": 0,
                       "max_queue_frames": 0, "old_events": 0, "updates": 0}

            async def produce():
                nonlocal produced
                for first in range(0, total, FRAME_SAMPLES):
                    count = min(FRAME_SAMPLES, total - first)
                    await asyncio.sleep(max(0, deadline + (first + count) / RATE - time.monotonic()))
                    if produced - sent + count > BUFFER_SAMPLES or queue.full():
                        raise RuntimeError("PC unsent audio exceeded two seconds")
                    queue.put_nowait(frame_at(audio, first, count))
                    produced += count
                    metrics["max_unsent_samples"] = max(metrics["max_unsent_samples"], produced - sent)
                    metrics["max_queue_frames"] = max(metrics["max_queue_frames"], queue.qsize())
                await queue.put(None)

            async def send():
                nonlocal sent
                while True:
                    frame = await queue.get()
                    if frame is None:
                        async with send_lock:
                            await ws.send(json.dumps({"type": "finish", "after_audio_samples": sent}))
                        metrics["finish_sent_seconds"] = round(time.monotonic() - deadline, 3)
                        return
                    count = len(frame) // 2
                    while sent + count > limit:
                        metrics["flow_waits"] += 1
                        credit.clear()
                        if sent + count <= limit:
                            break
                        await asyncio.wait_for(credit.wait(), 10)
                    async with send_lock:
                        sent += count
                        await ws.send(frame)
                    metrics["frames"] += 1

            async def receive():
                nonlocal limit, seq, received, processed, fixed
                async for raw in ws:
                    event = json.loads(raw)
                    if tuple(event[key] for key in identity_keys) != identity:
                        raise RuntimeError("PC stream identity changed")
                    if event["seq"] <= seq:
                        metrics["old_events"] += 1
                        continue
                    seq = event["seq"]
                    if "audio_send_limit" in event:
                        if event["audio_send_limit"] < limit:
                            raise RuntimeError("PC audio credit regressed")
                        limit = event["audio_send_limit"]
                        credit.set()
                    if "audio_received_samples" in event:
                        value = event["audio_received_samples"]
                        if not received <= value <= sent:
                            raise RuntimeError("PC received progress invalid")
                        received = value
                    if "audio_processed_samples" in event:
                        value = event["audio_processed_samples"]
                        if not processed <= value <= sent:
                            raise RuntimeError("PC processed progress invalid")
                        processed = value
                    if "text" in event:
                        if not event["text"].startswith(fixed):
                            raise RuntimeError("PC fixed text regressed")
                        if event["text"] != fixed:
                            metrics["updates"] += 1
                            fixed_times.append(round(time.monotonic() - deadline, 3))
                        fixed = event["text"]
                    if event["type"] == "error":
                        raise RuntimeError("PC stream error: " + event.get("code", "UNKNOWN"))
                    if event["type"] == "final":
                        metrics["complete"] = event.get("complete")
                        metrics["final_reason"] = event.get("reason")
                        metrics["final_seconds"] = round(time.monotonic() - deadline, 3)
                        done.set()
                        return
                raise RuntimeError("PC stream closed without final")

            async def keepalive():
                while not done.is_set():
                    await asyncio.sleep(10)
                    if not done.is_set():
                        async with send_lock:
                            await ws.send(json.dumps({"type": "keepalive"}))

            async def measure():
                while not done.is_set():
                    elapsed = time.monotonic() - deadline
                    if elapsed <= args.seconds:
                        samples.append((elapsed, max(0, elapsed - processed / RATE)))
                    await asyncio.sleep(.16)

            identity_keys = ("server_instance_id", "model_generation", "session_id")
            work = [asyncio.create_task(coro()) for coro in (produce, send, receive)]
            watches = [asyncio.create_task(coro()) for coro in (keepalive, measure)]
            try:
                gathered = asyncio.gather(*work)
                await asyncio.wait_for(gathered, args.seconds + 120)
            finally:
                for task in work + watches:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*work, *watches, return_exceptions=True)
                if not done.is_set():
                    with suppress(Exception):
                        await ws.send(json.dumps({"type": "cancel"}))

        after = await status(client)
    early = [lag for at, lag in samples if at < args.seconds / 3]
    late = [lag for at, lag in samples if at > args.seconds * 2 / 3]
    keyword_checks = [{"sha256": hashlib.sha256(word.encode()).hexdigest(),
                       "matched": " ".join(word.casefold().split()) in " ".join(fixed.casefold().split())}
                      for word in args.keyword]
    metrics.update({
        "seconds": args.seconds, "ten_minute_run": args.seconds >= 600,
        "model_identity": {"server_instance_id": identity[0], "model_generation": identity[1]},
        "model_preserved": after.get("model_generation") == generation[1] and after.get("model_loaded"),
        "sent_samples": sent, "received_samples": received, "processed_samples": processed,
        "expected_samples": total, "max_unsent_seconds": round(metrics["max_unsent_samples"] / RATE, 3),
        "fixed_characters": len(fixed), "first_fixed_seconds": fixed_times[0] if fixed_times else None,
        "fixed_updates_seconds": fixed_times,
        "processed_lag_p50_seconds": percentile([lag for _, lag in samples], 50),
        "processed_lag_p95_seconds": percentile([lag for _, lag in samples], 95),
        "early_lag_p95_seconds": percentile(early, 95),
        "late_lag_p95_seconds": percentile(late, 95),
        "finish_latency_seconds": round(metrics["final_seconds"] - metrics["finish_sent_seconds"], 3),
        "keywords": keyword_checks,
    })
    metrics["passed"] = (metrics["complete"] is True and sent == total == processed
                         and metrics["model_preserved"] and bool(fixed_times)
                         and all(item["matched"] for item in keyword_checks))
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8097")
    parser.add_argument("--pc-token-file", type=Path, required=True)
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--keyword", action="append", default=[])
    args = parser.parse_args()
    if not 0 < args.seconds <= 3600 or not args.seconds * RATE == round(args.seconds * RATE):
        parser.error("seconds must be within (0, 3600] and resolve to whole PCM samples")
    if any(not word.strip() for word in args.keyword):
        parser.error("keywords must not be empty")
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        # Exception messages from HTTP/WS libraries may contain headers or URLs.
        print(json.dumps({"passed": False, "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
