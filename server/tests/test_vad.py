"""Segmentation boundaries, frame continuity and silence with real WebRTC VAD."""

import struct
import unittest

from oneaxe_voice.vad import FRAME_BYTES, Segmenter

VOICE = struct.pack("<h", 1200) * 320
SILENCE = b"\0" * FRAME_BYTES


class Detector:
    def is_speech(self, data, rate):
        return data != SILENCE


class VADTests(unittest.TestCase):
    def test_pause_and_preroll(self):
        segmenter = Segmenter(detector=Detector())
        self.assertEqual(segmenter.feed(SILENCE * 20 + VOICE * 20 + SILENCE * 34), [])
        pieces = segmenter.feed(SILENCE)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].reason, "pause")
        self.assertIn(VOICE * 20, pieces[0].pcm)
        self.assertTrue(pieces[0].pcm.startswith(SILENCE))
        self.assertEqual(segmenter.finish(), [])

    def test_stop_flushes_partial_frame(self):
        segmenter = Segmenter(detector=Detector())
        segmenter.feed(VOICE * 20 + VOICE[:200])
        pieces = segmenter.finish()
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].reason, "stop")
        self.assertTrue(pieces[0].pcm.startswith(VOICE * 20 + VOICE[:200]))

    def test_forced_limit_preserves_continuation_and_short_tail(self):
        segmenter = Segmenter(max_seconds=3, detector=Detector())
        pieces = segmenter.feed(VOICE * 155) + segmenter.finish()
        self.assertEqual([p.reason for p in pieces], ["limit", "stop"])
        self.assertEqual(b"".join(p.pcm for p in pieces), VOICE * 155)

    def test_packets_can_split_individual_pcm_samples(self):
        data = VOICE * 20 + SILENCE * 40
        whole = Segmenter(detector=Detector()).feed(data)
        fragmented = Segmenter(detector=Detector())
        result = []
        for i in range(0, len(data), 333):
            result.extend(fragmented.feed(data[i:i+333]))
        result.extend(fragmented.finish())
        self.assertEqual(result, whole)

    def test_click_is_not_an_utterance(self):
        segmenter = Segmenter(detector=Detector())
        self.assertEqual(segmenter.feed(VOICE * 2 + SILENCE * 60) + segmenter.finish(), [])

    def test_real_webrtc_silence_remains_silent(self):
        segmenter = Segmenter()
        self.assertEqual(segmenter.feed(SILENCE * 200) + segmenter.finish(), [])


if __name__ == "__main__":
    unittest.main()
