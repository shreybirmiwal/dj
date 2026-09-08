from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import librosa
import numpy as np


AUDIO_SUFFIXES = {".aif", ".aiff", ".flac", ".m4a", ".mp3", ".wav"}


@dataclass(frozen=True)
class TrackAnalysis:
    path: str
    duration: float
    bpm: float
    beat_times: list[float]
    downbeat_offset: int
    cue_in: float
    cue_out: float
    active_end: float
    rms_db: float
    beat_confidence: float
    transition_bars: int
    phrase_offset: int = 0
    musical_key: str = "unknown"
    camelot_key: str = "unknown"
    key_confidence: float = 0.0

    @property
    def transition_seconds(self) -> float:
        return self.transition_bars * 4.0 * 60.0 / self.bpm


def _cache_key(path: Path, transition_bars: int) -> str:
    stat = path.stat()
    value = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{transition_bars}:v8"
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _normalize_grid(tempo: float, beats: np.ndarray) -> tuple[float, np.ndarray]:
    if tempo < 90.0 and len(beats) >= 2:
        dense: list[float] = []
        for left, right in zip(beats[:-1], beats[1:]):
            dense.extend((float(left), float((left + right) / 2.0)))
        dense.append(float(beats[-1]))
        return tempo * 2.0, np.asarray(dense)
    if tempo > 170.0:
        return tempo / 2.0, beats[::2]
    return tempo, beats


def _refined_tempo(y: np.ndarray, sr: int, fallback: float) -> tuple[float, np.ndarray, int]:
    """Estimate dance tempo on a fine grid instead of the coarse default hop."""
    hop = 64
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    onset = onset - float(np.mean(onset))
    maximum_lag = int(60.0 * sr / hop / 80.0)
    autocorrelation = librosa.autocorrelate(onset, max_size=maximum_lag)
    low_lag = int(60.0 * sr / hop / 170.0)
    high_lag = min(len(autocorrelation) - 2, int(60.0 * sr / hop / 90.0))
    candidates: list[tuple[float, float]] = []
    for lag in range(low_lag + 1, high_lag):
        if autocorrelation[lag] <= autocorrelation[lag - 1] or autocorrelation[lag] < autocorrelation[lag + 1]:
            continue
        left, center, right = autocorrelation[lag - 1 : lag + 2]
        denominator = left - 2.0 * center + right
        offset = 0.5 * (left - right) / denominator if abs(denominator) > 1e-12 else 0.0
        bpm = 60.0 * sr / hop / (lag + float(np.clip(offset, -0.5, 0.5)))
        distance = math.log2(max(bpm, 1e-6) / max(fallback, 1e-6))
        prior = math.exp(-0.5 * (distance / 0.16) ** 2)
        candidates.append((float(center) * prior, bpm))
    if not candidates:
        return fallback, onset, hop
    return max(candidates)[1], onset, hop


def _regularize_grid(beats: np.ndarray, tempo: float, duration: float) -> np.ndarray:
    """Fill skipped detections and remove jitter while retaining the detected phase."""
    if len(beats) == 0:
        return np.arange(0.0, duration, 60.0 / tempo)
    period = 60.0 / tempo
    reference = float(beats[0])
    indices = np.rint((beats - reference) / period)
    anchor = float(np.median(beats - indices * period))
    first = math.floor((0.0 - anchor) / period)
    last = math.ceil((duration - anchor) / period)
    grid = anchor + np.arange(first, last + 1) * period
    return grid[(grid >= 0.0) & (grid <= duration)]


def _refine_grid_phase(
    grid: np.ndarray,
    onset: np.ndarray,
    sr: int,
    hop: int,
    duration: float,
) -> np.ndarray:
    """Move the regular grid onto nearby waveform transients."""
    radius = max(1, round(0.10 * sr / hop))
    offsets: list[float] = []
    for beat in grid[4:-4]:
        center = round(float(beat) * sr / hop)
        left = max(0, center - radius)
        right = min(len(onset), center + radius + 1)
        if right <= left:
            continue
        peak = left + int(np.argmax(onset[left:right]))
        offsets.append((peak - center) * hop / sr)
    if not offsets:
        return grid
    shift = float(np.median(offsets))
    shifted = grid + shift
    return shifted[(shifted >= 0.0) & (shifted <= duration)]


def _active_bounds(y: np.ndarray, sr: int) -> tuple[float, float]:
    intervals = librosa.effects.split(y, top_db=38)
    if len(intervals) == 0:
        return 0.0, len(y) / sr
    return float(intervals[0, 0] / sr), float(intervals[-1, 1] / sr)


def _detect_key(y: np.ndarray, sr: int) -> tuple[str, str, float]:
    """Estimate a global major/minor key and its Camelot-wheel label."""
    chroma = librosa.feature.chroma_stft(y=y, sr=sr, n_fft=4096, hop_length=2048)
    rms = librosa.feature.rms(y=y, frame_length=4096, hop_length=2048)[0]
    usable = rms > np.percentile(rms, 25)
    profile = np.mean(chroma[:, usable], axis=1) if np.any(usable) else np.mean(chroma, axis=1)
    profile = (profile - np.mean(profile)) / (np.std(profile) + 1e-9)
    major = np.asarray([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    minor = np.asarray([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    pitch_names = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
    major_camelot = ("8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B")
    minor_camelot = ("5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A")
    choices: list[tuple[float, str, str]] = []
    for tonic in range(12):
        for mode, reference, camelot in (
            ("major", major, major_camelot),
            ("minor", minor, minor_camelot),
        ):
            rotated = np.roll(reference, tonic)
            score = float(np.corrcoef(profile, rotated)[0, 1])
            choices.append((score, f"{pitch_names[tonic]} {mode}", camelot[tonic]))
    score, name, camelot = max(choices)
    return name, camelot, round(float(np.clip(score, 0.0, 1.0)), 4)


def _downbeat_offset(onset: np.ndarray, beat_frames: np.ndarray) -> int:
    if len(beat_frames) < 8:
        return 0
    values = onset[np.clip(beat_frames, 0, len(onset) - 1)]
    scores = [float(np.mean(values[offset::4])) for offset in range(4)]
    return int(np.argmax(scores))


def _aligned_indices(length: int, offset: int, stride: int = 4) -> list[int]:
    return list(range(offset % stride, length, stride))


def _phrase_offset(
    beat_frames: np.ndarray,
    onset: np.ndarray,
    rms: np.ndarray,
    flatness: np.ndarray,
    downbeat_offset: int,
) -> int:
    """Find which downbeats most consistently begin eight-bar phrases."""
    starts = list(range(downbeat_offset, len(beat_frames) - 4, 4))
    features: list[list[float]] = []
    for index in starts:
        left = int(beat_frames[index])
        right = max(left + 1, int(beat_frames[index + 4]))
        features.append(
            [
                float(np.mean(onset[left:right])),
                float(np.mean(rms[left:right])),
                float(np.mean(flatness[left:right])),
            ]
        )
    if len(features) < 17:
        return downbeat_offset
    values = np.asarray(features)
    values = (values - np.mean(values, axis=0)) / (np.std(values, axis=0) + 1e-8)
    novelty = np.linalg.norm(np.diff(values, axis=0, prepend=values[:1]), axis=1)
    scores = [float(np.mean(novelty[offset::8])) for offset in range(8)]
    phrase_bar = int(np.argmax(scores))
    return (downbeat_offset + phrase_bar * 4) % 32


def _pick_cues(
    beat_times: np.ndarray,
    beat_frames: np.ndarray,
    onset: np.ndarray,
    rms: np.ndarray,
    flatness: np.ndarray,
    phrase_offset: int,
    active_start: float,
    active_end: float,
    transition_bars: int,
) -> tuple[float, float]:
    transition_beats = transition_bars * 4
    aligned = _aligned_indices(len(beat_times), phrase_offset, 32)
    usable = [i for i in aligned if beat_times[i] >= active_start - 0.25]
    if not usable:
        return active_start, max(active_start, active_end - transition_bars * 4 * 0.5)

    cue_in_idx = usable[0]
    need = transition_beats + 4
    candidates = [
        i
        for i in usable
        if i + need < len(beat_times)
        and beat_times[i + transition_beats] <= active_end + 0.2
    ]
    if not candidates:
        return float(beat_times[cue_in_idx]), max(float(beat_times[cue_in_idx]), active_end - 30.0)

    tail_candidates = candidates[-min(len(candidates), 36) :]
    frame_count = max(1, len(rms))
    active_rms = rms[rms > np.percentile(rms, 20)]
    energy_floor = float(np.percentile(active_rms, 12)) if len(active_rms) else 0.0

    best_idx = tail_candidates[-1]
    best_score = -math.inf
    for rank, idx in enumerate(tail_candidates):
        end_idx = idx + transition_beats
        frame_l = int(np.clip(beat_frames[idx], 0, frame_count - 1))
        frame_r = int(np.clip(beat_frames[end_idx], frame_l + 1, frame_count))
        window_rms = rms[frame_l:frame_r]
        if len(window_rms) == 0 or float(np.mean(window_rms)) < energy_floor:
            continue
        window_flat = flatness[frame_l:frame_r]
        window_onset = onset[frame_l:frame_r]
        position = rank / max(1, len(tail_candidates) - 1)
        steadiness = -float(np.std(window_rms) / (np.mean(window_rms) + 1e-9))
        percussion = float(np.mean(window_onset) / (np.mean(onset) + 1e-9))
        noise_like = float(np.mean(window_flat))
        score = 1.3 * position + 0.22 * steadiness + 0.12 * percussion + 0.08 * noise_like
        if score > best_score:
            best_score = score
            best_idx = idx

    return float(beat_times[cue_in_idx]), float(beat_times[best_idx])


def analyze_track(
    path: str | Path,
    *,
    transition_bars: int = 16,
    cache_dir: str | Path = ".setmix-cache/analysis",
    force: bool = False,
) -> TrackAnalysis:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in AUDIO_SUFFIXES:
        raise ValueError(f"Not a supported audio file: {source}")

    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    cached = cache_root / f"{_cache_key(source, transition_bars)}.json"
    if cached.exists() and not force:
        return TrackAnalysis(**json.loads(cached.read_text()))

    y, sr = librosa.load(source, sr=22050, mono=True)
    if len(y) < sr * 10:
        raise ValueError(f"Track is too short to mix: {source}")

    hop = 512
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    raw_tempo, raw_beat_frames = librosa.beat.beat_track(
        onset_envelope=onset,
        sr=sr,
        hop_length=hop,
        start_bpm=124.0,
        trim=False,
    )
    tempo = float(np.asarray(raw_tempo).reshape(-1)[0])
    raw_times = librosa.frames_to_time(raw_beat_frames, sr=sr, hop_length=hop)
    tempo, raw_times = _normalize_grid(tempo, np.asarray(raw_times))
    tempo, fine_onset, fine_hop = _refined_tempo(y, sr, tempo)
    beat_times = _regularize_grid(raw_times, tempo, len(y) / sr)
    beat_times = _refine_grid_phase(beat_times, fine_onset, sr, fine_hop, len(y) / sr)
    beat_frames = librosa.time_to_frames(beat_times, sr=sr, hop_length=hop)
    if len(beat_times) < transition_bars * 4 + 16:
        raise ValueError(f"Not enough detected beats in: {source}")

    active_start, active_end = _active_bounds(y, sr)
    musical_key, camelot_key, key_confidence = _detect_key(y, sr)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    flatness = librosa.feature.spectral_flatness(y=y, n_fft=2048, hop_length=hop)[0]
    length = min(len(onset), len(rms), len(flatness))
    onset, rms, flatness = onset[:length], rms[:length], flatness[:length]
    beat_frames = np.clip(beat_frames, 0, length - 1)
    downbeat = _downbeat_offset(onset, beat_frames)
    phrase = _phrase_offset(beat_frames, onset, rms, flatness, downbeat)
    cue_in, cue_out = _pick_cues(
        beat_times,
        beat_frames,
        onset,
        rms,
        flatness,
        phrase,
        active_start,
        active_end,
        transition_bars,
    )

    expected = 60.0 / tempo
    residuals = np.abs(raw_times[:, None] - beat_times[None, :]).min(axis=1)
    jitter = float(np.median(residuals) / expected)
    confidence = float(np.clip(1.0 - 4.0 * jitter, 0.0, 1.0))
    active = rms[rms > np.percentile(rms, 20)]
    rms_value = float(np.sqrt(np.mean(np.square(active)))) if len(active) else float(np.sqrt(np.mean(np.square(rms))))
    rms_db = float(20.0 * np.log10(max(rms_value, 1e-9)))

    result = TrackAnalysis(
        path=str(source),
        duration=float(len(y) / sr),
        bpm=round(tempo, 6),
        beat_times=[round(float(value), 6) for value in beat_times],
        downbeat_offset=downbeat,
        cue_in=round(cue_in, 6),
        cue_out=round(cue_out, 6),
        active_end=round(active_end, 6),
        rms_db=round(rms_db, 4),
        beat_confidence=round(confidence, 4),
        transition_bars=transition_bars,
        phrase_offset=phrase,
        musical_key=musical_key,
        camelot_key=camelot_key,
        key_confidence=key_confidence,
    )
    cached.write_text(json.dumps(asdict(result), indent=2) + "\n")
    return result


def discover_tracks(values: Iterable[str]) -> list[Path]:
    tracks: list[Path] = []
    for value in values:
        candidate = Path(value).expanduser()
        if candidate.suffix.lower() in {".txt", ".m3u", ".m3u8"} and candidate.is_file():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    item = Path(line).expanduser()
                    if not item.is_absolute():
                        item = candidate.parent / item
                    tracks.append(item.resolve())
        elif candidate.is_dir():
            tracks.extend(
                sorted(
                    path.resolve()
                    for path in candidate.iterdir()
                    if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
                )
            )
        else:
            tracks.append(candidate.resolve())
    if not tracks:
        raise ValueError("No audio tracks were provided")
    return tracks
