"""CPU WebRTC VAD and bounded, incremental utterance segmentation."""

from collections import deque
from dataclasses import dataclass
import math
import struct

import webrtcvad

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2


@dataclass(frozen=True)
class Segment:
    pcm: bytes
    reason: str
    end_seconds: float


class Segmenter:
    """Wait for speech, retain its onset, and cut at pauses or a length limit."""

    def __init__(self, pause_ms=700, max_seconds=15, mode=2, min_dbfs=-60, detector=None):
        self.detector = detector or webrtcvad.Vad(mode)
        self.pause_frames = math.ceil(pause_ms / FRAME_MS)
        self.max_frames = int(max_seconds * 1000 / FRAME_MS)
        self.min_dbfs = min_dbfs
        self.pre = deque(maxlen=12)  # 240 ms includes the onset decision window.
        self.recent = deque(maxlen=5)
        self.frames = []
        self.voiced = 0
        self.quiet = 0
        self.active = False
        self.continuing = False
        self.total_frames = 0
        self.pending = bytearray()

    def _speech(self, frame: bytes) -> bool:
        # Reject digital silence / very low electrical noise before the classifier.
        values = struct.unpack("<320h", frame)
        power = sum(value * value for value in values) / 320
        dbfs = 10 * math.log10(max(power, 1e-12) / 32768**2)
        return dbfs >= self.min_dbfs and self.detector.is_speech(frame, RATE)

    def feed(self, chunk: bytes) -> list[Segment]:
        """Accept arbitrary PCM packet sizes without losing frame boundaries."""
        self.pending.extend(chunk)
        result = []
        while len(self.pending) >= FRAME_BYTES:
            frame = bytes(self.pending[:FRAME_BYTES])
            del self.pending[:FRAME_BYTES]
            self.total_frames += 1
            speech = self._speech(frame)
            if not self.active:
                self.pre.append((frame, speech))
                self.recent.append(speech)
                if sum(self.recent) >= 2:
                    self.active = True
                    self.frames = [item[0] for item in self.pre]
                    self.voiced = sum(item[1] for item in self.pre)
                    self.quiet = 0 if speech else 1
                    self.pre.clear()
                    self.recent.clear()
                continue
            self.frames.append(frame)
            self.voiced += int(speech)
            self.quiet = 0 if speech else self.quiet + 1
            if self.quiet >= self.pause_frames:
                value = self._emit("pause")
                if value:
                    result.append(value)
            elif len(self.frames) >= self.max_frames:
                value = self._emit("limit", continuation=True)
                if value:
                    result.append(value)
        return result

    def _emit(self, reason: str, continuation=False) -> Segment | None:
        # Keep 160 ms trailing context; the rest of the pause need not reach ASR.
        trim = max(0, self.quiet - 8)
        frames = self.frames[:-trim] if trim else self.frames
        value = None
        if self.voiced >= (1 if self.continuing else 6) and frames:
            pcm = b"".join(frames)
            # Preserve even a short final syllable after a forced length boundary.
            pcm += b"\0" * max(0, 3200 - len(pcm))
            value = Segment(pcm, reason, self.total_frames * FRAME_MS / 1000)
        self.frames = []
        self.voiced = 0
        self.quiet = 0
        self.active = continuation
        self.continuing = continuation
        self.pre.clear()
        self.recent.clear()
        return value

    def finish(self) -> list[Segment]:
        """Flush the last voiced phrase at F8 stop, including a partial PCM frame."""
        result = []
        if self.pending:
            size = len(self.pending)
            if size % 2:
                del self.pending[-1]
            if self.pending:
                # One zero-padded frame is at most 20 ms and keeps a final syllable.
                self.pending.extend(b"\0" * (FRAME_BYTES - len(self.pending)))
                result.extend(self.feed(b""))
        if self.active:
            value = self._emit("stop")
            if value:
                result.append(value)
        return result
