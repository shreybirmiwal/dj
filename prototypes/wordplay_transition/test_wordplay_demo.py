from __future__ import annotations

import numpy as np

from wordplay_demo import _aligned_timing, _fit_frames, _loop_pattern, _make_loop


def test_fit_frames_converts_mono_and_pads() -> None:
    result = _fit_frames(np.ones(20, dtype=np.float32), 32)
    assert result.shape == (32, 2)
    assert np.all(result[:20] == 1.0)
    assert np.all(result[20:] == 0.0)


def test_make_loop_has_requested_length_and_soft_edges() -> None:
    fragment = np.ones((3000, 2), dtype=np.float32)
    result = _make_loop(fragment, beat_frames=2000, repeats=4)
    assert result.shape == (8000, 2)
    assert np.isfinite(result).all()
    assert np.allclose(result[0], 0.0)
    assert np.allclose(result[1999], 0.0)
    assert float(np.max(result)) <= 1.01


def test_loop_pattern_accelerates_to_requested_total() -> None:
    fragment = np.ones((4000, 2), dtype=np.float32) * 0.2
    result = _loop_pattern(fragment, [4, 2, 1, 0.5, 0.5], beat_seconds=0.5)
    assert result.shape == (round(8 * 0.5 * 44_100), 2)
    assert np.isfinite(result).all()


def test_aligned_timing_places_drop_when_incoming_reaches_word() -> None:
    config = {
        "target_bpm": 120.0,
        "left_bpm": 120.0,
        "right_bpm": 120.0,
        "left_context_start": 10.0,
        "left_phrase_end": 20.0,
        "post_seconds": 8.0,
    }
    phrase_end, drop, total = _aligned_timing(
        config,
        {"right_build_start": 30.0, "right_word_start": 46.0},
    )
    assert phrase_end == 10.0
    assert drop == 26.0
    assert total == 34.0
