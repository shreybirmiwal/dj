from __future__ import annotations

from setmix.analysis import TrackAnalysis
from pathlib import Path

from setmix import stems as stem_module
from setmix.stems import VocalMap, choose_vocal_safe_cues


def _track(path: str) -> TrackAnalysis:
    beats = [index * 0.5 for index in range(256)]
    return TrackAnalysis(
        path=path,
        duration=128.0,
        bpm=120.0,
        beat_times=beats,
        downbeat_offset=0,
        cue_in=0.0,
        cue_out=96.0,
        active_end=127.0,
        rms_db=-12.0,
        beat_confidence=1.0,
        transition_bars=8,
    )


def test_vocal_safe_cues_avoid_simultaneous_leads() -> None:
    left = _track("left.wav")
    right = _track("right.wav")
    left_vocals = VocalMap("left.wav", "test", 0.25, [[0.0, 105.0]], 0.8, 1.0)
    right_vocals = VocalMap("right.wav", "test", 0.25, [[24.0, 110.0]], 0.7, 1.0)
    left_cue, right_cue, overlap = choose_vocal_safe_cues(left, right, left_vocals, right_vocals)
    assert left_cue >= 96.0
    assert right_cue < 24.0
    assert overlap < 0.1


def test_activity_fraction_uses_timeline() -> None:
    vocals = VocalMap("track.wav", "test", 0.25, [[2.0, 4.0]], 0.2, 1.0)
    assert vocals.activity_fraction(2.0, 4.0) > 0.95
    assert vocals.activity_fraction(5.0, 7.0) == 0.0


def test_vocal_separation_reuses_four_stem_pass(tmp_path, monkeypatch) -> None:
    source = tmp_path / "track.wav"
    source.write_bytes(b"audio")
    folder = tmp_path / "stems4" / "track"
    folder.mkdir(parents=True)
    separated = {}
    for name in ("vocals", "drums", "bass", "other"):
        separated[name] = folder / f"{name}.mp3"
        separated[name].write_bytes(name.encode())
    monkeypatch.setattr(stem_module, "separate_stems", lambda *args, **kwargs: separated)

    commands = []

    def fake_run(command, check):
        commands.append(command)
        Path(command[-1]).write_bytes(b"mix")

    monkeypatch.setattr(stem_module.subprocess, "run", fake_run)
    vocals, accompaniment = stem_module.separate_vocals(source, cache_dir=tmp_path / "stems")

    assert vocals == separated["vocals"]
    assert accompaniment.read_bytes() == b"mix"
    assert len(commands) == 1
    assert "--two-stems" not in commands[0]


def test_new_four_stem_pass_is_lossless_flac(tmp_path, monkeypatch) -> None:
    source = tmp_path / "track.wav"
    source.write_bytes(b"audio")
    commands = []

    def fake_run(command, check):
        commands.append(command)
        output = Path(command[command.index("-o") + 1]) / "htdemucs" / source.stem
        output.mkdir(parents=True)
        for name in ("vocals", "drums", "bass", "other"):
            (output / f"{name}.flac").write_bytes(name.encode())

    monkeypatch.setattr(stem_module.subprocess, "run", fake_run)
    result = stem_module.separate_stems(source, cache_dir=tmp_path / "stems4")

    assert all(path.suffix == ".flac" for path in result.values())
    assert "--flac" in commands[0]
    assert "--mp3" not in commands[0]
    assert commands[0][commands[0].index("--clip-mode") + 1] == "clamp"


def test_two_tracks_share_one_demucs_model_load(tmp_path, monkeypatch) -> None:
    sources = [tmp_path / "left.wav", tmp_path / "right.wav"]
    for source in sources:
        source.write_bytes(b"audio")
    commands = []

    def fake_run(command, check):
        commands.append(command)
        output_root = Path(command[command.index("-o") + 1]) / "htdemucs"
        for source in sources:
            output = output_root / source.stem
            output.mkdir(parents=True)
            for name in ("vocals", "drums", "bass", "other"):
                (output / f"{name}.flac").write_bytes(name.encode())

    monkeypatch.setattr(stem_module.subprocess, "run", fake_run)
    first = stem_module.separate_stems_batch(sources, cache_dir=tmp_path / "stems4")
    second = stem_module.separate_stems_batch(sources, cache_dir=tmp_path / "stems4")

    assert len(commands) == 1
    assert all(str(source) in commands[0] for source in sources)
    assert first == second
    assert all(all(path.exists() for path in result.values()) for result in first)
