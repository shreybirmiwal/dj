from __future__ import annotations

import numpy as np

from setmix import analysis


def test_analysis_audio_decode_is_shared_in_memory(tmp_path, monkeypatch) -> None:
    source = tmp_path / "song.wav"
    source.write_bytes(b"audio")
    calls = []

    def fake_load(path, *, sr, mono):
        calls.append((path, sr, mono))
        return np.arange(32, dtype=np.float32), sr

    monkeypatch.setattr(analysis.librosa, "load", fake_load)
    analysis._DECODE_CACHE.clear()

    first, first_rate = analysis.load_analysis_audio(source)
    second, second_rate = analysis.load_analysis_audio(source)

    assert first is second
    assert first_rate == second_rate == 22050
    assert len(calls) == 1
