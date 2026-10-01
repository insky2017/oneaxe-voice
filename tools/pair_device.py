#!/usr/bin/env python3
"""Import a private one-time pairing file without putting a token in argv."""

import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pairing_file", type=Path)
    parser.add_argument("--serial", required=True)
    args = parser.parse_args()
    adb = ["adb", "-s", args.serial]
    subprocess.run(adb + ["reverse", "tcp:18097", "tcp:18097"], check=True)
    subprocess.run(adb + ["shell", "run-as", "com.oneaxe.pocket.voicelab", "mkdir", "-p", "files"], check=True)
    with args.pairing_file.open("rb") as source:
        subprocess.run(
            adb + ["exec-in", "run-as", "com.oneaxe.pocket.voicelab", "tee", "files/pairing.json"],
            stdin=source,
            stdout=subprocess.DEVNULL,
            check=True,
        )
    print("Pairing file imported into app-private storage. Tap Import in Voice Lab.")


if __name__ == "__main__":
    main()
