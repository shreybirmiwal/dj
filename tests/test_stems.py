from __future__ import annotations

from setmix.analysis import TrackAnalysis
from setmix.stems import VocalMap, choose_vocal_safe_cues


def _track(path: str) -> TrackAnalysis:
    beats = [index * 0.5 for index in range(256)]
    return TrackAnalysis(
        path=path,
        duration=128.0,
        bpm=120.0,
        beat_times=beats,
        downbeat_offset=0,
        cue_in=0.0,
        cue_out=96.0,
        active_end=127.0,
        rms_db=-12.0,
        beat_confidence=1.0,
        transition_bars=8,
    )


def test_vocal_safe_cues_avoid_simultaneous_leads() -> None:
    left = _track("left.wav")
    right = _track("right.wav")
    left_vocals = VocalMap("left.wav", "test", 0.25, [[0.0, 105.0]], 0.8, 1.0)
    right_vocals = VocalMap("right.wav", "test", 0.25, [[24.0, 110.0]], 0.7, 1.0)
    left_cue, right_cue, overlap = choose_vocal_safe_cues(left, right, left_vocals, right_vocals)
    assert left_cue >= 96.0
    assert right_cue < 24.0
    assert overlap < 0.1


def test_activity_fraction_uses_timeline() -> None:
    vocals = VocalMap("track.wav", "test", 0.25, [[2.0, 4.0]], 0.2, 1.0)
    assert vocals.activity_fraction(2.0, 4.0) > 0.95
    assert vocals.activity_fraction(5.0, 7.0) == 0.0
