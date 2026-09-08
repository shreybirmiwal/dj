from __future__ import annotations

import numpy as np

from setmix.engine import (
    _drum_alignment_transform,
    mix_four_stem_transition,
    mix_stem_transition,
    mix_transition,
)


def test_transition_has_expected_shape_and_finite_samples() -> None:
    frames = 44100 * 2
    time = np.arange(frames) / 44100.0
    left = np.column_stack((np.sin(time * 2 * np.pi * 90), np.sin(time * 2 * np.pi * 440))).astype("float32")
    right = np.column_stack((np.sin(time * 2 * np.pi * 110), np.sin(time * 2 * np.pi * 660))).astype("float32")
    result = mix_transition(left, right, 0.3, 0.3)
    assert result.shape == (frames, 2)
    assert np.isfinite(result).all()
    assert 0.01 < float(np.sqrt(np.mean(result * result))) < 0.5


def test_transition_reaches_both_sources() -> None:
    frames = 44100
    left = np.zeros((frames, 2), dtype="float32")
    right = np.zeros((frames, 2), dtype="float32")
    left[:, 0] = 0.25
    right[:, 1] = 0.25
    result = mix_transition(left, right, 1.0, 1.0)
    assert float(np.mean(np.abs(result[: frames // 10, 0]))) > 0.15
    assert float(np.mean(np.abs(result[-frames // 10 :, 1]))) > 0.15


def test_all_transition_techniques_are_finite() -> None:
    frames = 44100 * 2
    time = np.arange(frames) / 44100.0
    left = np.column_stack((np.sin(time * 2 * np.pi * 90), np.sin(time * 2 * np.pi * 440))).astype("float32") * 0.2
    right = np.column_stack((np.sin(time * 2 * np.pi * 110), np.sin(time * 2 * np.pi * 660))).astype("float32") * 0.2
    for technique in (
        "bass_swap",
        "filter_sweep",
        "highpass_out",
        "lowpass_reveal",
        "echo_out",
        "loop_filter",
        "reverb_tail",
    ):
        result = mix_transition(left, right, 1.0, 1.0, technique=technique, bars=2)
        assert result.shape == left.shape
        assert np.isfinite(result).all()
        assert float(np.max(np.abs(result))) < 1.0


def test_stem_transition_separates_vocal_handoffs() -> None:
    frames = 44100 * 4
    time = np.arange(frames) / 44100.0
    vocal_a = np.column_stack((np.sin(time * 2 * np.pi * 440),) * 2).astype("float32") * 0.1
    music_a = np.column_stack((np.sin(time * 2 * np.pi * 90),) * 2).astype("float32") * 0.1
    vocal_b = np.column_stack((np.sin(time * 2 * np.pi * 660),) * 2).astype("float32") * 0.1
    music_b = np.column_stack((np.sin(time * 2 * np.pi * 110),) * 2).astype("float32") * 0.1
    result = mix_stem_transition(vocal_a, music_a, vocal_b, music_b, 1.0, 1.0)
    assert result.shape == vocal_a.shape
    assert np.isfinite(result).all()
    assert 0.01 < float(np.sqrt(np.mean(result * result))) < 0.4


def test_four_stem_transition_is_finite() -> None:
    frames = 44100 * 2
    time = np.arange(frames) / 44100.0
    frequencies = {"vocals": 440.0, "drums": 120.0, "bass": 70.0, "other": 880.0}
    left = {
        name: np.column_stack((np.sin(time * 2 * np.pi * frequency),) * 2).astype("float32") * 0.05
        for name, frequency in frequencies.items()
    }
    right = {
        name: np.column_stack((np.sin(time * 2 * np.pi * (frequency + 20.0)),) * 2).astype("float32") * 0.05
        for name, frequency in frequencies.items()
    }
    result = mix_four_stem_transition(left, right, 1.0, 1.0)
    assert result.shape == (frames, 2)
    assert np.isfinite(result).all()
    assert float(np.max(np.abs(result))) < 1.0


def test_drum_alignment_tracks_gradual_tempo_drift() -> None:
    sample_rate = 44100
    seconds = 64
    frames = sample_rate * seconds
    outgoing = np.zeros((frames, 2), dtype="float32")
    incoming = np.zeros((frames, 2), dtype="float32")
    click = np.asarray([1.0, 0.65, 0.35, 0.15], dtype="float32")
    for beat in np.arange(0.5, seconds - 0.5, 0.5):
        left = round(beat * sample_rate)
        right = round((0.06 + beat * 1.002) * sample_rate)
        outgoing[left : left + len(click)] = click[:, None]
        incoming[right : right + len(click)] = click[:, None]
    intercept, slope = _drum_alignment_transform(outgoing, incoming, 120.0)
    assert abs(intercept - 0.06) < 0.025
    assert abs(slope - 0.002) < 0.001
