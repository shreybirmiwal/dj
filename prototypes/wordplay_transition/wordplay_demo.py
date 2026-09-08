#!/usr/bin/env python3
"""Render an isolated, manually anchored wordplay-transition experiment."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt


SAMPLE_RATE = 44_100
STEM_NAMES = ("vocals", "drums", "bass", "other")
ROOT = Path(__file__).resolve().parent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preset", type=Path)
    parser.add_argument(
        "--variant",
        default="all",
        help="clean, phrase-loop, word-roll, or all (default)",
    )
    parser.add_argument("--force-stems", action="store_true")
    return parser


def _stem_folder(source: Path) -> Path:
    return ROOT / "cache" / "stems" / "htdemucs" / source.stem


def _ensure_stems(source: Path, force: bool = False) -> dict[str, Path]:
    folder = _stem_folder(source)
    result = {name: folder / f"{name}.mp3" for name in STEM_NAMES}
    if not force and all(path.exists() for path in result.values()):
        return result

    command = [
        sys.executable,
        "-m",
        "demucs.separate",
        "--mp3",
        "--mp3-bitrate",
        "320",
        "-n",
        "htdemucs",
        "-o",
        str(ROOT / "cache" / "stems"),
        str(source),
    ]
    print(f"Separating stems for {source.name}", file=sys.stderr, flush=True)
    subprocess.run(command, check=True)
    if not all(path.exists() for path in result.values()):
        raise RuntimeError(f"Demucs did not produce the expected stems in {folder}")
    return result


def _fit_frames(audio: np.ndarray, frames: int) -> np.ndarray:
    if audio.ndim == 1:
        audio = np.vstack((audio, audio))
    if audio.shape[0] == 1:
        audio = np.vstack((audio, audio))
    audio = audio[:2]
    if audio.shape[1] < frames:
        audio = np.pad(audio, ((0, 0), (0, frames - audio.shape[1])))
    return audio[:, :frames].T.astype(np.float32)


def _load_timeline(
    path: Path,
    source_at_zero: float,
    output_seconds: float,
    rate: float,
) -> np.ndarray:
    source_seconds = output_seconds * rate + 0.1
    audio, _ = librosa.load(
        path,
        sr=SAMPLE_RATE,
        mono=False,
        offset=max(0.0, source_at_zero),
        duration=source_seconds,
    )
    if rate != 1.0:
        audio = librosa.effects.time_stretch(audio, rate=rate)
    return _fit_frames(audio, round(output_seconds * SAMPLE_RATE))


def _smoothstep(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, 0.0, 1.0)
    return values * values * (3.0 - 2.0 * values)


def _fade_in(frames: int, start: float, end: float) -> np.ndarray:
    positions = np.arange(frames) / SAMPLE_RATE
    return np.sin(_smoothstep((positions - start) / max(0.001, end - start)) * math.pi / 2.0)[:, None]


def _fade_out(frames: int, start: float, end: float) -> np.ndarray:
    positions = np.arange(frames) / SAMPLE_RATE
    return np.cos(_smoothstep((positions - start) / max(0.001, end - start)) * math.pi / 2.0)[:, None]


def _filter(audio: np.ndarray, cutoff: float, kind: str) -> np.ndarray:
    cutoff = float(np.clip(cutoff, 30.0, SAMPLE_RATE / 2.0 - 100.0))
    sos = butter(2, cutoff, btype=kind, fs=SAMPLE_RATE, output="sos")
    return sosfilt(sos, audio, axis=0).astype(np.float32)


def _make_loop(fragment: np.ndarray, beat_frames: int, repeats: int) -> np.ndarray:
    """Beat-fit a stereo vocal fragment and repeat it with click-free edges."""
    if repeats <= 0 or beat_frames <= 0 or len(fragment) == 0:
        return np.zeros((0, 2), dtype=np.float32)
    rate = len(fragment) / beat_frames
    fitted = librosa.effects.time_stretch(fragment.T, rate=rate)
    fitted = _fit_frames(fitted, beat_frames)
    source_peak = float(np.max(np.abs(fragment)))
    fitted_peak = float(np.max(np.abs(fitted)))
    if fitted_peak > source_peak > 0.0:
        fitted *= source_peak / fitted_peak
    edge = min(round(0.012 * SAMPLE_RATE), max(1, beat_frames // 6))
    envelope = np.ones(beat_frames, dtype=np.float32)
    envelope[:edge] = np.linspace(0.0, 1.0, edge, endpoint=False)
    envelope[-edge:] = np.linspace(1.0, 0.0, edge)
    return np.tile(fitted * envelope[:, None], (repeats, 1))


def _loop_pattern(fragment: np.ndarray, pattern_beats: list[float], beat_seconds: float) -> np.ndarray:
    """Build an accelerating loop pattern with progressively thinner repeats."""
    pieces: list[np.ndarray] = []
    count = max(1, len(pattern_beats))
    for index, beats in enumerate(pattern_beats):
        piece_frames = max(1, round(beats * beat_seconds * SAMPLE_RATE))
        piece = _make_loop(fragment, piece_frames, 1)
        progress = index / max(1, count - 1)
        piece = _filter(piece, 150.0 * (6.0**progress), "highpass")
        piece *= 1.0 - 0.22 * progress
        pieces.append(piece)
    if not pieces:
        return np.zeros((0, 2), dtype=np.float32)
    return np.concatenate(pieces)


def _echo(audio: np.ndarray, delay_frames: int) -> np.ndarray:
    result = audio.copy()
    for repeat, gain in ((1, 0.28), (2, 0.14)):
        offset = repeat * delay_frames
        if offset < len(audio):
            result[offset:] += audio[:-offset] * gain
    return result


def _aligned_timing(config: dict, variant: dict) -> tuple[float, float, float]:
    """Return phrase end, matching-word drop, and output duration."""
    left_rate = config["target_bpm"] / config["left_bpm"]
    right_rate = config["target_bpm"] / config["right_bpm"]
    phrase_end = (config["left_phrase_end"] - config["left_context_start"]) / left_rate
    build_seconds = (variant["right_word_start"] - variant["right_build_start"]) / right_rate
    drop = phrase_end + build_seconds
    return phrase_end, drop, drop + config["post_seconds"]


def _render_aligned_swap(
    config: dict,
    name: str,
    variant: dict,
    stems: tuple[dict[str, Path], dict[str, Path]],
) -> Path:
    """Loop song one's word while song two runs toward the same word."""
    left_rate = config["target_bpm"] / config["left_bpm"]
    right_rate = config["target_bpm"] / config["right_bpm"]
    beat_seconds = 60.0 / config["target_bpm"]
    phrase_end, drop, total_seconds = _aligned_timing(config, variant)
    frames = round(total_seconds * SAMPLE_RATE)
    left_paths, right_paths = stems

    left = {
        stem: _load_timeline(path, config["left_context_start"], total_seconds, left_rate)
        for stem, path in left_paths.items()
    }
    # At phrase_end the incoming timeline is exactly right_build_start; at
    # drop it is exactly right_word_start.
    right_source_at_zero = variant["right_build_start"] - phrase_end * right_rate
    right = {
        stem: _load_timeline(path, right_source_at_zero, total_seconds, right_rate)
        for stem, path in right_paths.items()
    }

    positions = np.arange(frames) / SAMPLE_RATE
    overlap = max(0.001, drop - phrase_end)
    incoming_build = _fade_in(frames, phrase_end, phrase_end + min(4.0 * beat_seconds, overlap * 0.45))
    incoming_bass = _fade_in(frames, drop - 2.0 * beat_seconds, drop + 0.15 * beat_seconds)

    # Song one stays recognizably present during the loop, but its bass and
    # melodic body make room for song two. Every outgoing stem is hard-zeroed
    # on the exact frame where song two sings the matching word.
    left_body = np.ones((frames, 1), dtype=np.float32)
    left_body *= 1.0 - 0.42 * _smoothstep((positions - phrase_end) / overlap)[:, None]
    # Twelve milliseconds still reads as a hard DJ cut while preventing a
    # sample discontinuity from producing a digital click.
    kill = _fade_out(frames, drop - 0.012, drop)
    left_body *= kill
    left_bass = _fade_out(frames, phrase_end, phrase_end + min(4.0 * beat_seconds, overlap * 0.55))
    left_bass *= kill

    right_other_filtered = _filter(right["other"], 4_200.0, "lowpass")
    after_drop = (positions >= drop).astype(np.float32)[:, None]
    before_drop = 1.0 - after_drop
    mixed = left["drums"] * left_body
    mixed += _filter(left["other"], 220.0, "highpass") * left_body
    mixed += left["bass"] * left_bass
    mixed += right["drums"] * incoming_build
    mixed += (right_other_filtered * before_drop + right["other"] * after_drop) * incoming_build
    mixed += right["bass"] * incoming_bass

    # The original outgoing vocal stops after saying the source phrase. The
    # loop replaces it until the target vocal arrives, then is hard-killed.
    left_vocal = np.ones((frames, 1), dtype=np.float32)
    left_vocal[positions >= phrase_end] = 0.0
    right_vocal = np.zeros((frames, 1), dtype=np.float32)
    right_vocal[positions >= drop] = 1.0
    mixed += left["vocals"] * left_vocal
    mixed += right["vocals"] * right_vocal

    source_start = round((variant["loop_start"] - config["left_context_start"]) / left_rate * SAMPLE_RATE)
    source_end = round((variant["loop_end"] - config["left_context_start"]) / left_rate * SAMPLE_RATE)
    fragment = left["vocals"][max(0, source_start) : max(source_start + 1, source_end)]
    loop_frames = max(1, round(variant["loop_beats"] * beat_seconds * SAMPLE_RATE))
    one_loop = _make_loop(fragment, loop_frames, 1)
    needed = max(0, round((drop - phrase_end) * SAMPLE_RATE))
    repeats = math.ceil(needed / max(1, len(one_loop)))
    loop = np.tile(one_loop, (repeats, 1))[:needed]
    # A slow high-pass progression adds tension while preserving the steady
    # repeated-word rhythm the transition is built around.
    for repeat in range(repeats):
        start = repeat * loop_frames
        end = min(len(loop), start + loop_frames)
        progress = repeat / max(1, repeats - 1)
        loop[start:end] = _filter(loop[start:end], 120.0 * (4.0**progress), "highpass")
        loop[start:end] *= 0.92 - 0.12 * progress
    tail = min(len(loop), round(0.012 * SAMPLE_RATE))
    if tail:
        loop[-tail:] *= np.linspace(1.0, 0.0, tail, dtype=np.float32)[:, None]
    loop_start_frame = round(phrase_end * SAMPLE_RATE)
    mixed[loop_start_frame : loop_start_frame + len(loop)] += loop

    peak = float(np.max(np.abs(mixed)))
    if peak > 0.88:
        mixed *= 0.88 / peak
    return _write_output(config, name, mixed)


def _write_output(config: dict, name: str, mixed: np.ndarray) -> Path:
    output_dir = ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = config.get("output_slug", "wordplay-transition")
    wav = output_dir / f"{slug}-{name}.wav"
    mp3 = wav.with_suffix(".mp3")
    sf.write(wav, mixed, SAMPLE_RATE, subtype="PCM_24")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(wav),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "256k",
            str(mp3),
        ],
        check=True,
    )
    wav.unlink()
    print(f"Wrote {mp3}", file=sys.stderr, flush=True)
    return mp3


def _render_variant(config: dict, name: str, variant: dict, stems: tuple[dict[str, Path], dict[str, Path]]) -> Path:
    if variant.get("mode") == "aligned_swap":
        return _render_aligned_swap(config, name, variant, stems)
    left_rate = config["target_bpm"] / config["left_bpm"]
    right_rate = config["target_bpm"] / config["right_bpm"]
    beat_seconds = 60.0 / config["target_bpm"]
    phrase_end = (config["left_phrase_end"] - config["left_context_start"]) / left_rate
    phrase_start = (config["left_phrase_start"] - config["left_context_start"]) / left_rate
    pattern_beats = variant.get(
        "pattern_beats",
        [variant.get("loop_beats", 0)] * variant.get("repeats", 0),
    )
    loop_seconds = sum(pattern_beats) * beat_seconds
    vacuum_seconds = variant.get("vacuum_beats", 0.0) * beat_seconds
    drop = phrase_end + loop_seconds + vacuum_seconds
    total_seconds = drop + config["post_seconds"]
    frames = round(total_seconds * SAMPLE_RATE)

    left_paths, right_paths = stems
    left = {
        stem: _load_timeline(path, config["left_context_start"], total_seconds, left_rate)
        for stem, path in left_paths.items()
    }
    right_source_at_zero = config["right_phrase_start"] - drop * right_rate
    right = {
        stem: _load_timeline(path, right_source_at_zero, total_seconds, right_rate)
        for stem, path in right_paths.items()
    }

    # Incoming drums establish the groove early. Bass waits until the drop;
    # melodic material rises behind a low-pass filter to keep the lyric clear.
    drum_cross_start = max(1.0, phrase_start - 8.0 * beat_seconds)
    drums_in = _fade_in(frames, drum_cross_start, drop)
    drums_out = _fade_out(frames, phrase_start, drop + 2.0 * beat_seconds)
    bass_in = _fade_in(frames, drop - beat_seconds, drop + beat_seconds)
    bass_out = _fade_out(frames, drop - beat_seconds, drop + 0.25 * beat_seconds)
    other_in = _fade_in(frames, phrase_start - 2.0 * beat_seconds, drop + beat_seconds)
    other_out = _fade_out(frames, phrase_start, drop)

    mixed = left["drums"] * drums_out + right["drums"] * drums_in
    mixed += left["bass"] * bass_out + right["bass"] * bass_in
    mixed += _filter(left["other"], 180.0, "highpass") * other_out
    mixed += _filter(right["other"], 4_800.0, "lowpass") * other_in

    if variant.get("riser") and loop_seconds > 0.0:
        loop_start_frame = round(phrase_end * SAMPLE_RATE)
        loop_end_frame = round((phrase_end + loop_seconds) * SAMPLE_RATE)
        riser_frames = max(1, loop_end_frame - loop_start_frame)
        rng = np.random.default_rng(7)
        noise = rng.normal(0.0, 1.0, (riser_frames, 2)).astype(np.float32)
        noise = _filter(noise, 2_500.0, "highpass")
        rise = np.linspace(0.0, 1.0, riser_frames, dtype=np.float32)[:, None] ** 2
        mixed[loop_start_frame:loop_end_frame] += noise * rise * 0.018

    # The last beat drops almost to silence. Restoring full level exactly at
    # the incoming phrase makes the wordplay land like a deliberate drop.
    if vacuum_seconds > 0.0:
        vacuum_start = drop - vacuum_seconds
        positions = np.arange(frames) / SAMPLE_RATE
        vacuum = np.ones(frames, dtype=np.float32)
        inside = (positions >= vacuum_start) & (positions < drop)
        vacuum[inside] = 1.0 - 0.94 * _smoothstep(
            (positions[inside] - vacuum_start) / vacuum_seconds
        )
        mixed *= vacuum[:, None]

    # Preserve the complete outgoing lyric, then make a deliberate vocal
    # pocket. The incoming matching phrase starts exactly on the drop.
    left_vocal_out = _fade_out(frames, phrase_end - 0.08, phrase_end + 0.04)
    right_vocal_in = _fade_in(frames, drop - 0.025, drop + 0.06)
    mixed += left["vocals"] * left_vocal_out
    mixed += right["vocals"] * right_vocal_in

    if pattern_beats:
        source_start = round((variant["loop_start"] - config["left_context_start"]) / left_rate * SAMPLE_RATE)
        source_end = round((variant["loop_end"] - config["left_context_start"]) / left_rate * SAMPLE_RATE)
        fragment = left["vocals"][max(0, source_start) : max(source_start + 1, source_end)]
        loop = _loop_pattern(fragment, pattern_beats, beat_seconds)
        loop = _echo(loop, max(1, round(beat_seconds * SAMPLE_RATE / 2.0)))
        loop_start_frame = round(phrase_end * SAMPLE_RATE)
        loop_end_frame = min(frames, loop_start_frame + len(loop))
        mixed[loop_start_frame:loop_end_frame] += loop[: loop_end_frame - loop_start_frame]

    peak = float(np.max(np.abs(mixed)))
    if peak > 0.88:
        mixed *= 0.88 / peak

    return _write_output(config, name, mixed)


def main() -> int:
    args = _parser().parse_args()
    config = json.loads(args.preset.expanduser().resolve().read_text())
    left_source = Path(config["left"]).expanduser().resolve()
    right_source = Path(config["right"]).expanduser().resolve()
    if not left_source.is_file() or not right_source.is_file():
        raise SystemExit("Both preset audio files must exist")
    variants = config["variants"]
    names = list(variants) if args.variant == "all" else [args.variant]
    missing = [name for name in names if name not in variants]
    if missing:
        raise SystemExit(f"Unknown variant: {', '.join(missing)}")

    stems = (
        _ensure_stems(left_source, args.force_stems),
        _ensure_stems(right_source, args.force_stems),
    )
    for name in names:
        _render_variant(config, name, variants[name], stems)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
