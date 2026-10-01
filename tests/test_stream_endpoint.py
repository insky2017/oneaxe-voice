"""Streaming sends audio before an endpoint and gates only idle silence."""

import unittest

from oneaxe_voice.vad import StreamEndpoint
from test_vad import Detector, SILENCE, VOICE


class StreamEndpointTests(unittest.TestCase):
    def endpoint(self):
        return StreamEndpoint(detector=Detector())

    def test_live_pcm_then_exact_pause_flush_once_and_resume(self):
        gate = self.endpoint()
        self.assertEqual(gate.feed(SILENCE * 200), [])
        first = gate.feed(VOICE * 30)
        self.assertTrue(first)
        self.assertNotIn('flush', first)
        before = gate.feed(SILENCE * 49)
        self.assertNotIn('flush', before)
        tail = gate.feed(SILENCE)
        self.assertEqual(tail[-1], 'flush')
        received = b''.join(item for item in first + before + tail if isinstance(item, bytes))
        self.assertEqual(received, SILENCE * 11 + VOICE * 30 + SILENCE * 50)
        self.assertEqual(gate.feed(SILENCE * 1700), [])
        self.assertEqual(gate.finish(), [])
        resumed = gate.feed(VOICE * 3) + gate.finish()
        self.assertEqual(b''.join(resumed), SILENCE * 11 + VOICE * 3)

    def test_fragmented_samples_preserve_audio_and_endpoint_order(self):
        audio = VOICE * 21 + SILENCE * 60 + VOICE * 10
        whole = self.endpoint()
        expected = whole.feed(audio) + whole.finish()
        fragmented = self.endpoint()
        actual = []
        for start in range(0, len(audio), 333):
            actual.extend(fragmented.feed(audio[start:start+333]))
        actual.extend(fragmented.finish())
        self.assertEqual(actual, expected)

    def test_f8_at_endpoint_does_not_flush_twice(self):
        gate = self.endpoint()
        events = gate.feed(VOICE * 10 + SILENCE * 50) + gate.finish()
        self.assertEqual(events.count('flush'), 1)
        self.assertEqual(gate.finish(), [])

    def test_short_partial_final_syllable_is_not_dropped(self):
        gate = self.endpoint()
        values = gate.feed(VOICE * 10 + VOICE[:222]) + gate.finish()
        audio = b''.join(values)
        self.assertTrue(audio.startswith(VOICE * 10 + VOICE[:222]))
        self.assertEqual(audio[len(VOICE * 10 + VOICE[:222]):], b'\0' * (640-222))

    def test_real_vad_digital_silence_never_schedules_asr(self):
        gate = StreamEndpoint()
        self.assertEqual(gate.feed(SILENCE * 1700) + gate.finish(), [])


if __name__ == '__main__':
    unittest.main()
