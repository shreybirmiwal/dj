from __future__ import annotations

import numpy as np

from setmix.analysis import TrackAnalysis
from setmix.engine import (
    _drum_alignment_curve,
    _drum_alignment_transform,
    mix_four_stem_transition,
    mix_loop_bridge_transition,
    mix_stem_transition,
    mix_transition,
)
from setmix.intelligence import (
    _event_schedule,
    LyricPhrase,
    SectionSpan,
    TrackIntelligence,
    VocalTranscript,
    WordTimestamp,
    rank_transition_candidates,
)
from setmix.stems import VocalMap


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
        "drop_cut",
    ):
        result = mix_transition(left, right, 1.0, 1.0, technique=technique, bars=2)
        assert result.shape == left.shape
        assert np.isfinite(result).all()
        assert float(np.max(np.abs(result))) < 1.0


def test_drop_cut_preserves_outgoing_then_switches_on_landmark() -> None:
    frames = 44100 * 4
    outgoing = np.zeros((frames, 2), dtype="float32")
    incoming = np.zeros((frames, 2), dtype="float32")
    outgoing[:, 0] = 0.20
    incoming[:, 1] = 0.20
    result = mix_transition(
        outgoing,
        incoming,
        1.0,
        1.0,
        technique="drop_cut",
        bars=2,
        drop_position=0.6,
    )
    assert float(np.mean(np.abs(result[: frames // 2, 0]))) > 0.15
    assert float(np.mean(np.abs(result[: frames // 2, 1]))) < 0.05
    assert float(np.mean(np.abs(result[-frames // 5 :, 1]))) > 0.15
    assert float(np.mean(np.abs(result[-frames // 5 :, 0]))) < 0.05
    # A drop handoff may be decisive, but it must not introduce a waveform jump.
    assert float(np.max(np.abs(np.diff(result, axis=0)))) < 0.05


def test_echo_out_does_not_leave_outgoing_low_end_under_incoming_beat() -> None:
    frames = 44100 * 4
    time = np.arange(frames) / 44100.0
    outgoing = np.zeros((frames, 2), dtype="float32")
    incoming = np.zeros((frames, 2), dtype="float32")
    outgoing[:, 0] = np.sin(time * 2 * np.pi * 80.0) * 0.2
    incoming[:, 1] = np.sin(time * 2 * np.pi * 110.0) * 0.2
    result = mix_transition(outgoing, incoming, 1.0, 1.0, technique="echo_out", bars=2)
    tail = result[-frames // 10 :]
    assert float(np.sqrt(np.mean(np.square(tail[:, 0])))) < 0.02
    assert float(np.sqrt(np.mean(np.square(tail[:, 1])))) > 0.10


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


def test_four_stem_transition_lifts_an_unexpectedly_empty_center() -> None:
    frames = 44100 * 8
    time = np.arange(frames) / 44100.0
    tone = np.column_stack((np.sin(time * 2 * np.pi * 110.0),) * 2).astype("float32")
    left_envelope = np.where(np.arange(frames) < frames // 3, 0.12, 0.03).astype("float32")
    right_envelope = np.where(np.arange(frames) > 2 * frames // 3, 0.12, 0.03).astype("float32")
    left = {name: tone * left_envelope[:, None] * 0.25 for name in ("vocals", "drums", "bass", "other")}
    right = {name: tone * right_envelope[:, None] * 0.25 for name in ("vocals", "drums", "bass", "other")}
    result = mix_four_stem_transition(left, right, 1.0, 1.0)
    center = result[frames * 3 // 8 : frames * 5 // 8]
    edge = np.concatenate((result[: frames // 8], result[-frames // 8 :]))
    center_db = 20.0 * np.log10(np.sqrt(np.mean(np.square(center))) + 1e-9)
    edge_db = 20.0 * np.log10(np.sqrt(np.mean(np.square(edge))) + 1e-9)
    assert center_db > edge_db - 12.0


def test_harmonic_protection_clears_incompatible_melodies_at_center() -> None:
    frames = 44100 * 4
    time = np.arange(frames) / 44100.0
    empty = np.zeros((frames, 2), dtype="float32")
    left_tone = np.zeros_like(empty)
    right_tone = np.zeros_like(empty)
    left_tone[:, 0] = np.sin(time * 2 * np.pi * 440.0) * 0.1
    right_tone[:, 1] = np.sin(time * 2 * np.pi * 659.25) * 0.1
    left = {"vocals": empty, "drums": empty, "bass": empty, "other": left_tone}
    right = {"vocals": empty, "drums": empty, "bass": empty, "other": right_tone}
    compatible = mix_four_stem_transition(left, right, 1.0, 1.0, harmonic_compatibility=0.9)
    protected = mix_four_stem_transition(left, right, 1.0, 1.0, harmonic_compatibility=0.35)
    middle = slice(frames * 9 // 20, frames * 11 // 20)
    compatible_rms = float(np.sqrt(np.mean(np.square(compatible[middle]))))
    protected_rms = float(np.sqrt(np.mean(np.square(protected[middle]))))
    assert protected_rms < compatible_rms * 0.25


def test_loop_bridge_keeps_a_rhythmic_bed_between_vocalists() -> None:
    frames = 44100 * 8
    time = np.arange(frames) / 44100.0
    empty = np.zeros((frames, 2), dtype="float32")
    drums = np.column_stack((np.sin(time * 2 * np.pi * 120.0),) * 2).astype("float32") * 0.08
    left_vocal = np.zeros_like(empty)
    right_vocal = np.zeros_like(empty)
    left_vocal[:, 0] = 0.08
    right_vocal[:, 1] = 0.08
    left = {"vocals": left_vocal, "drums": drums, "bass": empty, "other": empty}
    right = {"vocals": right_vocal, "drums": drums, "bass": empty, "other": empty}
    result = mix_loop_bridge_transition(left, right, 1.0, 1.0, bars=4)
    middle = result[frames * 2 // 5 : frames * 3 // 5]
    assert result.shape == (frames, 2)
    assert np.isfinite(result).all()
    assert float(np.sqrt(np.mean(np.square(middle)))) > 0.025
    # The center is carried by drums/loop, not two lead vocal constants.
    assert abs(float(np.mean(middle[:, 0]))) < 0.01
    assert abs(float(np.mean(middle[:, 1]))) < 0.01


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


def test_grid_repair_works_on_a_normal_sixteen_bar_transition() -> None:
    sample_rate = 44100
    seconds = 32
    frames = sample_rate * seconds
    outgoing = np.zeros((frames, 2), dtype="float32")
    incoming = np.zeros((frames, 2), dtype="float32")
    click = np.asarray([1.0, 0.65, 0.35, 0.15], dtype="float32")
    for beat in np.arange(0.5, seconds - 0.5, 0.5):
        left = round(beat * sample_rate)
        right = round((0.055 + beat * 1.0015) * sample_rate)
        outgoing[left : left + len(click)] = click[:, None]
        incoming[right : right + len(click)] = click[:, None]
    times, lags = _drum_alignment_curve(outgoing, incoming, 120.0)
    assert len(times) == len(lags) == 4
    assert 0.05 < lags[0] < lags[-1] < 0.12


def test_grid_repair_detects_a_full_beat_downbeat_error() -> None:
    sample_rate = 44100
    seconds = 32
    frames = sample_rate * seconds
    outgoing = np.zeros((frames, 2), dtype="float32")
    incoming = np.zeros((frames, 2), dtype="float32")
    click = np.asarray([1.0, 0.6, 0.25, 0.1], dtype="float32")
    for index, beat in enumerate(np.arange(0.5, seconds - 0.5, 0.5)):
        accent = 1.0 if index % 4 == 0 else 0.1
        left = round(beat * sample_rate)
        right = round((beat + 0.5) * sample_rate)
        outgoing[left : left + len(click)] = click[:, None] * accent
        incoming[right : right + len(click)] = click[:, None] * accent
    intercept, slope = _drum_alignment_transform(outgoing, incoming, 120.0)
    assert abs(intercept - 0.5) < 0.025
    assert abs(slope) < 0.0005


def _intelligent_track(path: str) -> TrackAnalysis:
    return TrackAnalysis(
        path=path,
        duration=64.0,
        bpm=120.0,
        beat_times=[index * 0.5 for index in range(128)],
        downbeat_offset=0,
        cue_in=0.0,
        cue_out=32.0,
        active_end=63.5,
        rms_db=-12.0,
        beat_confidence=0.95,
        transition_bars=8,
        phrase_offset=0,
        musical_key="C major",
        camelot_key="8B",
        key_confidence=0.9,
    )


def test_event_schedule_uses_detected_lyric_boundaries() -> None:
    left = _intelligent_track("left.wav")
    right = _intelligent_track("right.wav")
    section = SectionSpan(0.0, 64.0, "verse", 0.6, 0.5, 0.5, 0.5, 0.9)
    left_words = VocalTranscript(
        left.path,
        "test",
        "en",
        [WordTimestamp("done.", 5.0, 5.2, 0.99)],
        [LyricPhrase(3.0, 5.2, "done.")],
        0.99,
    )
    right_words = VocalTranscript(
        right.path,
        "test",
        "en",
        [WordTimestamp("hello", 12.8, 13.1, 0.99)],
        [LyricPhrase(12.8, 15.0, "hello")],
        0.99,
    )
    events = _event_schedule(
        TrackIntelligence(left.path, [section], left_words),
        TrackIntelligence(right.path, [section], right_words),
        VocalMap(left.path, "test", 0.25, [[0.0, 5.2]], 0.3, 1.0),
        VocalMap(right.path, "test", 0.25, [[12.8, 16.0]], 0.2, 1.0),
        0.0,
        0.0,
        16.0,
        16.0,
        None,
    )
    assert abs(events["outgoing_vocal_exit"] - 0.325) < 0.01
    assert abs(events["incoming_vocal_entry"] - 0.8) < 0.01
    assert events["outgoing_vocal_exit"] < events["bass_handoff"] < events["incoming_vocal_entry"]


def test_candidates_are_ranked_and_explain_their_scores() -> None:
    left = _intelligent_track("left.wav")
    right = _intelligent_track("right.wav")
    left_vocals = VocalMap(left.path, "test", 0.25, [[0.0, 22.0]], 0.34, 1.0)
    right_vocals = VocalMap(right.path, "test", 0.25, [[24.0, 60.0]], 0.56, 1.0)
    section = SectionSpan(0.0, 64.0, "verse", 0.6, 0.5, 0.5, 0.5, 0.9)
    left_transcript = VocalTranscript(
        left.path,
        "test",
        "en",
        [WordTimestamp("done.", 21.9, 22.0, 0.99)],
        [LyricPhrase(20.0, 22.0, "done.")],
        0.99,
    )
    right_transcript = VocalTranscript(
        right.path,
        "test",
        "en",
        [WordTimestamp("start", 24.0, 24.3, 0.99)],
        [LyricPhrase(24.0, 26.0, "start")],
        0.99,
    )
    candidates = rank_transition_candidates(
        left,
        right,
        left_vocals,
        right_vocals,
        TrackIntelligence(left.path, [section], left_transcript),
        TrackIntelligence(right.path, [section], right_transcript),
        target_bpm=120.0,
    )
    assert len(candidates) > 1
    assert candidates == sorted(candidates, key=lambda item: item.score, reverse=True)
    assert "word_boundaries" in candidates[0].score_breakdown
    assert candidates[0].reasons
    assert candidates[0].from_cue in left.beat_times[::32]
    assert candidates[0].to_cue in right.beat_times[::32]

    later = rank_transition_candidates(
        left,
        right,
        left_vocals,
        right_vocals,
        TrackIntelligence(left.path, [section], left_transcript),
        TrackIntelligence(right.path, [section], right_transcript),
        target_bpm=120.0,
        minimum_from_cue=30.0,
    )
    assert later
    assert all(candidate.from_cue >= 30.0 for candidate in later)


def test_planner_prefers_intro_then_cut_on_incoming_drop() -> None:
    left = _intelligent_track("left.wav")
    right = _intelligent_track("right.wav")
    quiet_left = VocalMap(left.path, "test", 0.25, [], 0.0, 1.0)
    quiet_right = VocalMap(right.path, "test", 0.25, [], 0.0, 1.0)
    left_sections = [SectionSpan(0.0, 64.0, "outro", 0.6, 0.5, 0.5, 0.0, 0.9)]
    right_sections = [
        SectionSpan(0.0, 24.0, "verse", 0.4, 0.5, 0.4, 0.0, 0.9),
        SectionSpan(24.0, 64.0, "drop", 0.9, 0.6, 0.9, 0.0, 0.9),
    ]
    candidates = rank_transition_candidates(
        left,
        right,
        quiet_left,
        quiet_right,
        TrackIntelligence(left.path, left_sections, None),
        TrackIntelligence(right.path, right_sections, None),
        target_bpm=120.0,
    )
    assert candidates[0].technique == "drop_cut"
    assert candidates[0].to_cue == 16.0
    assert candidates[0].drop_position == 0.5
    assert candidates[0].score_breakdown["drop_opportunity"] > 0.9


def test_long_stem_planner_avoids_two_quiet_breakdowns() -> None:
    left = _intelligent_track("left.wav")
    right = _intelligent_track("right.wav")
    quiet = VocalMap("quiet", "test", 0.25, [], 0.0, 1.0)
    left_sections = [
        SectionSpan(0.0, 32.0, "breakdown", 0.08, 0.3, 0.2, 0.0, 0.9),
        SectionSpan(32.0, 64.0, "drop", 0.90, 0.7, 0.9, 0.0, 0.9),
    ]
    right_sections = [
        SectionSpan(0.0, 32.0, "breakdown", 0.06, 0.3, 0.2, 0.0, 0.9),
        SectionSpan(32.0, 64.0, "drop", 0.92, 0.7, 0.9, 0.0, 0.9),
    ]
    candidates = rank_transition_candidates(
        left,
        right,
        quiet,
        quiet,
        TrackIntelligence(left.path, left_sections, None),
        TrackIntelligence(right.path, right_sections, None),
        target_bpm=120.0,
        technique="stem_phrase",
    )
    assert candidates
    assert "transition_energy_floor" in candidates[0].score_breakdown
    assert candidates[0].score_breakdown["drop_opportunity"] == 0.0
