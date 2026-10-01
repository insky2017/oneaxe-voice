#!/usr/bin/env python3
"""Generate public speech fixtures locally; no microphone or network is used."""

import argparse
from array import array
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import wave


SENTENCES = [
    "This is a phone voice test.",
    "I want to connect to the remote server.",
    "The network is ready.",
]
RATE = 16000


def save(path, samples):
    encoded = array("h", samples)
    if sys.byteorder != "little":
        encoded.byteswap()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(encoded.tobytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    samples = array("h", [0] * (RATE // 2))
    with tempfile.TemporaryDirectory(prefix="voice-fixture-source-") as work:
        for index, sentence in enumerate(SENTENCES):
            path = Path(work) / f"sentence-{index}.wav"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "lavfi", "-i", f"flite=text='{sentence}':voice=slt",
                "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", str(path),
            ], check=True)
            with wave.open(str(path), "rb") as source:
                part = array("h", source.readframes(source.getnframes()))
            if sys.byteorder != "little":
                part.byteswap()
            samples.extend(part)
            if index + 1 < len(SENTENCES):
                samples.extend([0] * RATE)
    # Last phrase has no added silence, exercising explicit stop/tail flush.
    report = {"sentences": SENTENCES, "sample_rate": RATE, "files": {}}
    for name, gain in [("normal.wav", 1.0), ("quiet.wav", 0.05)]:
        values = array("h", (int(value * gain) for value in samples))
        save(args.output_dir / name, values)
        rms = math.sqrt(sum(value * value for value in values) / len(values))
        report["files"][name] = {
            "seconds": len(values) / RATE,
            "rms": round(rms, 2),
            "peak": max(abs(value) for value in values),
        }
    save(args.output_dir / "silence.wav", [0] * (RATE * 3))
    report["files"]["silence.wav"] = {"seconds": 3, "rms": 0, "peak": 0}
    (args.output_dir / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
