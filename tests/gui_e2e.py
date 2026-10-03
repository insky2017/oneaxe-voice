"""Opt-in GTK/F9/Pulse/WSS acceptance test for the independent Linux client.

Run only after the user pauses dictation. This test never changes hotkeys,
the Voice server, F8, VPlus, or shared models. Reports contain counts and
hashes; the private target files and injected audio are deleted on exit.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import wave


APP_ID = "org.oneaxe.VoiceLinux"
MAX_BUFFERED_SAMPLES = 0
STATE_CODES = {"idle": 0, "connecting": 1, "recording": 2, "finishing": 3, "error": 4}
GTK_FOCUS_KEYS = (
    "entry_has_focus", "entry_is_focus", "window_is_active", "window_has_toplevel_focus",
)
FOCUS_CONTEXT = None
INITIAL_FOCUS = {}
LAST_COMMAND_FAILURE = None


class TestFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(str(code))


def require(condition, code):
    if not condition:
        raise TestFailure(code)


def command(*args, input_bytes=None, timeout=10, check=True):
    global LAST_COMMAND_FAILURE
    try:
        result = subprocess.run(
            [str(arg) for arg in args],
            input=input_bytes,
            stdout=subprocess.PIPE if input_bytes is None else subprocess.DEVNULL,
            stderr=subprocess.PIPE if input_bytes is None else subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise TestFailure(10) from None
    if check and result.returncode:
        LAST_COMMAND_FAILURE = {
            "program": Path(str(args[0])).name,
            "operation": str(args[1]) if len(args) > 1 and str(args[1]) != "-c" else "",
            "exit_code": result.returncode,
        }
        raise TestFailure(11)
    return result


def output(*args, **kwargs):
    return command(*args, **kwargs).stdout.decode("utf-8").strip()


def wait_for(predicate, timeout=15, code=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise TestFailure(code)


def client_running():
    value = output(
        "gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
        "--object-path", "/org/freedesktop/DBus", "--method",
        "org.freedesktop.DBus.NameHasOwner", APP_ID,
    )
    require(value in ("(true,)", "(false,)"), 13)
    return value == "(true,)"


def client_status(binary):
    global MAX_BUFFERED_SAMPLES
    observe_focus("before_status")
    require(client_running(), 14)
    try:
        value = json.loads(output(binary, "--status", timeout=5))
    except (ValueError, UnicodeError):
        raise TestFailure(15) from None
    require(isinstance(value, dict), 15)
    require(value.get("state") in ("idle", "connecting", "recording", "finishing", "error"), 15)
    require(all(isinstance(value.get(key), bool) for key in ("active", "capture_active", "configured")), 15)
    buffered = value.get("buffered_seconds")
    require(isinstance(buffered, (int, float)) and not isinstance(buffered, bool), 15)
    require(math.isfinite(buffered) and 0 <= buffered <= 2, 54)
    MAX_BUFFERED_SAMPLES = max(MAX_BUFFERED_SAMPLES, round(buffered * 16000))
    observe_focus("after_status")
    return value


def inactive(status):
    return not status["active"] and not status["capture_active"]


def idle_client(binary, timeout=20):
    def check():
        value = client_status(binary)
        require(value["state"] != "error", 16)
        return value if inactive(value) else None
    return wait_for(check, timeout=timeout, code=17)


def ready_capture(binary):
    def check():
        value = client_status(binary)
        require(value["state"] != "error", 18)
        if value["state"] == "recording" and value["capture_active"]:
            if FOCUS_CONTEXT is not None:
                FOCUS_CONTEXT["report"].setdefault("recording_ready", []).append({
                    "stage_code": FOCUS_CONTEXT["stage"],
                    "delivery_state": value.get("delivery_state", "none"),
                    "active_expected": FOCUS_CONTEXT["stats"].get("after_status_active_expected", 0),
                    "focus_expected": FOCUS_CONTEXT["stats"].get("after_status_focus_expected", 0),
                })
            return value
        return None
    return wait_for(check, timeout=30, code=19)


def target_state(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    require(isinstance(value, dict) and isinstance(value.get("text"), str), 20)
    require(isinstance(value.get("return_key_count"), int), 20)
    return value


def compact(text):
    return re.sub(r"[\s，。！？、,.!?；;：:\u2026]", "", text)


def phrase_count(text, phrase):
    return compact(text).count(compact(phrase))


def digest(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def private_write(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".oneaxe-e2e-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def window_for(process):
    require(process.poll() is None, 21)
    found = command(
        "xdotool", "search", "--all", "--onlyvisible", "--pid", process.pid,
        "--class", "^OneAxeVoiceLinuxE2E$", check=False,
    )
    if found.returncode == 1:
        return None
    require(found.returncode == 0, 22)
    ids = found.stdout.decode("ascii").splitlines()
    require(len(ids) <= 1, 22)
    if not ids:
        return None
    properties = output("xprop", "-id", ids[0], "_NET_WM_PID")
    pid = re.search(r"_NET_WM_PID\([^)]*\)\s*=\s*(\d+)", properties)
    require(pid is not None and int(pid.group(1)) == process.pid, 56)
    return ids[0]


def activate(window):
    command("xdotool", "windowactivate", "--sync", window)
    wait_for(lambda: output("xdotool", "getactivewindow", check=False) == window, code=23)


def observe_focus(moment):
    if FOCUS_CONTEXT is None:
        return
    stats = FOCUS_CONTEXT["stats"]
    stats["checks"] += 1
    try:
        active = output("xdotool", "getactivewindow")
        raw = command("xdotool", "getwindowfocus", "-f", check=False)
        raw_focus = raw.stdout.decode("ascii", errors="replace").strip()
        stats["raw_focus_checks"] += 1
        if raw.returncode == 0 and raw_focus.isdigit():
            stats["root_focus_checks"] += int(int(raw_focus) <= 1)
        else:
            stats["read_failures"] += 1
        focus = output("xdotool", "getwindowfocus")
        active_expected = int(active == FOCUS_CONTEXT["window"])
        focus_expected = int(focus == FOCUS_CONTEXT["focus"])
        stats["matching_active_checks"] += active_expected
        stats["matching_focus_checks"] += focus_expected
        stats[moment + "_active_expected"] = active_expected
        stats[moment + "_focus_expected"] = focus_expected
        state = target_state(FOCUS_CONTEXT["state_path"])
        entry_focused = int(state is not None and state.get("entry_has_focus") is True)
        stats["entry_has_focus_checks"] += entry_focused
        for key in GTK_FOCUS_KEYS:
            stats[moment + "_" + key] = int(state is not None and state.get(key) is True)
    except Exception:
        stats["read_failures"] += 1


def prepare_target(window, state_path, report, stage):
    """Activate only before the intended test action; never refocus during dictation."""
    global FOCUS_CONTEXT
    stats = report.setdefault("focus_checks", {}).setdefault(str(stage), {
        "checks": 0, "matching_active_checks": 0, "matching_focus_checks": 0,
        "entry_has_focus_checks": 0, "raw_focus_checks": 0,
        "root_focus_checks": 0, "read_failures": 0,
        "context_switches": 0, "preparation_checks": 0,
    })
    stats["context_switches"] += 1
    activate(window)
    settled_focus = []

    def settled():
        active = output("xdotool", "getactivewindow", check=False)
        focus = output("xdotool", "getwindowfocus", check=False)
        state = target_state(state_path)
        stats["preparation_checks"] += 1
        stats["prepare_active_expected"] = int(active == window)
        stats["prepare_focus_matches_window"] = int(focus == window)
        stats["prepare_focus_is_root"] = int(focus.isdigit() and int(focus) <= 1)
        for key in GTK_FOCUS_KEYS:
            stats["prepare_" + key] = int(state is not None and state.get(key) is True)
        valid = active == window and focus.isdigit() and int(focus) > 1
        valid = valid and state is not None and state.get("entry_has_focus") is True
        if not valid or settled_focus and focus != settled_focus[-1]:
            settled_focus.clear()
        if valid:
            settled_focus.append(focus)
        stats["prepare_consecutive_ready"] = len(settled_focus)
        return focus if len(settled_focus) >= 3 else None

    focus = wait_for(settled, timeout=5, code=58)
    INITIAL_FOCUS.setdefault(window, focus)
    FOCUS_CONTEXT = {
        "window": window, "focus": INITIAL_FOCUS[window], "state_path": state_path,
        "stats": stats, "report": report, "stage": stage,
    }
    observe_focus("prepared")


def clear_target(window, path):
    activate(window)
    wait_for(
        lambda: output("xdotool", "getactivewindow") == window
        and (value := target_state(path)) is not None and value.get("entry_has_focus") is True,
        timeout=5, code=58,
    )
    command("xdotool", "key", "--clearmodifiers", "ctrl+a", "BackSpace")
    wait_for(lambda: (value := target_state(path)) is not None and value["text"] == "", code=24)


def copy_from_client(binary, pid):
    """Activate the real GTK Copy button; do not read internal transcript state."""
    command(binary, "--show")
    script = r'''
import sys, time, gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
pid = int(sys.argv[1])
deadline = time.monotonic() + 6
while time.monotonic() < deadline:
    desktop = Atspi.get_desktop(0)
    roots = [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]
    stack = [item for item in roots if item is not None and item.get_process_id() == pid]
    while stack:
        item = stack.pop()
        if item.get_role() == Atspi.Role.PUSH_BUTTON and item.get_name() == "复制":
            if item.get_n_actions() and item.do_action(0):
                sys.exit(0)
        stack.extend(item.get_child_at_index(i) for i in range(item.get_child_count()))
        stack = [item for item in stack if item is not None]
    time.sleep(.1)
sys.exit(1)
'''
    command("/usr/bin/python3", "-c", script, str(pid), timeout=8)


def play(sink, audio):
    return subprocess.Popen(
        ["paplay", "--device=" + sink, str(audio)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def finish_play(process, duration):
    try:
        result = process.wait(timeout=duration + 10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise TestFailure(25) from None
    require(result == 0, 26)


def start_client(binary, children, *, persist=False):
    process = subprocess.Popen(
        [str(binary), "--background"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=persist,
    )
    children.append(process)
    wait_for(client_running, code=27)
    wait_for(lambda: client_status(binary)["configured"], timeout=15, code=28)
    idle_client(binary)


def stop_client(binary):
    if client_running():
        command(binary, "--quit", timeout=5)
        wait_for(lambda: not client_running(), timeout=20, code=29)


def failure_diagnostics(binary, state_paths):
    """Take the failing session's evidence before cancellation destroys its state."""
    result = {"snapshot_before_cleanup": 1, "snapshot_failures": 0, "targets": []}
    try:
        if client_running():
            status = client_status(binary)
            numeric_keys = (
                "fixed_chars", "pending_chars", "buffered_seconds",
                "audio_sent_samples", "bytes_sent",
            )
            client = {
                key: value for key in numeric_keys
                if isinstance((value := status.get(key)), (int, float))
                and not isinstance(value, bool) and math.isfinite(value)
            }
            client.update(
                running=1,
                state_code=STATE_CODES[status["state"]],
                active=int(status["active"]),
                capture_active=int(status["capture_active"]),
            )
            delivery = status.get("delivery_state")
            if delivery in (
                "none", "waiting_target", "clipboard_only", "pasted", "focus_changed",
                "copied", "cancelled", "failed", "empty",
            ):
                client["delivery_state"] = delivery
            result["client"] = client
        else:
            result["client"] = {"running": 0}
    except Exception:
        result["snapshot_failures"] += 1
    for index, path in enumerate(state_paths):
        try:
            state = target_state(path)
            if state is not None:
                result["targets"].append({
                    "index": index,
                    "chars": len(state["text"]),
                    "sha256": digest(state["text"]),
                    "return_key_count": state["return_key_count"],
                    **{key: int(state.get(key) is True) for key in GTK_FOCUS_KEYS},
                })
        except Exception:
            result["snapshot_failures"] += 1
    try:
        clipboard = command("xclip", "-selection", "clipboard", "-out", check=False, timeout=3)
        result["clipboard"] = {
            "read_exit_code": clipboard.returncode,
            "bytes": len(clipboard.stdout),
            "chars": len(clipboard.stdout.decode("utf-8", errors="replace")),
            "sha256": digest(clipboard.stdout),
        }
    except Exception:
        result["snapshot_failures"] += 1
    return result


def test(args):
    global FOCUS_CONTEXT
    started = time.monotonic()
    report = {
        "schema_version": 1,
        "success": 0,
        "failure_code": 0,
        "cleanup_failures": 0,
        "focus_change_requested": int(args.focus_change),
        "stage_code": 1,
        "expected_sha256": digest(args.expect),
        "checks": {},
    }
    children = []
    players = []
    modules = []
    state_paths = []
    original_config = None
    original_config_mode = None
    config_path = None
    clipboard = None
    old_window = None
    old_focus = None
    was_running = False
    client_changed = False
    work = None
    try:
        require(args.binary.is_absolute() and args.binary.is_file(), 30)
        require(os.environ.get("DISPLAY") and os.environ.get("XDG_SESSION_TYPE", "x11") == "x11", 31)
        require(all(shutil.which(name) for name in ("pactl", "paplay", "xclip", "xdotool", "gdbus")), 32)
        require(args.target_python.is_absolute() and args.target_python.is_file(), 33)
        locked = output(
            "gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver",
            "--object-path", "/org/gnome/ScreenSaver", "--method",
            "org.gnome.ScreenSaver.GetActive",
        )
        require(locked == "(false,)", 60)
        require(bool(compact(args.expect)), 34)
        with wave.open(str(args.audio), "rb") as audio:
            require((audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) == (16000, 1, 2, "NONE"), 35)
            duration = audio.getnframes() / 16000
            require(0.2 <= duration <= 60, 35)
            report["audio_frames"] = audio.getnframes()
        report["audio_sha256"] = digest(args.audio.read_bytes())
        root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        require(root.is_absolute(), 36)
        config_path = root / "oneaxe-voice-linux" / "config.json"
        require(config_path.is_file() and not config_path.is_symlink(), 37)
        original_config = config_path.read_bytes()
        original_config_mode = stat.S_IMODE(config_path.stat().st_mode)
        config = json.loads(original_config)
        require(config.get("shortcut", "F9") == "F9", 38)
        was_running = client_running()
        if was_running:
            status = client_status(args.binary)
            require(inactive(status), 39)
            require(status["state"] in ("idle", "error"), 39)
        saved_clipboard = command("xclip", "-selection", "clipboard", "-out", check=False, timeout=3)
        clipboard = saved_clipboard.stdout if saved_clipboard.returncode == 0 else b""
        old_window = output("xdotool", "getactivewindow", check=False)
        old_focus = output("xdotool", "getwindowfocus", check=False)
        work = tempfile.TemporaryDirectory(prefix="oneaxe-voice-linux-e2e-")
        directory = Path(work.name)
        suffix = str(os.getpid())
        sink = "oneaxe_voice_linux_e2e_sink_" + suffix
        source = "oneaxe_voice_linux_e2e_source_" + suffix
        modules.append(output(
            "pactl", "load-module", "module-null-sink", "sink_name=" + sink,
            "rate=16000", "channels=1", "channel_map=mono",
            "sink_properties=device.description=OneAxeVoiceLinuxE2E",
        ))
        modules.append(output(
            "pactl", "load-module", "module-remap-source", "master=" + sink + ".monitor",
            "source_name=" + source, "channels=1", "master_channel_map=mono", "channel_map=mono",
            "source_properties=device.description=OneAxeVoiceLinuxE2E",
        ))
        require(all(module.isdigit() for module in modules), 40)
        report["stage_code"] = 2
        client_changed = True
        stop_client(args.binary)
        config.update(microphone=source, auto_paste=True, pause_ms=1000)
        private_write(config_path, json.dumps(config, ensure_ascii=False).encode("utf-8") + b"\n")
        start_client(args.binary, children)
        state_a = directory / "target-a.json"
        state_paths.append(state_a)
        target = subprocess.Popen(
            [str(args.target_python), str(Path(__file__).with_name("input_target.py")),
             "--state", str(state_a), "--title", "OneAxe Voice Linux E2E " + suffix],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        children.append(target)
        window_a = wait_for(lambda: window_for(target), code=41)
        wait_for(lambda: target_state(state_a), code=42)
        report["stage_code"] = 3
        prepare_target(window_a, state_a, report, 3)
        observe_focus("before_hotkey")
        command("xdotool", "key", "--clearmodifiers", "F9")
        observe_focus("after_hotkey")
        ready_capture(args.binary)
        report["checks"]["f9_started_after_ready"] = 1
        writer = play(sink, args.audio)
        players.append(writer)
        finish_play(writer, duration)

        def phrase_at_pause():
            state = target_state(state_a)
            status = client_status(args.binary)
            require(status["capture_active"] and status["state"] == "recording", 43)
            return state if state and phrase_count(state["text"], args.expect) == 1 else None

        at_pause = wait_for(phrase_at_pause, timeout=45, code=44)
        require(at_pause["return_key_count"] == 0, 45)
        report["checks"]["pause_delivered_while_recording"] = 1
        report["pause_chars"] = len(at_pause["text"])
        report["pause_sha256"] = digest(at_pause["text"])
        writer = play(sink, args.audio)
        players.append(writer)
        finish_play(writer, duration)
        command("xdotool", "key", "--clearmodifiers", "F9")
        final_status = idle_client(args.binary, timeout=45)
        def final_text():
            value = target_state(state_a)
            return value if value and phrase_count(value["text"], args.expect) == 2 else None

        final = wait_for(final_text, timeout=15, code=46)
        require(final["return_key_count"] == 0, 45)
        report["checks"]["f9_stop_delivered_tail"] = 1
        report["checks"]["no_enter"] = 1
        report["final_chars"] = len(final["text"])
        report["final_sha256"] = digest(final["text"])
        report["audio_sent_samples"] = int(final_status.get("audio_sent_samples", 0))
        report["bytes_sent"] = int(final_status.get("bytes_sent", 0))
        require(report["audio_sent_samples"] > 0 and report["bytes_sent"] > 0, 47)
        require(report["bytes_sent"] == 2 * report["audio_sent_samples"], 47)
        copy_from_client(args.binary, final_status["pid"])
        def copied_matches_target():
            value = output("xclip", "-selection", "clipboard", "-out")
            return value if value == final["text"] else None

        copied_final = wait_for(copied_matches_target, timeout=5, code=61)
        require(final_status.get("fixed_chars") == len(final["text"]), 62)
        report["checks"]["copied_full_text_matches_target_exactly"] = 1
        report["copied_final_sha256"] = digest(copied_final)

        if args.focus_change:
            report["stage_code"] = 4
            clear_target(window_a, state_a)
            state_b = directory / "target-b.json"
            state_paths.append(state_b)
            other = subprocess.Popen(
                [str(args.target_python), str(Path(__file__).with_name("input_target.py")),
                 "--state", str(state_b), "--title", "OneAxe Voice Linux E2E alternate " + suffix],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            children.append(other)
            window_b = wait_for(lambda: window_for(other), code=48)
            require(window_b != window_a, 57)
            report["checks"]["distinct_test_windows"] = 1
            wait_for(lambda: target_state(state_b), code=49)
            prepare_target(window_a, state_a, report, 4)
            command("xclip", "-selection", "clipboard", "-in", input_bytes=b"", timeout=3)
            observe_focus("before_toggle")
            command(args.binary, "--toggle")
            observe_focus("after_toggle")
            ready_capture(args.binary)
            prepare_target(window_b, state_b, report, 4)
            writer = play(sink, args.audio)
            players.append(writer)
            finish_play(writer, duration)
            def copied_at_pause():
                status = client_status(args.binary)
                require(status["state"] != "error", 16)
                return phrase_count(output("xclip", "-selection", "clipboard", "-out"), args.expect) >= 1

            wait_for(copied_at_pause, timeout=45, code=50)
            require(target_state(state_a)["text"] == target_state(state_b)["text"] == "", 51)
            report["stage_code"] = 5
            prepare_target(window_a, state_a, report, 5)
            writer = play(sink, args.audio)
            players.append(writer)
            finish_play(writer, duration)
            command(args.binary, "--toggle")
            idle_client(args.binary, timeout=45)
            def copied_twice():
                value = output("xclip", "-selection", "clipboard", "-out")
                return value if phrase_count(value, args.expect) == 2 else None

            copied = wait_for(copied_twice, timeout=15, code=55)
            time.sleep(0.4)
            require(target_state(state_a)["text"] == target_state(state_b)["text"] == "", 52)
            report["checks"]["changed_focus_only_copied"] = 1
            report["checks"]["returned_focus_stayed_copy_only"] = 1
            report["copied_chars"] = len(copied)
            report["copied_sha256"] = digest(copied)

        report["stage_code"] = 6
        clear_target(window_a, state_a)
        prepare_target(window_a, state_a, report, 6)
        observe_focus("before_toggle")
        command(args.binary, "--toggle")
        observe_focus("after_toggle")
        ready_capture(args.binary)
        writer = play(sink, args.audio)
        players.append(writer)
        time.sleep(min(1.0, duration / 2))
        command(args.binary, "--cancel")
        after_cancel = target_state(state_a)["text"]
        idle_client(args.binary, timeout=20)
        time.sleep(3)
        require(target_state(state_a)["text"] == after_cancel, 53)
        require(target_state(state_a)["return_key_count"] == 0, 45)
        report["checks"]["cancel_blocked_late_input"] = 1
        report["cancel_chars"] = len(after_cancel)
        report["cancel_sha256"] = digest(after_cancel)
        report["success"] = 1
    except TestFailure as error:
        report["failure_code"] = error.code
        if error.code == 11:
            report["command_failure"] = LAST_COMMAND_FAILURE
    except Exception:
        report["failure_code"] = 99
    finally:
        if not report["success"] and client_changed:
            report["failure_diagnostics"] = failure_diagnostics(args.binary, state_paths)
        FOCUS_CONTEXT = None

        def cleanup(action):
            try:
                action()
            except Exception:
                report["cleanup_failures"] += 1

        for player in players:
            if player.poll() is None:
                cleanup(player.terminate)
                cleanup(lambda player=player: player.wait(timeout=3))
        if client_changed:
            cleanup(lambda: stop_client(args.binary))
        for child in children:
            if child.poll() is None:
                cleanup(child.terminate)
                cleanup(lambda child=child: child.wait(timeout=3))
                if child.poll() is None:
                    cleanup(child.kill)
                    cleanup(lambda child=child: child.wait(timeout=3))
        if client_changed and original_config is not None:
            cleanup(lambda: private_write(config_path, original_config))
            cleanup(lambda: config_path.chmod(original_config_mode))
        for module in reversed(modules):
            cleanup(lambda module=module: command("pactl", "unload-module", module))
        if client_changed and was_running:
            cleanup(lambda: start_client(args.binary, [], persist=True))
        if clipboard is not None:
            cleanup(lambda: command("xclip", "-selection", "clipboard", "-in", input_bytes=clipboard, timeout=3))
        if old_window:
            cleanup(lambda: command("xdotool", "windowactivate", "--sync", old_window))
        if old_focus and old_focus not in ("0", "1"):
            cleanup(lambda: command("xdotool", "windowfocus", "--sync", old_focus))
        if work is not None:
            cleanup(work.cleanup)
        report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        report["max_buffered_samples"] = MAX_BUFFERED_SAMPLES
        if report["cleanup_failures"]:
            report["success"] = 0
        args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_write(args.output, json.dumps(report, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["success"] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--expect", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--focus-change", action="store_true")
    parser.add_argument("--target-python", type=Path, default=Path("/usr/bin/python3"))
    return test(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
