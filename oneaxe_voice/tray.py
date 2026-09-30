"""GTK3 / Ayatana AppIndicator UI; runs with the distro's GI Python.

The DBusMenu root's opened/closed events are used because GNOME renders its
menu remotely: local Gtk.Menu visibility is not a reliable focus guard.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import socket
import subprocess

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("Dbusmenu", "0.4")
from gi.repository import Gtk, Gdk, GLib, AyatanaAppIndicator3 as AppIndicator

from .config import ROOT, Settings
from .modes import MODES


def request(settings, action, **options):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(settings.runtime_dir / "desktop.sock"))
        client.sendall((json.dumps({"action": action, **options}) + "\n").encode())
        with client.makefile("rb") as stream:
            value = json.loads(stream.readline(65536))
    if "error" in value:
        raise RuntimeError(value["error"])
    return value


class Tray:
    def __init__(self):
        self.settings = Settings.from_env()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.polling = False
        self.syncing = False
        self.menu_open = False
        self.state = {}
        self.last_icon = None
        self.closing = False
        self.indicator = AppIndicator.Indicator.new(
            "oneaxe-voice", "audio-input-microphone", AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_title("OneAxe Voice")
        self.indicator.set_icon_theme_path(str(ROOT / "assets/icons"))
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.title = self.item("OneAxe Voice · 连接中", sensitive=False)
        self.device = self.item("麦克风：DJI Mic", sensitive=False)
        self.next_mode = self.item("", sensitive=False)
        self.error = self.item("", sensitive=False)
        self.separator()
        self.toggle = self.item("开始听写    F8", lambda *_: self.command("toggle"))
        self.cancel = self.item("取消本轮后续输入", lambda *_: self.command("cancel"))
        self.separator()
        self.item("听写模式", sensitive=False)
        self.radios = {}
        group = None
        for mode, label in MODES.items():
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
            group = group or item
            item.connect("toggled", self.choose_mode, mode)
            self.menu.append(item)
            self.radios[mode] = item
        self.separator()
        self.auto = Gtk.CheckMenuItem(label="自动输入到原窗口")
        self.auto.connect("toggled", lambda item: self.configure(clipboard_only=not item.get_active()))
        self.menu.append(self.auto)
        self.show_preview = Gtk.CheckMenuItem(label="显示实时字幕")
        self.show_preview.connect("toggled", lambda item: self.configure(preview=item.get_active()))
        self.menu.append(self.show_preview)
        self.item("复制本轮全文", lambda *_: self.command("copy"))
        self.item("设置…", self.settings_window)
        self.separator()
        self.item("退出", self.quit)
        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        server = self.indicator.get_property("dbus-menu-server")
        self.menu_root = server.get_property("root-node")
        self.menu_root.connect("event", self.menu_event)
        self.preview = self.make_preview()
        GLib.timeout_add(250, self.poll)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.quit)
        self.poll()

    def item(self, label, callback=None, sensitive=True):
        item = Gtk.MenuItem(label=label)
        item.set_sensitive(sensitive)
        if callback:
            item.connect("activate", callback)
        self.menu.append(item)
        return item

    def separator(self):
        self.menu.append(Gtk.SeparatorMenuItem())

    def menu_event(self, _root, event, _value, _timestamp):
        if event in {"opened", "closed"}:
            self.menu_open = event == "opened"
            self.command("menu", opened=self.menu_open)
        return False

    def command(self, action, **options):
        if self.closing:
            return
        future = self.executor.submit(request, self.settings, action, **options)
        future.add_done_callback(lambda done: GLib.idle_add(self.completed, done, False))

    def configure(self, **options):
        if not self.syncing:
            self.command("configure", **options)

    def choose_mode(self, item, mode):
        if item.get_active():
            self.configure(mode=mode)

    def poll(self):
        if not self.polling and not self.closing:
            self.polling = True
            future = self.executor.submit(request, self.settings, "ui", menu_open=self.menu_open)
            future.add_done_callback(lambda done: GLib.idle_add(self.completed, done, True))
        return not self.closing

    def completed(self, future, polling):
        if polling:
            self.polling = False
        if self.closing:
            return False
        try:
            state = future.result()
        except Exception:
            self.title.set_label("OneAxe Voice · 服务未连接")
            self.set_icon("error", "服务未连接")
            self.preview.hide()
            return False
        self.state.update(state)
        state = self.state
        mode = state.get("mode", "vad")
        selected = state.get("selected_mode", "vad")
        active = state.get("state") not in {"idle", "error", "stopped"}
        if state.get("last_error"):
            status, icon = "出现错误", "error"
        elif state.get("warming") or state.get("preparing"):
            status, icon = "加载模型", "loading"
        elif state.get("paste_paused") or state.get("clipboard_only") and active:
            status, icon = "仅复制", "copied"
        elif state.get("capture_active"):
            status, icon = "正在听写", "recording"
        elif active:
            status, icon = "补齐文字", "finishing"
        else:
            status, icon = "待机", "idle"
            mode = selected
        self.title.set_label(f"OneAxe Voice · {status} · {MODES.get(mode, mode).split(' · ')[0]}")
        self.device.set_label("麦克风：" + state.get("source", "DJI Mic"))
        self.error.set_label((state.get("last_error") or "")[:100])
        self.error.set_visible(bool(state.get("last_error")))
        self.next_mode.set_label("下一轮：" + MODES[selected])
        self.next_mode.set_visible(active and selected != mode)
        self.toggle.set_label("结束听写并补齐尾部    F8" if active else "开始听写    F8")
        self.toggle.set_sensitive(not active or bool(state.get("capture_active")))
        self.cancel.set_sensitive(active)
        self.syncing = True
        self.radios[selected].set_active(True)
        self.auto.set_active(not state.get("clipboard_only", False))
        self.show_preview.set_active(state.get("preview_enabled", True))
        self.syncing = False
        self.set_icon(icon, f"{MODES[mode]} · {status}")
        if state.get("preview_enabled") and active and state.get("preview"):
            self.preview_label.set_text(state["preview"][-220:])
            self.preview.show_all()
        else:
            self.preview.hide()
        return False

    def set_icon(self, icon, description):
        config_file = self.settings.runtime_dir / "desktop.json"
        try:
            theme = json.loads(config_file.read_text()).get("icon_theme", "light")
        except (OSError, ValueError):
            theme = "light"
        name = f"oneaxe-{icon}-{theme}"
        if self.last_icon != (name, description):
            self.indicator.set_icon_full(name, description)
            self.last_icon = (name, description)

    def make_preview(self):
        window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        window.set_title("OneAxe Voice 实时字幕")
        window.set_wmclass("oneaxe-voice-preview", "OneAxeVoicePreview")
        window.set_decorated(False)
        window.set_accept_focus(False)
        window.set_focus_on_map(False)
        window.set_skip_taskbar_hint(True)
        window.set_skip_pager_hint(True)
        window.set_keep_above(True)
        window.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.preview_label = Gtk.Label()
        self.preview_label.set_line_wrap(True)
        self.preview_label.set_max_width_chars(65)
        self.preview_label.set_width_chars(55)
        self.preview_label.set_margin_top(12)
        self.preview_label.set_margin_bottom(12)
        self.preview_label.set_margin_start(18)
        self.preview_label.set_margin_end(18)
        window.add(self.preview_label)
        def place(*_):
            display = Gdk.Display.get_default()
            monitor = display.get_primary_monitor() or display.get_monitor(0)
            area = monitor.get_workarea()
            width, height = window.get_size()
            window.move(area.x + (area.width - width) // 2, area.y + area.height - height - 55)
            window.input_shape_combine_region(__import__("cairo").Region())
        window.connect("size-allocate", place)
        return window

    def settings_window(self, *_):
        dialog = Gtk.Dialog(title="OneAxe Voice 设置")
        dialog.add_button("关闭", Gtk.ResponseType.CLOSE)
        dialog.connect("response", lambda widget, *_: widget.destroy())
        box = dialog.get_content_area()
        box.set_spacing(12)
        box.set_border_width(18)
        box.add(Gtk.Label(label="设置在下一轮听写生效；F8 开始 / 结束。"))
        config_path = self.settings.runtime_dir / "desktop.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        source = Gtk.ComboBoxText()
        source.append("auto", "自动选择 DJI Mic")
        try:
            result = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True,
                                    text=True, check=True, timeout=3)
            for device in json.loads(result.stdout):
                if not device["name"].endswith(".monitor"):
                    source.append(device["name"], device["description"])
        except Exception:
            pass
        source.set_active_id(config.get("source") or "auto")
        source.connect("changed", lambda item: self.configure(source=None if item.get_active_id() == "auto" else item.get_active_id()))
        box.add(Gtk.Label(label="输入设备")); box.add(source)
        pause = Gtk.SpinButton.new_with_range(300, 2000, 100)
        pause.set_value(config.get("pause_ms", 700))
        pause.connect("value-changed", lambda item: self.configure(pause_ms=item.get_value()))
        box.add(Gtk.Label(label="稳听停顿阈值（毫秒，仅影响稳听）")); box.add(pause)
        dark = Gtk.CheckButton(label="使用深色图标（浅色顶栏）")
        dark.set_active(config.get("icon_theme") == "dark")
        dark.connect("toggled", lambda item: self.configure(icon_theme="dark" if item.get_active() else "light"))
        box.add(dark)
        box.add(Gtk.Label(label="模型在本机运行；口头语按识别原文保留。"))
        dialog.show_all()

    def quit(self, *_):
        if self.closing:
            return False
        self.closing = True
        self.preview.hide()
        def stop():
            try:
                request(self.settings, "cancel")
                request(self.settings, "menu", opened=False)
            except Exception:
                pass
            GLib.idle_add(Gtk.main_quit)
        self.executor.submit(stop)
        self.indicator.set_status(AppIndicator.IndicatorStatus.PASSIVE)
        return False


def main():
    Tray()
    Gtk.main()


if __name__ == "__main__":
    main()
