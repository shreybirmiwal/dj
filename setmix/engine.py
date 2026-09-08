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
from .stems import VocalMap, choose_vocal_safe_cues, separate_stems, separate_vocals


SAMPLE_RATE = 44100
CHANNELS = 2
BLOCK_FRAMES = SAMPLE_RATE * 2


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


@dataclass(frozen=True)
class MixPlan:
    target_bpm: float
    tracks: list[TrackAnalysis]
    transitions: list[Transition]
    estimated_duration: float

    def to_dict(self) -> dict:
        return {
            "target_bpm": self.target_bpm,
            "tracks": [asdict(track) for track in self.tracks],
            "transitions": [asdict(item) for item in self.transitions],
            "estimated_duration": self.estimated_duration,
        }


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
    max_tempo_change: float = 0.08,
    vocal_maps: dict[str, VocalMap] | None = None,
    technique: str = "auto",
) -> MixPlan:
    if len(analyses) < 2:
        raise ValueError("A mix needs at least two tracks")
    target = analyses[0].bpm
    for track in analyses:
        change = abs(target / track.bpm - 1.0)
        if change > max_tempo_change:
            raise ValueError(
                f"Tempo difference is too large for the prototype: {Path(track.path).name} "
                f"is {track.bpm:.2f} BPM versus {target:.2f} BPM ({change:.1%})"
            )

    transitions: list[Transition] = []
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
        if vocal_maps and left.path in vocal_maps and right.path in vocal_maps:
            left_source_cue, right_source_cue, vocal_overlap = choose_vocal_safe_cues(
                left,
                right,
                vocal_maps[left.path],
                vocal_maps[right.path],
            )
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
        selected = technique
        if technique == "varied":
            selected = varied_techniques[transition_index % len(varied_techniques)]
        elif technique == "auto":
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
            )
        )
        set_time += duration
        current_source = right_cue + duration

    last = analyses[-1]
    last_ratio = target / last.bpm
    estimated = set_time + max(0.0, last.active_end / last_ratio - current_source)
    return MixPlan(target, analyses, transitions, round(estimated, 6))


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


def _read_segment(handle: sf.SoundFile, start: int, frames: int) -> np.ndarray:
    handle.seek(max(0, min(start, len(handle))))
    data = handle.read(frames, dtype="float32", always_2d=True)
    if len(data) < frames:
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
        mixed = _filter_sweep_mix(outgoing, incoming)
        mixed += _echo_tail(outgoing, beat_frames)
    elif technique == "loop_filter":
        mixed = _filter_sweep_mix(_loop_roll(outgoing, beat_frames), incoming)
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
) -> np.ndarray:
    """Layer a long transition with independent musical schedules per stem."""
    names = ("vocals", "drums", "bass", "other")
    frames = min(len(outgoing[name]) for name in names)
    frames = min(frames, *(len(incoming[name]) for name in names))
    left = {name: outgoing[name][:frames] * outgoing_gain for name in names}
    right = {name: incoming[name][:frames] * incoming_gain for name in names}
    position = np.linspace(0.0, 1.0, frames, endpoint=False, dtype=np.float32)

    def fade_out(start: float, end: float) -> np.ndarray:
        return np.cos(_smoothstep((position - start) / (end - start)) * (math.pi / 2.0))[:, None]

    def fade_in(start: float, end: float) -> np.ndarray:
        return np.sin(_smoothstep((position - start) / (end - start)) * (math.pi / 2.0))[:, None]

    # Drums span almost the whole transition, but equal-power scheduling keeps
    # the combined groove stable. Bass changes only around the middle phrase.
    drum_a, drum_b = _equal_power(_smoothstep((position - 0.04) / 0.92))
    bass_a, bass_b = _equal_power(_smoothstep((position - 0.38) / 0.24))

    # Melodic material crosses later and is filtered to avoid a harmonic pileup.
    other_a = _swept_filter(left["other"], 30.0, 3600.0, "highpass")
    other_b = _swept_filter(right["other"], 650.0, 19000.0, "lowpass")
    other_gain_a = fade_out(0.22, 0.60)
    other_gain_b = fade_in(0.30, 0.66)

    # Never present two lead singers together. The instrumental gap is long
    # enough to finish one lyrical thought before the next vocalist appears.
    vocal_gain_a = fade_out(0.24, 0.42)
    vocal_gain_b = fade_in(0.48, 0.68)

    mixed = left["drums"] * drum_a + right["drums"] * drum_b
    mixed += left["bass"] * bass_a + right["bass"] * bass_b
    mixed += other_a * other_gain_a + other_b * other_gain_b
    mixed += left["vocals"] * vocal_gain_a + right["vocals"] * vocal_gain_b
    mixed += _reverb_tail(left["vocals"] * vocal_gain_a) * 0.12
    return (mixed * 0.90).astype(np.float32)


def _drum_alignment_transform(
    outgoing: np.ndarray,
    incoming: np.ndarray,
    bpm: float,
) -> tuple[float, float]:
    """Return reliable phase and drift corrections from isolated drum stems."""
    hop = 128
    left = librosa.onset.onset_strength(y=np.mean(outgoing, axis=1), sr=SAMPLE_RATE, hop_length=hop)
    right = librosa.onset.onset_strength(y=np.mean(incoming, axis=1), sr=SAMPLE_RATE, hop_length=hop)
    frames_per_block = round(8.0 * 4.0 * 60.0 / bpm * SAMPLE_RATE / hop)
    radius = round(0.25 * SAMPLE_RATE / hop)
    lags: list[float] = []
    times: list[float] = []
    for block in range(4):
        start = block * frames_per_block
        end = min(len(left), len(right), (block + 1) * frames_per_block)
        if end - start < frames_per_block // 2:
            break
        first = left[start:end]
        second = right[start:end]
        first = (first - np.mean(first)) / (np.std(first) + 1e-9)
        second = (second - np.mean(second)) / (np.std(second) + 1e-9)
        scores: list[float] = []
        candidates = range(-radius, radius + 1)
        for lag in candidates:
            if lag < 0:
                score = np.dot(first[-lag:], second[: len(second) + lag])
            elif lag > 0:
                score = np.dot(first[: len(first) - lag], second[lag:])
            else:
                score = np.dot(first, second)
            scores.append(float(score))
        lag = list(candidates)[int(np.argmax(scores))] * hop / SAMPLE_RATE
        lags.append(lag)
        times.append((block + 0.5) * frames_per_block * hop / SAMPLE_RATE)
    if len(lags) < 4:
        return 0.0, 0.0
    slope, intercept = np.polyfit(times, lags, 1)
    predicted = np.polyval((slope, intercept), times)
    variance = float(np.sum(np.square(np.asarray(lags) - np.mean(lags))))
    residual = float(np.sum(np.square(np.asarray(lags) - predicted)))
    r_squared = 1.0 - residual / max(variance, 1e-9)
    if r_squared < 0.90 or (abs(intercept) < 0.025 and abs(slope) < 0.0008):
        return 0.0, 0.0
    return float(np.clip(intercept, -0.20, 0.20)), float(np.clip(slope, -0.005, 0.005))


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
    intercept, slope = _drum_alignment_transform(left["drums"], right_probe, bpm)
    positions = cue_to + intercept * SAMPLE_RATE + np.arange(frames, dtype=np.float64) * (1.0 + slope)
    source_start = max(0, math.floor(float(positions[0])) - 2)
    relative = positions - source_start
    source_frames = math.ceil(float(positions[-1])) - source_start + 3
    right: dict[str, np.ndarray] = {}
    for name, handle in incoming.items():
        source = _read_segment(handle, source_start, source_frames)
        channels = [np.interp(relative, np.arange(len(source)), source[:, channel]) for channel in range(CHANNELS)]
        right[name] = np.column_stack(channels).astype(np.float32)
    consumed_end = round(float(positions[-1]) + 1.0 + slope)
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
            if transition.technique == "stem_phrase"
        } | {
            index + 1
            for index, transition in enumerate(plan.transitions)
            if transition.technique == "stem_phrase"
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

            if transition.technique == "stem_phrase":
                left_stems, right_stems, consumed_end = _read_aligned_stems(
                    stem_handles[index],
                    stem_handles[index + 1],
                    cue_from,
                    cue_to,
                    transition_frames,
                    plan.target_bpm,
                )
                yield mix_four_stem_transition(
                    left_stems,
                    right_stems,
                    gains[index],
                    gains[index + 1],
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

        limiter = "alimiter=limit=0.891251:attack=5:release=80:level=false"
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
        if transition.technique == "stem_phrase"
    } | {
        index + 1
        for index, transition in enumerate(plan.transitions)
        if transition.technique == "stem_phrase"
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
            after = _read_segment(incoming, cue_to + frames, context_frames)
            selected_techniques = (transition.technique,) if selected_only else techniques
            for technique in selected_techniques:
                if technique == "stem_phrase":
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
                        mixed = mix_four_stem_transition(
                            aligned_left,
                            aligned_right,
                            _track_gain(plan.tracks[index]),
                            _track_gain(plan.tracks[index + 1]),
                        )
                        after = _read_segment(incoming, consumed_end, context_frames)
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
                            "alimiter=limit=0.891251:attack=5:release=80:level=false",
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
