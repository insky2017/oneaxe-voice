"""Private GTK3 input target for the opt-in Linux client end-to-end test."""

import argparse
import json
import os
from pathlib import Path
import tempfile

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402


def private_json(path, value):
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".target-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()
    args.state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = {
        "text": "", "changes": 0, "return_key_count": 0,
        "entry_has_focus": False, "entry_is_focus": False,
        "window_is_active": False, "window_has_toplevel_focus": False,
    }
    window = Gtk.Window(title=args.title)
    window.set_wmclass("oneaxe-voice-linux-e2e", "OneAxeVoiceLinuxE2E")
    window.set_default_size(620, 140)
    window.set_border_width(16)
    entry = Gtk.Entry()
    entry.set_hexpand(True)
    window.add(entry)

    def changed(widget):
        state["text"] = widget.get_text()
        state["changes"] += 1
        private_json(args.state, state)

    def key_pressed(_widget, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            state["return_key_count"] += 1
            private_json(args.state, state)
        return False

    def focus_changed(_widget, _property):
        state.update(
            entry_has_focus=bool(entry.has_focus()),
            entry_is_focus=bool(entry.is_focus()),
            window_is_active=bool(window.is_active()),
            window_has_toplevel_focus=bool(window.has_toplevel_focus()),
        )
        private_json(args.state, state)

    entry.connect("changed", changed)
    entry.connect("key-press-event", key_pressed)
    entry.connect("notify::has-focus", focus_changed)
    window.connect("notify::is-active", focus_changed)
    window.connect("notify::has-toplevel-focus", focus_changed)
    window.connect("destroy", Gtk.main_quit)
    private_json(args.state, state)
    window.show_all()
    entry.grab_focus()
    Gtk.main()


if __name__ == "__main__":
    main()
