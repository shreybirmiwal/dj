import subprocess
from pathlib import Path

from ui import server


def test_browser_media_keeps_supported_audio(tmp_path: Path):
    source = tmp_path / "track.mp3"
    source.write_bytes(b"audio")

    assert server.browser_media_path(source) == source


def test_waveform_summary_uses_real_samples_and_cache(tmp_path: Path, monkeypatch):
    import numpy as np

    source = tmp_path / "song.wav"
    source.write_bytes(b"wave")
    cache = tmp_path / "cache"
    monkeypatch.setattr(server, "WAVEFORM_CACHE_DIR", cache)
    calls = []
    audio = (np.sin(np.linspace(0, 40, 12000, dtype=np.float32)) * 0.7).astype("<f4")

    class Result:
        stdout = audio.tobytes()

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    first = server.waveform_summary(source, bins=24)
    second = server.waveform_summary(source, bins=24)

    assert len(first["bands"]) == 24
    assert any(sum(row) > 0 for row in first["bands"])
    assert first == second
    assert len(calls) == 1


def test_browser_media_transcodes_flac_once(tmp_path: Path, monkeypatch):
    source = tmp_path / "track.flac"
    source.write_bytes(b"flac")
    media_cache = tmp_path / "cache"
    calls = []

    def fake_run(command, check):
        calls.append((command, check))
        Path(command[-1]).write_bytes(b"mp3")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(server, "MEDIA_CACHE_DIR", media_cache)
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    first = server.browser_media_path(source)
    second = server.browser_media_path(source)

    assert first == second
    assert first.suffix == ".mp3"
    assert first.read_bytes() == b"mp3"
    assert len(calls) == 1
    assert calls[0][0][calls[0][0].index("-c:a") + 1] == "libmp3lame"
