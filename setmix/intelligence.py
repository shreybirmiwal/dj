from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import librosa
import numpy as np

from .analysis import TrackAnalysis
from .stems import VocalMap, separate_vocals


@dataclass(frozen=True)
class SectionSpan:
    start: float
    end: float
    label: str
    energy: float
    brightness: float
    percussion: float
    vocal_fraction: float
    confidence: float


@dataclass(frozen=True)
class WordTimestamp:
    word: str
    start: float
    end: float
    probability: float


@dataclass(frozen=True)
class LyricPhrase:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class VocalTranscript:
    path: str
    model: str
    language: str
    words: list[WordTimestamp]
    phrases: list[LyricPhrase]
    confidence: float


@dataclass(frozen=True)
class TrackIntelligence:
    path: str
    sections: list[SectionSpan]
    transcript: VocalTranscript | None

    def section_at(self, second: float) -> SectionSpan | None:
        return next(
            (section for section in self.sections if section.start <= second < section.end),
            self.sections[-1] if self.sections else None,
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TransitionCandidate:
    from_cue: float
    to_cue: float
    duration: float
    bars: int
    technique: str
    score: float
    score_breakdown: dict[str, float]
    reasons: list[str]
    from_section: str
    to_section: str
    vocal_overlap: float
    outgoing_word_boundary: float
    incoming_word_boundary: float
    drop_position: float | None = None
    events: dict[str, float] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _cache_key(path: Path, suffix: str) -> str:
    stat = path.stat()
    value = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{suffix}:v1"
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _phrase_boundaries(track: TrackAnalysis) -> list[float]:
    beats = np.asarray(track.beat_times, dtype=np.float64)
    starts = [float(beats[index]) for index in range(track.phrase_offset, len(beats), 32)]
    starts = [value for value in starts if value < track.active_end - 2.0]
    boundaries = sorted({round(max(0.0, value), 6) for value in starts} | {0.0, track.active_end})
    return boundaries if len(boundaries) >= 2 else [0.0, track.active_end]


def _section_label(
    position: float,
    energy: float,
    vocal_fraction: float,
    previous_energy: float | None,
) -> str:
    if position < 0.16 and vocal_fraction < 0.22:
        return "intro"
    if position > 0.80 and vocal_fraction < 0.28:
        return "outro"
    if previous_energy is not None and energy - previous_energy > 0.32 and energy > 0.66:
        return "drop"
    if energy < 0.30:
        return "breakdown"
    if vocal_fraction < 0.20:
        return "instrumental"
    if energy > 0.60 and vocal_fraction > 0.38:
        return "chorus"
    return "verse"


def _merge_sections(sections: list[SectionSpan]) -> list[SectionSpan]:
    merged: list[SectionSpan] = []
    for section in sections:
        if not merged or merged[-1].label != section.label:
            merged.append(section)
            continue
        previous = merged[-1]
        left_duration = previous.end - previous.start
        right_duration = section.end - section.start
        total = max(1e-9, left_duration + right_duration)

        def average(left: float, right: float) -> float:
            return (left * left_duration + right * right_duration) / total

        merged[-1] = SectionSpan(
            start=previous.start,
            end=section.end,
            label=previous.label,
            energy=round(average(previous.energy, section.energy), 4),
            brightness=round(average(previous.brightness, section.brightness), 4),
            percussion=round(average(previous.percussion, section.percussion), 4),
            vocal_fraction=round(average(previous.vocal_fraction, section.vocal_fraction), 4),
            confidence=round(average(previous.confidence, section.confidence), 4),
        )
    return merged


def analyze_sections(
    track: TrackAnalysis,
    vocals: VocalMap,
    *,
    cache_dir: str | Path = ".setmix-cache/intelligence",
    force: bool = False,
) -> list[SectionSpan]:
    """Detect musically useful sections on phrase-aligned boundaries."""
    source = Path(track.path)
    root = Path(cache_dir) / _cache_key(source, f"sections-{track.transition_bars}")
    manifest = root / "sections.json"
    if manifest.exists() and not force:
        return [SectionSpan(**item) for item in json.loads(manifest.read_text())]

    y, sr = librosa.load(source, sr=22050, mono=True)
    hop = 512
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, n_fft=2048, hop_length=hop)[0]
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    boundaries = _phrase_boundaries(track)
    raw: list[tuple[float, float, float, float, float]] = []
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        left = int(np.clip(librosa.time_to_frames(start, sr=sr, hop_length=hop), 0, len(rms) - 1))
        right = int(np.clip(librosa.time_to_frames(end, sr=sr, hop_length=hop), left + 1, len(rms)))
        raw.append(
            (
                start,
                end,
                float(np.mean(rms[left:right])),
                float(np.mean(centroid[left:right])),
                float(np.mean(onset[left:right])),
            )
        )

    def normalized(values: list[float]) -> np.ndarray:
        array = np.asarray(values, dtype=np.float64)
        low, high = np.percentile(array, (10, 90))
        return np.clip((array - low) / max(high - low, 1e-9), 0.0, 1.0)

    energy = normalized([item[2] for item in raw])
    brightness = normalized([item[3] for item in raw])
    percussion = normalized([item[4] for item in raw])
    sections: list[SectionSpan] = []
    previous_energy: float | None = None
    for index, (start, end, *_unused) in enumerate(raw):
        vocal_fraction = vocals.activity_fraction(start, end)
        position = ((start + end) / 2.0) / max(track.active_end, 1e-9)
        label = _section_label(position, float(energy[index]), vocal_fraction, previous_energy)
        distinctness = max(
            abs(float(energy[index]) - 0.30),
            abs(float(energy[index]) - 0.60),
            abs(vocal_fraction - 0.22),
        )
        sections.append(
            SectionSpan(
                start=round(start, 6),
                end=round(end, 6),
                label=label,
                energy=round(float(energy[index]), 4),
                brightness=round(float(brightness[index]), 4),
                percussion=round(float(percussion[index]), 4),
                vocal_fraction=round(vocal_fraction, 4),
                confidence=round(float(np.clip(0.58 + 0.25 * distinctness + 0.17 * track.beat_confidence, 0.0, 1.0)), 4),
            )
        )
        previous_energy = float(energy[index])
    result = _merge_sections(sections)
    root.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps([asdict(item) for item in result], indent=2) + "\n")
    return result


def transcribe_vocals(
    path: str | Path,
    *,
    model_name: str = "base",
    cache_dir: str | Path = ".setmix-cache/intelligence",
    stem_cache_dir: str | Path = ".setmix-cache/stems",
    force: bool = False,
) -> VocalTranscript:
    """Transcribe a separated vocal stem with word-level timestamps."""
    source = Path(path).expanduser().resolve()
    root = Path(cache_dir) / _cache_key(source, f"words-{model_name}")
    manifest = root / "transcript.json"
    if manifest.exists() and not force:
        payload = json.loads(manifest.read_text())
        payload["words"] = [WordTimestamp(**item) for item in payload["words"]]
        payload["phrases"] = [LyricPhrase(**item) for item in payload["phrases"]]
        return VocalTranscript(**payload)
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise RuntimeError(
            "Word-level vocal timing requires requirements-intelligence.txt"
        ) from error

    vocals, _ = separate_vocals(source, cache_dir=stem_cache_dir)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    raw_segments, info = model.transcribe(
        str(vocals),
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    words: list[WordTimestamp] = []
    phrases: list[LyricPhrase] = []
    for segment in raw_segments:
        text = segment.text.strip()
        if text:
            phrases.append(LyricPhrase(round(segment.start, 3), round(segment.end, 3), text))
        for word in segment.words or []:
            token = word.word.strip()
            if token and word.start is not None and word.end is not None:
                words.append(
                    WordTimestamp(
                        word=token,
                        start=round(float(word.start), 3),
                        end=round(float(word.end), 3),
                        probability=round(float(word.probability or 0.0), 4),
                    )
                )
    confidence = float(np.mean([word.probability for word in words])) if words else 0.0
    result = VocalTranscript(
        path=str(source),
        model=f"faster-whisper/{model_name}",
        language=info.language or "unknown",
        words=words,
        phrases=phrases,
        confidence=round(confidence, 4),
    )
    root.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(asdict(result), indent=2) + "\n")
    return result


def analyze_intelligence(
    track: TrackAnalysis,
    vocals: VocalMap,
    *,
    word_model: str = "base",
    transcribe: bool = True,
    force: bool = False,
    cache_dir: str | Path = ".setmix-cache/intelligence",
    stem_cache_dir: str | Path = ".setmix-cache/stems",
) -> TrackIntelligence:
    transcript = (
        transcribe_vocals(
            track.path,
            model_name=word_model,
            cache_dir=cache_dir,
            stem_cache_dir=stem_cache_dir,
            force=force,
        )
        if transcribe
        else None
    )
    return TrackIntelligence(
        path=track.path,
        sections=analyze_sections(track, vocals, cache_dir=cache_dir, force=force),
        transcript=transcript,
    )


def _candidate_indices(
    track: TrackAnalysis,
    *,
    incoming: bool,
    minimum_second: float = 0.0,
) -> list[int]:
    required = track.transition_bars * 4
    aligned = range(track.phrase_offset, len(track.beat_times), 32)
    usable = [
        index
        for index in aligned
        if track.beat_times[index] >= minimum_second
        and index + required < len(track.beat_times)
        and track.beat_times[index + required] <= track.active_end + 0.2
    ]
    # Incoming songs should still feel like songs rather than arbitrary excerpts.
    # Four phrase choices are enough to find an intro/verse/chorus without silently
    # discarding half the track for a marginally cleaner lyric boundary.
    return usable[:4] if incoming else usable[-8:]


def _vocal_overlap(
    left: VocalMap,
    right: VocalMap,
    left_start: float,
    right_start: float,
    left_duration: float,
    right_duration: float,
) -> float:
    points = np.linspace(0.0, 1.0, 128)
    left_active = np.asarray([left.active_at(left_start + value * left_duration) for value in points])
    right_active = np.asarray([right.active_at(right_start + value * right_duration) for value in points])
    collision = float(np.mean(left_active & right_active))
    # Penalize a backwards handoff even if stem gains would technically hide it:
    # the incoming singer should be quiet early and the outgoing singer quiet late.
    wrong_way = 0.5 * float(np.mean(right_active[points < 0.48]))
    wrong_way += 0.5 * float(np.mean(left_active[points > 0.42]))
    return float(np.clip(0.7 * collision + 0.3 * wrong_way, 0.0, 1.0))


def _word_boundary_score(
    transcript: VocalTranscript | None,
    second: float,
    *,
    outgoing: bool,
) -> tuple[float, float]:
    if transcript is None or not transcript.words:
        return 0.5, math.inf
    for word in transcript.words:
        if word.start < second < word.end:
            return 0.0, 0.0
    if outgoing:
        eligible = [word for word in transcript.words if word.end <= second]
        if not eligible:
            return 0.2, math.inf
        nearest = eligible[-1]
        distance = second - nearest.end
        punctuation = nearest.word.endswith((".", "!", "?", ",", ";", ":"))
    else:
        eligible = [word for word in transcript.words if word.start >= second]
        if not eligible:
            return 0.2, math.inf
        nearest = eligible[0]
        distance = nearest.start - second
        punctuation = False
    score = math.exp(-distance / (1.5 if outgoing else 2.0))
    if punctuation:
        score = min(1.0, score + 0.18)
    return float(score), float(distance)


def _event_schedule(
    left: TrackIntelligence,
    right: TrackIntelligence,
    left_vocals: VocalMap,
    right_vocals: VocalMap,
    left_start: float,
    right_start: float,
    left_duration: float,
    right_duration: float,
    drop_position: float | None,
) -> dict[str, float]:
    """Place mix events on detected words, quiet pockets, and section changes."""

    def normalized(second: float, start: float, duration: float) -> float:
        return float(np.clip((second - start) / max(duration, 1e-9), 0.0, 1.0))

    exit_candidates: list[tuple[float, float]] = []
    if left.transcript:
        for phrase in left.transcript.phrases:
            position = normalized(phrase.end, left_start, left_duration)
            if 0.14 <= position <= 0.52:
                quiet = 1.0 - left_vocals.activity_fraction(
                    phrase.end,
                    min(left_start + left_duration, phrase.end + 2.5),
                )
                exit_candidates.append((0.65 * quiet + 0.35 * math.exp(-abs(position - 0.34) / 0.18), position))
    for _start, end in left_vocals.segments:
        position = normalized(end, left_start, left_duration)
        if 0.14 <= position <= 0.52:
            quiet = 1.0 - left_vocals.activity_fraction(end, min(left_start + left_duration, end + 2.5))
            exit_candidates.append((0.75 * quiet + 0.25 * math.exp(-abs(position - 0.34) / 0.18), position))
    vocal_exit = max(exit_candidates)[1] if exit_candidates else 0.36

    entry_candidates: list[tuple[float, float]] = []
    if right.transcript:
        for phrase in right.transcript.phrases:
            position = normalized(phrase.start, right_start, right_duration)
            if 0.50 <= position <= 0.90:
                quiet = 1.0 - right_vocals.activity_fraction(
                    max(right_start, phrase.start - 2.5),
                    phrase.start,
                )
                entry_candidates.append((0.65 * quiet + 0.35 * math.exp(-abs(position - 0.70) / 0.18), position))
    for start, _end in right_vocals.segments:
        position = normalized(start, right_start, right_duration)
        if 0.50 <= position <= 0.90:
            quiet = 1.0 - right_vocals.activity_fraction(max(right_start, start - 2.5), start)
            entry_candidates.append((0.75 * quiet + 0.25 * math.exp(-abs(position - 0.70) / 0.18), position))
    vocal_entry = max(entry_candidates)[1] if entry_candidates else 0.68

    boundary_candidates: list[tuple[float, float]] = []
    previous = right.section_at(right_start)
    for section in right.sections:
        position = normalized(section.start, right_start, right_duration)
        if not 0.30 <= position <= 0.72:
            continue
        rise = section.energy - (previous.energy if previous else section.energy)
        importance = 0.55 * max(0.0, rise) + 0.30 * section.percussion + 0.15 * section.confidence
        boundary_candidates.append((importance, position))
        previous = section
    bass_handoff = (
        drop_position
        if drop_position is not None
        else (max(boundary_candidates)[1] if boundary_candidates else 0.50)
    )
    bass_handoff = float(np.clip(bass_handoff, 0.38, 0.70))

    vocal_exit = float(np.clip(vocal_exit, 0.14, min(0.52, bass_handoff - 0.02)))
    vocal_entry = float(
        np.clip(vocal_entry, max(0.54, bass_handoff + 0.08, vocal_exit + 0.18), 0.90)
    )
    melody_handoff = float(np.clip(bass_handoff + 0.04, vocal_exit + 0.06, vocal_entry - 0.06))
    drum_handoff = float(np.clip(bass_handoff - 0.10, 0.25, 0.60))
    return {
        "outgoing_vocal_exit": round(vocal_exit, 6),
        "drum_handoff": round(drum_handoff, 6),
        "bass_handoff": round(bass_handoff, 6),
        "melody_handoff": round(melody_handoff, 6),
        "incoming_vocal_entry": round(vocal_entry, 6),
    }


def camelot_compatibility(left: str, right: str) -> float:
    try:
        left_number, left_mode = int(left[:-1]), left[-1]
        right_number, right_mode = int(right[:-1]), right[-1]
    except (ValueError, IndexError):
        return 0.5
    wheel_distance = min((left_number - right_number) % 12, (right_number - left_number) % 12)
    if left_number == right_number and left_mode == right_mode:
        return 1.0
    if left_number == right_number:
        return 0.95
    if wheel_distance == 1 and left_mode == right_mode:
        return 0.9
    return 0.35


def _section_score(left: SectionSpan | None, right: SectionSpan | None) -> float:
    if left is None or right is None:
        return 0.5
    preferred = {
        ("outro", "intro"),
        ("outro", "verse"),
        ("chorus", "intro"),
        ("chorus", "breakdown"),
        ("instrumental", "intro"),
        ("breakdown", "drop"),
    }
    if (left.label, right.label) in preferred:
        return 1.0
    if left.label == "chorus" and right.label == "chorus":
        return 0.45
    if left.label == "verse" and right.label == "verse":
        return 0.55
    return 0.72


def _select_technique(
    requested: str,
    left: SectionSpan | None,
    right: SectionSpan | None,
    beat_score: float,
    key_score: float,
    overlap: float,
    drop_position: float | None,
) -> str:
    if requested not in {"auto", "varied"}:
        return requested
    # When both records are vocally busy, a repeated instrumental phrase acts
    # as a third, simpler deck and creates room for a clean singer handoff.
    if overlap >= 0.55 and left and right and left.vocal_fraction >= 0.25 and right.vocal_fraction >= 0.25:
        return "loop_bridge"
    # A known destination drop is a stronger musical landmark than global key
    # or beat confidence. Cutting there also avoids a long incompatible mash.
    if drop_position is not None:
        return "drop_cut"
    if beat_score < 0.58:
        return "echo_out"
    if key_score < 0.5:
        return "filter_sweep"
    if left and right and left.label == "breakdown" and right.label in {"drop", "chorus"}:
        return "lowpass_reveal"
    if left and right and left.vocal_fraction < 0.2 and right.vocal_fraction < 0.2:
        return "bass_swap"
    return "stem_phrase" if overlap > 0.0 or requested == "auto" else "bass_swap"


def _incoming_drop_position(
    intelligence: TrackIntelligence,
    start: float,
    duration: float,
) -> float | None:
    drops = [
        section
        for section in intelligence.sections
        if section.label == "drop"
        and section.confidence >= 0.65
        # A drop transition needs enough audible setup to feel intentional.
        # Earlier energy rises are treated as section changes and receive a
        # gradual handoff instead of an impact edit.
        and start + 0.32 * duration <= section.start <= start + 0.80 * duration
    ]
    if not drops:
        return None
    return float((drops[0].start - start) / duration)


def _early_energy_stability(
    intelligence: TrackIntelligence,
    start: float,
    duration: float,
) -> float:
    """Penalize surprise energy jumps before a gradual blend is established."""
    current = intelligence.section_at(start)
    if current is None:
        return 0.5
    previous_energy = current.energy
    largest_rise = 0.0
    for section in intelligence.sections:
        if section.start <= start:
            continue
        if section.start > start + 0.32 * duration:
            break
        largest_rise = max(largest_rise, section.energy - previous_energy)
        previous_energy = section.energy
    return float(np.clip(1.0 - largest_rise, 0.0, 1.0))


def _transition_energy_floor(
    left: TrackIntelligence,
    right: TrackIntelligence,
    left_start: float,
    right_start: float,
    left_duration: float,
    right_duration: float,
) -> float:
    """Estimate whether a long handoff has enough musical bed throughout.

    Matching two equally quiet breakdowns looks good to a simple energy-
    difference metric but produces exactly the dead center listeners dislike.
    Sample the staged handoff and score its quietest point instead.
    """
    values: list[float] = []
    for position in np.linspace(0.0, 1.0, 9):
        left_section = left.section_at(left_start + position * left_duration)
        right_section = right.section_at(right_start + position * right_duration)
        left_energy = left_section.energy if left_section else 0.5
        right_energy = right_section.energy if right_section else 0.5
        # Approximate the four-stem schedule: outgoing ownership early,
        # destination ownership late, and both instrumental beds in the middle.
        outgoing_weight = float(np.cos(position * math.pi / 2.0) ** 2)
        incoming_weight = float(np.sin(position * math.pi / 2.0) ** 2)
        values.append(outgoing_weight * left_energy + incoming_weight * right_energy)
    return float(np.clip(min(values), 0.0, 1.0))


def rank_transition_candidates(
    left: TrackAnalysis,
    right: TrackAnalysis,
    left_vocals: VocalMap,
    right_vocals: VocalMap,
    left_intelligence: TrackIntelligence,
    right_intelligence: TrackIntelligence,
    *,
    target_bpm: float,
    technique: str = "auto",
    limit: int = 8,
    minimum_from_cue: float = 0.0,
) -> list[TransitionCandidate]:
    """Generate phrase-aligned transition choices and return them best-first."""
    left_candidates = _candidate_indices(
        left,
        incoming=False,
        minimum_second=minimum_from_cue,
    )
    right_candidates = _candidate_indices(right, incoming=True)
    if not left_candidates or not right_candidates:
        return []
    duration = left.transition_bars * 4.0 * 60.0 / target_bpm
    left_source_duration = duration * target_bpm / left.bpm
    right_source_duration = duration * target_bpm / right.bpm
    key_score = camelot_compatibility(left.camelot_key, right.camelot_key)
    beat_score = (left.beat_confidence + right.beat_confidence) / 2.0
    tempo_score = max(0.0, 1.0 - abs(left.bpm - right.bpm) / max(left.bpm, right.bpm) / 0.08)
    candidates: list[TransitionCandidate] = []
    for left_rank, left_index in enumerate(left_candidates):
        left_start = left.beat_times[left_index]
        left_section = left_intelligence.section_at(left_start)
        for right_rank, right_index in enumerate(right_candidates):
            right_start = right.beat_times[right_index]
            right_section = right_intelligence.section_at(right_start)
            drop_position = _incoming_drop_position(
                right_intelligence,
                right_start,
                right_source_duration,
            )
            early_energy_stability = _early_energy_stability(
                right_intelligence,
                right_start,
                right_source_duration,
            )
            energy_floor = _transition_energy_floor(
                left_intelligence,
                right_intelligence,
                left_start,
                right_start,
                left_source_duration,
                right_source_duration,
            )
            overlap = _vocal_overlap(
                left_vocals,
                right_vocals,
                left_start,
                right_start,
                left_source_duration,
                right_source_duration,
            )
            events = _event_schedule(
                left_intelligence,
                right_intelligence,
                left_vocals,
                right_vocals,
                left_start,
                right_start,
                left_source_duration,
                right_source_duration,
                drop_position,
            )
            left_word, left_gap = _word_boundary_score(
                left_intelligence.transcript,
                left_start + events["outgoing_vocal_exit"] * left_source_duration,
                outgoing=True,
            )
            right_word, right_gap = _word_boundary_score(
                right_intelligence.transcript,
                right_start + events["incoming_vocal_entry"] * right_source_duration,
                outgoing=False,
            )
            section_score = _section_score(left_section, right_section)
            energy_score = 1.0 - abs(
                (left_section.energy if left_section else 0.5)
                - (right_section.energy if right_section else 0.5)
            )
            position_score = 0.5 * left_rank / max(1, len(left_candidates) - 1)
            position_score += 0.5 * (1.0 - right_rank / max(1, len(right_candidates) - 1))
            breakdown = {
                "vocal_safety": 1.0 - overlap,
                "word_boundaries": (left_word + right_word) / 2.0,
                "section_compatibility": section_score,
                "energy_continuity": energy_score,
                "beat_confidence": beat_score,
                "harmonic_compatibility": key_score,
                "tempo_compatibility": tempo_score,
                "playlist_position": position_score,
                "early_energy_stability": early_energy_stability,
                "transition_energy_floor": energy_floor,
                "drop_opportunity": (
                    math.exp(-abs(drop_position - 0.52) / 0.22)
                    if drop_position is not None and technique in {"auto", "varied", "drop_cut"}
                    else 0.0
                ),
            }
            if technique in {"stem_phrase", "loop_bridge"}:
                weights = {
                    "vocal_safety": 0.19,
                    "word_boundaries": 0.15,
                    "section_compatibility": 0.11,
                    "energy_continuity": 0.06,
                    "beat_confidence": 0.07,
                    "harmonic_compatibility": 0.05,
                    "tempo_compatibility": 0.03,
                    "playlist_position": 0.03,
                    "early_energy_stability": 0.07,
                    "transition_energy_floor": 0.24,
                    "drop_opportunity": 0.0,
                }
            else:
                weights = {
                    "vocal_safety": 0.14,
                    "word_boundaries": 0.12,
                    "section_compatibility": 0.11,
                    "energy_continuity": 0.07,
                    "beat_confidence": 0.07,
                    "harmonic_compatibility": 0.05,
                    "tempo_compatibility": 0.04,
                    "playlist_position": 0.05,
                    "early_energy_stability": 0.09,
                    "transition_energy_floor": 0.11,
                    "drop_opportunity": 0.15,
                }
            score = sum(breakdown[name] * weight for name, weight in weights.items())
            chosen = _select_technique(
                technique,
                left_section,
                right_section,
                beat_score,
                key_score,
                overlap,
                drop_position,
            )
            reasons = [
                f"{left_section.label if left_section else 'unknown'} to {right_section.label if right_section else 'unknown'}",
                f"{overlap:.1%} lead-vocal collision",
            ]
            if math.isfinite(left_gap):
                reasons.append(f"outgoing word ends {left_gap:.2f}s before its handoff")
            if math.isfinite(right_gap):
                reasons.append(f"incoming word begins {right_gap:.2f}s after its handoff")
            if drop_position is not None:
                reasons.append(f"incoming drop lands at {drop_position:.1%} of the transition")
            candidates.append(
                TransitionCandidate(
                    from_cue=round(left_start, 6),
                    to_cue=round(right_start, 6),
                    duration=round(duration, 6),
                    bars=left.transition_bars,
                    technique=chosen,
                    score=round(score, 6),
                    score_breakdown={name: round(value, 4) for name, value in breakdown.items()},
                    reasons=reasons,
                    from_section=left_section.label if left_section else "unknown",
                    to_section=right_section.label if right_section else "unknown",
                    vocal_overlap=round(overlap, 4),
                    outgoing_word_boundary=round(left_word, 4),
                    incoming_word_boundary=round(right_word, 4),
                    drop_position=round(drop_position, 6) if drop_position is not None else None,
                    events=events,
                )
            )
    return sorted(candidates, key=lambda item: item.score, reverse=True)[:limit]
