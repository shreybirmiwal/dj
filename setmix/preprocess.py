from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .analysis import TrackAnalysis, analyze_track
from .intelligence import analyze_intelligence
from .stems import analyze_vocals, separate_stems


PREPARATION_LEVELS = ("basic", "smart", "full")


def _write_index(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


def prepare_library(
    paths: Iterable[Path],
    *,
    level: str = "smart",
    transition_bars: int = 16,
    workers: int = 3,
    word_model: str = "base",
    force: bool = False,
    progress: Callable[[str], None] | None = None,
    cache_root: str | Path = ".setmix-cache",
) -> dict:
    """Precompute reusable track knowledge without choosing a playlist.

    Basic creates beat/downbeat/phrase, key, energy, and local-tempo data.
    Smart adds vocal activity, sections, and word timestamps. Full additionally
    stores four Demucs stems so a later transition can render immediately.
    Every stage uses content-aware caches, making interrupted batches resumable.
    """
    if level not in PREPARATION_LEVELS:
        raise ValueError(f"Preparation level must be one of: {', '.join(PREPARATION_LEVELS)}")
    sources = [Path(path).expanduser().resolve() for path in paths]
    root = Path(cache_root).expanduser().resolve()
    index_path = root / "library-index.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text())
        except (json.JSONDecodeError, OSError):
            index = {"version": 1, "tracks": {}}
    else:
        index = {"version": 1, "tracks": {}}
    index.setdefault("tracks", {})

    analyses: dict[Path, TrackAnalysis] = {}
    failures: list[dict[str, str]] = []
    if progress:
        progress(f"Preparing {len(sources)} tracks at {level} level")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(
                analyze_track,
                source,
                transition_bars=transition_bars,
                cache_dir=root / "analysis",
                force=force,
            ): source
            for source in sources
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                analyses[source] = future.result()
                if progress:
                    progress(f"basic  {source.name}")
            except Exception as error:  # keep a large batch resumable
                failures.append({"path": str(source), "error": str(error)})
                if progress:
                    progress(f"failed {source.name}: {error}")

    completed: list[dict] = []
    for source in sources:
        analysis = analyses.get(source)
        if analysis is None:
            continue
        record: dict = {
            "path": str(source),
            "level": "basic",
            "analysis": asdict(analysis),
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            if level in {"smart", "full"}:
                vocals = analyze_vocals(source, cache_dir=root / "stems", force=force)
                intelligence = analyze_intelligence(
                    analysis,
                    vocals,
                    word_model=word_model,
                    force=force,
                    cache_dir=root / "intelligence",
                    stem_cache_dir=root / "stems",
                )
                record["vocal_map"] = asdict(vocals)
                record["intelligence"] = intelligence.to_dict()
                record["level"] = "smart"
                if progress:
                    progress(f"smart  {source.name}")
            if level == "full":
                stems = separate_stems(source, cache_dir=root / "stems4")
                record["stems"] = {name: str(path) for name, path in stems.items()}
                record["level"] = "full"
                if progress:
                    progress(f"full   {source.name}")
        except Exception as error:  # preserve earlier successful stages
            record["error"] = str(error)
            failures.append({"path": str(source), "error": str(error)})
            if progress:
                progress(f"failed {source.name}: {error}")
        index["tracks"][str(source)] = record
        completed.append(record)
        _write_index(index_path, index)

    return {
        "level": level,
        "requested": len(sources),
        "completed": len(completed),
        "failures": failures,
        "index": str(index_path),
        "tracks": completed,
    }
