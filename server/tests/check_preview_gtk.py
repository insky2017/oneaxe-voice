"""Isolated GTK smoke check for overlay resizing; no desktop service or GPU."""

import argparse
import os
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--xvfb", required=True)
    parser.add_argument("--screenshot-dir", type=Path)
    args = parser.parse_args()
    xvfb = subprocess.Popen([args.xvfb, ":101", "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.environ["DISPLAY"] = ":101"
        time.sleep(.3)
        if xvfb.poll() is not None:
            raise RuntimeError("Xvfb failed to start")
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gtk, Gdk
        from oneaxe_voice.tray import Tray

        tray = Tray.__new__(Tray)
        tray.adjusting = False
        tray.drag_origin = None
        tray.target_geometry_id = None
        tray.target_geometry = None
        tray.preview_area = None
        tray.placing = False
        tray.preview_dimensions = None
        tray.state = {"state": "recording", "preview_enabled": True, "preview_position": "top",
                      "preview_anchor": [0.5, 0.05], "target_window": None,
                      "committed_text": "这是一个已经发送的很长句子，" * 20,
                      "fixed_text": "这是一个已经发送的很长句子，" * 20,
                      "pending_text": "这是还没有确认的后续内容，" * 20,
                      "delivery_state": "pasted"}
        tray.preview = tray.make_preview()

        def pump():
            deadline = time.monotonic() + .25
            while time.monotonic() < deadline:
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
                time.sleep(.01)

        def screenshot(name):
            if args.screenshot_dir:
                args.screenshot_dir.mkdir(parents=True, exist_ok=True)
                window = tray.preview.get_window()
                pixbuf = Gdk.pixbuf_get_from_window(window, 0, 0, *tray.preview.get_size())
                if pixbuf is None:
                    raise AssertionError("GTK overlay screenshot is blank")
                pixbuf.savev(str(args.screenshot_dir / name), "png", [], [])

        tray.update_preview()
        pump()
        long_size = tray.preview.get_size()
        assert long_size[0] <= 620, long_size
        assert all(row.get_visible() for row, _, _ in tray.preview_rows)
        assert tray.preview_rows[0][1].get_text() == "已发送 ·"
        assert tray.preview_rows[1][1].get_text() == "待确认 ·"
        screenshot("preview-long.png")

        tray.state.update(committed_text="好。", fixed_text="好。", pending_text="")
        tray.update_preview()
        pump()
        short_size = tray.preview.get_size()
        assert short_size[0] < long_size[0], (long_size, short_size)
        assert short_size[1] < long_size[1], (long_size, short_size)
        assert not tray.preview_rows[1][0].get_visible()
        assert tray.preview_rows[0][1].get_text() == "已发送 ·"
        screenshot("preview-short.png")
        tray.state.update(committed_text="已发送" * 120, fixed_text="已发送" * 120,
                          queued_text="追加的标点。", delivery_state="queued")
        tray.update_preview()
        pump()
        assert tray.preview_rows[0][1].get_text() == "等待输入 ·"
        assert tray.preview_rows[0][2].get_text() == "追加的标点。"
        print({"long": long_size, "short": short_size,
               "tags": [row[1].get_text() for row in tray.preview_rows]})
        tray.preview.destroy()
    finally:
        xvfb.terminate()
        try:
            xvfb.wait(timeout=3)
        except subprocess.TimeoutExpired:
            xvfb.kill()
            xvfb.wait()


if __name__ == "__main__":
    main()
