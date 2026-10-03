"""Measure the legacy Qwen request/reply stream without retaining transcripts.

Capture runs independently of acknowledgements, as on the desktop. The two
second ACK target is an acceptance threshold; the legacy queue holds 400 items.
Only the first ten seconds of a consented PCM16 WAV are looped. This module does
not prepare models, launch workers, change services, or write evidence files.
"""

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import wave

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


SAMPLE_RATE = 16000
FRAME_SAMPLES = 2560
SAMPLE_SECONDS = 10
INFERENCE_SAMPLES = 32000
QUEUE_LIMIT = 400
OPEN_TIMEOUT = 10
READY_TIMEOUT = 250
SEND_TIMEOUT = 15
REPLY_TIMEOUT = 250
CLOSE_TIMEOUT = 10
DRAIN_TIMEOUT = 300
ACK_LAG_P95_TARGET = 2.0


class CaseFailure(Exception):
    """Use fixed failure codes so exceptions cannot leak service transcripts."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise CaseFailure(code)


def percentiles(values):
    values = sorted(values)
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}

    def at(fraction):
        position = (len(values) - 1) * fraction
        low, high = math.floor(position), math.ceil(position)
        return round(values[low] + (values[high] - values[low]) * (position - low), 6)

    return {"count": len(values), "p50": at(.50), "p95": at(.95),
            "p99": at(.99), "max": round(values[-1], 6)}


def websocket_url(url):
    parts = urlsplit(url)
    require(parts.scheme in ("http", "https", "ws", "wss") and parts.netloc,
            "invalid_url")
    require(not parts.username and not parts.password and not parts.query
            and not parts.fragment, "url_must_not_contain_credentials_or_query")
    path = parts.path.rstrip("/")
    require(path in ("", "/api/dictation/stream"), "unexpected_stream_path")
    return urlunsplit(({"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme),
                       parts.netloc, "/api/dictation/stream", "", ""))


def sample_pcm(path):
    with wave.open(str(path), "rb") as audio:
        require((audio.getframerate(), audio.getnchannels(), audio.getsampwidth(),
                 audio.getcomptype()) == (SAMPLE_RATE, 1, 2, "NONE"),
                "sample_must_be_16khz_mono_pcm16_wav")
        require(audio.getnframes() >= SAMPLE_SECONDS * SAMPLE_RATE,
                "sample_must_contain_at_least_ten_seconds")
        sample = audio.readframes(SAMPLE_SECONDS * SAMPLE_RATE)
    require(len(sample) == SAMPLE_SECONDS * SAMPLE_RATE * 2, "truncated_sample")
    return sample


def audio_frame(sample, offset, count):
    start = (offset * 2) % len(sample)
    return (sample[start:] + sample[:start])[:count * 2]


def numeric(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


async def run_case(url, token, audio_path, *, seconds=120, flush_seconds=40,
                   cancel_after=None):
    """Return JSON-safe counters, hashes, timings, and acceptance evidence.

    Cancellation sends the legacy plain-text command at the capture deadline.
    Queued audio may be discarded; a normal close without a final is expected.
    ``request_id`` and mode bind this legacy stream; it has no V1 generation or
    cancellation-final contract. Caller cancellation still propagates after
    socket/task cleanup. Other failures return partial counters without details
    from upstream exceptions.
    """
    loop = asyncio.get_running_loop()
    invoked = loop.time()
    counters = {key: 0 for key in (
        "captured_audio", "audio_sent", "audio_acked", "flush_sent", "flush_acked",
        "finish_sent", "finish_acked", "cancel_sent", "ready", "partial", "final",
        "error", "other", "total_received", "fixed_updates", "pending_updates",
        "qwen_2s_boundary_acks", "max_queue")}
    result = {"schema_version": 1, "protocol": "qwen-legacy-request-reply",
              "passed": False, "failure": None, "event_counts": counters,
              "ready_binding": None, "close": {"observed_before_cleanup": False,
              "code": None, "cleanup_completed": False}, "flushes": [],
              "finish": None, "cancel": None, "checks": {}, "stream_hash_chain": [],
              "thresholds": {"ack_lag_p95_seconds": ACK_LAG_P95_TARGET,
                             "capture_queue_items": QUEUE_LIMIT,
                             "window_seconds": 30}}
    ws, tasks, queue = None, [], asyncio.Queue(maxsize=QUEUE_LIMIT)
    started, duration, total_samples, expected_flushes = None, None, 0, 0
    sent_samples, acked_samples, captured_samples, sequence = 0, 0, 0, 0
    decoder_samples, max_span_samples, max_window = 0, 0, 0.0
    previous, previous_pending = "", ""
    fixed_times, pending_times = [], []
    lag, queue_delay, roundtrip, inference, producer_delay, heavy = [], [], [], [], [], []
    sent_hash = hashlib.sha256()
    stream_digest = bytes(32)
    cancel_requested = asyncio.Event()
    cancel_sent = asyncio.Event()
    stage = "input"

    def close_evidence():
        result["close"].update(observed_before_cleanup=True, code=ws.close_code)

    async def receive():
        raw = await asyncio.wait_for(ws.recv(), REPLY_TIMEOUT)
        counters["total_received"] += 1
        try:
            item = json.loads(raw)
        except (ValueError, TypeError):
            raise CaseFailure("invalid_json_reply") from None
        require(isinstance(item, dict), "reply_must_be_object")
        kind = item.get("type")
        counters[kind if kind in ("ready", "partial", "final", "error") else "other"] += 1
        require(kind != "error", "server_error")
        return item

    def validate_reply(item, kind):
        nonlocal previous, previous_pending, sequence, max_window, stream_digest
        require(item.get("type") == ("final" if kind == "finish" else "partial"),
                "unexpected_reply_type")
        require(item.get("request_id") == result["ready_binding"]["request_id"],
                "ready_request_binding_changed")
        seq = item.get("sequence")
        require(isinstance(seq, int) and not isinstance(seq, bool)
                and seq == sequence + 1, "sequence_not_contiguous")
        require(numeric(item.get("audio_seconds"))
                and abs(item["audio_seconds"] - sent_samples / SAMPLE_RATE) < 1e-8,
                "audio_seconds_not_equal_to_sent_audio")
        require(item.get("device") == "cuda:0", "unexpected_device")
        window = item.get("window_seconds")
        require(numeric(window) and 0 <= window <= 30, "window_exceeds_30_seconds")
        timing = item.get("inference_ms")
        require(numeric(timing) and timing >= 0, "invalid_inference_ms")
        text, pending = item.get("text"), item.get("pending")
        require(isinstance(text, str) and isinstance(pending, str), "invalid_text_fields")
        require(text.startswith(previous), "fixed_text_prefix_regressed")
        if kind == "finish":
            require(not pending, "final_has_pending_text")
        now = loop.time() - started
        if text != previous:
            counters["fixed_updates"] += 1
            fixed_times.append(now)
        if pending and pending != previous_pending:
            counters["pending_updates"] += 1
            pending_times.append(now)
        previous, previous_pending, sequence = text, pending, seq
        max_window = max(max_window, window)
        canonical = json.dumps({key: item[key] for key in (
            "sequence", "type", "audio_seconds", "text", "pending")},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        stream_digest = hashlib.sha256(stream_digest + canonical).digest()
        result["stream_hash_chain"].append({"sequence": seq, "sha256": stream_digest.hex()})
        return timing

    async def server_close():
        await asyncio.wait_for(ws.wait_closed(), CLOSE_TIMEOUT)
        close_evidence()
        require(ws.close_code == 1000, "unexpected_close_code")

    async def produce():
        nonlocal captured_samples
        next_flush = flush_seconds if flush_seconds is not None else math.inf
        for offset in range(0, total_samples, FRAME_SAMPLES):
            count = min(FRAME_SAMPLES, total_samples - offset)
            deadline = started + (offset + count) / SAMPLE_RATE
            await asyncio.sleep(max(0, deadline - loop.time()))
            captured_at = loop.time()
            producer_delay.append(max(0, captured_at - deadline))
            try:
                queue.put_nowait(("audio", captured_at, offset, count))
                captured_samples += count
                counters["captured_audio"] += 1
                counters["max_queue"] = max(counters["max_queue"], queue.qsize())
                while (offset + count) / SAMPLE_RATE + 1e-9 >= next_flush and next_flush < duration - 1e-9:
                    queue.put_nowait(("flush", captured_at, next_flush, 0))
                    next_flush += flush_seconds
                    counters["max_queue"] = max(counters["max_queue"], queue.qsize())
            except asyncio.QueueFull:
                raise CaseFailure("capture_queue_overflow") from None
        result["capture_stop"] = {
            "at_seconds": round(loop.time() - started, 6),
            "sent_samples": sent_samples, "acked_samples": acked_samples,
            "queued_items": queue.qsize()}
        if cancel_after is not None:
            cancel_requested.set()
            stage_at = loop.time()
            result["cancel"] = {"requested_at_seconds": round(stage_at - started, 6),
                                "sent_at_seconds": None, "close_latency_seconds": None,
                                "final_received": False}
            await asyncio.wait_for(ws.send("cancel"), SEND_TIMEOUT)
            counters["cancel_sent"] += 1
            result["cancel"]["sent_at_seconds"] = round(loop.time() - started, 6)
            cancel_sent.set()
        else:
            try:
                queue.put_nowait(("finish", loop.time(), 0, 0))
                counters["max_queue"] = max(counters["max_queue"], queue.qsize())
            except asyncio.QueueFull:
                raise CaseFailure("capture_queue_overflow") from None

    async def consume():
        nonlocal sent_samples, acked_samples, decoder_samples, max_span_samples, stage
        while True:
            if cancel_requested.is_set():
                stage = "cancel_close"
                await asyncio.wait_for(cancel_sent.wait(), SEND_TIMEOUT)
                try:
                    item = await receive()
                except ConnectionClosed:
                    close_evidence()
                    require(ws.close_code == 1000, "unexpected_cancel_close_code")
                    result["cancel"]["close_latency_seconds"] = round(
                        loop.time() - started - result["cancel"]["sent_at_seconds"], 6)
                    return
                result["cancel"]["final_received"] = item.get("type") == "final"
                raise CaseFailure("unexpected_reply_after_cancel")
            # A producer failure is supervised by gather; an idle queue cannot
            # leave a healthy capture waiting indefinitely.
            kind, captured_at, offset, count = await asyncio.wait_for(queue.get(), 30)
            if cancel_requested.is_set():
                continue
            stage = kind
            send_at = loop.time()
            await asyncio.wait_for(ws.send(audio_frame(sample, offset, count)
                                           if kind == "audio" else kind), SEND_TIMEOUT)
            counters[kind + "_sent"] += 1
            boundary = False
            if kind == "audio":
                sent_samples += count
                sent_hash.update(audio_frame(sample, offset, count))
                boundary = decoder_samples // INFERENCE_SAMPLES != (decoder_samples + count) // INFERENCE_SAMPLES
                decoder_samples += count
                max_span_samples = max(max_span_samples, decoder_samples)
            item = await receive()
            received_at = loop.time()
            timing = validate_reply(item, kind)
            counters[kind + "_acked"] += 1
            if kind == "audio":
                acked_samples += count
                point = (sent_samples / SAMPLE_RATE, max(0, received_at - captured_at))
                lag.append(point)
                queue_delay.append(max(0, send_at - captured_at))
                roundtrip.append(received_at - send_at)
                inference.append(timing)
                if boundary:
                    counters["qwen_2s_boundary_acks"] += 1
                    heavy.append({"audio_seconds": round(point[0], 6),
                                  "ack_lag_seconds": round(point[1], 6),
                                  "queue_delay_seconds": round(send_at - captured_at, 6),
                                  "roundtrip_seconds": round(received_at - send_at, 6),
                                  "inference_ms": timing})
            else:
                evidence = {"sent_at_seconds": round(send_at - started, 6),
                            "reply_at_seconds": round(received_at - started, 6),
                            "roundtrip_seconds": round(received_at - send_at, 6),
                            "queue_delay_seconds": round(send_at - captured_at, 6),
                            "audio_seconds": sent_samples / SAMPLE_RATE,
                            "inference_ms": timing,
                            "text_sha256": hashlib.sha256(previous.encode()).hexdigest(),
                            "characters": len(previous), "pending_characters": len(previous_pending)}
                if kind == "flush":
                    evidence["scheduled_audio_seconds"] = offset
                    result["flushes"].append(evidence)
                    decoder_samples = 0
                else:
                    result["finish"] = evidence
                    stage = "finish_close"
                    await server_close()
                    return

    try:
        require(numeric(seconds) and 0 < seconds <= 3600, "invalid_seconds")
        require(flush_seconds is None or numeric(flush_seconds) and flush_seconds > 0,
                "invalid_flush_seconds")
        require(cancel_after is None or numeric(cancel_after) and 0 < cancel_after <= seconds,
                "invalid_cancel_after")
        require(isinstance(token, str) and bool(token.strip()), "empty_token")
        duration = seconds if cancel_after is None else cancel_after
        total_samples = round(duration * SAMPLE_RATE)
        require(total_samples > 0, "empty_capture")
        expected_flushes = (max(0, math.ceil((duration - 1e-9) / flush_seconds) - 1)
                            if flush_seconds is not None else 0)
        result["expected"] = {"capture_seconds": duration, "audio_samples": total_samples,
                              "audio_frames": math.ceil(total_samples / FRAME_SAMPLES),
                              "flushes": expected_flushes,
                              "finish": int(cancel_after is None),
                              "cancel": int(cancel_after is not None)}
        sample = sample_pcm(audio_path)
        result["input_audio"] = {"sample_seconds": SAMPLE_SECONDS,
                                 "sample_sha256": hashlib.sha256(sample).hexdigest(),
                                 "frame_samples": FRAME_SAMPLES, "sample_rate": SAMPLE_RATE}
        stage = "connect"
        ws = await asyncio.wait_for(connect(
            websocket_url(url), proxy=None,
            additional_headers={"Authorization": "Bearer " + token},
            open_timeout=OPEN_TIMEOUT, close_timeout=2, max_size=2 * 1024 * 1024,
            max_queue=2, ping_interval=20, ping_timeout=READY_TIMEOUT), OPEN_TIMEOUT + 2)
        stage = "ready"
        await asyncio.wait_for(ws.send(json.dumps({"mode": "qwen-stream"})), SEND_TIMEOUT)
        ready = await asyncio.wait_for(receive(), READY_TIMEOUT)
        require(ready.get("type") == "ready", "missing_ready")
        require(ready.get("mode") == "qwen-stream", "unexpected_ready_mode")
        require(isinstance(ready.get("request_id"), str) and bool(ready["request_id"]),
                "missing_ready_request_id")
        result["ready_binding"] = {"mode": ready["mode"], "request_id": ready["request_id"]}
        result["ready_seconds"] = round(loop.time() - invoked, 6)
        started = loop.time()
        tasks = [asyncio.create_task(produce()), asyncio.create_task(consume())]
        await asyncio.wait_for(asyncio.gather(*tasks), duration + DRAIN_TIMEOUT)
        checks = {"ready_binding": True, "sequence_and_prefix": True,
                  "sent_audio_progress_and_device": True, "window_within_30_seconds": True,
                  "capture_completed": captured_samples == total_samples,
                  "normal_server_close": result["close"]["observed_before_cleanup"]
                                         and result["close"]["code"] == 1000}
        p95 = percentiles([value for _, value in lag])["p95"]
        if cancel_after is None:
            checks.update(all_audio_acked=sent_samples == acked_samples == total_samples,
                          expected_event_counts=counters["audio_acked"] == result["expected"]["audio_frames"]
                          and counters["flush_acked"] == expected_flushes
                          and counters["finish_acked"] == counters["final"] == 1,
                          final_without_pending=result["finish"] is not None and not previous_pending,
                          nonempty_final=bool(previous))
            if duration >= 120:
                checks.update(ack_lag_p95_within_target=p95 is not None and p95 <= ACK_LAG_P95_TARGET,
                              realtime_fixed=bool(fixed_times) and fixed_times[0] < duration,
                              realtime_pending=bool(pending_times) and pending_times[0] < duration,
                              crossed_30_second_window=max_span_samples > 30 * SAMPLE_RATE)
        else:
            checks.update(cancel_sent=counters["cancel_sent"] == 1,
                          actual_audio_acked=counters["audio_acked"] > 0,
                          cancelled_without_final=counters["final"] == 0
                          and counters["finish_sent"] == 0)
        result["checks"] = checks
        result["passed"] = all(checks.values())
    except ConnectionClosed as exc:
        if ws is not None:
            close_evidence()
        result["failure"] = {"stage": stage, "kind": type(exc).__name__,
                             "code": "closed_before_expected_reply"}
    except Exception as exc:
        result["failure"] = {"stage": stage, "kind": type(exc).__name__,
                             "code": exc.code if isinstance(exc, CaseFailure) else "client_exception"}
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), CLOSE_TIMEOUT)
                await asyncio.wait_for(ws.wait_closed(), CLOSE_TIMEOUT)
                result["close"]["cleanup_completed"] = True
                if result["close"]["code"] is None:
                    result["close"]["code"] = ws.close_code
            except Exception as exc:
                result["close"]["cleanup_failure_kind"] = type(exc).__name__
                result["passed"] = False
        result["elapsed_seconds"] = round(loop.time() - invoked, 6)
        result["audio"] = {"captured_samples": captured_samples, "sent_samples": sent_samples,
                           "acked_samples": acked_samples,
                           "unsent_samples": captured_samples - sent_samples,
                           "unacked_samples": sent_samples - acked_samples,
                           "sent_pcm_sha256": sent_hash.hexdigest()}
        result["text"] = {"latest_sha256": hashlib.sha256(previous.encode()).hexdigest(),
                          "final_sha256": (hashlib.sha256(previous.encode()).hexdigest()
                                           if result["finish"] is not None else None),
                          "characters": len(previous), "pending_characters": len(previous_pending),
                          "first_fixed_seconds": round(fixed_times[0], 6) if fixed_times else None,
                          "first_pending_seconds": round(pending_times[0], 6) if pending_times else None,
                          "live_fixed_updates": sum(value < (duration or 0) for value in fixed_times),
                          "live_pending_updates": sum(value < (duration or 0) for value in pending_times)}
        result["stream_sha256"] = stream_digest.hex()
        result["windows"] = {"max_reported_seconds": max_window,
                             "max_contiguous_audio_seconds": max_span_samples / SAMPLE_RATE,
                             "crossed_30_second_window": max_span_samples > 30 * SAMPLE_RATE}
        result["latency"] = {
            "ack_lag_seconds": percentiles([value for _, value in lag]),
            "lag_early": percentiles([value for at, value in lag if at <= (duration or 0) / 3]),
            "lag_late": percentiles([value for at, value in lag if at >= (duration or 0) * 2 / 3]),
            "queue_delay_seconds": percentiles(queue_delay),
            "ack_roundtrip_seconds": percentiles(roundtrip),
            "audio_inference_ms": percentiles(inference),
            "producer_schedule_lag_seconds": percentiles(producer_delay),
            "qwen_2s_boundary_ack_lag_seconds": percentiles([item["ack_lag_seconds"] for item in heavy]),
            "qwen_2s_boundary_inference_ms": percentiles([item["inference_ms"] for item in heavy]),
            "qwen_2s_boundary_events": heavy}
        early, late = result["latency"]["lag_early"]["p95"], result["latency"]["lag_late"]["p95"]
        result["latency"]["late_minus_early_p95_seconds"] = (
            round(late - early, 6) if early is not None and late is not None else None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--flush-seconds", type=float, default=40)
    parser.add_argument("--cancel-after", type=float)
    args = parser.parse_args()
    try:
        token = args.token_file.read_text().strip()
        result = asyncio.run(run_case(args.url, token, args.audio, seconds=args.seconds,
                                      flush_seconds=args.flush_seconds, cancel_after=args.cancel_after))
    except Exception as exc:
        result = {"passed": False, "failure": {"kind": type(exc).__name__, "code": "cli_exception"}}
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
