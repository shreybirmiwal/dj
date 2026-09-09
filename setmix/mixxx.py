from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Sequence


MACOS_MIXXX_PATHS = (
    Path("/Applications/Mixxx.app/Contents/MacOS/mixxx"),
    Path.home() / "Applications" / "Mixxx.app" / "Contents" / "MacOS" / "mixxx",
)


def find_mixxx(explicit_path: str | Path | None = None) -> Path:
    """Locate a Mixxx executable without installing or changing anything."""
    configured = explicit_path or os.environ.get("SETMIX_MIXXX_PATH")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        raise RuntimeError(f"Mixxx executable is not runnable: {candidate}")

    on_path = shutil.which("mixxx")
    if on_path:
        return Path(on_path).resolve()
    for candidate in MACOS_MIXXX_PATHS:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise RuntimeError(
        "Mixxx was not found. Install Mixxx 2.5.6+ or pass --mixxx-path "
        "(/Applications/Mixxx.app/Contents/MacOS/mixxx on macOS)."
    )


def build_mixxx_command(
    tracks: Sequence[Path],
    *,
    executable: str | Path | None = None,
    settings_path: str | Path | None = None,
    resource_path: str | Path | None = None,
    start_autodj: bool = False,
    developer: bool = False,
) -> list[str]:
    """Build a Mixxx launch command using only its documented CLI surface."""
    if not tracks:
        raise ValueError("At least one track is required")
    if len(tracks) > 4:
        raise ValueError(
            "Mixxx can load at most four command-line tracks into its four decks; "
            "use --playlist-output to export longer sets."
        )

    resolved: list[Path] = []
    for track in tracks:
        source = Path(track).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"Track does not exist: {source}")
        resolved.append(source)

    command = [str(find_mixxx(executable))]
    if settings_path:
        command.extend(("--settings-path", str(Path(settings_path).expanduser().resolve())))
    if resource_path:
        command.extend(("--resource-path", str(Path(resource_path).expanduser().resolve())))
    if start_autodj:
        command.append("--start-autodj")
    if developer:
        command.append("--developer")
    command.extend(str(track) for track in resolved)
    return command


def write_mixxx_playlist(tracks: Iterable[Path], destination: str | Path) -> Path:
    """Write an absolute UTF-8 M3U playlist that Mixxx can import."""
    output = Path(destination).expanduser().resolve()
    sources = [Path(track).expanduser().resolve() for track in tracks]
    if not sources:
        raise ValueError("At least one track is required")
    missing = [source for source in sources if not source.is_file()]
    if missing:
        raise ValueError(f"Track does not exist: {missing[0]}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "#EXTM3U\n" + "".join(f"{source}\n" for source in sources),
        encoding="utf-8",
    )
    return output


def launch_mixxx(command: Sequence[str]) -> int:
    """Launch Mixxx detached from the SetMix CLI and return its process id."""
    process = subprocess.Popen(list(command), start_new_session=True)
    return process.pid
