#!/usr/bin/env python3
"""Serve the SetMix UI and expose a read-only local music catalog."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


AUDIO_SUFFIXES = {".aif", ".aiff", ".flac", ".m4a", ".mp3", ".wav"}
UI_DIR = Path(__file__).resolve().parent
PROJECT_DIR = UI_DIR.parent
CACHE_ROOT = Path(os.environ.get("SETMIX_CACHE_DIR", PROJECT_DIR / ".setmix-cache")).expanduser()
MIX_CACHE_DIR = CACHE_ROOT / "ui-mixes"
MEDIA_CACHE_DIR = CACHE_ROOT / "ui-media"
WAVEFORM_CACHE_DIR = CACHE_ROOT / "ui-waveforms"
INTELLIGENCE_CACHE_DIR = CACHE_ROOT / "intelligence"
LRCLIB_CACHE_DIR = CACHE_ROOT / "lyrics-lrclib"
MEDIA_CACHE_LOCK = threading.Lock()
WAVEFORM_CACHE_LOCK = threading.Lock()
LRCLIB_LOCK = threading.Lock()
LRCLIB_USER_AGENT = "SetMix/0.1.0 (https://github.com/shreybirmiwal/dj)"
_lrclib_last_request = 0.0
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


def default_music_dir() -> Path:
    configured = os.environ.get("SETMIX_MUSIC_DIR")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path.home() / "Documents" / "DJ Music",
        Path.home() / "Documents" / "dj-music",
        Path.home() / "Desktop" / "dj-music",
    ]
    return next((path for path in candidates if path and path.is_dir()), candidates[1])


def _text_tag(tags: object, *names: str) -> str | None:
    if not tags:
        return None
    for name in names:
        value = tags.get(name)  # type: ignore[union-attr]
        if isinstance(value, (list, tuple)) and value:
            return str(value[0]).strip() or None
        if value:
            return str(value).strip() or None
    return None


def _filename_metadata(path: Path) -> tuple[str, str]:
    name = re.sub(r"[_]+", " ", path.stem).strip()
    name = re.sub(r"^\d{1,3}\s*[-.]\s*", "", name)
    parts = [part.strip() for part in name.split(" - ") if part.strip()]
    if len(parts) >= 2:
        return " - ".join(parts[1:]), parts[0]
    return name, "Unknown artist"


def _analysis_cache() -> dict[str, dict]:
    values: dict[str, dict] = {}
    for path in (CACHE_ROOT / "analysis").glob("*.json"):
        try:
            record = json.loads(path.read_text())
            source = record.get("path")
            if source:
                values[source] = record
        except (OSError, json.JSONDecodeError):
            continue
    return values


def build_catalog(music_dir: Path) -> list[dict]:
    try:
        import mutagen
    except ImportError:
        mutagen = None

    cached = _analysis_cache()
    records: list[dict] = []
    paths = sorted(
        (path for path in music_dir.rglob("*") if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES),
        key=lambda path: str(path).casefold(),
    )
    for index, path in enumerate(paths):
        fallback_title, fallback_artist = _filename_metadata(path)
        title, artist = fallback_title, fallback_artist
        genre = None
        album = None
        duration = 0.0
        tagged_bpm: float | None = None
        if mutagen:
            try:
                audio = mutagen.File(path, easy=True)
                if audio:
                    title = _text_tag(audio.tags, "title") or title
                    artist = _text_tag(audio.tags, "artist", "albumartist") or artist
                    genre = _text_tag(audio.tags, "genre")
                    album = _text_tag(audio.tags, "album")
                    duration = float(getattr(audio.info, "length", 0.0) or 0.0)
                    bpm_value = _text_tag(audio.tags, "bpm")
                    if bpm_value:
                        tagged_bpm = float(bpm_value)
            except (OSError, ValueError, TypeError):
                pass

        analysis = cached.get(str(path.resolve()), {})
        bpm_value = analysis.get("bpm", tagged_bpm)
        bpm = round(float(bpm_value), 1) if bpm_value else None
        duration = float(analysis.get("duration", duration) or duration)
        musical_key = analysis.get("musical_key")
        camelot = analysis.get("camelot_key")
        rms_db = analysis.get("rms_db")
        energy = max(1, min(5, round((float(rms_db) + 31.0) / 4.0))) if rms_db is not None else 3
        relative = path.relative_to(music_dir)
        if not genre:
            genre = relative.parts[0].replace("-", " ").title() if len(relative.parts) > 1 else "Library"
        track_id = hashlib.sha1(str(relative).encode()).hexdigest()[:16]
        records.append(
            {
                "id": track_id,
                "title": title,
                "artist": artist,
                "album": album,
                "bpm": bpm,
                "key": musical_key if musical_key and musical_key != "unknown" else None,
                "camelot": camelot if camelot and camelot != "unknown" else None,
                "energy": energy,
                "length": round(duration),
                "genre": genre,
                "folder": str(relative.parent) if str(relative.parent) != "." else "Library",
                "cover": index % 8 + 1,
                "match": 80 + (index * 7 % 17),
                "technique": "Bass swap",
                "mediaUrl": f"/media/{track_id}",
                "cueIn": analysis.get("cue_in"),
                "cueOut": analysis.get("cue_out"),
                "activeEnd": analysis.get("active_end"),
                "_path": path,
            }
        )
    return records


def browser_media_path(path: Path) -> Path:
    """Return audio WebKit/Chromium can decode, caching one MP3 per source revision."""
    if path.suffix.lower() in {".m4a", ".mp3", ".wav"}:
        return path
    stat = path.stat()
    fingerprint = hashlib.sha256(
        f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    ).hexdigest()[:24]
    output = MEDIA_CACHE_DIR / f"{fingerprint}.mp3"
    if output.exists():
        return output
    with MEDIA_CACHE_LOCK:
        if output.exists():
            return output
        MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = MEDIA_CACHE_DIR / f"{fingerprint}.tmp.mp3"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(path),
                    "-map_metadata",
                    "-1",
                    "-c:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(temporary),
                ],
                check=True,
            )
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    return output


def _vocal_segments(path: Path) -> list[list[float]]:
    resolved = str(path.resolve())
    for record_path in (CACHE_ROOT / "stems").glob("*/vocal-map.json"):
        try:
            record = json.loads(record_path.read_text())
            if record.get("path") == resolved:
                return [[float(start), float(end)] for start, end in record.get("segments", [])]
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return []


def waveform_summary(path: Path, *, bins: int = 900) -> dict:
    """Build a cached, real three-band waveform suitable for deck rendering."""
    import numpy as np

    stat = path.stat()
    fingerprint = hashlib.sha256(
        f"waveform-v3:{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{bins}".encode()
    ).hexdigest()[:24]
    output = WAVEFORM_CACHE_DIR / f"{fingerprint}.json"
    if output.exists():
        return json.loads(output.read_text())

    with WAVEFORM_CACHE_LOCK:
        if output.exists():
            return json.loads(output.read_text())
        from setmix.analysis import load_analysis_audio

        samples, sample_rate = load_analysis_audio(path)
        if samples.size == 0:
            raise ValueError(f"No waveform samples decoded from {path}")

        edges = np.linspace(0, samples.size, bins + 1, dtype=np.int64)
        rows = np.zeros((bins, 3), dtype=np.float64)
        peaks = np.zeros(bins, dtype=np.float64)
        frequency_bins = np.fft.rfftfreq(512, 1 / sample_rate)
        masks = (
            frequency_bins < 250,
            (frequency_bins >= 250) & (frequency_bins < 2500),
            frequency_bins >= 2500,
        )
        for index in range(bins):
            segment = samples[edges[index]:edges[index + 1]]
            if segment.size == 0:
                continue
            peaks[index] = float(np.percentile(np.abs(segment), 97))
            if segment.size < 32:
                spectrum = np.abs(np.fft.rfft(segment, n=512))
            else:
                take = np.linspace(0, segment.size - 1, 512, dtype=np.int64)
                spectrum = np.abs(np.fft.rfft(segment[take] * np.hanning(512)))
            energy = np.asarray([float(np.sqrt(np.mean(spectrum[mask] ** 2))) for mask in masks])
            total = float(energy.sum())
            rows[index] = energy / total if total else (0, 0, 0)

        reference = float(np.percentile(peaks, 99)) or 1.0
        envelope = np.clip(peaks / reference, 0, 1)
        rows *= envelope[:, None]
        payload = {
            "duration": round(samples.size / sample_rate, 3),
            "sampleRate": sample_rate,
            "bands": np.round(rows, 4).tolist(),
            "vocalSegments": _vocal_segments(path),
        }
        WAVEFORM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")))
        temporary.replace(output)
        return payload


def _lrclib_cache_path(path: Path) -> Path:
    source = path.expanduser().resolve()
    stat = source.stat()
    fingerprint = hashlib.sha256(
        f"lrclib-v1:{source}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    ).hexdigest()[:24]
    return LRCLIB_CACHE_DIR / f"{fingerprint}.json"


def _parse_synced_lyrics(value: str) -> list[dict]:
    parsed: list[tuple[float, str]] = []
    timestamp = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
    for line in value.splitlines():
        matches = list(timestamp.finditer(line))
        text = timestamp.sub("", line).strip()
        if not text:
            continue
        for match in matches:
            parsed.append((int(match.group(1)) * 60 + float(match.group(2)), text))
    parsed.sort(key=lambda item: item[0])
    return [
        {
            "start": round(start, 3),
            "end": round(max(start + 0.25, parsed[index + 1][0] if index + 1 < len(parsed) else start + 5.0), 3),
            "text": text,
        }
        for index, (start, text) in enumerate(parsed)
    ]


def _lrclib_request(endpoint: str, parameters: dict[str, object]) -> object:
    global _lrclib_last_request
    with LRCLIB_LOCK:
        url = f"https://lrclib.net{endpoint}?{urlencode(parameters)}"
        request = Request(url, headers={"User-Agent": LRCLIB_USER_AGENT, "Accept": "application/json"})
        for attempt in range(2):
            wait = 0.25 - (time.monotonic() - _lrclib_last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                with urlopen(request, timeout=8) as response:  # noqa: S310 - fixed trusted API host
                    return json.loads(response.read())
            except HTTPError as error:
                if error.code != 429 or attempt:
                    raise
                retry_after = max(0.0, float(error.headers.get("Retry-After", "1")))
                time.sleep(retry_after)
            finally:
                _lrclib_last_request = time.monotonic()
        raise RuntimeError("LRCLIB retry exhausted")


def _lrclib_match(metadata: dict, records: list[dict]) -> dict | None:
    title = str(metadata.get("title") or "").casefold()
    artist = str(metadata.get("artist") or "").casefold()
    duration = float(metadata.get("length") or 0)

    def score(record: dict) -> float:
        if not record.get("syncedLyrics"):
            return -1000.0
        result = 4.0 * difflib.SequenceMatcher(None, title, str(record.get("trackName", "")).casefold()).ratio()
        result += 3.0 * difflib.SequenceMatcher(None, artist, str(record.get("artistName", "")).casefold()).ratio()
        if duration and record.get("duration"):
            result -= min(6.0, abs(duration - float(record["duration"])) / 2.0)
        return result

    if not records:
        return None
    best = max(records, key=score)
    title_match = difflib.SequenceMatcher(None, title, str(best.get("trackName", "")).casefold()).ratio()
    artist_match = difflib.SequenceMatcher(None, artist, str(best.get("artistName", "")).casefold()).ratio()
    duration_gap = abs(duration - float(best.get("duration") or duration)) if duration else 0.0
    if not best.get("syncedLyrics") or title_match < 0.72 or artist_match < 0.62 or duration_gap > 15.0:
        return None
    return best


def fetch_lrclib_lyrics(path: Path, metadata: dict, *, force: bool = False) -> dict | None:
    """Fetch and cache synchronized LRCLIB lines for one local track."""
    output = _lrclib_cache_path(path)
    if output.exists() and not force:
        payload = json.loads(output.read_text())
        return payload if payload.get("status") == "ready" else None
    title = str(metadata.get("title") or "").strip()
    artist = str(metadata.get("artist") or "").strip()
    if not title or not artist or artist.casefold() == "unknown artist":
        return None
    record: dict | None = None
    parameters: dict[str, object] = {"track_name": title, "artist_name": artist}
    if metadata.get("album"):
        parameters["album_name"] = metadata["album"]
    if metadata.get("length"):
        parameters["duration"] = int(round(float(metadata["length"])))
    try:
        try:
            exact = _lrclib_request("/api/get", parameters)
            record = exact if isinstance(exact, dict) else None
        except HTTPError as error:
            if error.code != 404:
                raise
        if not record or not record.get("syncedLyrics"):
            searched = _lrclib_request(
                "/api/search",
                {"track_name": title, "artist_name": artist},
            )
            if isinstance(searched, list):
                record = _lrclib_match(metadata, searched)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    phrases = _parse_synced_lyrics(str((record or {}).get("syncedLyrics") or ""))
    if not phrases:
        LRCLIB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"status": "missing", "path": str(path.resolve())}) + "\n")
        return None
    payload = {
        "status": "ready",
        "source": "LRCLIB",
        "model": f"lrclib/{record.get('id', 'match')}",
        "language": "unknown",
        "confidence": 0.99,
        "phrases": phrases,
        "words": [],
        "path": str(path.resolve()),
        "provider": {
            "id": record.get("id"),
            "trackName": record.get("trackName"),
            "artistName": record.get("artistName"),
            "albumName": record.get("albumName"),
        },
    }
    LRCLIB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(output)
    return payload


def lyrics_summary(path: Path) -> dict:
    """Return cached machine-transcribed lyrics without starting expensive work."""
    resolved = str(path.expanduser().resolve())
    provider_cache = _lrclib_cache_path(path)
    if provider_cache.exists():
        try:
            provider = json.loads(provider_cache.read_text())
            if provider.get("status") == "ready" and provider.get("path") == resolved:
                return provider
        except (OSError, json.JSONDecodeError):
            pass
    candidates = sorted(
        INTELLIGENCE_CACHE_DIR.glob("*/transcript.json"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    for transcript_path in candidates:
        try:
            payload = json.loads(transcript_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("path") != resolved:
            continue
        phrases = [
            {
                "start": float(item["start"]),
                "end": float(item["end"]),
                "text": str(item["text"]).strip(),
            }
            for item in payload.get("phrases", [])
            if str(item.get("text", "")).strip()
        ]
        words = [
            {
                "word": str(item["word"]).strip(),
                "start": float(item["start"]),
                "end": float(item["end"]),
                "probability": float(item.get("probability", 0.0)),
            }
            for item in payload.get("words", [])
            if str(item.get("word", "")).strip()
        ]
        return {
            "status": "ready",
            "source": "LOCAL AI",
            "model": str(payload.get("model", "unknown")),
            "language": str(payload.get("language", "unknown")),
            "confidence": float(payload.get("confidence", 0.0)),
            "phrases": phrases,
            "words": words,
        }
    return {
        "status": "missing",
        "model": None,
        "language": None,
        "confidence": 0.0,
        "phrases": [],
        "words": [],
    }


class LyricsManager:
    """Run optional per-track stem separation and transcription in the background."""

    def __init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="setmix-lyrics")
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}

    def create(
        self,
        track_id: str,
        path: Path,
        model: str,
        metadata: dict,
        *,
        provider_only: bool = False,
    ) -> dict:
        cached = lyrics_summary(path)
        if cached["status"] == "ready":
            return cached
        with self.lock:
            existing = self.jobs.get(track_id)
            if existing and existing["status"] in {"queued", "working"}:
                return dict(existing)
            job = {
                "status": "queued",
                "stage": "queued",
                "progress": 2,
                "message": "Waiting for the lyric analysis engine",
            }
            self.jobs[track_id] = job
        self.pool.submit(self._prepare, track_id, path, model, metadata, provider_only)
        return dict(job)

    def _update(self, track_id: str, **values: object) -> None:
        with self.lock:
            self.jobs[track_id].update(values)

    def _prepare(
        self,
        track_id: str,
        path: Path,
        model: str,
        metadata: dict,
        provider_only: bool,
    ) -> None:
        try:
            from setmix.analysis import analyze_track
            from setmix.intelligence import analyze_intelligence
            from setmix.stems import analyze_vocals, separate_stems

            self._update(
                track_id,
                status="working",
                stage="lrclib",
                progress=15,
                message="Checking LRCLIB for synchronized lyrics",
            )
            provider = fetch_lrclib_lyrics(path, metadata)
            if provider:
                self._update(
                    track_id,
                    **{
                        **provider,
                        "status": "ready",
                        "stage": "ready",
                        "progress": 100,
                        "message": "Synchronized LRCLIB lyrics ready",
                    },
                )
                return
            if provider_only:
                self._update(
                    track_id,
                    status="missing",
                    stage="provider-miss",
                    progress=100,
                    message="No synchronized LRCLIB match",
                )
                return
            self._update(
                track_id,
                stage="stems",
                progress=30,
                message="LRCLIB unavailable; separating vocals for local timing",
            )
            analysis = analyze_track(path, transition_bars=32)
            vocals = analyze_vocals(path)
            self._update(
                track_id,
                stage="words",
                progress=62,
                message="Transcribing word-level vocal timestamps",
            )
            analyze_intelligence(analysis, vocals, word_model=model)
            result = lyrics_summary(path)
            self._update(
                track_id,
                **{
                    **result,
                    "status": "ready",
                    "stage": "ready",
                    "progress": 100,
                    "message": "Timestamped local transcript ready",
                },
            )
        except Exception as error:
            self._update(
                track_id,
                status="error",
                stage="error",
                progress=0,
                message=str(error),
                error=type(error).__name__,
            )

    def get(self, track_id: str, path: Path) -> dict:
        with self.lock:
            job = self.jobs.get(track_id)
            if job and job["status"] in {"queued", "working", "error"}:
                return dict(job)
        return lyrics_summary(path)


class MixManager:
    """Prepare expensive two-track handoffs without blocking HTTP playback."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        # One neural render at a time avoids competing Demucs/Whisper processes.
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="setmix-render")
        MIX_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key(left: Path, right: Path, options: dict) -> str:
        left_stat, right_stat = left.stat(), right.stat()
        value = json.dumps(
            {
                "left": [str(left), left_stat.st_size, left_stat.st_mtime_ns],
                "right": [str(right), right_stat.st_size, right_stat.st_mtime_ns],
                "options": options,
                "version": 3,
            },
            sort_keys=True,
        )
        return hashlib.sha256(value.encode()).hexdigest()[:24]

    def create(self, left: Path, right: Path, options: dict) -> dict:
        job_id = self._key(left, right, options)
        output = MIX_CACHE_DIR / f"{job_id}.mp3"
        metadata_path = output.with_suffix(output.suffix + ".json")
        legacy_output = output.with_suffix(".flac")
        legacy_metadata = legacy_output.with_suffix(legacy_output.suffix + ".json")
        if not output.exists() and legacy_output.exists() and legacy_metadata.exists():
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(legacy_output),
                    "-c:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(output),
                ],
                check=True,
            )
            metadata_path.write_text(legacy_metadata.read_text())
        with self.lock:
            existing = self.jobs.get(job_id)
            if existing and existing["status"] not in {"error"}:
                return self.public(existing)
            if output.exists() and metadata_path.exists():
                job = {
                    "id": job_id,
                    "status": "ready",
                    "stage": "ready",
                    "message": "Full smart transition is ready",
                    "progress": 100,
                    "createdAt": time.time(),
                    "output": output,
                    "result": json.loads(metadata_path.read_text()),
                    "options": options,
                }
                self.jobs[job_id] = job
                return self.public(job)
            job = {
                "id": job_id,
                "status": "queued",
                "stage": "queued",
                "message": "Waiting for the neural mix engine",
                "progress": 2,
                "createdAt": time.time(),
                "output": output,
                "result": None,
                "options": options,
            }
            self.jobs[job_id] = job
        self.pool.submit(self._prepare, job_id, left, right, options)
        return self.public(job)

    def _update(self, job_id: str, **values: object) -> None:
        with self.lock:
            self.jobs[job_id].update(values)

    def _prepare(self, job_id: str, left: Path, right: Path, options: dict) -> None:
        try:
            from setmix.engine import analyze_ordered, create_plan, render_pair_handoff
            from setmix.intelligence import (
                LyricPhrase,
                TrackIntelligence,
                VocalTranscript,
                analyze_intelligence,
                analyze_sections,
            )
            from setmix.stems import analyze_vocals, separate_stems_batch

            def analysis_progress(message: str) -> None:
                self._update(
                    job_id,
                    status="working",
                    stage="analysis",
                    message=message,
                    progress=12,
                )

            analyses = analyze_ordered(
                [left, right],
                transition_bars=options["bars"],
                workers=2,
                progress=analysis_progress,
            )
            self._update(
                job_id,
                status="working",
                stage="vocals",
                message="Separating both tracks in one neural stem session",
                progress=28,
            )
            separate_stems_batch([analysis.path for analysis in analyses])
            with ThreadPoolExecutor(max_workers=2) as pool:
                vocal_futures = {
                    analysis.path: pool.submit(analyze_vocals, analysis.path)
                    for analysis in analyses
                }
                vocal_maps = {
                    path: future.result() for path, future in vocal_futures.items()
                }
            self._update(
                job_id,
                stage="intelligence",
                message="Detecting sections and synchronized lyric boundaries",
                progress=48,
            )
            metadata_by_id = {
                item["id"]: item for item in options.get("trackMetadata", [])
            }

            def prepare_intelligence(analysis, track_id: str) -> TrackIntelligence:
                metadata = metadata_by_id.get(track_id, {})
                provider = fetch_lrclib_lyrics(Path(analysis.path), metadata)
                if provider:
                    transcript = VocalTranscript(
                        path=analysis.path,
                        model=str(provider["model"]),
                        language=str(provider.get("language", "unknown")),
                        words=[],
                        phrases=[LyricPhrase(**item) for item in provider["phrases"]],
                        confidence=float(provider.get("confidence", 0.99)),
                    )
                    return TrackIntelligence(
                        path=analysis.path,
                        sections=analyze_sections(analysis, vocal_maps[analysis.path]),
                        transcript=transcript,
                    )
                return analyze_intelligence(
                    analysis,
                    vocal_maps[analysis.path],
                    word_model=options["wordModel"],
                )

            track_ids = (options["fromId"], options["toId"])
            with ThreadPoolExecutor(max_workers=2) as pool:
                intelligence_futures = {
                    analysis.path: pool.submit(prepare_intelligence, analysis, track_id)
                    for analysis, track_id in zip(analyses, track_ids)
                }
                intelligence: dict[str, TrackIntelligence] = {
                    path: future.result()
                    for path, future in intelligence_futures.items()
                }
            self._update(
                job_id,
                stage="planning",
                message="Ranking phrase-safe transition windows",
                progress=65,
            )
            plan = create_plan(
                analyses,
                target_bpm=options["targetBpm"],
                vocal_maps=vocal_maps,
                intelligence=intelligence,
                technique=options["technique"],
            )

            def render_progress(message: str) -> None:
                self._update(
                    job_id,
                    stage="rendering",
                    message=message,
                    progress=82,
                )

            result = render_pair_handoff(
                plan,
                self.jobs[job_id]["output"],
                pre_roll_seconds=options["preRoll"],
                progress=render_progress,
            )
            result.update(
                {
                    "mediaUrl": f"/mixes/{job_id}",
                    "fromId": options["fromId"],
                    "toId": options["toId"],
                    "bars": options["bars"],
                    "technique": result["transition"]["technique"],
                    "score": result["transition"].get("candidate_score"),
                    "reasons": result["transition"].get("reasons") or [],
                    "fromSection": result["transition"].get("from_section"),
                    "toSection": result["transition"].get("to_section"),
                }
            )
            self.jobs[job_id]["output"].with_suffix(
                self.jobs[job_id]["output"].suffix + ".json"
            ).write_text(json.dumps(result, indent=2) + "\n")
            self._update(
                job_id,
                status="ready",
                stage="ready",
                message="Full smart transition is ready",
                progress=100,
                result=result,
            )
        except Exception as error:  # keep the audio server alive and expose the failure
            self._update(
                job_id,
                status="error",
                stage="error",
                message=str(error),
                progress=0,
                error=type(error).__name__,
            )

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return self.public(job) if job else None

    @staticmethod
    def public(job: dict) -> dict:
        return {key: value for key, value in job.items() if key != "output"}

    def output(self, job_id: str) -> Path | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["status"] != "ready":
                return None
            return job["output"]


class PrefetchManager:
    """Warm likely next-track caches without blocking the interactive request."""

    def __init__(self) -> None:
        # Keep speculative analysis from competing with realtime audio or a
        # user-requested render. Selected mixes still use their dedicated pool.
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="setmix-prefetch")
        self.lock = threading.Lock()
        self.jobs: dict[str, str] = {}

    def create(self, tracks: list[dict]) -> dict:
        scheduled: list[str] = []
        for track in tracks[:3]:
            path = track["_path"]
            stat = path.stat()
            key = hashlib.sha256(
                f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:prefetch-v1".encode()
            ).hexdigest()[:24]
            with self.lock:
                if self.jobs.get(key) in {"queued", "working", "ready"}:
                    continue
                self.jobs[key] = "queued"
            self.pool.submit(self._prepare, key, path)
            scheduled.append(track["id"])
        return {"status": "accepted", "scheduled": scheduled}

    def _prepare(self, key: str, path: Path) -> None:
        try:
            from setmix.analysis import analyze_track

            with self.lock:
                self.jobs[key] = "working"
            analyze_track(path, transition_bars=32)
            waveform_summary(path)
            with self.lock:
                self.jobs[key] = "ready"
        except Exception:
            with self.lock:
                self.jobs[key] = "error"


MIX_MANAGER = MixManager()
LYRICS_MANAGER = LyricsManager()
PREFETCH_MANAGER = PrefetchManager()


class SetMixHandler(SimpleHTTPRequestHandler):
    catalog: list[dict] = []
    catalog_by_id: dict[str, dict] = {}
    music_dir: Path

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def _track(self, track_id: str) -> dict | None:
        return self.catalog_by_id.get(track_id)

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/catalog":
            query = parse_qs(urlparse(self.path).query)
            if query.get("refresh") == ["1"]:
                self.__class__.catalog = build_catalog(self.music_dir)
                self.__class__.catalog_by_id = {track["id"]: track for track in self.catalog}
            payload = {
                "source": str(self.music_dir),
                "count": len(self.catalog),
                "tracks": [{key: value for key, value in track.items() if key != "_path"} for track in self.catalog],
            }
            self._send_json(payload)
            return
        if route.startswith("/api/waveforms/"):
            track_id = route.removeprefix("/api/waveforms/")
            track = self._track(track_id)
            if not track:
                self.send_error(404, "Track not found")
                return
            try:
                self._send_json(waveform_summary(track["_path"]))
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                self._send_json({"error": f"Could not analyze waveform: {error}"}, status=500)
            return
        if route.startswith("/api/lyrics/"):
            track_id = route.removeprefix("/api/lyrics/")
            track = self._track(track_id)
            if not track:
                self.send_error(404, "Track not found")
                return
            self._send_json(LYRICS_MANAGER.get(track_id, track["_path"]))
            return
        if route.startswith("/api/mixes/"):
            job_id = route.removeprefix("/api/mixes/")
            job = MIX_MANAGER.get(job_id)
            if not job:
                self.send_error(404, "Mix job not found")
                return
            self._send_json(job)
            return
        if route.startswith("/mixes/"):
            job_id = route.removeprefix("/mixes/")
            output = MIX_MANAGER.output(job_id)
            if not output:
                self.send_error(404, "Prepared mix not found")
                return
            self._serve_media(output)
            return
        if route.startswith("/media/"):
            track_id = route.removeprefix("/media/")
            track = self._track(track_id)
            if not track:
                self.send_error(404, "Track not found")
                return
            try:
                self._serve_media(browser_media_path(track["_path"]))
            except (OSError, subprocess.CalledProcessError) as error:
                self.send_error(500, f"Could not prepare browser audio: {error}")
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/prefetch":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                ids = [str(value) for value in payload.get("trackIds", [])][:3]
                candidates = [self.catalog_by_id[track_id] for track_id in ids if track_id in self.catalog_by_id]
                for track in candidates:
                    LYRICS_MANAGER.create(
                        track["id"],
                        track["_path"],
                        "base",
                        track,
                        provider_only=True,
                    )
                self._send_json(PREFETCH_MANAGER.create(candidates), status=202)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                self._send_json({"error": str(error)}, status=400)
            return
        if route.startswith("/api/lyrics/"):
            track_id = route.removeprefix("/api/lyrics/")
            track = self._track(track_id)
            if not track:
                self.send_error(404, "Track not found")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                model = str(payload.get("wordModel", "base"))
                if model not in {"tiny", "base", "small", "medium"}:
                    raise ValueError("Unsupported word model")
                result = LYRICS_MANAGER.create(track_id, track["_path"], model, track)
                self._send_json(result, status=200 if result["status"] == "ready" else 202)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                self._send_json({"error": str(error)}, status=400)
            return
        if route != "/api/mixes":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            left = self.catalog_by_id[str(payload["fromId"])]
            right = self.catalog_by_id[str(payload["toId"])]
            if left["id"] == right["id"]:
                raise ValueError("Choose a different next track")
            bars = int(payload.get("bars", 32))
            if bars not in {8, 16, 32}:
                raise ValueError("Transition bars must be 8, 16, or 32")
            technique = str(payload.get("technique", "stem_phrase"))
            if technique not in {"auto", "stem_phrase"}:
                raise ValueError("The UI supports auto or stem_phrase mixing")
            target_bpm = float(payload.get("targetBpm") or left.get("bpm") or 0)
            if target_bpm <= 0:
                raise ValueError("The current track must be BPM-analyzed first")
            options = {
                "fromId": left["id"],
                "toId": right["id"],
                "bars": bars,
                "technique": technique,
                "wordModel": str(payload.get("wordModel", "base")),
                "targetBpm": target_bpm,
                "preRoll": 8.0,
                "trackMetadata": [
                    {
                        "id": item["id"],
                        "title": item["title"],
                        "artist": item["artist"],
                        "album": item.get("album"),
                        "length": item.get("length"),
                    }
                    for item in (left, right)
                ],
            }
            job = MIX_MANAGER.create(left["_path"], right["_path"], options)
            self._send_json(job, status=202 if job["status"] != "ready" else 200)
        except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as error:
            self._send_json({"error": str(error) or "Track not found"}, status=400)

    def _send_json(self, payload: dict, *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route.startswith("/mixes/"):
            job_id = route.removeprefix("/mixes/")
            output = MIX_MANAGER.output(job_id)
            if not output:
                self.send_error(404, "Prepared mix not found")
                return
            self._serve_media(output, head_only=True)
            return
        if route.startswith("/media/"):
            track_id = route.removeprefix("/media/")
            track = self._track(track_id)
            if not track:
                self.send_error(404, "Track not found")
                return
            try:
                self._serve_media(browser_media_path(track["_path"]), head_only=True)
            except (OSError, subprocess.CalledProcessError) as error:
                self.send_error(500, f"Could not prepare browser audio: {error}")
            return
        super().do_HEAD()

    def _serve_media(self, path: Path, *, head_only: bool = False) -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                self.send_error(416, "Invalid byte range")
                return
            if match.group(1):
                start = int(match.group(1))
            if match.group(2):
                end = min(int(match.group(2)), size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return

        content_length = end - start + 1
        self.send_response(206 if range_header else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(content_length))
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = content_length
            while remaining:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


def create_server(music_dir: Path, port: int = 4173) -> ThreadingHTTPServer:
    """Create a local SetMix HTTP server for the browser or desktop shell."""
    music_dir = music_dir.expanduser().resolve()
    if not music_dir.is_dir():
        raise ValueError(f"Music folder does not exist: {music_dir}")

    SetMixHandler.music_dir = music_dir
    SetMixHandler.catalog = build_catalog(music_dir)
    SetMixHandler.catalog_by_id = {track["id"]: track for track in SetMixHandler.catalog}
    return ThreadingHTTPServer(("127.0.0.1", port), SetMixHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve SetMix with a local music catalog")
    parser.add_argument("--music-dir", type=Path, default=default_music_dir())
    parser.add_argument("--port", type=int, default=4173)
    args = parser.parse_args()
    try:
        server = create_server(args.music_dir, args.port)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    music_dir = SetMixHandler.music_dir
    print(f"SetMix: {len(SetMixHandler.catalog)} tracks from {music_dir}")
    print(f"Open http://localhost:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
