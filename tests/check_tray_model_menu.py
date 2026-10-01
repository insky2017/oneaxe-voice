"""Exercise GTK model controls and tray rendering without desktop services."""

import argparse
from concurrent.futures import Future
import os
from pathlib import Path
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--xvfb", required=True)
    args = parser.parse_args()
    xvfb = subprocess.Popen([args.xvfb, ":102", "-screen", "0", "800x600x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.environ["DISPLAY"] = ":102"
        os.environ["NO_AT_BRIDGE"] = "1"
        time.sleep(.3)
        assert xvfb.poll() is None, "Xvfb failed to start"
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        from oneaxe_voice.config import ROOT, Settings
        from oneaxe_voice.modes import MODES
        from oneaxe_voice.tray import Tray

        tray = Tray.__new__(Tray)
        tray.menu = Gtk.Menu()
        tray.syncing = False
        tray.closing = False
        tray.polling = False
        tray.was_active = False
        tray.state = {}
        tray.last_icon = None
        actions = []
        tray.command = lambda action, **options: actions.append((action, options or None))
        tray.title = tray.item("", sensitive=False)
        tray.device = tray.item("", sensitive=False)
        tray.next_mode = tray.item("", sensitive=False)
        tray.error = tray.item("", sensitive=False)
        tray.toggle = tray.item("")
        tray.cancel = tray.item("")
        tray.radios = {}
        group = None
        for mode, label in MODES.items():
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
            group = group or item
            item.connect("activate", tray.choose_mode, mode)
            tray.menu.append(item)
            tray.radios[mode] = item
        tray.create_model_menu()
        tray.auto = Gtk.CheckMenuItem()
        tray.show_preview = Gtk.CheckMenuItem()
        tray.position_items = {"top": Gtk.RadioMenuItem.new_with_label_from_widget(None, "top")}
        tray.preview = Gtk.Window()
        tray.update_preview = lambda: None
        icons = []

        class Indicator:
            def set_icon_full(self, name, description):
                icons.append((name, description))

        tray.indicator = Indicator()
        tray.menu.show_all()

        def check(state, can_load, can_unload, status):
            tray.update_model_menu(state)
            assert tray.model_root.get_label() == "模型 · " + status
            assert tray.model_load.get_sensitive() == can_load
            assert tray.model_unload.get_sensitive() == can_unload

        base = {"state": "idle", "mode": "vad", "selected_mode": "vad",
                "capture_active": False, "warming": False, "preparing": False,
                "model_unloading": False, "last_error": None, "model_error": None,
                "paste_paused": False, "clipboard_only": False}
        check({**base, "model_status": {"state": "unloaded", "model_loaded": False,
                                        "auto_unload": False}}, True, False, "未加载")
        assert not tray.auto_unload.get_active()
        assert not actions, "status synchronization sent a command"
        tray.model_load.activate()
        assert actions.pop() == ("model_load", None)
        tray.auto_unload.set_active(True)
        assert actions.pop() == ("configure", {"auto_unload": True})

        ready = {"state": "ready", "model_loaded": True, "mode": "vad", "auto_unload": True}
        check({**base, "model_status": ready}, False, True, "已就绪")
        assert tray.auto_unload.get_active()
        tray.model_unload.activate()
        assert actions.pop() == ("model_unload", None)
        check({**base, "model_status": {**ready, "state": "loading"}}, False, False, "加载中")
        check({**base, "model_status": {**ready, "state": "unloading"}}, False, False, "卸载中")
        check({**base, "model_status": {**ready, "state": "error", "model_loaded": False}},
              True, False, "加载失败")
        check({**base, "state": "recording", "model_status": ready}, False, False, "已就绪")
        check({**base, "state": "finishing", "model_status": ready}, False, False, "已就绪")
        check({**base, "preparing": True, "model_status": ready}, False, False, "加载中")
        check({**base, "model_unloading": True, "model_status": ready}, False, False, "卸载中")
        check({**base, "preparing": True, "model_unloading": True, "model_status": ready},
              False, False, "卸载中")
        check({**base, "model_status": {**ready, "busy": True}}, False, False, "已就绪")
        check({**base, "model_status": {**ready, "state": "transcribing"}}, False, False, "识别中")
        check({**base, "selected_mode": "r2t2", "model_status": ready}, True, True, "已就绪")
        tray.update_model_menu({**base, "model_status": {**ready, "last_error": "model failed"}})
        assert tray.model_error.get_visible() and tray.model_error.get_label() == "model failed"
        tray.update_model_menu({**base, "model_error": "unload failed", "model_status": ready})
        assert tray.model_error.get_label() == "unload failed"
        tray.update_model_menu({**base, "model_status": ready})
        assert not tray.model_error.get_visible() and tray.model_error.get_label() == ""
        tray.update_model_menu(base, connected=False)
        assert tray.model_root.get_label() == "模型 · 服务未连接"
        assert not tray.model_load.get_sensitive() and not tray.model_unload.get_sensitive()
        assert not tray.auto_unload.get_sensitive()

        for mode in MODES:
            tray.radios[mode].activate()
            tray.radios[mode].activate()
            assert actions == [("configure", {"mode": mode})] * 2, (mode, actions)
            actions.clear()
        tray.syncing = True
        tray.radios["vad"].set_active(True)
        tray.radios["vad"].activate()
        assert not actions, "radio synchronization sent a command"
        tray.syncing = False

        with tempfile.TemporaryDirectory() as runtime:
            tray.settings = Settings(runtime_dir=Path(runtime))

            def check_display(changes, status, icon, mode="vad"):
                future = Future()
                future.set_result({**base, "model_status": ready, **changes})
                tray.polling = True
                tray.completed(future, True)
                assert tray.title.get_label() == f"OneAxe Voice · {status} · {MODES[mode].split(' · ')[0]}"
                expected_icon = f"oneaxe-{icon}-light"
                assert icons[-1] == (expected_icon, f"{MODES[mode]} · {status}"), icons[-1]
                assert (ROOT / "assets/icons" / (expected_icon + ".svg")).is_file()
                assert not actions, "rendering status sent a command"
                assert not tray.polling

            for model_state, status, icon in (
                ("ready", "模型已就绪", "idle"),
                ("unloaded", "模型未加载", "idle"),
                ("loading", "加载模型", "loading"),
                ("unloading", "卸载模型", "loading"),
                ("error", "模型出错", "error"),
                ("unavailable", "服务未连接", "error"),
                ("transcribing", "模型识别中", "finishing"),
            ):
                check_display({"model_status": {**ready, "state": model_state}}, status, icon)
            check_display({"preparing": True}, "加载模型", "loading")
            check_display({"model_unloading": True, "preparing": True}, "卸载模型", "loading")
            check_display({"model_error": "load failed"}, "模型出错", "error")
            check_display({"model_status": {**ready, "last_error": "worker failed"}}, "模型出错", "error")
            check_display({}, "模型已就绪", "idle")
            for model_state in ("ready", "loading", "unloading", "error", "unavailable"):
                check_display({"state": "recording", "capture_active": True,
                               "model_status": {**ready, "state": model_state}}, "正在听写", "recording")
                assert tray.toggle.get_sensitive() and tray.cancel.get_sensitive()
                assert not tray.model_load.get_sensitive() and not tray.model_unload.get_sensitive()
            check_display({"state": "starting", "capture_active": True, "warming": True}, "加载模型", "loading")
            check_display({"state": "finishing"}, "补齐文字", "finishing")
            assert not tray.toggle.get_sensitive() and tray.cancel.get_sensitive()
            check_display({"state": "recording", "capture_active": True, "clipboard_only": True}, "仅复制", "copied")
            check_display({"state": "recording", "capture_active": True, "paste_paused": True}, "仅复制", "copied")
            check_display({"last_error": "capture failed"}, "出现错误", "error")
            check_display({"selected_mode": "r2t2"}, "模型已就绪", "idle", mode="r2t2")
            check_display({"state": "recording", "capture_active": True, "selected_mode": "r2t2"},
                          "正在听写", "recording")
            assert tray.next_mode.get_visible() and tray.next_mode.get_label() == "下一轮：" + MODES["r2t2"]
            failed = Future()
            failed.set_exception(ConnectionError("desktop unavailable"))
            tray.polling = True
            tray.completed(failed, True)
            assert tray.title.get_label() == "OneAxe Voice · 服务未连接"
            assert icons[-1] == ("oneaxe-error-light", "服务未连接")
            assert not tray.model_load.get_sensitive() and not tray.model_unload.get_sensitive()
            assert not tray.auto_unload.get_sensitive()
            assert not tray.polling
        tray.preview.destroy()
        print("GTK tray: lifecycle controls, title/icons, capture priority, same-mode activation passed")
    finally:
        xvfb.terminate()
        try:
            xvfb.wait(timeout=3)
        except subprocess.TimeoutExpired:
            xvfb.kill()
            xvfb.wait()


if __name__ == "__main__":
    main()
