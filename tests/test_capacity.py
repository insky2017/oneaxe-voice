"""CPU-only checks for benchmark safety, pacing, privacy, and paired comparisons."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

try:
    from .e2e_capacity import (EvidenceError, Stream, compare_results, lag_summary,
                               load_manifest, timing_summary, validate_url)
except ImportError:
    from e2e_capacity import (EvidenceError, Stream, compare_results, lag_summary,
                              load_manifest, timing_summary, validate_url)


def args(seconds=48):
    return SimpleNamespace(seconds=seconds, flush_seconds=24, max_lag_seconds=2,
                           max_fixed_gap_seconds=30)


def spec(name="pc", role="pc"):
    return {"name": name, "role": role, "token": "PRIVATE_TOKEN", "audio": b"\x01\x00" * 16000,
            "source_key": "zh", "baseline_key": "zh0", "audio_sha256": "a" * 64,
            "offset_samples": 0, "keywords": ["PRIVATE_PHRASE"]}


def result(names=("pc", "phone")):
    sessions = [{"name": name, "role": "pc" if name == "pc" else "mobile",
                 "audio_sha256": "a" * 64 if name == "pc" else "b" * 64,
                 "source_key": "zh" if name == "pc" else "en", "offset_samples": 0,
                 "baseline_key": None,
                 "steps": {"audio": {"inference_ms": {"p95": 100, "p99": 150}}},
                 "processed_lag_seconds": {"p95": .3}, "late_minus_early_p95_seconds": .01,
                 "first_fixed_seconds": 2, "finish_latency_seconds": .2,
                 "flush_latency_seconds": {"p95": .3}} for name in names]
    return {"stream_count": len(sessions), "passed": True, "generation": ["instance", 4],
            "model_identity": {"model": "Confucius4-R2T2", "mode": "r2t2", "device": "cuda:0"},
            "capture_seconds": 120, "flush_seconds": 24, "sessions": sessions,
            "telemetry_complete": True, "correctness": {"passed": True},
            "absolute_realtime": {"passed": True}}


class CapacityTests(unittest.TestCase):
    def test_production_and_non_loopback_urls_are_rejected(self):
        for url in ("http://127.0.0.1:8097", "https://127.0.0.1:18098", "http://example.com:18098",
                    "http://user:secret@127.0.0.1:18098", "http://127.0.0.1:18098/api",
                    "http://127.0.0.1:18098?token=secret"):
            with self.subTest(url=url), self.assertRaises(EvidenceError):
                validate_url(url)
        for url in ("http://127.0.0.1:18096", "http://localhost:18098", "http://[::1]:18099/"):
            self.assertEqual(validate_url(url), url.rstrip("/"))

    def test_manifest_requires_independent_credentials_and_retains_offsets(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with wave.open(str(root / "test.wav"), "wb") as output:
                output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                output.writeframes(b"\x01\x00" * 16000)
            (root / "pc.token").write_text("pc-secret")
            (root / "phone.token").write_text("phone-secret")
            rows = [{"role": "pc", "token_file": "pc.token", "audio_file": "test.wav"},
                    {"role": "mobile", "token_file": "phone.token", "audio_file": "test.wav", "offset": .16}]
            path = root / "manifest.json"
            path.write_text(json.dumps({"streams": rows}))
            streams, admin = load_manifest(path)
            self.assertEqual(streams[1]["offset_samples"], 2560)
            self.assertEqual(admin, "pc-secret")
            (root / "phone.token").write_text("pc-secret")
            with self.assertRaisesRegex(EvidenceError, "INDEPENDENT_TOKENS"):
                load_manifest(path)

    def test_timing_categories_and_minute_lag_are_separate(self):
        events = [{"step_kind": kind, "inference_ms": value} for kind, value in
                  (("audio", 10), ("audio", 20), ("flush", 500), ("finish", 900))]
        report = timing_summary(events)
        self.assertEqual(report["audio"]["event_count"], 2)
        self.assertEqual(report["flush"]["inference_ms"]["p95"], 500)
        lag = lag_summary([(0, .1), (30, .2), (60, .3), (119, .4)], 120)
        self.assertEqual([row["sample_count"] for row in lag["lag_by_minute"]], [2, 2])
        self.assertAlmostEqual(lag["late_minus_early_p95_seconds"], .3)

    def test_comparison_pairs_extra_streams_by_audio_and_rejects_offset_mismatch(self):
        baseline = result()
        candidate = result(("pc", "phone", "extra-zh", "extra-en"))
        candidate["sessions"][2].update(audio_sha256="a" * 64, source_key="zh")
        self.assertTrue(compare_results(baseline, candidate)["passed"])
        candidate["sessions"][2]["offset_samples"] = 2560
        comparison = compare_results(baseline, candidate)
        self.assertFalse(comparison["relative_speed"]["passed"])
        self.assertFalse(comparison["sessions"][2]["matched_baseline"])

    def test_worst_stream_and_correctness_are_independent(self):
        baseline, candidate = result(), result()
        candidate["sessions"][1]["steps"]["audio"]["inference_ms"]["p95"] = 111
        comparison = compare_results(baseline, candidate)
        self.assertFalse(comparison["relative_speed"]["passed"])
        self.assertTrue(comparison["correctness"]["passed"])
        self.assertTrue(comparison["absolute_realtime"]["passed"])
        candidate = deepcopy(baseline)
        candidate["correctness"]["passed"] = False
        comparison = compare_results(baseline, candidate)
        self.assertTrue(comparison["relative_speed"]["passed"])
        self.assertFalse(comparison["passed"])

    def test_multiple_dual_baselines_cover_four_unique_sources_across_generations(self):
        first, second = result(), result(("garden", "planet"))
        for index, row in enumerate(second["sessions"]):
            row.update(audio_sha256=("c" if index == 0 else "d") * 64,
                       source_key=row["name"], baseline_key=row["name"])
        candidate = deepcopy(first)
        candidate.update(stream_count=4, generation=["other-instance", 8])
        candidate["sessions"] += deepcopy(second["sessions"])
        comparison = compare_results([first, second], candidate)
        self.assertTrue(comparison["passed"])
        self.assertFalse(comparison["generation_observation"]["all_equal"])
        self.assertEqual([row["baseline_index"] for row in comparison["sessions"]], [0, 0, 1, 1])

    def test_first_fixed_flush_and_final_regressions_fail_relative_speed(self):
        baseline = result()
        for key in ("first_fixed_seconds", "finish_latency_seconds", "flush_latency_seconds"):
            candidate = deepcopy(baseline)
            if key == "flush_latency_seconds":
                candidate["sessions"][1][key]["p95"] = 20
            else:
                candidate["sessions"][1][key] = 20
            with self.subTest(key=key):
                comparison = compare_results(baseline, candidate)
                self.assertFalse(comparison["relative_speed"]["passed"])
                self.assertTrue(comparison["correctness"]["passed"])
        candidate = deepcopy(baseline)
        candidate["sessions"][1].update(first_fixed_seconds=2.4, finish_latency_seconds=.3,
                                         flush_latency_seconds={"p95": .4})
        comparison = compare_results(baseline, candidate)
        self.assertTrue(comparison["passed"])
        self.assertEqual(comparison["sessions"][1]["tail_latency_checks"]["first_fixed"]["limit_seconds"], 2.4)


class CapacityAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_flush_does_not_block_capture_and_preserves_audio_barrier(self):
        stream = Stream(spec(), "http://127.0.0.1:18098", ("instance", 4), args(24.32))
        stream.started = 0
        stream.limit = 10 ** 9
        sent = []

        async def send(value):
            sent.append(value)

        stream.ws = SimpleNamespace(send=send)
        real_sleep = asyncio.sleep

        async def clock_sleep(_):
            await real_sleep(0)

        with patch(Stream.__module__ + ".asyncio.sleep", new=clock_sleep):
            await asyncio.gather(stream.produce(), stream.send())
        self.assertEqual(stream.produced, 389120)
        controls = [json.loads(value) for value in sent if isinstance(value, str)]
        self.assertEqual(controls, [{"type": "flush", "after_audio_samples": 384000},
                                    {"type": "finish", "after_audio_samples": 389120}])
        self.assertEqual(stream.metrics["flush_sent"], 1)
        self.assertEqual(stream.flush_latencies, [])

    async def test_report_preserves_failure_evidence_without_private_content(self):
        stream = Stream(spec(), "http://127.0.0.1:18098", ("instance", 4), args())
        stream.fixed = "PRIVATE_PHRASE"
        stream.metrics["failure"] = {"kind": "EvidenceError", "code": "FIXED_PREFIX_REGRESSED"}
        report = stream.report([spec()])
        encoded = json.dumps(report)
        self.assertNotIn("PRIVATE_TOKEN", encoded)
        self.assertNotIn("PRIVATE_PHRASE", encoded)
        self.assertIn("FIXED_PREFIX_REGRESSED", encoded)
        self.assertFalse(report["correctness"]["passed"])
        self.assertEqual(report["keyword_isolation"]["own_matches"], [True])

    async def test_failed_session_cleanup_cancels_own_socket(self):
        stream = Stream(spec(), "http://127.0.0.1:18098", ("instance", 4), args())
        stream.ws = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        await stream.cleanup()
        stream.ws.send.assert_awaited_once_with('{"type": "cancel"}')
        self.assertTrue(stream.metrics["cleanup"]["cancel_attempted"])
        self.assertTrue(stream.metrics["cleanup"]["socket_closed"])

    async def test_missing_slow_audio_event_invalidates_measurement_and_comparison(self):
        stream = Stream(spec(), "http://127.0.0.1:18098", ("instance", 4), args())
        stream.metrics["frames"] = 3
        # The server merged away one slow audio RPC; surviving fields are valid.
        stream.events = [{"step_kind": kind, "inference_ms": 10, "queue_ms": 0,
                          "prepare_ms": 1, "generate_ms": 8, "apply_ms": 1}
                         for kind in ("audio", "audio", "finish")]
        report = stream.report([spec()])
        self.assertEqual(report["telemetry_missing_events"], 0)
        self.assertFalse(report["rpc_event_counts"]["passed"])
        self.assertFalse(report["telemetry_complete"])
        baseline, candidate = result(), result()
        candidate["telemetry_complete"] = report["telemetry_complete"]
        self.assertFalse(compare_results(baseline, candidate)["relative_speed"]["passed"])
