from __future__ import annotations

from pathlib import Path

import pytest

from setmix.mixxx import build_mixxx_command, find_mixxx, write_mixxx_playlist


def executable(tmp_path: Path) -> Path:
    path = tmp_path / "mixxx"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_find_mixxx_accepts_an_explicit_executable(tmp_path: Path) -> None:
    path = executable(tmp_path)
    assert find_mixxx(path) == path.resolve()


def test_build_mixxx_command_uses_documented_options(tmp_path: Path) -> None:
    binary = executable(tmp_path)
    tracks = [tmp_path / "one.mp3", tmp_path / "two.flac"]
    for track in tracks:
        track.write_bytes(b"audio")

    command = build_mixxx_command(
        tracks,
        executable=binary,
        settings_path=tmp_path / "settings",
        resource_path=tmp_path / "resources",
        start_autodj=True,
        developer=True,
    )

    assert command == [
        str(binary.resolve()),
        "--settings-path",
        str((tmp_path / "settings").resolve()),
        "--resource-path",
        str((tmp_path / "resources").resolve()),
        "--start-autodj",
        "--developer",
        *(str(track.resolve()) for track in tracks),
    ]


def test_build_mixxx_command_rejects_more_than_four_decks(tmp_path: Path) -> None:
    binary = executable(tmp_path)
    tracks = [tmp_path / f"{index}.mp3" for index in range(5)]
    for track in tracks:
        track.write_bytes(b"audio")

    with pytest.raises(ValueError, match="four"):
        build_mixxx_command(tracks, executable=binary)


def test_write_mixxx_playlist_preserves_order_and_absolute_paths(tmp_path: Path) -> None:
    tracks = [tmp_path / "two.mp3", tmp_path / "one.mp3"]
    for track in tracks:
        track.write_bytes(b"audio")

    output = write_mixxx_playlist(tracks, tmp_path / "set.m3u8")

    assert output.read_text(encoding="utf-8").splitlines() == [
        "#EXTM3U",
        str(tracks[0].resolve()),
        str(tracks[1].resolve()),
    ]
