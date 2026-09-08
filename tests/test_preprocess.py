from __future__ import annotations

import json

from setmix.analysis import TrackAnalysis
from setmix.preprocess import prepare_library


def test_basic_library_preparation_writes_a_resumable_index(tmp_path, monkeypatch) -> None:
    song = tmp_path / "song.wav"
    song.write_bytes(b"test")

    def fake_analysis(path, **_kwargs):
        return TrackAnalysis(
            path=str(path),
            duration=64.0,
            bpm=120.0,
            beat_times=[index * 0.5 for index in range(128)],
            downbeat_offset=0,
            cue_in=0.0,
            cue_out=32.0,
            active_end=63.5,
            rms_db=-12.0,
            beat_confidence=0.9,
            transition_bars=16,
            tempo_map=[[0.0, 120.0]],
        )

    monkeypatch.setattr("setmix.preprocess.analyze_track", fake_analysis)
    result = prepare_library([song], level="basic", cache_root=tmp_path / "cache")

    assert result["failures"] == []
    assert result["completed"] == 1
    index = json.loads((tmp_path / "cache" / "library-index.json").read_text())
    assert index["tracks"][str(song)]["level"] == "basic"
    assert index["tracks"][str(song)]["analysis"]["bpm"] == 120.0
