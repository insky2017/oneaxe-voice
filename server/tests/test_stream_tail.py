"""Pause finalization and mutable suffix behavior for streaming decoders."""

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

import numpy as np

from oneaxe_voice.stream_worker import StreamDecoder


def model_state(**kwargs):
    return SimpleNamespace(
        **kwargs,
        chunk_size_samples=int(kwargs["chunk_size_sec"] * 16000),
        chunk_id=0, chunk_text=[], last_fixed_text="",
        audio_accum=np.zeros(0, dtype=np.float32),
        buffer=np.zeros(0, dtype=np.float32), text="", _raw_decoded="",
    )


def speech(samples=5120):
    return np.ones(samples, dtype="<i2").tobytes()


class StreamTailTests(unittest.TestCase):
    def model(self):
        model = MagicMock()
        model.init_streaming_state.side_effect = model_state
        model.processor.tokenizer.encode.side_effect = list
        model.processor.tokenizer.decode.side_effect = "".join
        return model

    def test_r2_punctuation_commits_without_waiting_for_next_chunk(self):
        model = self.model()

        def transcribe(audio, state, **kwargs):
            self.assertTrue(kwargs["rollback_punctuation"])
            state.chunk_text = ["测试。"]
            state.last_fixed_text = "测试。"
            state.audio_accum = np.ones(len(audio), dtype=np.float32)
            return "测试。", state.last_fixed_text

        model.streaming_transcribe_no_reset.side_effect = transcribe
        decoder = StreamDecoder(model, "r2t2")
        result = decoder.feed(speech())
        self.assertEqual((result["text"], result["pending"], result["preview"]),
                         ("测试。", "", "测试。"))

    def test_r2_pause_releases_tail_then_next_utterance_keeps_global_prefix(self):
        model = self.model()
        phrases = iter([("体验", "体", ["体"]), ("继续。", "继续。", ["继续。"])])

        def transcribe(audio, state, **kwargs):
            candidate, fixed, chunks = next(phrases)
            state.chunk_text = chunks
            state.last_fixed_text = fixed
            state.audio_accum = np.ones(len(audio), dtype=np.float32)
            return candidate, fixed

        model.streaming_transcribe_no_reset.side_effect = transcribe
        model.finish_streaming_transcribe_no_reset.side_effect = ["体验。", "继续。"]
        decoder = StreamDecoder(model, "r2t2")
        partial = decoder.feed(speech())
        self.assertEqual((partial["text"], partial["pending"], partial["preview"]),
                         ("体", "验", "体验"))
        pause = decoder.flush()
        self.assertEqual((pause["text"], pause["pending"]), ("体验。", ""))
        self.assertEqual(decoder.flush()["text"], "体验。")
        self.assertEqual(decoder.finish()["text"], "体验。")
        self.assertEqual(model.finish_streaming_transcribe_no_reset.call_count, 1)
        next_result = decoder.feed(speech())
        self.assertEqual((next_result["text"], next_result["pending"]),
                         ("体验。继续。", ""))
        self.assertEqual(model.streaming_transcribe_no_reset.call_args.kwargs["max_new_tokens"], 4)
        self.assertEqual(decoder.finish()["text"], "体验。继续。")

    def test_short_tail_and_idle_silence_do_not_repeat_or_generate(self):
        model = self.model()
        model.finish_streaming_transcribe_no_reset.return_value = "吧。"
        decoder = StreamDecoder(model, "r2t2")
        decoder.feed(np.zeros(2560, dtype="<i2").tobytes())
        self.assertEqual(decoder.flush()["text"], "")
        model.finish_streaming_transcribe_no_reset.assert_not_called()
        decoder.feed(speech(1200))
        self.assertEqual(decoder.flush()["text"], "吧。")
        self.assertEqual(decoder.flush()["text"], "吧。")
        self.assertEqual(decoder.finish()["text"], "吧。")
        model.finish_streaming_transcribe_no_reset.assert_called_once()

    def test_rolling_window_candidate_uses_current_window_fixed_text(self):
        model = self.model()
        responses = iter([
            ("第一句。后面", "第一句。", ["第一句。"]),
            ("后面部分内容", "第一句。后面部分", ["后面部分"]),
            ("修订内容", "第一句。后面部分", ["不同前缀"]),
        ])

        def transcribe(audio, state, **kwargs):
            candidate, fixed, chunks = next(responses)
            state.chunk_text = chunks
            state.audio_accum = np.ones(len(audio), dtype=np.float32)
            return candidate, fixed

        model.streaming_transcribe_no_reset.side_effect = transcribe
        decoder = StreamDecoder(model, "r2t2")
        self.assertEqual(decoder.feed(speech())["pending"], "后面")
        rolled = decoder.feed(speech(2560))
        self.assertEqual((rolled["text"], rolled["pending"], rolled["preview"]),
                         ("第一句。后面部分", "内容", "第一句。后面部分内容"))
        mismatch = decoder.feed(speech(2560))
        self.assertEqual(mismatch["pending"], "")
        self.assertEqual(mismatch["text"], "第一句。后面部分")

    def test_qwen_pause_and_final_preserve_one_global_text(self):
        model = self.model()
        phrases = iter(["第一句ABC", "第二句XYZ"])

        def transcribe(audio, state):
            state.audio_accum = np.concatenate((state.audio_accum, audio))
            state.chunk_id += 1
            state.text = state._raw_decoded = next(phrases)

        model.streaming_transcribe.side_effect = transcribe
        model.finish_streaming_transcribe.side_effect = lambda state: None
        decoder = StreamDecoder(model, "qwen-stream")
        first = decoder.feed(speech(32000))
        self.assertEqual(first["pending"], "第一句ABC")
        self.assertEqual(decoder.flush()["text"], "第一句ABC")
        self.assertEqual(decoder.flush()["text"], "第一句ABC")
        self.assertEqual(decoder.feed(speech(32000))["preview"], "第一句ABC第二句XYZ")
        self.assertEqual(decoder.finish()["text"], "第一句ABC第二句XYZ")
        self.assertEqual(model.finish_streaming_transcribe.call_count, 2)

    def test_qwen_pause_at_automatic_rollover_does_not_finalize_twice(self):
        model = self.model()

        def transcribe(audio, state):
            state.audio_accum = np.concatenate((state.audio_accum, audio))
            state.chunk_id += 1
            state.text = state._raw_decoded = "已说完。"

        model.streaming_transcribe.side_effect = transcribe
        model.finish_streaming_transcribe.side_effect = lambda state: None
        decoder = StreamDecoder(model, "qwen-stream")
        for _ in range(15):
            decoder.feed(speech(32000))
        self.assertEqual(decoder.result()["text"], "已说完。")
        self.assertEqual(decoder.flush()["text"], "已说完。")
        self.assertEqual(decoder.finish()["text"], "已说完。")
        self.assertEqual(model.finish_streaming_transcribe.call_count, 1)
        self.assertEqual(decoder.feed(speech(32000))["preview"], "已说完。已说完。")

    def test_r2_english_pause_keeps_one_boundary_space_through_partial_and_final(self):
        model = self.model()
        outputs = iter([("Hello", "Hell"), ("world", "w"), ("world", "world")])

        def transcribe(audio, state, **kwargs):
            candidate, fixed = next(outputs)
            state.chunk_text = [fixed]
            state.audio_accum = np.ones(len(audio), dtype=np.float32)
            return candidate, fixed

        model.streaming_transcribe_no_reset.side_effect = transcribe
        model.finish_streaming_transcribe_no_reset.side_effect = ["Hello", "world"]
        decoder = StreamDecoder(model, "r2t2")
        decoder.feed(speech())
        self.assertEqual(decoder.flush()["text"], "Hello")
        partial = decoder.feed(speech())
        self.assertEqual((partial["text"], partial["pending"], partial["preview"]),
                         ("Hello w", "orld", "Hello world"))
        stable = decoder.feed(speech(2560))
        self.assertEqual((stable["text"], stable["pending"]), ("Hello world", ""))
        self.assertEqual(decoder.finish()["text"], "Hello world")

    def test_qwen_english_pause_keeps_one_boundary_space_and_model_spacing(self):
        model = self.model()
        phrases = iter(["Hello", "world"])

        def transcribe(audio, state):
            state.audio_accum = np.concatenate((state.audio_accum, audio))
            state.chunk_id += 1
            state.text = state._raw_decoded = next(phrases)

        model.streaming_transcribe.side_effect = transcribe
        decoder = StreamDecoder(model, "qwen-stream")
        decoder.feed(speech(32000))
        self.assertEqual(decoder.flush()["text"], "Hello")
        partial = decoder.feed(speech(32000))
        self.assertEqual((partial["text"], partial["pending"], partial["preview"]),
                         ("Hello", " world", "Hello world"))
        self.assertEqual(decoder.finish()["text"], "Hello world")
        # The model's own leading space is preserved without adding another.
        self.assertEqual(decoder._global(" next"), "Hello next")


if __name__ == "__main__":
    unittest.main()
