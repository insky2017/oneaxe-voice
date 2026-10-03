"""GTK3 / Ayatana AppIndicator UI; runs with the distro's GI Python.

The DBusMenu root's opened/closed events are used because GNOME renders its
menu remotely: local Gtk.Menu visibility is not a reliable focus guard.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("Dbusmenu", "0.4")
from gi.repository import Gtk, Gdk, GLib, Pango, AyatanaAppIndicator3 as AppIndicator

from .config import ROOT, Settings
from .modes import MODES
from .preview_layout import anchor_from_position, monitor_for_window, overlay_position


POSITIONS = {"top": "顶部居中", "bottom": "底部居中", "left": "左侧居中",
             "right": "右侧居中", "custom": "自定义位置"}
MODEL_STATES = {"unloaded": "未加载", "loading": "加载中", "ready": "已就绪",
                "transcribing": "识别中",
                "unloading": "卸载中", "error": "加载失败", "unavailable": "服务未连接"}


def display_status(state):
    active = state.get("state", "idle") not in {"idle", "error", "stopped"}
    if state.get("last_error"):
        return "出现错误", "error"
    if state.get("warming"):
        return "加载模型", "loading"
    if active:
        if state.get("paste_paused") or state.get("clipboard_only"):
            return "仅复制", "copied"
        if state.get("capture_active"):
            return "正在听写", "recording"
        return "补齐文字", "finishing"
    model = state.get("model_status") or {}
    model_state = model.get("state", "unavailable")
    if state.get("model_unloading") or model_state == "unloading":
        return "卸载模型", "loading"
    if state.get("preparing") or model_state == "loading":
        return "加载模型", "loading"
    if state.get("model_error") or model.get("last_error") or model_state == "error":
        return "模型出错", "error"
    return {
        "ready": ("模型已就绪", "idle"),
        "transcribing": ("模型识别中", "finishing"),
        "loading": ("加载模型", "loading"),
        "unloaded": ("模型未加载", "idle"),
        "unavailable": ("服务未连接", "error"),
    }.get(model_state, ("模型状态未知", "error"))


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
        self.adjusting = False
        self.drag_origin = None
        self.target_geometry_id = None
        self.target_geometry = None
        self.preview_area = None
        self.placing = False
        self.was_active = False
        self.preview_dimensions = None
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
            item.connect("activate", self.choose_mode, mode)
            self.menu.append(item)
            self.radios[mode] = item
        self.create_model_menu()
        self.separator()
        self.auto = Gtk.CheckMenuItem(label="自动输入到原窗口")
        self.auto.connect("toggled", lambda item: self.configure(clipboard_only=not item.get_active()))
        self.menu.append(self.auto)
        self.show_preview = Gtk.CheckMenuItem(label="显示实时字幕")
        self.show_preview.connect("toggled", lambda item: self.configure(preview=item.get_active()))
        self.menu.append(self.show_preview)
        self.position_menu = Gtk.Menu()
        self.position_items = {}
        position_root = Gtk.MenuItem(label="字幕位置")
        position_root.set_submenu(self.position_menu)
        self.menu.append(position_root)
        group = None
        for key, label in POSITIONS.items():
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
            group = group or item
            item.connect("toggled", self.choose_position, key)
            self.position_menu.append(item)
            self.position_items[key] = item
        self.adjust_position = Gtk.CheckMenuItem(label="调整字幕位置")
        self.adjust_position.connect("toggled", self.toggle_adjustment)
        self.menu.append(self.adjust_position)
        self.item("复制本轮全文", lambda *_: self.command("copy"))
        self.item("设置…", self.settings_window)
        self.separator()
        self.item("退出", self.quit)
        self.menu.show_all()
        self.update_model_menu({}, connected=False)
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
        if item.get_active() and not self.syncing:
            self.configure(mode=mode)

    def create_model_menu(self):
        self.model_root = Gtk.MenuItem(label="模型 · 服务未连接")
        self.model_menu = Gtk.Menu()
        self.model_root.set_submenu(self.model_menu)
        self.menu.append(self.model_root)
        self.model_error = Gtk.MenuItem(label="")
        self.model_error.set_sensitive(False)
        self.model_menu.append(self.model_error)
        self.model_load = Gtk.MenuItem(label="立即加载")
        self.model_load.connect("activate", lambda *_: self.command("model_load"))
        self.model_menu.append(self.model_load)
        self.model_unload = Gtk.MenuItem(label="立即卸载")
        self.model_unload.connect("activate", lambda *_: self.command("model_unload"))
        self.model_menu.append(self.model_unload)
        self.model_menu.append(Gtk.SeparatorMenuItem())
        self.auto_unload = Gtk.CheckMenuItem(label="空闲 2 分钟后自动卸载")
        self.auto_unload.connect("toggled", lambda item: self.configure(auto_unload=item.get_active()))
        self.model_menu.append(self.auto_unload)
        self.update_model_menu({}, connected=False)

    def update_model_menu(self, state, connected=True):
        model = state.get("model_status") or {}
        model_state = model.get("state", "unavailable") if connected else "unavailable"
        if connected and state.get("model_unloading"):
            model_state = "unloading"
        elif connected and state.get("preparing"):
            model_state = "loading"
        selected = state.get("selected_mode", "vad")
        loaded_mode = model.get("mode")
        ready = model_state in {"ready", "transcribing"} and model.get("model_loaded") and loaded_mode == selected
        active = state.get("state", "idle") not in {"idle", "error", "stopped"}
        busy = (active or model.get("pc_busy", model.get("busy")) or state.get("preparing") or
                state.get("model_unloading") or model_state == "transcribing")
        transitioning = model_state in {"loading", "unloading"}
        connected = connected and model_state != "unavailable"
        self.model_root.set_label("模型 · " + MODEL_STATES.get(model_state, "状态未知"))
        error = state.get("model_error") or model.get("last_error")
        self.model_error.set_label(str(error)[:100] if error else "")
        self.model_error.set_visible(bool(error))
        self.model_load.set_sensitive(connected and not busy and not transitioning and not ready)
        self.model_unload.set_sensitive(connected and not busy and not transitioning and
                                        bool(model.get("model_loaded")))
        self.auto_unload.set_sensitive(connected)
        self.syncing = True
        self.auto_unload.set_active(bool(model.get("auto_unload", False)))
        self.syncing = False

    def choose_position(self, item, position):
        if item.get_active() and not self.syncing:
            self.configure(preview_position=position)

    def toggle_adjustment(self, item):
        self.adjusting = item.get_active()
        self.drag_origin = None
        if self.preview.get_window():
            self.preview.input_shape_combine_region(
                None if self.adjusting else __import__("cairo").Region())
            cursor = (Gdk.Cursor.new_for_display(Gdk.Display.get_default(), Gdk.CursorType.FLEUR)
                      if self.adjusting else None)
            self.preview.get_window().set_cursor(cursor)
        self.update_preview()

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
            self.update_model_menu({}, connected=False)
            self.preview.hide()
            return False
        was_active = self.was_active
        now_active = state.get("state") not in {"idle", "error", "stopped"}
        if now_active and not was_active:
            self.target_geometry_id = None
            self.target_geometry = None
        self.was_active = now_active
        self.state.update(state)
        state = self.state
        mode = state.get("mode", "vad")
        selected = state.get("selected_mode", "vad")
        active = state.get("state") not in {"idle", "error", "stopped"}
        status, icon = display_status(state)
        if not active:
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
        self.update_model_menu(state)
        self.syncing = True
        self.radios[selected].set_active(True)
        self.auto.set_active(not state.get("clipboard_only", False))
        self.show_preview.set_active(state.get("preview_enabled", True))
        self.position_items.get(state.get("preview_position", "top"), self.position_items["top"]).set_active(True)
        self.syncing = False
        self.set_icon(icon, f"{MODES[mode]} · {status}")
        self.update_preview()
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
        self.preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.preview_box.set_margin_top(10)
        self.preview_box.set_margin_bottom(10)
        self.preview_box.set_margin_start(16)
        self.preview_box.set_margin_end(16)
        self.preview_rows = []
        for _ in range(2):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            tag = Gtk.Label()
            tag.set_xalign(0)
            body = Gtk.Label()
            body.set_xalign(0)
            body.set_single_line_mode(True)
            body.set_ellipsize(Pango.EllipsizeMode.START)
            body.set_selectable(False)
            row.pack_start(tag, False, False, 0)
            row.pack_start(body, True, True, 0)
            self.preview_box.pack_start(row, False, False, 0)
            self.preview_rows.append((row, tag, body))
        self.preview_box.show()
        window.add(self.preview_box)
        window.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                          Gdk.EventMask.POINTER_MOTION_MASK)
        window.connect("button-press-event", self.begin_preview_drag)
        window.connect("motion-notify-event", self.move_preview_drag)
        window.connect("button-release-event", self.end_preview_drag)
        window.connect("size-allocate", lambda *_: self.place_preview())
        window.connect("map-event", lambda *_: GLib.idle_add(self.preview_mapped))
        return window

    def preview_mapped(self):
        if self.preview.get_window():
            self.preview.input_shape_combine_region(
                None if self.adjusting else __import__("cairo").Region())
            if self.adjusting:
                self.preview.get_window().set_cursor(
                    Gdk.Cursor.new_for_display(Gdk.Display.get_default(), Gdk.CursorType.FLEUR))
        self.place_preview()
        return False

    def target_window_geometry(self, target):
        if target == self.target_geometry_id:
            return self.target_geometry
        self.target_geometry_id = target
        self.target_geometry = None
        if not target or not re.fullmatch(r"[0-9]+", str(target)):
            return None
        try:
            result = subprocess.run(["xdotool", "getwindowgeometry", "--shell", str(target)],
                                    capture_output=True, text=True, check=True, timeout=.7)
            values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            self.target_geometry = tuple(int(values[key]) for key in ("X", "Y", "WIDTH", "HEIGHT"))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass
        return self.target_geometry

    def monitor_area(self):
        display = Gdk.Display.get_default()
        if display is None:
            return (0, 0, 800, 600)
        primary = display.get_primary_monitor()
        monitors = [primary] if primary else []
        monitors += [display.get_monitor(index) for index in range(display.get_n_monitors())
                     if display.get_monitor(index) is not primary]
        areas = []
        for monitor in monitors:
            area = monitor.get_workarea()
            areas.append((area.x, area.y, area.width, area.height))
        if not areas:
            return (0, 0, 800, 600)
        target = self.target_window_geometry(self.state.get("target_window"))
        return monitor_for_window(areas, target)

    def place_preview(self):
        if self.placing or self.drag_origin or not self.preview.get_visible():
            return
        self.placing = True
        try:
            area = self.monitor_area()
            self.preview_area = area
            position = self.state.get("preview_position", "top")
            anchor = self.state.get("preview_anchor")
            self.preview.move(*overlay_position(area, self.preview.get_size(), position, anchor))
        finally:
            self.placing = False

    def update_preview(self):
        state = self.state
        if self.adjusting:
            rows = [("调整字幕位置", "")]
        else:
            committed = state.get("committed_text", "")
            fixed = state.get("fixed_text", committed)
            pending = state.get("pending_text", "")
            delivery = state.get("delivery_state", "idle")
            queued = state.get("queued_text")
            if queued is None and fixed != committed:
                queued = fixed[len(committed):] if fixed.startswith(committed) else fixed
            if queued and delivery == "queued":
                waiting = queued
                rows = [("等待输入 ·", waiting[-160:])]
            elif committed:
                prefix = "已复制" if delivery == "copied" else "已发送"
                rows = [(prefix + " ·", committed[-160:])]
            else:
                rows = []
            if pending:
                rows.append(("待确认 ·", pending[-160:]))
            elif not rows and "pending_text" not in state and state.get("preview"):
                rows.append(("待确认 ·", state["preview"][-160:]))
            rows = rows[:2]
        active = state.get("state") not in {"idle", "error", "stopped"}
        if not self.adjusting and (not state.get("preview_enabled") or not active or not rows):
            self.preview.hide()
            return
        area = self.monitor_area()
        max_width = max(180, min(620, int(area[2] * .72)))
        max_width = min(max_width, max(100, area[2] - 32))
        context = self.preview_rows[0][1].get_pango_context()
        width = 0
        line_height = 0
        for index, (row, tag, body) in enumerate(self.preview_rows):
            if index < len(rows):
                tag_text, body_text = rows[index]
                tag.set_text(tag_text)
                body.set_text(body_text)
                layout = Pango.Layout.new(context)
                layout.set_text(tag_text + " " + body_text, -1)
                width = max(width, layout.get_pixel_size()[0])
                line_height = max(line_height, layout.get_pixel_size()[1])
                if tag_text.startswith("待确认"):
                    row.get_style_context().add_class("dim-label")
                else:
                    row.get_style_context().remove_class("dim-label")
                tag.show()
                body.show()
                row.show()
            else:
                row.hide()
        box_width = min(max_width, max(120, width + 32))
        box_height = 20 + len(rows) * max(20, line_height) + max(0, len(rows) - 1) * 2
        dimensions = (box_width, box_height)
        if dimensions != self.preview_dimensions:
            self.preview_dimensions = dimensions
            self.preview.set_size_request(*dimensions)
            self.preview.resize(*dimensions)
        self.preview.show()
        self.place_preview()

    def begin_preview_drag(self, _window, event):
        if self.adjusting and event.button == 1:
            x, y = self.preview.get_position()
            self.drag_origin = (event.x_root - x, event.y_root - y)
            return True
        return False

    def move_preview_drag(self, _window, event):
        if not self.drag_origin:
            return False
        area = self.preview_area or self.monitor_area()
        width, height = self.preview.get_size()
        x = area[0] + max(0, min(max(0, area[2] - width), round(event.x_root - self.drag_origin[0]) - area[0]))
        y = area[1] + max(0, min(max(0, area[3] - height), round(event.y_root - self.drag_origin[1]) - area[1]))
        self.preview.move(x, y)
        return True

    def end_preview_drag(self, _window, event):
        if not self.drag_origin or event.button != 1:
            return False
        self.move_preview_drag(_window, event)
        self.drag_origin = None
        anchor = anchor_from_position(self.preview_area or self.monitor_area(),
                                      self.preview.get_size(), self.preview.get_position())
        self.state.update(preview_position="custom", preview_anchor=anchor)
        self.configure(preview_position="custom", preview_anchor=anchor)
        return True

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
        stream_pause = Gtk.SpinButton.new_with_range(500, 2000, 100)
        stream_pause.set_value(config.get("stream_pause_ms", 1000))
        stream_pause.connect("value-changed", lambda item: self.configure(stream_pause_ms=item.get_value()))
        box.add(Gtk.Label(label="流式停顿收尾（毫秒）")); box.add(stream_pause)
        position = Gtk.ComboBoxText()
        for key, label in POSITIONS.items():
            position.append(key, label)
        position.set_active_id(config.get("preview_position", "top"))
        position.connect("changed", lambda item: self.configure(preview_position=item.get_active_id()))
        box.add(Gtk.Label(label="字幕位置")); box.add(position)
        box.add(Gtk.Label(label="自定义位置可从托盘菜单进入调整模式后拖动。"))
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
