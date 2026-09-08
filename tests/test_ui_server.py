import subprocess
import json
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


def test_lyrics_summary_returns_timestamped_machine_transcript(tmp_path: Path, monkeypatch):
    source = tmp_path / "song.flac"
    source.write_bytes(b"audio")
    cache = tmp_path / "intelligence"
    transcript = cache / "fingerprint" / "transcript.json"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({
        "path": str(source.resolve()),
        "model": "faster-whisper/base",
        "language": "en",
        "confidence": 0.91,
        "phrases": [{"start": 12.5, "end": 15.0, "text": "A lyric line"}],
        "words": [{"word": "lyric", "start": 12.9, "end": 13.4, "probability": 0.95}],
    }))
    monkeypatch.setattr(server, "INTELLIGENCE_CACHE_DIR", cache)

    result = server.lyrics_summary(source)

    assert result["status"] == "ready"
    assert result["language"] == "en"
    assert result["phrases"][0]["start"] == 12.5
    assert result["words"][0]["probability"] == 0.95


def test_lyrics_summary_reports_missing_without_starting_analysis(tmp_path: Path, monkeypatch):
    source = tmp_path / "song.flac"
    source.write_bytes(b"audio")
    monkeypatch.setattr(server, "INTELLIGENCE_CACHE_DIR", tmp_path / "empty")

    result = server.lyrics_summary(source)

    assert result == {
        "status": "missing",
        "model": None,
        "language": None,
        "confidence": 0.0,
        "phrases": [],
        "words": [],
    }


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
