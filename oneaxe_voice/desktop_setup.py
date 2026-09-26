"""Install a checkout-specific desktop unit and one GNOME custom shortcut."""

import ast
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

from .config import ROOT, Settings
from .desktop import preferences
from .install import install as install_api, render_unit, systemd_path, systemd_quote

MEDIA_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/oneaxe-voice/"
CUSTOM_SCHEMA = MEDIA_SCHEMA + ".custom-keybinding:" + CUSTOM_PATH


def run(*args: str) -> str:
    return subprocess.run(args, text=True, capture_output=True, check=True, timeout=20).stdout.strip()


def bindings() -> list[str]:
    value = run("gsettings", "get", MEDIA_SCHEMA, "custom-keybindings")
    return [] if value == "@as []" else ast.literal_eval(value)


def verify_shortcut(shortcut: str) -> None:
    """Support the three offered bindings and refuse an existing GNOME assignment."""
    if shortcut not in {"F8", "<Control><Alt>space", "<Super><Alt>space"}:
        raise ValueError("支持 F8、<Control><Alt>space、<Super><Alt>space")
    for path in bindings():
        if path != CUSTOM_PATH:
            value = run("gsettings", "get", MEDIA_SCHEMA + ".custom-keybinding:" + path, "binding")
            if ast.literal_eval(value).lower() == shortcut.lower():
                raise ValueError("该快捷键已有 GNOME 自定义绑定，请先调整冲突")
    for schema in ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings", MEDIA_SCHEMA):
        for line in run("gsettings", "list-recursively", schema).splitlines():
            value = line.split(" ", 2)[-1]
            if value.startswith("["):
                if shortcut.lower() in [str(s).lower() for s in ast.literal_eval(value)]:
                    raise ValueError("该快捷键已有 GNOME 系统绑定，请先调整冲突")


def install_desktop(settings: Settings, shortcut: str) -> None:
    """Set up local units without enabling login startup or touching VPlus."""
    if os.environ.get("XDG_SESSION_TYPE") != "x11":
        raise ValueError("目前仅支持 GNOME X11 桌面")
    for tool in ("parec", "pactl", "xdotool", "xprop", "xclip", "notify-send", "gsettings"):
        if shutil.which(tool) is None:
            raise ValueError(f"缺少桌面依赖：{tool}")
    verify_shortcut(shortcut)
    api_unit = install_api()
    project_dir = ROOT.resolve()
    template = (project_dir / "systemd/oneaxe-voice-desktop.service").read_text()
    environment = {key: os.environ[key] for key in
                   ("DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS", "PULSE_SERVER")
                   if key in os.environ}
    environment["ONEAXE_VOICE_RUNTIME_DIR"] = str(settings.runtime_dir)
    # Environment= performs specifier expansion, but does not expand dollar signs.
    env_lines = "\n".join("Environment=" + systemd_quote(key + "=" + value).replace("$$", "$")
                          for key, value in environment.items())
    rendered = render_unit(template, {
        "PROJECT_DIR": systemd_path(project_dir),
        "DESKTOP_COMMAND": systemd_quote(project_dir / "bin/oneaxe-voice") + " desktop-run",
        "DESKTOP_ENVIRONMENT": env_lines,
    })
    unit = api_unit.with_name("oneaxe-voice-desktop.service")
    with tempfile.NamedTemporaryFile("w", dir=unit.parent, delete=False) as output:
        output.write(rendered)
        temporary = Path(output.name)
    try:
        temporary.replace(unit)
    finally:
        temporary.unlink(missing_ok=True)
    value = preferences(settings)
    value["shortcut"] = shortcut
    path = settings.runtime_dir / "desktop.json"
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)
    command = shlex.join([str(project_dir / "bin/oneaxe-voice"), "toggle"])
    run("gsettings", "set", CUSTOM_SCHEMA, "name", "OneAxe Voice 听写")
    run("gsettings", "set", CUSTOM_SCHEMA, "command", command)
    run("gsettings", "set", CUSTOM_SCHEMA, "binding", shortcut)
    existing = bindings()
    if CUSTOM_PATH not in existing:
        run("gsettings", "set", MEDIA_SCHEMA, "custom-keybindings", repr(existing + [CUSTOM_PATH]))
    run("systemctl", "--user", "daemon-reload")
    run("systemctl", "--user", "restart", "oneaxe-voice-desktop.service")
    print(f"已安装桌面听写：{shortcut} 开始/结束录音；服务未设置登录自启。")


def remove_shortcut() -> None:
    """Remove only this project's binding and stop its recording controller."""
    run("gsettings", "set", MEDIA_SCHEMA, "custom-keybindings",
        repr([path for path in bindings() if path != CUSTOM_PATH]))
    run("systemctl", "--user", "stop", "oneaxe-voice-desktop.service")
    print("已移除 OneAxe Voice 快捷键并停止桌面录音服务。")
