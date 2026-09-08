from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

import librosa
import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt, sosfiltfilt

from .analysis import TrackAnalysis, analyze_track
from .intelligence import TrackIntelligence, camelot_compatibility, rank_transition_candidates
from .stems import VocalMap, choose_vocal_safe_cues, separate_stems, separate_vocals


SAMPLE_RATE = 44100
CHANNELS = 2
BLOCK_FRAMES = SAMPLE_RATE * 2
LIMITER_FILTER = "alimiter=limit=0.841395:attack=5:release=80:level=false"
STEM_TECHNIQUES = frozenset({"stem_phrase", "loop_bridge"})


@dataclass(frozen=True)
class Transition:
    from_path: str
    to_path: str
    set_time: float
    from_cue: float
    to_cue: float
    duration: float
    bars: int
    tempo_ratio_from: float
    tempo_ratio_to: float
    technique: str = "bass_swap"
    vocal_overlap: float | None = None
    phase_adjustment_ms: float = 0.0
    local_bpm_from: float | None = None
    local_bpm_to: float | None = None
    candidate_score: float | None = None
    score_breakdown: dict[str, float] | None = None
    reasons: list[str] | None = None
    from_section: str | None = None
    to_section: str | None = None
    drop_position: float | None = None
    harmonic_compatibility: float = 0.5
    events: dict[str, float] | None = None


@dataclass(frozen=True)
class MixPlan:
    target_bpm: float
    tracks: list[TrackAnalysis]
    transitions: list[Transition]
    estimated_duration: float
    ranked_candidates: list[list[dict]] | None = None
    track_intelligence: dict[str, dict] | None = None

    def to_dict(self) -> dict:
        payload = {
            "target_bpm": self.target_bpm,
            "tracks": [asdict(track) for track in self.tracks],
            "transitions": [asdict(item) for item in self.transitions],
            "estimated_duration": self.estimated_duration,
        }
        if self.ranked_candidates is not None:
            payload["ranked_candidates"] = self.ranked_candidates
        if self.track_intelligence is not None:
            payload["track_intelligence"] = self.track_intelligence
        return payload


def _tempo_at(track: TrackAnalysis, second: float) -> float:
    if not track.tempo_map:
        return track.bpm
    times = np.asarray([item[0] for item in track.tempo_map], dtype=np.float64)
    tempos = np.asarray([item[1] for item in track.tempo_map], dtype=np.float64)
    return float(np.interp(second, times, tempos))


def analyze_ordered(
    paths: Iterable[Path],
    *,
    transition_bars: int = 16,
    workers: int = 3,
    progress: Callable[[str], None] | None = None,
) -> list[TrackAnalysis]:
    ordered = list(paths)
    if progress:
        progress(f"Analyzing {len(ordered)} tracks with {workers} workers")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(analyze_track, path, transition_bars=transition_bars)
            for path in ordered
        ]
        results = []
        for path, future in zip(ordered, futures):
            result = future.result()
            results.append(result)
            if progress:
                progress(f"{result.bpm:6.2f} BPM  {path.name}")
    return results


def create_plan(
    analyses: list[TrackAnalysis],
    *,
    target_bpm: float | None = None,
    max_tempo_change: float = 0.08,
    vocal_maps: dict[str, VocalMap] | None = None,
    intelligence: dict[str, TrackIntelligence] | None = None,
    technique: str = "auto",
) -> MixPlan:
    if len(analyses) < 2:
        raise ValueError("A mix needs at least two tracks")
    target = analyses[0].bpm if target_bpm is None else float(target_bpm)
    for track in analyses:
        change = abs(target / track.bpm - 1.0)
        if change > max_tempo_change:
            raise ValueError(
                f"Tempo difference is too large for the prototype: {Path(track.path).name} "
                f"is {track.bpm:.2f} BPM versus {target:.2f} BPM ({change:.1%})"
            )

    transitions: list[Transition] = []
    ranked_candidates: list[list[dict]] = []
    set_time = 0.0
    current_source = 0.0
    varied_techniques = (
        "bass_swap",
        "highpass_out",
        "lowpass_reveal",
        "echo_out",
        "loop_filter",
        "reverb_tail",
        "filter_sweep",
    )
    for transition_index, (left, right) in enumerate(zip(analyses[:-1], analyses[1:])):
        left_ratio = target / left.bpm
        right_ratio = target / right.bpm
        left_source_cue = left.cue_out
        right_source_cue = right.cue_in
        vocal_overlap: float | None = None
        selected_candidate = None
        pair_candidates = []
        # Once a track has entered through the previous transition, never
        # rewind it to reach a highly scored earlier cue. Reserve eight bars
        # of solo playback when the track is long enough.
        minimum_left_source_cue = 0.0
        if transition_index > 0:
            solo_seconds = 8.0 * 4.0 * 60.0 / target
            minimum_left_source_cue = (current_source + solo_seconds) * left_ratio
        if (
            vocal_maps
            and intelligence
            and left.path in vocal_maps
            and right.path in vocal_maps
            and left.path in intelligence
            and right.path in intelligence
        ):
            pair_candidates = rank_transition_candidates(
                left,
                right,
                vocal_maps[left.path],
                vocal_maps[right.path],
                intelligence[left.path],
                intelligence[right.path],
                target_bpm=target,
                technique=technique,
                minimum_from_cue=minimum_left_source_cue,
            )
            if pair_candidates:
                selected_candidate = pair_candidates[0]
                left_source_cue = selected_candidate.from_cue
                right_source_cue = selected_candidate.to_cue
                vocal_overlap = selected_candidate.vocal_overlap
        if selected_candidate is None and vocal_maps and left.path in vocal_maps and right.path in vocal_maps:
            left_source_cue, right_source_cue, vocal_overlap = choose_vocal_safe_cues(
                left,
                right,
                vocal_maps[left.path],
                vocal_maps[right.path],
            )
        if left_source_cue < minimum_left_source_cue:
            required_beats = left.transition_bars * 4
            later_phrases = [
                left.beat_times[index]
                for index in range(left.phrase_offset, len(left.beat_times), 32)
                if left.beat_times[index] >= minimum_left_source_cue
                and index + required_beats < len(left.beat_times)
                and left.beat_times[index + required_beats] <= left.active_end + 0.2
            ]
            if later_phrases:
                left_source_cue = later_phrases[0]
            else:
                # A short song may not fit both eight solo bars and the full
                # transition. It may mix again immediately, but it must never
                # replay already-consumed material.
                consumed_source = current_source * left_ratio
                no_rewind_phrases = [
                    left.beat_times[index]
                    for index in range(left.phrase_offset, len(left.beat_times), 32)
                    if left.beat_times[index] >= consumed_source
                    and index + required_beats < len(left.beat_times)
                    and left.beat_times[index + required_beats] <= left.active_end + 0.2
                ]
                if no_rewind_phrases:
                    left_source_cue = no_rewind_phrases[0]
                else:
                    left_source_cue = consumed_source
        left_local_bpm = _tempo_at(left, left_source_cue)
        right_local_bpm = _tempo_at(right, right_source_cue)
        duration = left.transition_bars * 4.0 * 60.0 / target
        left_phase = _local_transient_phase(
            left.path,
            left_source_cue,
            left_local_bpm,
            duration * left_ratio,
        )
        right_phase = _local_transient_phase(
            right.path,
            right_source_cue,
            right_local_bpm,
            duration * right_ratio,
        )
        source_shift = right_phase - right_ratio * left_phase / left_ratio
        source_shift = float(np.clip(source_shift, -0.12, 0.12))
        if abs(source_shift / right_ratio) < 0.04:
            source_shift = 0.0
        right_source_cue = max(0.0, right_source_cue + source_shift)
        left_cue = left_source_cue / left_ratio
        right_cue = right_source_cue / right_ratio
        selected = selected_candidate.technique if selected_candidate is not None else technique
        if selected_candidate is None and technique == "varied":
            selected = varied_techniques[transition_index % len(varied_techniques)]
        elif selected_candidate is None and technique == "auto":
            if vocal_overlap is None:
                selected = "bass_swap"
            elif left.transition_bars >= 16:
                selected = "stem_phrase"
            else:
                left_density = vocal_maps[left.path].activity_fraction(
                    left_source_cue,
                    left_source_cue + duration * left_ratio,
                )
                right_density = vocal_maps[right.path].activity_fraction(
                    right_source_cue,
                    right_source_cue + duration * right_ratio,
                )
                if vocal_overlap <= 0.04 and max(left_density, right_density) < 0.25:
                    selected = "loop_filter"
                elif vocal_overlap <= 0.08:
                    selected = "filter_sweep"
                else:
                    selected = "echo_out"
        set_time += max(0.0, left_cue - current_source)
        transitions.append(
            Transition(
                from_path=left.path,
                to_path=right.path,
                set_time=round(set_time, 6),
                from_cue=round(left_cue, 6),
                to_cue=round(right_cue, 6),
                duration=round(duration, 6),
                bars=left.transition_bars,
                tempo_ratio_from=round(left_ratio, 8),
                tempo_ratio_to=round(right_ratio, 8),
                technique=selected,
                vocal_overlap=vocal_overlap,
                phase_adjustment_ms=round(source_shift / right_ratio * 1000.0, 3),
                local_bpm_from=round(left_local_bpm, 6),
                local_bpm_to=round(right_local_bpm, 6),
                candidate_score=selected_candidate.score if selected_candidate else None,
                score_breakdown=selected_candidate.score_breakdown if selected_candidate else None,
                reasons=selected_candidate.reasons if selected_candidate else None,
                from_section=selected_candidate.from_section if selected_candidate else None,
                to_section=selected_candidate.to_section if selected_candidate else None,
                drop_position=selected_candidate.drop_position if selected_candidate else None,
                harmonic_compatibility=round(
                    camelot_compatibility(left.camelot_key, right.camelot_key),
                    4,
                ),
                events=selected_candidate.events if selected_candidate else None,
            )
        )
        ranked_candidates.append([candidate.to_dict() for candidate in pair_candidates])
        set_time += duration
        current_source = right_cue + duration

    last = analyses[-1]
    last_ratio = target / last.bpm
    estimated = set_time + max(0.0, last.active_end / last_ratio - current_source)
    serialized_intelligence = (
        {path: value.to_dict() for path, value in intelligence.items()}
        if intelligence is not None
        else None
    )
    return MixPlan(
        target,
        analyses,
        transitions,
        round(estimated, 6),
        ranked_candidates=ranked_candidates if intelligence is not None else None,
        track_intelligence=serialized_intelligence,
    )


def _stretched_audio_path(source: Path, tempo_ratio: float, cache_dir: Path) -> Path:
    stat = source.stat()
    key = hashlib.sha256(
        f"{source}:{stat.st_size}:{stat.st_mtime_ns}:{tempo_ratio:.10f}:v3".encode()
    ).hexdigest()[:24]
    output = cache_dir / f"{key}.wav"
    if output.exists():
        return output
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".partial.wav")
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-af",
        f"atempo={tempo_ratio:.10f}",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        "-c:a",
        "pcm_f32le",
        str(temporary),
    ]
    subprocess.run(command, check=True)
    temporary.replace(output)
    return output


def _stretched_audio_segment_path(
    source: Path,
    tempo_ratio: float,
    start: float,
    duration: float,
    cache_dir: Path,
) -> Path:
    """Time-stretch only the window needed by an interactive handoff."""
    stat = source.stat()
    start = max(0.0, float(start))
    duration = max(0.05, float(duration))
    key = hashlib.sha256(
        (
            f"{source}:{stat.st_size}:{stat.st_mtime_ns}:{tempo_ratio:.10f}:"
            f"{start:.6f}:{duration:.6f}:segment-v1"
        ).encode()
    ).hexdigest()[:24]
    output = cache_dir / f"{key}.wav"
    if output.exists():
        return output
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".partial.wav")
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-ss",
        f"{start * tempo_ratio:.9f}",
        "-i",
        str(source),
        "-vn",
        "-t",
        f"{duration:.9f}",
        "-af",
        f"atempo={tempo_ratio:.10f}",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        "-c:a",
        "pcm_f32le",
        str(temporary),
    ]
    subprocess.run(command, check=True)
    temporary.replace(output)
    return output


def _stretched_path(track: TrackAnalysis, target_bpm: float, cache_dir: Path) -> Path:
    return _stretched_audio_path(
        Path(track.path),
        target_bpm / track.bpm,
        cache_dir,
    )


def _stretched_stems(
    track: TrackAnalysis,
    target_bpm: float,
    cache_dir: Path,
) -> tuple[Path, Path]:
    vocals, accompaniment = separate_vocals(track.path)
    ratio = target_bpm / track.bpm
    return (
        _stretched_audio_path(vocals, ratio, cache_dir),
        _stretched_audio_path(accompaniment, ratio, cache_dir),
    )


def _stretched_four_stems(
    track: TrackAnalysis,
    target_bpm: float,
    cache_dir: Path,
) -> dict[str, Path]:
    stems = separate_stems(track.path)
    ratio = target_bpm / track.bpm
    return {
        name: _stretched_audio_path(path, ratio, cache_dir)
        for name, path in stems.items()
    }


def _stretched_four_stem_segments(
    track: TrackAnalysis,
    target_bpm: float,
    start: float,
    duration: float,
    cache_dir: Path,
) -> dict[str, Path]:
    stems = separate_stems(track.path)
    ratio = target_bpm / track.bpm
    return {
        name: _stretched_audio_segment_path(path, ratio, start, duration, cache_dir)
        for name, path in stems.items()
    }


def _track_gain(track: TrackAnalysis, target_db: float = -15.0) -> float:
    gain_db = float(np.clip(target_db - track.rms_db, -6.0, 6.0))
    return float(10.0 ** (gain_db / 20.0))


def _local_transient_phase(
    path: str,
    cue: float,
    bpm: float,
    duration: float,
) -> float:
    """Measure the median attack displacement around a chosen beat phrase."""
    margin = 0.12
    y, sr = librosa.load(
        path,
        sr=22050,
        mono=True,
        offset=max(0.0, cue - margin),
        duration=duration + 2.0 * margin,
    )
    hop = 64
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    period = 60.0 / bpm
    offsets: list[float] = []
    for beat in np.arange(margin, max(margin, len(y) / sr - margin), period):
        center = round(beat * sr / hop)
        radius = max(1, round(0.08 * sr / hop))
        left = max(0, center - radius)
        right = min(len(onset), center + radius + 1)
        if right <= left:
            continue
        peak = left + int(np.argmax(onset[left:right]))
        offsets.append((peak - center) * hop / sr)
    return float(np.median(offsets)) if offsets else 0.0


def _read_segment(
    handle: sf.SoundFile,
    start: int,
    frames: int,
    *,
    pad: bool = True,
) -> np.ndarray:
    handle.seek(max(0, min(start, len(handle))))
    data = handle.read(frames, dtype="float32", always_2d=True)
    if pad and len(data) < frames:
        data = np.pad(data, ((0, frames - len(data)), (0, 0)))
    return data


def _smoothstep(value: np.ndarray) -> np.ndarray:
    clipped = np.clip(value, 0.0, 1.0)
    return clipped * clipped * (3.0 - 2.0 * clipped)


def _split_low(data: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    sos = butter(4, 180.0, btype="lowpass", fs=SAMPLE_RATE, output="sos")
    low = sosfiltfilt(sos, data, axis=0).astype(np.float32)
    return low, data - low


def _split_three(data: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    low_sos = butter(4, 180.0, btype="lowpass", fs=SAMPLE_RATE, output="sos")
    high_sos = butter(4, 2600.0, btype="highpass", fs=SAMPLE_RATE, output="sos")
    low = sosfiltfilt(low_sos, data, axis=0).astype(np.float32)
    high = sosfiltfilt(high_sos, data, axis=0).astype(np.float32)
    return low, data - low - high, high


def _equal_power(position: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    angle = np.clip(position, 0.0, 1.0) * (math.pi / 2.0)
    return np.cos(angle)[:, None], np.sin(angle)[:, None]


def _stabilize_transition_energy(
    mixed: np.ndarray,
    outgoing_reference: np.ndarray,
    incoming_reference: np.ndarray,
) -> np.ndarray:
    """Lift only unexpectedly empty transition pockets with a smooth envelope."""
    block_frames = SAMPLE_RATE // 2

    def block_rms(signal: np.ndarray) -> np.ndarray:
        return np.asarray(
            [
                float(np.sqrt(np.mean(np.square(signal[start : start + block_frames])) + 1e-12))
                for start in range(0, len(signal), block_frames)
            ],
            dtype=np.float64,
        )

    mixed_rms = block_rms(mixed)
    left_rms = block_rms(outgoing_reference)
    right_rms = block_rms(incoming_reference)
    if not len(mixed_rms):
        return mixed
    left_reference = float(np.percentile(left_rms, 75))
    right_reference = float(np.percentile(right_rms, 75))
    positions = (np.arange(len(mixed_rms), dtype=np.float64) + 0.5) / len(mixed_rms)
    reference = (1.0 - positions) * left_reference + positions * right_reference
    floor = 0.66 * reference
    gains = np.clip(floor / np.maximum(mixed_rms, 1e-6), 1.0, 3.5)

    # Prevent pumping: attack over two half-second blocks and release over four.
    for index in range(1, len(gains)):
        coefficient = 0.50 if gains[index] > gains[index - 1] else 0.25
        gains[index] = gains[index - 1] + coefficient * (gains[index] - gains[index - 1])
    for index in range(len(gains) - 2, -1, -1):
        if gains[index] > gains[index + 1]:
            gains[index] = gains[index + 1] + 0.35 * (gains[index] - gains[index + 1])

    centers = (np.arange(len(gains), dtype=np.float64) + 0.5) * block_frames
    frame_gain = np.interp(
        np.arange(len(mixed), dtype=np.float64),
        centers,
        gains,
        left=float(gains[0]),
        right=float(gains[-1]),
    )
    # Join the untouched tracks without a gain discontinuity.
    edge = min(SAMPLE_RATE, len(frame_gain) // 8)
    if edge > 1:
        frame_gain[:edge] = 1.0 + (frame_gain[:edge] - 1.0) * np.linspace(0.0, 1.0, edge)
        frame_gain[-edge:] = 1.0 + (frame_gain[-edge:] - 1.0) * np.linspace(1.0, 0.0, edge)
    return mixed * frame_gain[:, None].astype(np.float32)


def _filter_sweep_mix(outgoing: np.ndarray, incoming: np.ndarray) -> np.ndarray:
    low_a, mid_a, high_a = _split_three(outgoing)
    low_b, mid_b, high_b = _split_three(incoming)
    position = np.linspace(0.0, 1.0, len(outgoing), endpoint=False, dtype=np.float32)
    high_ga, high_gb = _equal_power(position)
    mid_ga, mid_gb = _equal_power(_smoothstep((position - 0.12) / 0.76))
    low_ga, low_gb = _equal_power(_smoothstep((position - 0.36) / 0.28))
    return (
        low_a * low_ga
        + low_b * low_gb
        + mid_a * mid_ga
        + mid_b * mid_gb
        + high_a * high_ga
        + high_b * high_gb
    )


def _echo_handoff_mix(
    outgoing: np.ndarray,
    incoming: np.ndarray,
    beat_frames: int,
) -> np.ndarray:
    """Hand off rhythmic ownership before adding a non-bass echo tail."""
    low_a, mid_a, high_a = _split_three(outgoing)
    low_b, mid_b, high_b = _split_three(incoming)
    position = np.linspace(0.0, 1.0, len(outgoing), endpoint=False, dtype=np.float32)
    low_ga, low_gb = _equal_power(_smoothstep((position - 0.34) / 0.20))
    mid_ga, mid_gb = _equal_power(_smoothstep((position - 0.25) / 0.45))
    high_ga, high_gb = _equal_power(_smoothstep((position - 0.18) / 0.55))
    mixed = low_a * low_ga + low_b * low_gb
    mixed += mid_a * mid_ga + mid_b * mid_gb
    mixed += high_a * high_ga + high_b * high_gb

    # Echo only material above 2.6 kHz. Delaying the full outgoing master leaves
    # old kicks and bass notes underneath the established incoming groove.
    tail_fade = 1.0 - _smoothstep((position - 0.70) / 0.24)
    mixed += _echo_tail(high_a, beat_frames) * tail_fade[:, None] * 0.22
    return mixed


def _echo_tail(signal: np.ndarray, beat_frames: int) -> np.ndarray:
    result = np.zeros_like(signal)
    for repeat, decay in ((1, 0.24), (2, 0.14), (3, 0.08)):
        delay = beat_frames * repeat
        if delay < len(signal):
            result[delay:] += signal[:-delay] * decay
    position = np.linspace(0.0, 1.0, len(signal), endpoint=False, dtype=np.float32)
    return result * _smoothstep((position - 0.38) / 0.30)[:, None]


def _reverb_tail(signal: np.ndarray) -> np.ndarray:
    result = np.zeros_like(signal)
    taps = ((0.071, 0.13), (0.113, 0.10), (0.173, 0.075), (0.263, 0.055), (0.397, 0.035))
    for seconds, decay in taps:
        delay = round(seconds * SAMPLE_RATE)
        if delay < len(signal):
            result[delay:] += signal[:-delay] * decay
    position = np.linspace(0.0, 1.0, len(signal), endpoint=False, dtype=np.float32)
    return result * _smoothstep((position - 0.45) / 0.25)[:, None]


def _loop_roll(signal: np.ndarray, beat_frames: int) -> np.ndarray:
    result = signal.copy()
    loop_frames = beat_frames * 4
    start = len(signal) // 2
    source_start = max(0, start - loop_frames)
    loop = signal[source_start:start]
    if len(loop) == 0:
        return result
    result[start:] = np.resize(loop, result[start:].shape)
    return result


def _swept_filter(
    signal: np.ndarray,
    start_hz: float,
    end_hz: float,
    kind: str,
) -> np.ndarray:
    """Apply a continuously stepped cutoff while preserving filter state."""
    result = np.empty_like(signal)
    state: np.ndarray | None = None
    chunk = 1024
    total = max(1, len(signal))
    for start in range(0, len(signal), chunk):
        end = min(len(signal), start + chunk)
        position = (start + end) / (2.0 * total)
        cutoff = start_hz * ((end_hz / start_hz) ** position)
        cutoff = float(np.clip(cutoff, 25.0, SAMPLE_RATE / 2.0 - 100.0))
        sos = butter(2, cutoff, btype=kind, fs=SAMPLE_RATE, output="sos")
        if state is None:
            state = np.zeros((len(sos), 2, signal.shape[1]), dtype=np.float64)
        filtered, state = sosfilt(sos, signal[start:end], axis=0, zi=state)
        result[start:end] = filtered
    return result


def _cutoff_mix(outgoing: np.ndarray, incoming: np.ndarray, mode: str) -> np.ndarray:
    position = np.linspace(0.0, 1.0, len(outgoing), endpoint=False, dtype=np.float32)
    gain_a, gain_b = _equal_power(position)
    if mode in {"both", "highpass_out"}:
        outgoing = _swept_filter(outgoing, 35.0, 5200.0, "highpass")
    if mode in {"both", "lowpass_reveal"}:
        incoming = _swept_filter(incoming, 420.0, 19000.0, "lowpass")
    return outgoing * gain_a + incoming * gain_b


def mix_transition(
    outgoing: np.ndarray,
    incoming: np.ndarray,
    outgoing_gain: float,
    incoming_gain: float,
    technique: str = "bass_swap",
    bars: int = 16,
    drop_position: float | None = None,
) -> np.ndarray:
    frames = min(len(outgoing), len(incoming))
    outgoing = outgoing[:frames] * outgoing_gain
    incoming = incoming[:frames] * incoming_gain
    supported = {
        "bass_swap",
        "filter_sweep",
        "highpass_out",
        "lowpass_reveal",
        "echo_out",
        "loop_filter",
        "reverb_tail",
        "drop_cut",
    }
    if technique not in supported:
        raise ValueError(f"Unsupported transition technique: {technique}")
    beat_frames = max(1, frames // max(1, bars * 4))

    if technique == "bass_swap":
        low_a, high_a = _split_low(outgoing)
        low_b, high_b = _split_low(incoming)
        position = np.linspace(0.0, 1.0, frames, endpoint=False, dtype=np.float32)
        high_gain_a, high_gain_b = _equal_power(position)
        low_gain_a, low_gain_b = _equal_power(_smoothstep((position - 0.36) / 0.28))
        mixed = high_a * high_gain_a + high_b * high_gain_b
        mixed += low_a * low_gain_a + low_b * low_gain_b
    elif technique == "filter_sweep":
        mixed = _cutoff_mix(outgoing, incoming, "both")
    elif technique == "highpass_out":
        mixed = _cutoff_mix(outgoing, incoming, "highpass_out")
    elif technique == "lowpass_reveal":
        mixed = _cutoff_mix(outgoing, incoming, "lowpass_reveal")
    elif technique == "echo_out":
        mixed = _echo_handoff_mix(outgoing, incoming, beat_frames)
    elif technique == "loop_filter":
        mixed = _filter_sweep_mix(_loop_roll(outgoing, beat_frames), incoming)
    elif technique == "drop_cut":
        cut = float(np.clip(drop_position if drop_position is not None else 0.5, 0.15, 0.85))
        position = np.linspace(0.0, 1.0, frames, endpoint=False, dtype=np.float32)
        beat = beat_frames / max(frames, 1)
        low_a, mid_a, high_a = _split_three(outgoing)
        low_b, mid_b, high_b = _split_three(incoming)

        # "Cut on the drop" means a decisive change of rhythmic weight, not a
        # full-spectrum edit. Remove the outgoing bass over the final beat,
        # introduce the new bass just after the landmark, and overlap the mids
        # and highs over two/four beats so the musical phrase still connects.
        low_gain_a = np.cos(
            _smoothstep((position - (cut - beat)) / max(beat, 1e-6)) * (math.pi / 2.0)
        )[:, None]
        low_gain_b = np.sin(
            _smoothstep((position - cut) / max(0.25 * beat, 1e-6)) * (math.pi / 2.0)
        )[:, None]
        mid_gain_a, mid_gain_b = _equal_power(
            _smoothstep((position - (cut - beat)) / max(2.0 * beat, 1e-6))
        )
        high_gain_a, high_gain_b = _equal_power(
            _smoothstep((position - (cut - 2.0 * beat)) / max(4.0 * beat, 1e-6))
        )

        # Establish the destination before its drop without exposing its bass.
        incoming_high = mid_b * 0.45 + high_b
        tease = _swept_filter(incoming_high, 650.0, 4200.0, "lowpass")
        tease_start = max(0.0, cut - 8.0 * beat)
        tease_gain = 0.16 * _smoothstep(
            (position - tease_start) / max(cut - tease_start, 1e-6)
        )
        tease_gain *= 1.0 - _smoothstep(
            (position - (cut - 2.0 * beat)) / max(2.0 * beat, 1e-6)
        )

        mixed = low_a * low_gain_a + low_b * low_gain_b
        mixed += mid_a * mid_gain_a + mid_b * mid_gain_b
        mixed += high_a * high_gain_a + high_b * high_gain_b
        mixed += tease * tease_gain[:, None]

        tail_in = _smoothstep((position - cut) / max(0.25 * beat, 1e-6))
        tail_out = 1.0 - _smoothstep(
            (position - (cut + 2.0 * beat)) / max(2.0 * beat, 1e-6)
        )
        mixed += _echo_tail(mid_a + high_a, beat_frames) * (tail_in * tail_out)[:, None] * 0.16
    else:
        mixed = _filter_sweep_mix(outgoing, incoming)
        mixed += _reverb_tail(outgoing)
    return (mixed * 0.93).astype(np.float32)


def mix_stem_transition(
    outgoing_vocals: np.ndarray,
    outgoing_instrumental: np.ndarray,
    incoming_vocals: np.ndarray,
    incoming_instrumental: np.ndarray,
    outgoing_gain: float,
    incoming_gain: float,
) -> np.ndarray:
    """Perform a phrase handoff while keeping dense layers and singers apart."""
    frames = min(
        len(outgoing_vocals),
        len(outgoing_instrumental),
        len(incoming_vocals),
        len(incoming_instrumental),
    )
    vocal_a = outgoing_vocals[:frames] * outgoing_gain
    music_a = outgoing_instrumental[:frames] * outgoing_gain
    vocal_b = incoming_vocals[:frames] * incoming_gain
    music_b = incoming_instrumental[:frames] * incoming_gain
    position = np.linspace(0.0, 1.0, frames, endpoint=False, dtype=np.float32)

    # Keep most of the phrase to one instrumental at a time. Only a narrow
    # center window overlaps; a minute-long equal-power blend sounds like a mash.
    low_a, high_a = _split_low(music_a)
    low_b, high_b = _split_low(music_b)
    high_gain_a = np.cos(
        _smoothstep((position - 0.38) / 0.20) * (math.pi / 2.0)
    )[:, None]
    high_gain_b = np.sin(
        _smoothstep((position - 0.42) / 0.20) * (math.pi / 2.0)
    )[:, None]
    low_gain_a = np.cos(
        _smoothstep((position - 0.43) / 0.08) * (math.pi / 2.0)
    )[:, None]
    low_gain_b = np.sin(
        _smoothstep((position - 0.49) / 0.08) * (math.pi / 2.0)
    )[:, None]
    music_b_filtered = _swept_filter(high_b, 700.0, 19000.0, "lowpass")
    music_a_filtered = _swept_filter(high_a, 30.0, 4200.0, "highpass")

    # Finish the outgoing line, leave a short instrumental pocket, then reveal
    # the incoming singer. This is the important difference from a stereo fade.
    vocal_gain_a = np.cos(
        _smoothstep((position - 0.18) / 0.20) * (math.pi / 2.0)
    )[:, None]
    vocal_gain_b = np.sin(
        _smoothstep((position - 0.68) / 0.20) * (math.pi / 2.0)
    )[:, None]

    mixed = music_a_filtered * high_gain_a + music_b_filtered * high_gain_b
    mixed += low_a * low_gain_a + low_b * low_gain_b
    mixed += vocal_a * vocal_gain_a + vocal_b * vocal_gain_b
    mixed += _reverb_tail(vocal_a * vocal_gain_a) * 0.22
    return (mixed * 0.90).astype(np.float32)


def mix_four_stem_transition(
    outgoing: dict[str, np.ndarray],
    incoming: dict[str, np.ndarray],
    outgoing_gain: float,
    incoming_gain: float,
    harmonic_compatibility: float = 1.0,
    *,
    bars: int = 16,
    events: dict[str, float] | None = None,
) -> np.ndarray:
    """Layer a long transition with independent musical schedules per stem."""
    names = ("vocals", "drums", "bass", "other")
    frames = min(len(outgoing[name]) for name in names)
    frames = min(frames, *(len(incoming[name]) for name in names))
    left = {name: outgoing[name][:frames] * outgoing_gain for name in names}
    right = {name: incoming[name][:frames] * incoming_gain for name in names}
    position = np.linspace(0.0, 1.0, frames, endpoint=False, dtype=np.float32)
    schedule = {
        "outgoing_vocal_exit": 0.42,
        "drum_handoff": 0.46,
        "bass_handoff": 0.50,
        "melody_handoff": 0.54,
        "incoming_vocal_entry": 0.62,
        **(events or {}),
    }
    beat = 1.0 / max(1, bars * 4)

    def fade_out(start: float, end: float) -> np.ndarray:
        return np.cos(_smoothstep((position - start) / (end - start)) * (math.pi / 2.0))[:, None]

    def fade_in(start: float, end: float) -> np.ndarray:
        return np.sin(_smoothstep((position - start) / (end - start)) * (math.pi / 2.0))[:, None]

    # Drums span almost the whole transition, but equal-power scheduling keeps
    # the combined groove stable. Bass changes only around the middle phrase.
    drum_center = schedule["drum_handoff"]
    drum_width = max(8.0 * beat, 0.16)
    drum_a, drum_b = _equal_power(
        _smoothstep((position - (drum_center - drum_width / 2.0)) / drum_width)
    )
    bass_width = 0.24 if harmonic_compatibility >= 0.5 else 0.10
    bass_width = max(4.0 * beat, bass_width)
    bass_center = schedule["bass_handoff"]
    bass_a, bass_b = _equal_power(
        _smoothstep((position - (bass_center - bass_width / 2.0)) / bass_width)
    )

    # Melodic material crosses later and is filtered to avoid a harmonic pileup.
    other_a = _swept_filter(left["other"], 30.0, 3600.0, "highpass")
    other_b = _swept_filter(right["other"], 650.0, 19000.0, "lowpass")
    melody = schedule["melody_handoff"]
    melodic_fade = max(4.0 * beat, 0.08)
    if harmonic_compatibility >= 0.5:
        other_gain_a = fade_out(melody - melodic_fade, melody + melodic_fade)
        other_gain_b = fade_in(melody - melodic_fade, melody + melodic_fade)
    else:
        # Incompatible keys can still share a beat, but not a long melodic
        # overlap. Clear the old harmony before revealing the new one.
        other_gain_a = fade_out(melody - 2.0 * melodic_fade, melody - 0.5 * melodic_fade)
        other_gain_b = fade_in(melody + 0.5 * melodic_fade, melody + 2.0 * melodic_fade)

    # Never present two lead singers together. The instrumental gap is long
    # enough to finish one lyrical thought before the next vocalist appears.
    vocal_fade = max(4.0 * beat, 0.06)
    vocal_exit = schedule["outgoing_vocal_exit"]
    vocal_entry = schedule["incoming_vocal_entry"]
    vocal_gain_a = fade_out(vocal_exit - vocal_fade, vocal_exit)
    vocal_gain_b = fade_in(vocal_entry, vocal_entry + vocal_fade)

    mixed = left["drums"] * drum_a + right["drums"] * drum_b
    mixed += left["bass"] * bass_a + right["bass"] * bass_b
    mixed += other_a * other_gain_a + other_b * other_gain_b
    mixed += left["vocals"] * vocal_gain_a + right["vocals"] * vocal_gain_b
    mixed += _reverb_tail(left["vocals"] * vocal_gain_a) * 0.12
    mixed = _stabilize_transition_energy(
        mixed,
        sum(left.values()),
        sum(right.values()),
    )
    return (mixed * 0.90).astype(np.float32)


def _best_drum_loop(drums: np.ndarray, beat_frames: int) -> np.ndarray:
    """Choose a stable two-bar loop from the first half of a transition."""
    loop_frames = max(64, beat_frames * 8)
    if len(drums) <= loop_frames:
        return drums
    candidates: list[tuple[float, int]] = []
    search_end = max(loop_frames, len(drums) // 2)
    for start in range(0, search_end - loop_frames + 1, loop_frames):
        window = drums[start : start + loop_frames]
        chunks = np.array_split(window, 8)
        rms = np.asarray([np.sqrt(np.mean(np.square(chunk)) + 1e-12) for chunk in chunks])
        energy = float(np.mean(rms))
        steadiness = 1.0 / (1.0 + 4.0 * float(np.std(rms) / max(energy, 1e-9)))
        candidates.append((energy * steadiness, start))
    start = max(candidates)[1] if candidates else 0
    return drums[start : start + loop_frames]


def _repeat_to_length(loop: np.ndarray, frames: int) -> np.ndarray:
    if len(loop) == 0:
        return np.zeros((frames, CHANNELS), dtype=np.float32)
    repeats = math.ceil(frames / len(loop))
    return np.tile(loop, (repeats, 1))[:frames]


def mix_loop_bridge_transition(
    outgoing: dict[str, np.ndarray],
    incoming: dict[str, np.ndarray],
    outgoing_gain: float,
    incoming_gain: float,
    *,
    bars: int,
    harmonic_compatibility: float = 1.0,
    events: dict[str, float] | None = None,
) -> np.ndarray:
    """Use a repeated instrumental phrase as a simple third-deck bridge."""
    names = ("vocals", "drums", "bass", "other")
    frames = min(*(len(outgoing[name]) for name in names), *(len(incoming[name]) for name in names))
    left = {name: outgoing[name][:frames] * outgoing_gain for name in names}
    right = {name: incoming[name][:frames] * incoming_gain for name in names}
    position = np.linspace(0.0, 1.0, frames, endpoint=False, dtype=np.float32)
    schedule = {
        "outgoing_vocal_exit": 0.32,
        "drum_handoff": 0.43,
        "bass_handoff": 0.56,
        "melody_handoff": 0.62,
        "incoming_vocal_entry": 0.74,
        **(events or {}),
    }
    beat = 1.0 / max(1, bars * 4)

    def fade_out(start: float, end: float) -> np.ndarray:
        return np.cos(_smoothstep((position - start) / (end - start)) * (math.pi / 2.0))[:, None]

    def fade_in(start: float, end: float) -> np.ndarray:
        return np.sin(_smoothstep((position - start) / (end - start)) * (math.pi / 2.0))[:, None]

    beat_frames = max(1, frames // max(1, bars * 4))
    loop = _repeat_to_length(_best_drum_loop(left["drums"], beat_frames), frames)
    vocal_exit = schedule["outgoing_vocal_exit"]
    vocal_entry = schedule["incoming_vocal_entry"]
    loop_gain = fade_in(max(0.02, vocal_exit - 8.0 * beat), vocal_exit)
    loop_gain *= fade_out(max(vocal_exit + 4.0 * beat, vocal_entry - 8.0 * beat), vocal_entry) * 0.78

    # The live outgoing groove hands control to a predictable repeated beat.
    # The destination drums can then arrive under it without lyrical or melodic
    # clutter, before the loop disappears and the destination vocalist enters.
    drum = schedule["drum_handoff"]
    bass = schedule["bass_handoff"]
    melody = schedule["melody_handoff"]
    event_fade = max(4.0 * beat, 0.06)
    drum_a = fade_out(drum - 2.0 * event_fade, drum)
    drum_b = fade_in(drum - event_fade, drum + event_fade)
    bass_a = fade_out(bass - event_fade, bass)
    bass_b = fade_in(bass, bass + event_fade)
    other_a = _swept_filter(left["other"], 30.0, 4200.0, "highpass")
    other_b = _swept_filter(right["other"], 900.0, 19000.0, "lowpass")
    if harmonic_compatibility >= 0.5:
        other_gain_a = fade_out(melody - 2.0 * event_fade, melody)
        other_gain_b = fade_in(melody - event_fade, melody + event_fade)
    else:
        other_gain_a = fade_out(melody - 2.0 * event_fade, melody - event_fade)
        other_gain_b = fade_in(melody + event_fade, melody + 2.0 * event_fade)
    vocal_a = fade_out(vocal_exit - event_fade, vocal_exit)
    vocal_b = fade_in(vocal_entry, vocal_entry + event_fade)

    mixed = left["drums"] * drum_a + loop * loop_gain + right["drums"] * drum_b
    mixed += left["bass"] * bass_a + right["bass"] * bass_b
    mixed += other_a * other_gain_a + other_b * other_gain_b
    mixed += left["vocals"] * vocal_a + right["vocals"] * vocal_b
    mixed += _echo_tail(left["vocals"] * vocal_a, beat_frames) * 0.10
    mixed = _stabilize_transition_energy(mixed, sum(left.values()), sum(right.values()))
    return (mixed * 0.88).astype(np.float32)


def _mix_four_stem_technique(
    technique: str,
    outgoing: dict[str, np.ndarray],
    incoming: dict[str, np.ndarray],
    outgoing_gain: float,
    incoming_gain: float,
    transition: Transition,
) -> np.ndarray:
    if technique == "loop_bridge":
        return mix_loop_bridge_transition(
            outgoing,
            incoming,
            outgoing_gain,
            incoming_gain,
            bars=transition.bars,
            harmonic_compatibility=transition.harmonic_compatibility,
            events=transition.events,
        )
    return mix_four_stem_transition(
        outgoing,
        incoming,
        outgoing_gain,
        incoming_gain,
        harmonic_compatibility=transition.harmonic_compatibility,
        bars=transition.bars,
        events=transition.events,
    )


def _drum_alignment_transform(
    outgoing: np.ndarray,
    incoming: np.ndarray,
    bpm: float,
) -> tuple[float, float]:
    """Return a linear summary of the locally repaired drum grid.

    Rendering uses the full piecewise curve below.  This wrapper remains useful
    for diagnostics and for callers that only understand phase plus tempo drift.
    """
    times, lags = _drum_alignment_curve(outgoing, incoming, bpm)
    if len(lags) < 2:
        return 0.0, 0.0
    slope, intercept = np.polyfit(times, lags, 1)
    return float(np.clip(intercept, -1.0, 1.0)), float(np.clip(slope, -0.008, 0.008))


def _drum_alignment_curve(
    outgoing: np.ndarray,
    incoming: np.ndarray,
    bpm: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Repair phase and local drift using four-bar drum-stem landmarks.

    A single BPM and a single phase offset are insufficient when an analysis
    grid slips inside a song.  We correlate short blocks independently and
    return a smoothed lag curve, allowing the renderer to correct each part of
    the transition without changing the rest of either track.
    """
    hop = 128
    left = librosa.onset.onset_strength(y=np.mean(outgoing, axis=1), sr=SAMPLE_RATE, hop_length=hop)
    right = librosa.onset.onset_strength(y=np.mean(incoming, axis=1), sr=SAMPLE_RATE, hop_length=hop)
    usable = min(len(left), len(right))
    # Four-bar blocks give four independent measurements in the common
    # sixteen-bar transition; the old eight-bar/four-block requirement silently
    # disabled repair for that case.
    frames_per_block = max(32, round(4.0 * 4.0 * 60.0 / bpm * SAMPLE_RATE / hop))
    beat_seconds = 60.0 / bpm
    radius_seconds = max(0.25, min(0.75, 1.25 * beat_seconds))
    radius = round(radius_seconds * SAMPLE_RATE / hop)
    # Establish a global phase first.  This can identify a one-beat downbeat
    # error when kicks are accented, while the small penalty keeps an
    # indistinguishable four-on-the-floor pattern close to zero.
    global_left = (left[:usable] - np.mean(left[:usable])) / (np.std(left[:usable]) + 1e-9)
    global_right = (right[:usable] - np.mean(right[:usable])) / (np.std(right[:usable]) + 1e-9)
    global_scores: list[float] = []
    broad_candidates = list(range(-radius, radius + 1))
    beat_frames = max(4, round(beat_seconds * SAMPLE_RATE / hop))
    accent_starts = list(range(0, usable - beat_frames, beat_frames))
    left_accents = np.asarray(
        [float(np.max(left[start : start + beat_frames])) for start in accent_starts]
    )
    right_accents_zero = np.asarray(
        [float(np.max(right[start : start + beat_frames])) for start in accent_starts]
    )

    def metrical_contrast(values: np.ndarray) -> float:
        phase_means = np.asarray([np.mean(values[offset::4]) for offset in range(4)])
        return float((np.max(phase_means) - np.min(phase_means)) / (np.mean(phase_means) + 1e-9))

    has_downbeat_accents = (
        len(left_accents) >= 12
        and metrical_contrast(left_accents) >= 0.25
        and metrical_contrast(right_accents_zero) >= 0.25
    )
    for lag in broad_candidates:
        if lag < 0:
            a, b = global_left[-lag:], global_right[:usable + lag]
        elif lag > 0:
            a, b = global_left[:usable - lag], global_right[lag:]
        else:
            a, b = global_left, global_right
        score = float(np.dot(a, b) / max(1, len(a))) - 0.05 * abs(lag) / max(1, radius)
        valid = [index for index, start in enumerate(accent_starts) if 0 <= start + lag and start + lag + beat_frames <= usable]
        if has_downbeat_accents and len(valid) >= 12:
            right_accents = np.asarray(
                [float(np.max(right[accent_starts[index] + lag : accent_starts[index] + lag + beat_frames])) for index in valid]
            )
            selected_left = left_accents[valid]
            if float(np.std(selected_left)) > 1e-5 and float(np.std(right_accents)) > 1e-5:
                # Beat-bin accents disambiguate a one-beat downbeat error that
                # ordinary sample correlation sees as a perfect kick match.
                score += 0.18 * float(np.corrcoef(selected_left, right_accents)[0, 1])
        global_scores.append(score)
    coarse_lag = broad_candidates[int(np.argmax(global_scores))]
    local_radius = max(2, round(0.22 * SAMPLE_RATE / hop))
    lags: list[float] = []
    times: list[float] = []
    block_count = min(8, math.ceil(usable / frames_per_block))
    for block in range(block_count):
        start = block * frames_per_block
        end = min(usable, (block + 1) * frames_per_block)
        if end - start < max(32, frames_per_block // 2):
            break
        first = left[start:end]
        second = right[start:end]
        if float(np.std(first)) < 1e-5 or float(np.std(second)) < 1e-5:
            continue
        first = (first - np.mean(first)) / (np.std(first) + 1e-9)
        second = (second - np.mean(second)) / (np.std(second) + 1e-9)
        scores: list[float] = []
        candidates = list(
            range(max(-radius, coarse_lag - local_radius), min(radius, coarse_lag + local_radius) + 1)
        )
        for lag in candidates:
            if lag < 0:
                a, b = first[-lag:], second[: len(second) + lag]
            elif lag > 0:
                a, b = first[: len(first) - lag], second[lag:]
            else:
                a, b = first, second
            correlation = float(np.dot(a, b) / max(1, len(a)))
            # A light prior avoids choosing an entire beat of displacement when
            # two unaccented four-on-the-floor patterns are indistinguishable.
            correlation -= 0.01 * abs(lag - coarse_lag) / max(1, local_radius)
            scores.append(correlation)
        best = int(np.argmax(scores))
        lag = candidates[best] * hop / SAMPLE_RATE
        excluded = max(1, round(0.035 * SAMPLE_RATE / hop))
        alternatives = scores[: max(0, best - excluded)] + scores[min(len(scores), best + excluded + 1) :]
        margin = scores[best] - (max(alternatives) if alternatives else 0.0)
        if scores[best] < 0.08 or margin < 0.0004:
            continue
        lags.append(lag)
        times.append((block + 0.5) * frames_per_block * hop / SAMPLE_RATE)
    if len(lags) < 2:
        return np.asarray([], dtype=np.float64), np.asarray([], dtype=np.float64)

    values = np.asarray(lags, dtype=np.float64)
    # Remove isolated octave/beat-period errors, then smooth remaining local
    # measurements without erasing gradual drift.
    if len(values) >= 3:
        padded = np.pad(values, (1, 1), mode="edge")
        median = np.asarray([np.median(padded[index : index + 3]) for index in range(len(values))])
        outlier = np.abs(values - median) > max(0.08, beat_seconds * 0.35)
        values[outlier] = median[outlier]
        padded = np.pad(values, (1, 1), mode="edge")
        values = np.asarray(
            [0.25 * padded[index] + 0.5 * padded[index + 1] + 0.25 * padded[index + 2] for index in range(len(values))]
        )
    if float(np.max(np.abs(values))) < 0.018 and float(np.ptp(values)) < 0.012:
        return np.asarray([], dtype=np.float64), np.asarray([], dtype=np.float64)
    # Reject wildly discontinuous repairs; a genuine local tempo drift changes
    # slowly compared with the four-bar observation window.
    if len(values) > 1 and float(np.max(np.abs(np.diff(values)))) > max(0.20, beat_seconds * 0.60):
        return np.asarray([], dtype=np.float64), np.asarray([], dtype=np.float64)
    return np.asarray(times, dtype=np.float64), np.clip(values, -1.0, 1.0)


def _read_aligned_stems(
    outgoing: dict[str, sf.SoundFile],
    incoming: dict[str, sf.SoundFile],
    cue_from: int,
    cue_to: int,
    frames: int,
    bpm: float,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], int]:
    left = {name: _read_segment(handle, cue_from, frames) for name, handle in outgoing.items()}
    right_probe = _read_segment(incoming["drums"], cue_to, frames)
    repair_times, repair_lags = _drum_alignment_curve(left["drums"], right_probe, bpm)
    output_times = np.arange(frames, dtype=np.float64) / SAMPLE_RATE
    if len(repair_lags):
        lag_curve = np.interp(output_times, repair_times, repair_lags)
        # Limit the local warp to ±0.8% while preserving its measured phase.
        increments = np.clip(np.diff(lag_curve), -0.008 / SAMPLE_RATE, 0.008 / SAMPLE_RATE)
        lag_curve = lag_curve[0] + np.concatenate(([0.0], np.cumsum(increments)))
    else:
        lag_curve = np.zeros(frames, dtype=np.float64)
    positions = cue_to + (output_times + lag_curve) * SAMPLE_RATE
    source_start = max(0, math.floor(float(positions[0])) - 2)
    relative = positions - source_start
    source_frames = math.ceil(float(positions[-1])) - source_start + 3
    right: dict[str, np.ndarray] = {}
    for name, handle in incoming.items():
        source = _read_segment(handle, source_start, source_frames)
        channels = [np.interp(relative, np.arange(len(source)), source[:, channel]) for channel in range(CHANNELS)]
        right[name] = np.column_stack(channels).astype(np.float32)
    consumed_end = round(float(positions[-1]) + 1.0)
    return left, right, consumed_end


def iter_mix_blocks(
    plan: MixPlan,
    *,
    cache_dir: str | Path = ".setmix-cache/stretched",
) -> Iterator[np.ndarray]:
    cache_root = Path(cache_dir)
    prepared = [_stretched_path(track, plan.target_bpm, cache_root) for track in plan.tracks]
    handles = [sf.SoundFile(path) for path in prepared]
    stem_handles: dict[int, dict[str, sf.SoundFile]] = {}
    try:
        stem_indices = {
            index
            for index, transition in enumerate(plan.transitions)
            if transition.technique in STEM_TECHNIQUES
        } | {
            index + 1
            for index, transition in enumerate(plan.transitions)
            if transition.technique in STEM_TECHNIQUES
        }
        for index in sorted(stem_indices):
            stem_paths = _stretched_four_stems(
                plan.tracks[index],
                plan.target_bpm,
                cache_root / "stems4",
            )
            stem_handles[index] = {
                name: sf.SoundFile(path) for name, path in stem_paths.items()
            }
        gains = [_track_gain(track) for track in plan.tracks]
        source_position = 0
        for index, transition in enumerate(plan.transitions):
            outgoing = handles[index]
            incoming = handles[index + 1]
            cue_from = round(transition.from_cue * SAMPLE_RATE)
            cue_to = round(transition.to_cue * SAMPLE_RATE)
            transition_frames = round(transition.duration * SAMPLE_RATE)

            outgoing.seek(min(source_position, len(outgoing)))
            remaining = max(0, cue_from - source_position)
            while remaining:
                count = min(BLOCK_FRAMES, remaining)
                block = outgoing.read(count, dtype="float32", always_2d=True)
                if len(block) == 0:
                    break
                yield (block * gains[index]).astype(np.float32)
                remaining -= len(block)

            if transition.technique in STEM_TECHNIQUES:
                left_stems, right_stems, consumed_end = _read_aligned_stems(
                    stem_handles[index],
                    stem_handles[index + 1],
                    cue_from,
                    cue_to,
                    transition_frames,
                    plan.target_bpm,
                )
                yield _mix_four_stem_technique(
                    transition.technique,
                    left_stems,
                    right_stems,
                    gains[index],
                    gains[index + 1],
                    transition,
                )
                source_position = consumed_end
            else:
                left = _read_segment(outgoing, cue_from, transition_frames)
                right = _read_segment(incoming, cue_to, transition_frames)
                yield mix_transition(
                    left,
                    right,
                    gains[index],
                    gains[index + 1],
                    transition.technique,
                    transition.bars,
                    transition.drop_position,
                )
                source_position = cue_to + transition_frames

        final = handles[-1]
        end = min(len(final), round(plan.tracks[-1].active_end / (plan.target_bpm / plan.tracks[-1].bpm) * SAMPLE_RATE))
        final.seek(min(source_position, len(final)))
        remaining = max(0, end - source_position)
        while remaining:
            count = min(BLOCK_FRAMES, remaining)
            block = final.read(count, dtype="float32", always_2d=True)
            if len(block) == 0:
                break
            yield (block * gains[-1]).astype(np.float32)
            remaining -= len(block)
    finally:
        for handle in handles:
            handle.close()
        for stems in stem_handles.values():
            for handle in stems.values():
                handle.close()


def render_mix(
    plan: MixPlan,
    output: str | Path,
    *,
    preview_dir: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="setmix-") as temp_dir:
        raw = Path(temp_dir) / "mix-float.wav"
        peak = 0.0
        frames_written = 0
        with sf.SoundFile(raw, "w", SAMPLE_RATE, CHANNELS, subtype="FLOAT") as handle:
            for block in iter_mix_blocks(plan):
                peak = max(peak, float(np.max(np.abs(block))))
                handle.write(block)
                frames_written += len(block)
                if progress and frames_written % (SAMPLE_RATE * 60) < BLOCK_FRAMES:
                    progress(f"Rendered {frames_written / SAMPLE_RATE:7.1f}s")

        limiter = LIMITER_FILTER
        codec = "pcm_s24le" if destination.suffix.lower() == ".wav" else "flac"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(raw),
                "-af",
                limiter,
                "-c:a",
                codec,
                str(destination),
            ],
            check=True,
        )

    plan_path = destination.with_suffix(destination.suffix + ".plan.json")
    payload = plan.to_dict()
    payload["render"] = {
        "frames": frames_written,
        "sample_rate": SAMPLE_RATE,
        "pre_limiter_peak_dbfs": 20.0 * math.log10(max(peak, 1e-9)),
    }
    plan_path.write_text(json.dumps(payload, indent=2) + "\n")

    if preview_dir is not None:
        root = Path(preview_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        for index, transition in enumerate(plan.transitions, 1):
            start = max(0.0, transition.set_time - 8.0)
            duration = transition.duration + 16.0
            preview = root / f"transition-{index:02d}.mp3"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    f"{start:.6f}",
                    "-i",
                    str(destination),
                    "-t",
                    f"{duration:.6f}",
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(preview),
                ],
                check=True,
            )
    if progress:
        progress(f"Wrote {destination}")
    return destination


def render_pair_handoff(
    plan: MixPlan,
    output: str | Path,
    *,
    pre_roll_seconds: float = 8.0,
    post_roll_seconds: float = 8.0,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Render a compact browser-ready tail and transition capsule.

    The asset begins shortly before the selected transition.  A player already
    running the outgoing track at ``plan.target_bpm`` can therefore join this
    file at the matching source position, cross the transition, and return to
    live playback of the incoming track after a short post-roll.
    """
    if len(plan.tracks) != 2 or len(plan.transitions) != 1:
        raise ValueError("A handoff render requires exactly two tracks")

    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    transition = plan.transitions[0]
    cache_root = Path(".setmix-cache/capsules")
    start = max(0.0, transition.from_cue - max(0.0, pre_roll_seconds))
    before_duration = transition.from_cue - start
    transition_frames = round(transition.duration * SAMPLE_RATE)
    post_roll_seconds = max(2.0, float(post_roll_seconds))
    outgoing_path = _stretched_audio_segment_path(
        Path(plan.tracks[0].path),
        transition.tempo_ratio_from,
        start,
        before_duration + transition.duration,
        cache_root / "tracks",
    )
    incoming_path = _stretched_audio_segment_path(
        Path(plan.tracks[1].path),
        transition.tempo_ratio_to,
        transition.to_cue,
        transition.duration + post_roll_seconds + 0.5,
        cache_root / "tracks",
    )
    gains = [_track_gain(track) for track in plan.tracks]

    with tempfile.TemporaryDirectory(prefix="setmix-handoff-") as temp_dir:
        raw = Path(temp_dir) / "handoff-float.wav"
        with sf.SoundFile(outgoing_path) as outgoing, sf.SoundFile(incoming_path) as incoming:
            before = _read_segment(
                outgoing,
                0,
                round(before_duration * SAMPLE_RATE),
                pad=False,
            )
            if progress:
                progress("Rendering the selected transition")
            if transition.technique in STEM_TECHNIQUES:
                alignment_margin = 0.3
                left_stem_start = max(0.0, transition.from_cue - alignment_margin)
                right_stem_start = max(0.0, transition.to_cue - alignment_margin)
                left_paths = _stretched_four_stem_segments(
                    plan.tracks[0],
                    plan.target_bpm,
                    left_stem_start,
                    transition.duration + 2 * alignment_margin,
                    cache_root / "stems4",
                )
                right_paths = _stretched_four_stem_segments(
                    plan.tracks[1],
                    plan.target_bpm,
                    right_stem_start,
                    transition.duration + post_roll_seconds + 2 * alignment_margin,
                    cache_root / "stems4",
                )
                left_stems = {name: sf.SoundFile(path) for name, path in left_paths.items()}
                right_stems = {name: sf.SoundFile(path) for name, path in right_paths.items()}
                try:
                    aligned_left, aligned_right, consumed_end = _read_aligned_stems(
                        left_stems,
                        right_stems,
                        round((transition.from_cue - left_stem_start) * SAMPLE_RATE),
                        round((transition.to_cue - right_stem_start) * SAMPLE_RATE),
                        transition_frames,
                        plan.target_bpm,
                    )
                    mixed = _mix_four_stem_technique(
                        transition.technique,
                        aligned_left,
                        aligned_right,
                        gains[0],
                        gains[1],
                        transition,
                    )
                finally:
                    for handle in (*left_stems.values(), *right_stems.values()):
                        handle.close()
                consumed_global = right_stem_start + consumed_end / SAMPLE_RATE
                consumed_incoming = round(
                    max(0.0, consumed_global - transition.to_cue) * SAMPLE_RATE
                )
            else:
                left = _read_segment(outgoing, round(before_duration * SAMPLE_RATE), transition_frames)
                right = _read_segment(incoming, 0, transition_frames)
                mixed = mix_transition(
                    left,
                    right,
                    gains[0],
                    gains[1],
                    transition.technique,
                    transition.bars,
                    transition.drop_position,
                )
                consumed_global = transition.to_cue + transition.duration
                consumed_incoming = transition_frames

            with sf.SoundFile(raw, "w", SAMPLE_RATE, CHANNELS, subtype="FLOAT") as handle:
                handle.write((before * gains[0]).astype(np.float32))
                handle.write(mixed)
                incoming.seek(min(consumed_incoming, len(incoming)))
                remaining = max(
                    0,
                    min(len(incoming), consumed_incoming + round(post_roll_seconds * SAMPLE_RATE))
                    - consumed_incoming,
                )
                written = len(before) + len(mixed)
                while remaining:
                    block = incoming.read(
                        min(BLOCK_FRAMES, remaining), dtype="float32", always_2d=True
                    )
                    if len(block) == 0:
                        break
                    handle.write((block * gains[1]).astype(np.float32))
                    remaining -= len(block)
                    written += len(block)
                    if progress and written % (SAMPLE_RATE * 60) < BLOCK_FRAMES:
                        progress(f"Rendered {written / SAMPLE_RATE:7.1f}s")

        suffix = destination.suffix.lower()
        codec_args = {
            ".flac": ["-c:a", "flac"],
            ".mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
            ".m4a": ["-c:a", "aac", "-b:a", "256k"],
        }.get(suffix, ["-c:a", "pcm_s24le"])
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(raw),
                "-af",
                LIMITER_FILTER,
                *codec_args,
                str(destination),
            ],
            check=True,
        )

    transition_offset = transition.from_cue - start
    incoming_offset = transition_offset + transition.duration
    metadata = {
        "target_bpm": plan.target_bpm,
        "handoff_start": round(start, 6),
        "transition_offset": round(transition_offset, 6),
        "incoming_offset": round(incoming_offset, 6),
        "incoming_consumed": round(consumed_global, 6),
        "incoming_resume_source": round(
            (consumed_global + post_roll_seconds) * transition.tempo_ratio_to,
            6,
        ),
        "post_roll": round(post_roll_seconds, 6),
        "capsule": True,
        "duration": round(sf.info(destination).duration, 6),
        "transition": asdict(transition),
    }
    destination.with_suffix(destination.suffix + ".json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )
    if progress:
        progress(f"Wrote {destination}")
    return metadata


def render_transition_auditions(
    plan: MixPlan,
    output_dir: str | Path,
    *,
    techniques: Iterable[str] = (
        "bass_swap",
        "filter_sweep",
        "highpass_out",
        "lowpass_reveal",
        "echo_out",
        "loop_filter",
        "reverb_tail",
    ),
    selected_only: bool = False,
) -> list[Path]:
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    cache_root = Path(".setmix-cache/stretched")
    prepared = [_stretched_path(track, plan.target_bpm, cache_root) for track in plan.tracks]
    prepared_stems: dict[int, dict[str, Path]] = {}
    stem_indices = {
        index
        for index, transition in enumerate(plan.transitions)
        if transition.technique in STEM_TECHNIQUES
    } | {
        index + 1
        for index, transition in enumerate(plan.transitions)
        if transition.technique in STEM_TECHNIQUES
    }
    for index in sorted(stem_indices):
        prepared_stems[index] = _stretched_four_stems(
            plan.tracks[index],
            plan.target_bpm,
            cache_root / "stems4",
        )
    outputs: list[Path] = []
    context_frames = SAMPLE_RATE * 8
    for index, transition in enumerate(plan.transitions):
        with sf.SoundFile(prepared[index]) as outgoing, sf.SoundFile(prepared[index + 1]) as incoming:
            cue_from = round(transition.from_cue * SAMPLE_RATE)
            cue_to = round(transition.to_cue * SAMPLE_RATE)
            frames = round(transition.duration * SAMPLE_RATE)
            before = _read_segment(outgoing, max(0, cue_from - context_frames), context_frames)
            left = _read_segment(outgoing, cue_from, frames)
            right = _read_segment(incoming, cue_to, frames)
            incoming_active_end = round(
                plan.tracks[index + 1].active_end
                / transition.tempo_ratio_to
                * SAMPLE_RATE
            )
            after_start = cue_to + frames
            after = _read_segment(
                incoming,
                after_start,
                min(context_frames, max(0, incoming_active_end - after_start)),
                pad=False,
            )
            selected_techniques = (transition.technique,) if selected_only else techniques
            for technique in selected_techniques:
                if technique in STEM_TECHNIQUES:
                    left_stems = {
                        name: sf.SoundFile(path) for name, path in prepared_stems[index].items()
                    }
                    right_stems = {
                        name: sf.SoundFile(path) for name, path in prepared_stems[index + 1].items()
                    }
                    try:
                        aligned_left, aligned_right, consumed_end = _read_aligned_stems(
                            left_stems,
                            right_stems,
                            cue_from,
                            cue_to,
                            frames,
                            plan.target_bpm,
                        )
                        audition_transition = Transition(
                            **{
                                **asdict(transition),
                                "technique": technique,
                            }
                        )
                        mixed = _mix_four_stem_technique(
                            technique,
                            aligned_left,
                            aligned_right,
                            _track_gain(plan.tracks[index]),
                            _track_gain(plan.tracks[index + 1]),
                            audition_transition,
                        )
                        after = _read_segment(
                            incoming,
                            consumed_end,
                            min(context_frames, max(0, incoming_active_end - consumed_end)),
                            pad=False,
                        )
                    finally:
                        for handle in (*left_stems.values(), *right_stems.values()):
                            handle.close()
                else:
                    mixed = mix_transition(
                        left,
                        right,
                        _track_gain(plan.tracks[index]),
                        _track_gain(plan.tracks[index + 1]),
                        technique,
                        transition.bars,
                        transition.drop_position if technique == "drop_cut" else None,
                    )
                audition = np.concatenate(
                    (
                        before * _track_gain(plan.tracks[index]),
                        mixed,
                        after * _track_gain(plan.tracks[index + 1]),
                    )
                )
                destination = root / f"transition-{index + 1:02d}-{technique}.mp3"
                with tempfile.TemporaryDirectory(prefix="setmix-audition-") as temp_dir:
                    wave = Path(temp_dir) / "audition.wav"
                    sf.write(wave, audition, SAMPLE_RATE, subtype="FLOAT")
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-v",
                            "error",
                            "-y",
                            "-i",
                            str(wave),
                            "-af",
                            LIMITER_FILTER,
                            "-codec:a",
                            "libmp3lame",
                            "-q:a",
                            "2",
                            str(destination),
                        ],
                        check=True,
                    )
                outputs.append(destination)
    manifest = {
        "plan": plan.to_dict(),
        "auditions": [str(path) for path in outputs],
    }
    (root / "auditions.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return outputs


def stream_mix(
    plan: MixPlan,
    *,
    start_seconds: float = 0.0,
    max_seconds: float | None = None,
) -> None:
    player = shutil.which("ffplay")
    if not player:
        raise RuntimeError("ffplay is required for live playback")
    process = subprocess.Popen(
        [
            player,
            "-v",
            "error",
            "-nodisp",
            "-autoexit",
            "-f",
            "f32le",
            "-sample_rate",
            str(SAMPLE_RATE),
            "-ch_layout",
            "stereo",
            "-i",
            "pipe:0",
        ],
        stdin=subprocess.PIPE,
    )
    assert process.stdin is not None
    skip_frames = max(0, round(start_seconds * SAMPLE_RATE))
    remaining_frames = None if max_seconds is None else max(0, round(max_seconds * SAMPLE_RATE))
    try:
        for block in iter_mix_blocks(plan):
            if skip_frames:
                skipped = min(skip_frames, len(block))
                block = block[skipped:]
                skip_frames -= skipped
                if len(block) == 0:
                    continue
            if remaining_frames is not None:
                block = block[:remaining_frames]
            if len(block):
                process.stdin.write(np.asarray(block, dtype="<f4").tobytes())
            if remaining_frames is not None:
                remaining_frames -= len(block)
                if remaining_frames <= 0:
                    break
    except BrokenPipeError:
        pass
    finally:
        process.stdin.close()
        process.wait()


def _prepare_live_track(
    future: Future[TrackAnalysis],
    target_bpm: float,
    cache_dir: Path,
    max_tempo_change: float,
) -> tuple[TrackAnalysis, Path]:
    analysis = future.result()
    change = abs(target_bpm / analysis.bpm - 1.0)
    if change > max_tempo_change:
        raise ValueError(
            f"Tempo difference is too large for live mixing: {Path(analysis.path).name} "
            f"is {analysis.bpm:.2f} BPM versus {target_bpm:.2f} BPM ({change:.1%})"
        )
    return analysis, _stretched_path(analysis, target_bpm, cache_dir)


def stream_ordered_live(
    paths: Iterable[Path],
    *,
    transition_bars: int = 16,
    workers: int = 3,
    max_seconds: float | None = None,
    start_near_first_transition: bool = False,
    max_tempo_change: float = 0.08,
    technique: str = "bass_swap",
    progress: Callable[[str], None] | None = None,
) -> None:
    """Start after track one is ready while future tracks prepare in the background."""
    ordered = list(paths)
    if len(ordered) < 2:
        raise ValueError("A live mix needs at least two tracks")
    player = shutil.which("ffplay")
    if not player:
        raise RuntimeError("ffplay is required for live playback")

    analysis_pool = ThreadPoolExecutor(max_workers=max(1, workers))
    prepare_pool = ThreadPoolExecutor(max_workers=2)
    analysis_futures = [
        analysis_pool.submit(analyze_track, path, transition_bars=transition_bars)
        for path in ordered
    ]
    first = analysis_futures[0].result()
    target_bpm = first.bpm
    cache_root = Path(".setmix-cache/stretched")
    prepared = [
        prepare_pool.submit(
            _prepare_live_track,
            future,
            target_bpm,
            cache_root,
            max_tempo_change,
        )
        for future in analysis_futures
    ]
    if progress:
        progress(f"Live master tempo: {target_bpm:.2f} BPM")
        progress(f"Playing: {ordered[0].name}")
        progress(f"Preparing {len(ordered) - 1} future tracks asynchronously")

    process = subprocess.Popen(
        [
            player,
            "-v",
            "error",
            "-nodisp",
            "-autoexit",
            "-f",
            "f32le",
            "-sample_rate",
            str(SAMPLE_RATE),
            "-ch_layout",
            "stereo",
            "-i",
            "pipe:0",
        ],
        stdin=subprocess.PIPE,
    )
    assert process.stdin is not None
    remaining_output = None if max_seconds is None else max(0, round(max_seconds * SAMPLE_RATE))

    def emit(block: np.ndarray) -> bool:
        nonlocal remaining_output
        if remaining_output is not None:
            block = block[:remaining_output]
        if len(block):
            process.stdin.write(np.asarray(block, dtype="<f4").tobytes())
        if remaining_output is not None:
            remaining_output -= len(block)
            return remaining_output > 0
        return True

    handles: list[sf.SoundFile] = []
    try:
        current_analysis, current_path = prepared[0].result()
        current = sf.SoundFile(current_path)
        handles.append(current)
        source_position = 0
        if start_near_first_transition:
            ratio = target_bpm / current_analysis.bpm
            source_position = max(0, round((current_analysis.cue_out / ratio - 8.0) * SAMPLE_RATE))

        keep_playing = True
        for index in range(len(ordered) - 1):
            if not keep_playing:
                break
            ratio = target_bpm / current_analysis.bpm
            cue_from = round(current_analysis.cue_out / ratio * SAMPLE_RATE)
            current.seek(min(source_position, len(current)))
            remaining = max(0, cue_from - source_position)
            while remaining and keep_playing:
                block = current.read(min(BLOCK_FRAMES, remaining), dtype="float32", always_2d=True)
                if len(block) == 0:
                    break
                keep_playing = emit((block * _track_gain(current_analysis)).astype(np.float32))
                remaining -= len(block)
            if not keep_playing:
                break

            if progress:
                progress(f"Committing transition {index + 1}: {ordered[index].name} -> {ordered[index + 1].name}")
            next_analysis, next_path = prepared[index + 1].result()
            incoming = sf.SoundFile(next_path)
            handles.append(incoming)
            next_ratio = target_bpm / next_analysis.bpm
            cue_to = round(next_analysis.cue_in / next_ratio * SAMPLE_RATE)
            transition_frames = round(transition_bars * 4.0 * 60.0 / target_bpm * SAMPLE_RATE)
            left = _read_segment(current, cue_from, transition_frames)
            right = _read_segment(incoming, cue_to, transition_frames)
            keep_playing = emit(
                mix_transition(
                    left,
                    right,
                    _track_gain(current_analysis),
                    _track_gain(next_analysis),
                    technique,
                    transition_bars,
                )
            )
            current = incoming
            current_analysis = next_analysis
            source_position = cue_to + transition_frames
            if progress and keep_playing:
                progress(f"Playing: {ordered[index + 1].name}")

        if keep_playing:
            ratio = target_bpm / current_analysis.bpm
            end = min(len(current), round(current_analysis.active_end / ratio * SAMPLE_RATE))
            current.seek(min(source_position, len(current)))
            remaining = max(0, end - source_position)
            while remaining and keep_playing:
                block = current.read(min(BLOCK_FRAMES, remaining), dtype="float32", always_2d=True)
                if len(block) == 0:
                    break
                keep_playing = emit((block * _track_gain(current_analysis)).astype(np.float32))
                remaining -= len(block)
    except BrokenPipeError:
        pass
    finally:
        for handle in handles:
            handle.close()
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        process.wait()
        prepare_pool.shutdown(wait=False, cancel_futures=True)
        analysis_pool.shutdown(wait=False, cancel_futures=True)


def validate_render(audio_path: str | Path, plan_path: str | Path | None = None) -> dict:
    audio = Path(audio_path).expanduser().resolve()
    plan_file = (
        Path(plan_path).expanduser().resolve()
        if plan_path is not None
        else audio.with_suffix(audio.suffix + ".plan.json")
    )
    payload = json.loads(plan_file.read_text())
    transitions = payload.get("transitions", [])

    with sf.SoundFile(audio) as handle:
        if handle.samplerate != SAMPLE_RATE or handle.channels != CHANNELS:
            raise ValueError(
                f"Expected {SAMPLE_RATE} Hz stereo output, got {handle.samplerate} Hz/{handle.channels} channels"
            )
        peak = 0.0
        sum_squares = 0.0
        samples = 0
        clipped = 0
        dc_sum = np.zeros(CHANNELS, dtype=np.float64)
        for block in handle.blocks(BLOCK_FRAMES, dtype="float32", always_2d=True):
            peak = max(peak, float(np.max(np.abs(block))))
            sum_squares += float(np.sum(np.square(block, dtype=np.float64)))
            samples += int(block.size)
            clipped += int(np.count_nonzero(np.abs(block) >= 0.999))
            dc_sum += np.sum(block, axis=0, dtype=np.float64)
        duration = len(handle) / handle.samplerate

    def db(value: float) -> float:
        return float(20.0 * math.log10(max(value, 1e-12)))

    reports = []
    with sf.SoundFile(audio) as handle:
        for index, transition in enumerate(transitions, 1):
            start = float(transition["set_time"])
            length = float(transition["duration"])
            points = {
                "before": max(0.0, start - 4.0),
                "start": start + 1.0,
                "middle": start + length / 2.0 - 1.0,
                "end": start + length - 3.0,
                "after": min(duration - 2.0, start + length + 1.0),
            }
            levels: dict[str, float] = {}
            for name, second in points.items():
                handle.seek(max(0, round(second * SAMPLE_RATE)))
                block = handle.read(SAMPLE_RATE * 2, dtype="float32", always_2d=True)
                levels[name] = round(db(float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))), 3)
            values = list(levels.values())
            reports.append(
                {
                    "transition": index,
                    "set_time": start,
                    "duration": length,
                    "rms_dbfs": levels,
                    "range_db": round(max(values) - min(values), 3),
                    "minimum_dbfs": round(min(values), 3),
                }
            )

    rms = math.sqrt(sum_squares / max(1, samples))
    result = {
        "audio": str(audio),
        "duration": round(duration, 6),
        "peak_dbfs": round(db(peak), 3),
        "rms_dbfs": round(db(rms), 3),
        "clipped_samples": clipped,
        "dc_offset": [round(float(value / max(1, samples / CHANNELS)), 8) for value in dc_sum],
        "transitions": reports,
    }
    issues = []
    if clipped:
        issues.append(f"{clipped} clipped samples")
    if peak > 10.0 ** (-0.5 / 20.0):
        issues.append("peak headroom is below 0.5 dB")
    for report in reports:
        if report["minimum_dbfs"] < -40.0:
            issues.append(f"transition {report['transition']} contains a near-silent region")
        if report["range_db"] > 8.0:
            issues.append(f"transition {report['transition']} changes loudness by more than 8 dB")
    result["issues"] = issues
    result["passed"] = not issues
    return result
