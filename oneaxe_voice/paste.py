"""X11 clipboard delivery to the window where dictation was started."""

from dataclasses import dataclass
import re
import subprocess
import unicodedata


def command(*args: str) -> str:
    """Run an X11 query with a short timeout and no shell interpolation."""
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=3).stdout.strip()


@dataclass(frozen=True)
class Target:
    """Top-level window and focused child identity; never retain window titles."""

    window: str
    focus: str
    wm_class: str
    pid: str


def current_target() -> Target:
    """Read the current X11 destination without moving focus."""
    window = command("xdotool", "getactivewindow")
    properties = command("xprop", "-id", window, "WM_CLASS", "_NET_WM_PID")
    wm_class = next((line.partition(" = ")[2] for line in properties.splitlines()
                     if line.startswith("WM_CLASS(")), "")
    pid = next((line.partition(" = ")[2] for line in properties.splitlines()
                if line.startswith("_NET_WM_PID(")), "")
    return Target(window, command("xdotool", "getwindowfocus"), wm_class, pid)


def plain_text(value: str) -> str:
    """Keep dictation on one line and strip control characters before terminal paste."""
    value = " ".join(value.split())
    return "".join(c for c in value if unicodedata.category(c) not in {"Cc", "Cs"}).strip()


def paste_key(wm_class: str) -> str:
    """Use terminal clipboard bindings where Ctrl+V is a literal-input command."""
    terminals = r"gnome-terminal|kgx|konsole|kitty|alacritty|terminator|tilix|xfce4-terminal|wezterm"
    if re.search(terminals, wm_class, re.I):
        return "ctrl+shift+v"
    if re.search(r"xterm|urxvt|rxvt", wm_class, re.I):
        return "shift+Insert"
    # Shift+Insert is accepted by both VS Code's editor and integrated terminal.
    if re.search(r'"(?:code|code-oss|vscodium)"', wm_class, re.I):
        return "shift+Insert"
    return "ctrl+v"


def copy_text(value: str) -> None:
    """Own the clipboard without generating a key event."""
    subprocess.run(
        ["xclip", "-selection", "clipboard", "-in"], input=value.encode("utf-8"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=3,
    )


def append_delta(previous: str, text: str) -> str:
    """Keep separate English words readable when appending recognized segments."""
    if (previous and text and previous[-1].isascii() and text[0].isascii()
            and text[0].isalnum() and not previous[-1].isspace()):
        return " " + text
    return text


def deliver(value: str, target: Target, clipboard_only: bool = False, *, prefix: str = "") -> str:
    """Leave text on the clipboard; paste only while the recorded target still matches."""
    text = plain_text(value)
    if not text:
        return "empty"
    if prefix not in {"", " "}:
        raise ValueError("粘贴前缀只允许一个空格")
    copy_text(prefix + text)
    if clipboard_only:
        return "copied"
    try:
        if current_target() != target:
            return "focus_changed"
    except (OSError, subprocess.SubprocessError):
        return "focus_changed"
    subprocess.run(
        ["xdotool", "key", "--clearmodifiers", paste_key(target.wm_class)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=3,
    )
    return "pasted"
