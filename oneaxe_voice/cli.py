"""Local command-line client; proxy settings never affect loopback requests."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import subprocess
from urllib.parse import urlsplit

import httpx

from .config import Settings


def initialize(settings: Settings) -> None:
    """Create a private capability once without printing or replacing its value."""
    settings.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.runtime_dir.chmod(0o700)
    try:
        descriptor = os.open(settings.token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"本机令牌已存在：{settings.token_path}")
        return
    with os.fdopen(descriptor, "w") as target:
        target.write(secrets.token_urlsafe(32) + "\n")
    print(f"已生成本机令牌：{settings.token_path}")


def local_url(value: str) -> str:
    """Reject remote destinations so a local token is not forwarded elsewhere."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("服务地址须为本机 HTTP 地址，例如 http://127.0.0.1:8097")
    return value.rstrip("/")


def main(argv: list[str] | None = None) -> int:
    """Initialize credentials, inspect status, or transcribe a local WAV file."""
    parser = argparse.ArgumentParser(description="OneAxe Voice 本地 GPU 语音识别")
    parser.add_argument("--url", default="http://127.0.0.1:8097")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="生成仅供本机客户端使用的令牌")
    commands.add_parser("health", help="检查 HTTP 服务")
    commands.add_parser("status", help="检查模型、设备和忙碌状态")
    commands.add_parser("devices", help="列出麦克风输入设备")
    commands.add_parser("toggle", help="开始录音，或结束录音并识别输入")
    commands.add_parser("cancel", help="取消本次桌面听写")
    commands.add_parser("desktop-status", help="查看录音、识别和粘贴状态")
    commands.add_parser("desktop-run", help="运行桌面录音控制服务")
    mode = commands.add_parser("set-mode", help="选择稳听 / 随听 / 即听；录音中下轮生效")
    mode.add_argument("mode", choices=["vad", "qwen-stream", "r2t2"])
    setup = commands.add_parser("desktop-setup", help="安装 GNOME 桌面服务与全局快捷键")
    setup.add_argument("--shortcut", default="F8")
    setup.add_argument("--remove", action="store_true", help="移除本项目的快捷键并停止桌面服务")
    transcribe = commands.add_parser("transcribe", help="识别 0.1–60 秒 PCM WAV")
    transcribe.add_argument("audio", type=Path)
    transcribe.add_argument("--json", action="store_true", help="输出完整结果和耗时")
    transcribe.add_argument("--output", type=Path, help="把识别文字保存为 UTF-8 文件")
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
        if args.command == "init":
            initialize(settings)
            return 0
        if args.command == "devices":
            from .capture import sources
            print(json.dumps([{k: item[k] for k in ("name", "description", "mute")}
                              for item in sources()], ensure_ascii=False, indent=2))
            return 0
        if args.command == "desktop-setup":
            from .desktop_setup import install_desktop, remove_shortcut
            if args.remove:
                remove_shortcut()
            else:
                initialize(settings)
                install_desktop(settings, args.shortcut)
            return 0
        if args.command == "desktop-run":
            from .desktop import serve
            asyncio.run(serve(settings))
            return 0
        if args.command == "set-mode":
            from .desktop import control
            print(json.dumps(control(settings, "configure", mode=args.mode), ensure_ascii=False, indent=2))
            return 0
        if args.command in {"toggle", "cancel", "desktop-status"}:
            from .desktop import control
            action = "status" if args.command == "desktop-status" else args.command
            print(json.dumps(control(settings, action), ensure_ascii=False, indent=2))
            return 0
        url = local_url(args.url)
        headers = {}
        if args.command != "health":
            headers["Authorization"] = "Bearer " + settings.token_path.read_text().strip()
        with httpx.Client(
            base_url=url, headers=headers, trust_env=False, timeout=180,
            follow_redirects=False,
        ) as client:
            if args.command == "transcribe":
                if args.audio.stat().st_size > settings.max_audio_bytes:
                    raise ValueError("音频文件超过 12 MiB")
                with args.audio.open("rb") as recording:
                    response = client.post(
                        "/api/dictation/transcribe",
                        files={"file": (args.audio.name, recording, "audio/wav")},
                    )
            else:
                path = "/health" if args.command == "health" else "/api/dictation/status"
                response = client.get(path)
        payload = response.json()
        if response.is_error:
            raise ValueError(f"HTTP {response.status_code}: {payload.get('detail', '请求失败')}")
        if args.command == "transcribe":
            if args.output:
                args.output.write_text(payload["text"] + "\n", encoding="utf-8")
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(payload["text"])
                print(
                    f"device={payload.get('device')} "
                    f"audio={payload['audio_seconds']}s timing_ms={payload['timing_ms']}",
                    file=sys.stderr,
                )
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, httpx.HTTPError, subprocess.SubprocessError) as exc:
        print(f"OneAxe Voice: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
