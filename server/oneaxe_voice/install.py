"""Render and install the user-level systemd unit for this checkout."""

from pathlib import Path
import os
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "systemd" / "oneaxe-voice.service"


def systemd_quote(value: str | Path) -> str:
    """Quote one systemd unit value, including paths containing spaces."""
    escaped = str(value).replace("%", "%%").replace("$", "$$").replace("\\", "\\\\")
    escaped = escaped.replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def systemd_path(value: str | Path) -> str:
    """Escape a path-valued directive such as WorkingDirectory."""
    escaped = str(value).replace("%", "%%").replace("\\", "\\x5c")
    escaped = escaped.replace(" ", "\\x20").replace("\t", "\\x09")
    escaped = escaped.replace('"', "\\x22").replace("\n", "\\x0a")
    return escaped


def render_unit(template: str, replacements: dict[str, str | Path]) -> str:
    """Replace template markers with already escaped systemd values."""
    rendered = template
    for name, value in replacements.items():
        rendered = rendered.replace("{{" + name + "}}", str(value))
    if "{{" in rendered:
        raise ValueError("Unresolved systemd template marker")
    return rendered


def config_unit_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    return config_home / "systemd" / "user" / "oneaxe-voice.service"


def install() -> Path:
    project_dir = ROOT.resolve(strict=True)
    unit_path = config_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_unit(
        TEMPLATE.read_text(encoding="utf-8"),
        {
            "PROJECT_DIR": systemd_path(project_dir),
            "SERVE_COMMAND": systemd_quote(project_dir / "bin" / "serve"),
        },
    )
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=unit_path.parent,
            prefix=f".{unit_path.name}.", delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(rendered)
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, unit_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    return unit_path


def main() -> int:
    try:
        unit_path = install()
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"Could not install OneAxe Voice systemd unit: {exc}", file=sys.stderr)
        return 1
    print(f"Installed user unit: {unit_path}")
    print("Start it with: systemctl --user start oneaxe-voice.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
