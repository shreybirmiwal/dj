from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

import librosa
import numpy as np

from .analysis import TrackAnalysis


STEM_CACHE_LOCK = threading.RLock()


@dataclass(frozen=True)
class VocalMap:
    path: str
    model: str
    resolution: float
    segments: list[list[float]]
    vocal_fraction: float
    confidence: float

    def active_at(self, second: float) -> bool:
        return any(start <= second <= end for start, end in self.segments)

    def activity_fraction(self, start: float, end: float, samples: int = 96) -> float:
        if end <= start:
            return 0.0
        points = np.linspace(start, end, samples)
        return float(np.mean([self.active_at(float(point)) for point in points]))


def _key(path: Path) -> str:
    stat = path.stat()
    value = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:htdemucs-flac-v3"
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _stem_paths(path: Path, cache_root: Path) -> tuple[Path, Path, Path]:
    root = cache_root / _key(path)
    candidates = list((root / "htdemucs").glob("*/vocals.flac"))
    if candidates:
        vocal = candidates[0]
        return root, vocal, vocal.with_name("no_vocals.flac")
    return root, root / "missing-vocals.flac", root / "missing-no-vocals.flac"


def _resolve_device(device: str) -> str:
    if device != "auto":
        return device
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    return "cpu"


def _four_stem_key(path: Path) -> str:
    stat = path.stat()
    value = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:htdemucs-4-flac-v3"
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _cached_four_stems(source: Path, cache_root: Path) -> dict[str, Path] | None:
    model_root = cache_root / _four_stem_key(source) / "htdemucs"
    candidates = list(model_root.glob("*/vocals.flac"))
    if not candidates:
        return None
    folder = candidates[0].parent
    result = {name: folder / f"{name}.flac" for name in ("vocals", "drums", "bass", "other")}
    return result if all(item.exists() for item in result.values()) else None


def separate_stems(
    path: str | Path,
    *,
    cache_dir: str | Path = ".setmix-cache/stems4",
    device: str = "auto",
) -> dict[str, Path]:
    """Separate vocals, drums, bass, and other with cached Demucs output."""
    source = Path(path).expanduser().resolve()
    cache_root = Path(cache_dir)
    root = cache_root / _four_stem_key(source)
    cached = _cached_four_stems(source, cache_root)
    if cached:
        return cached
    with STEM_CACHE_LOCK:
        cached = _cached_four_stems(source, cache_root)
        if cached:
            return cached
        root.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "setmix.demucs_runner",
            "--flac",
            "--int24",
            "--clip-mode",
            "clamp",
            "-n",
            "htdemucs",
            "-d",
            _resolve_device(device),
            "-o",
            str(root),
            str(source),
        ]
        try:
            subprocess.run(command, check=True)
        except (subprocess.CalledProcessError, ModuleNotFoundError) as error:
            raise RuntimeError(
                "Four-stem mixing requires the optional stem dependencies from requirements-stems.txt"
            ) from error
    result = _cached_four_stems(source, cache_root)
    if not result:
        raise RuntimeError(f"Stem separation did not produce expected outputs for {source}")
    return result


def separate_stems_batch(
    paths: list[str | Path],
    *,
    cache_dir: str | Path = ".setmix-cache/stems4",
    device: str = "auto",
) -> list[dict[str, Path]]:
    """Separate multiple uncached tracks with one Demucs model load.

    Demucs processes the tracks in sequence internally, so this saves startup
    and model-transfer time without multiplying GPU memory use.
    """
    sources = [Path(path).expanduser().resolve() for path in paths]
    cache_root = Path(cache_dir)
    results = [_cached_four_stems(source, cache_root) for source in sources]
    missing = [source for source, result in zip(sources, results) if result is None]
    if len(missing) < 2 or len({source.stem for source in missing}) != len(missing):
        return [
            result or separate_stems(source, cache_dir=cache_root, device=device)
            for source, result in zip(sources, results)
        ]

    with STEM_CACHE_LOCK:
        results = [_cached_four_stems(source, cache_root) for source in sources]
        missing = [source for source, result in zip(sources, results) if result is None]
        if len(missing) < 2 or len({source.stem for source in missing}) != len(missing):
            return [
                result or separate_stems(source, cache_dir=cache_root, device=device)
                for source, result in zip(sources, results)
            ]

        cache_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".batch-", dir=cache_root) as temporary:
            batch_root = Path(temporary)
            command = [
                sys.executable,
                "-m",
                "setmix.demucs_runner",
                "--flac",
                "--int24",
                "--clip-mode",
                "clamp",
                "-n",
                "htdemucs",
                "-d",
                _resolve_device(device),
                "-o",
                str(batch_root),
                *(str(source) for source in missing),
            ]
            try:
                subprocess.run(command, check=True)
            except (subprocess.CalledProcessError, ModuleNotFoundError) as error:
                raise RuntimeError(
                    "Four-stem mixing requires the optional stem dependencies from requirements-stems.txt"
                ) from error
            for source in missing:
                generated = batch_root / "htdemucs" / source.stem
                if not (generated / "vocals.flac").exists():
                    raise RuntimeError(f"Batch stem separation was incomplete for {source}")
                target = cache_root / _four_stem_key(source) / "htdemucs" / source.stem
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(generated), str(target))

    final = [_cached_four_stems(source, cache_root) for source in sources]
    if any(result is None for result in final):
        raise RuntimeError("Batch stem separation did not produce all expected outputs")
    return [result for result in final if result is not None]


def separate_vocals(
    path: str | Path,
    *,
    cache_dir: str | Path = ".setmix-cache/stems",
    device: str = "auto",
) -> tuple[Path, Path]:
    source = Path(path).expanduser().resolve()
    root, vocals, accompaniment = _stem_paths(source, Path(cache_dir))
    if vocals.exists() and accompaniment.exists():
        return vocals, accompaniment

    # Demucs' two-stem mode still runs the full separator internally. Keep old
    # two-stem caches readable, but create all new vocal data from one reusable
    # four-stem pass so rendering does not perform the same inference twice.
    four_stem_root = Path(cache_dir).expanduser().parent / "stems4"
    stems = separate_stems(source, cache_dir=four_stem_root, device=device)
    extension = stems["vocals"].suffix
    accompaniment = stems["vocals"].with_name(f"no_vocals{extension}")
    if not accompaniment.exists():
        with STEM_CACHE_LOCK:
            if not accompaniment.exists():
                temporary = accompaniment.with_name(f"no_vocals.partial{extension}")
                codec = ["-c:a", "flac"] if extension == ".flac" else ["-c:a", "libmp3lame", "-b:a", "320k"]
                command = [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(stems["drums"]),
                    "-i",
                    str(stems["bass"]),
                    "-i",
                    str(stems["other"]),
                    "-filter_complex",
                    "amix=inputs=3:duration=longest:normalize=0,alimiter=limit=0.95",
                    *codec,
                    str(temporary),
                ]
                try:
                    subprocess.run(command, check=True)
                    temporary.replace(accompaniment)
                finally:
                    temporary.unlink(missing_ok=True)
    return stems["vocals"], accompaniment


def _segments(active: np.ndarray, resolution: float) -> list[list[float]]:
    values: list[list[float]] = []
    start: int | None = None
    for index, enabled in enumerate(active):
        if enabled and start is None:
            start = index
        elif not enabled and start is not None:
            values.append([round(start * resolution, 3), round(index * resolution, 3)])
            start = None
    if start is not None:
        values.append([round(start * resolution, 3), round(len(active) * resolution, 3)])
    return [value for value in values if value[1] - value[0] >= 0.75]


def analyze_vocals(
    path: str | Path,
    *,
    cache_dir: str | Path = ".setmix-cache/stems",
    force: bool = False,
    device: str = "auto",
) -> VocalMap:
    source = Path(path).expanduser().resolve()
    root = Path(cache_dir) / _key(source)
    manifest = root / "vocal-map.json"
    if manifest.exists() and not force:
        return VocalMap(**json.loads(manifest.read_text()))
    vocals, accompaniment = separate_vocals(source, cache_dir=cache_dir, device=device)

    resolution = 0.25
    sr = 22050
    hop = round(sr * resolution)
    frame = round(sr * 0.75)
    vocal, _ = librosa.load(vocals, sr=sr, mono=True)
    music, _ = librosa.load(accompaniment, sr=sr, mono=True)
    size = min(len(vocal), len(music))
    vocal = vocal[:size]
    music = music[:size]
    vocal_rms = librosa.feature.rms(y=vocal, frame_length=frame, hop_length=hop, center=True)[0]
    music_rms = librosa.feature.rms(y=music, frame_length=frame, hop_length=hop, center=True)[0]
    vocal_db = 20.0 * np.log10(np.maximum(vocal_rms, 1e-8))
    ratio = vocal_rms / (vocal_rms + music_rms + 1e-8)
    dynamic_floor = max(-42.0, float(np.percentile(vocal_db, 28)))
    active = (vocal_db > dynamic_floor) & (ratio > 0.105)
    votes = np.convolve(active.astype(np.int8), np.ones(5, dtype=np.int8), mode="same")
    active = votes >= 2
    result = VocalMap(
        path=str(source),
        model="htdemucs",
        resolution=resolution,
        segments=_segments(active, resolution),
        vocal_fraction=round(float(np.mean(active)), 4),
        confidence=0.86,
    )
    root.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(asdict(result), indent=2) + "\n")
    return result


def _candidate_indices(track: TrackAnalysis, *, incoming: bool) -> list[int]:
    aligned = list(range(track.phrase_offset, len(track.beat_times), 32))
    required = track.transition_bars * 4
    usable = [
        index
        for index in aligned
        if index + required < len(track.beat_times)
        and track.beat_times[index + required] <= track.active_end + 0.2
    ]
    # Do not sacrifice the body of the outgoing song merely to find a silent
    # vocal window. Search early phrases for the incoming track and only the
    # final few complete phrases for the outgoing track.
    return usable[:6] if incoming else usable[-5:]


def choose_vocal_safe_cues(
    left: TrackAnalysis,
    right: TrackAnalysis,
    left_vocals: VocalMap,
    right_vocals: VocalMap,
) -> tuple[float, float, float]:
    left_candidates = _candidate_indices(left, incoming=False)
    right_candidates = _candidate_indices(right, incoming=True)
    if not left_candidates or not right_candidates:
        return left.cue_out, right.cue_in, 1.0
    required = left.transition_bars * 4
    best: tuple[float, float, float] | None = None
    best_score = float("inf")
    latest_left = left_candidates[-1]
    for left_index in left_candidates:
        left_start = left.beat_times[left_index]
        left_end = left.beat_times[left_index + required]
        for right_index in right_candidates:
            right_start = right.beat_times[right_index]
            right_end = right.beat_times[right_index + required]
            points = np.linspace(0.0, 1.0, 96)
            left_active = np.asarray(
                [left_vocals.active_at(left_start + value * (left_end - left_start)) for value in points]
            )
            right_active = np.asarray(
                [right_vocals.active_at(right_start + value * (right_end - right_start)) for value in points]
            )
            overlap = float(np.mean(left_active & right_active))
            # Vocal handoff is desirable: outgoing first, incoming second.
            wrong_way = float(np.mean(right_active[:48])) + float(np.mean(left_active[48:]))
            incoming_skip = right_index / max(1, right_candidates[-1])
            outgoing_early = (latest_left - left_index) / max(1, latest_left)
            score = 8.0 * overlap + 0.7 * wrong_way + 0.22 * incoming_skip + 0.18 * outgoing_early
            if score < best_score:
                best_score = score
                best = (left_start, right_start, overlap)
    assert best is not None
    return round(best[0], 6), round(best[1], 6), round(best[2], 4)
