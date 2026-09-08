#!/usr/bin/env python3
"""Serve the SetMix UI and expose a read-only local music catalog."""

from __future__ import annotations

import argparse
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
from urllib.parse import urlparse


AUDIO_SUFFIXES = {".aif", ".aiff", ".flac", ".m4a", ".mp3", ".wav"}
UI_DIR = Path(__file__).resolve().parent
PROJECT_DIR = UI_DIR.parent
MIX_CACHE_DIR = PROJECT_DIR / ".setmix-cache" / "ui-mixes"
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
    for path in (PROJECT_DIR / ".setmix-cache" / "analysis").glob("*.json"):
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
        duration = 0.0
        tagged_bpm: float | None = None
        if mutagen:
            try:
                audio = mutagen.File(path, easy=True)
                if audio:
                    title = _text_tag(audio.tags, "title") or title
                    artist = _text_tag(audio.tags, "artist", "albumartist") or artist
                    genre = _text_tag(audio.tags, "genre")
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
                "version": 2,
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
            from setmix.intelligence import analyze_intelligence
            from setmix.stems import analyze_vocals

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
                message="Separating vocal and instrumental stems",
                progress=28,
            )
            vocal_maps = {analysis.path: analyze_vocals(analysis.path) for analysis in analyses}
            self._update(
                job_id,
                stage="intelligence",
                message="Detecting sections and word-level vocal boundaries",
                progress=48,
            )
            intelligence = {
                analysis.path: analyze_intelligence(
                    analysis,
                    vocal_maps[analysis.path],
                    word_model=options["wordModel"],
                )
                for analysis in analyses
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


MIX_MANAGER = MixManager()


class SetMixHandler(SimpleHTTPRequestHandler):
    catalog: list[dict] = []
    music_dir: Path

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/catalog":
            self.__class__.catalog = build_catalog(self.music_dir)
            payload = {
                "source": str(self.music_dir),
                "count": len(self.catalog),
                "tracks": [{key: value for key, value in track.items() if key != "_path"} for track in self.catalog],
            }
            self._send_json(payload)
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
            track = next((item for item in self.catalog if item["id"] == track_id), None)
            if not track:
                self.send_error(404, "Track not found")
                return
            self._serve_media(track["_path"])
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route != "/api/mixes":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            left = next(item for item in self.catalog if item["id"] == str(payload["fromId"]))
            right = next(item for item in self.catalog if item["id"] == str(payload["toId"]))
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
            track = next((item for item in self.catalog if item["id"] == track_id), None)
            if not track:
                self.send_error(404, "Track not found")
                return
            self._serve_media(track["_path"], head_only=True)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve SetMix with a local music catalog")
    parser.add_argument("--music-dir", type=Path, default=default_music_dir())
    parser.add_argument("--port", type=int, default=4173)
    args = parser.parse_args()
    music_dir = args.music_dir.expanduser().resolve()
    if not music_dir.is_dir():
        raise SystemExit(f"Music folder does not exist: {music_dir}")

    SetMixHandler.music_dir = music_dir
    SetMixHandler.catalog = build_catalog(music_dir)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), SetMixHandler)
    print(f"SetMix: {len(SetMixHandler.catalog)} tracks from {music_dir}")
    print(f"Open http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
