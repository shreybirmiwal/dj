from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analysis import analyze_track, discover_tracks
from .engine import (
    STEM_TECHNIQUES,
    analyze_ordered,
    create_plan,
    render_mix,
    render_transition_auditions,
    stream_mix,
    stream_ordered_live,
    validate_render,
)
from .intelligence import analyze_intelligence
from .mixxx import build_mixxx_command, launch_mixxx, write_mixxx_playlist
from .preprocess import PREPARATION_LEVELS, prepare_library
from .stems import analyze_vocals


TECHNIQUES = (
    "auto",
    "varied",
    "stem_phrase",
    "loop_bridge",
    "bass_swap",
    "filter_sweep",
    "highpass_out",
    "lowpass_reveal",
    "echo_out",
    "loop_filter",
    "reverb_tail",
    "drop_cut",
)


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setmix",
        description="Analyze and beat-mix songs in the exact order provided.",
    )
    parser.add_argument("--bars", type=int, default=16, help="transition length in bars")
    parser.add_argument("--workers", type=int, default=3, help="parallel analysis workers")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="print cached track analysis")
    analyze.add_argument("tracks", nargs="+")
    analyze.add_argument("--force", action="store_true")

    prepare = sub.add_parser(
        "prepare",
        help="precompute reusable track knowledge for fast future mixes",
    )
    prepare.add_argument("tracks", nargs="+")
    prepare.add_argument("--level", choices=PREPARATION_LEVELS, default="smart")
    prepare.add_argument("--word-model", default="base")
    prepare.add_argument("--force", action="store_true")

    plan = sub.add_parser("plan", help="compile an ordered playlist to JSON")
    plan.add_argument("tracks", nargs="+")
    plan.add_argument("-o", "--output", required=True)
    plan.add_argument("--vocals", action="store_true", help="use neural vocal maps when planning")
    plan.add_argument("--smart", action="store_true", help="rank section- and word-aware transition candidates")
    plan.add_argument("--word-model", default="base", help="faster-whisper model used by --smart")
    plan.add_argument("--technique", choices=TECHNIQUES, default="auto")

    render = sub.add_parser("render", help="render an ordered playlist to WAV or FLAC")
    render.add_argument("tracks", nargs="+")
    render.add_argument("-o", "--output", required=True)
    render.add_argument("--previews", help="directory for transition MP3 previews")
    render.add_argument("--vocals", action="store_true", help="use neural vocal maps when planning")
    render.add_argument("--smart", action="store_true", help="rank section- and word-aware transition candidates")
    render.add_argument("--word-model", default="base", help="faster-whisper model used by --smart")
    render.add_argument("--technique", choices=TECHNIQUES, default="auto")

    stream = sub.add_parser("stream", help="play the generated mix through ffplay")
    stream.add_argument("tracks", nargs="+")
    stream.add_argument("--seconds", type=float, help="stop after this many seconds")
    stream.add_argument("--transition", type=int, help="start eight seconds before transition N")
    stream.add_argument("--vocals", action="store_true", help="use neural vocal maps when planning")
    stream.add_argument("--smart", action="store_true", help="rank section- and word-aware transition candidates")
    stream.add_argument("--word-model", default="base", help="faster-whisper model used by --smart")
    stream.add_argument("--technique", choices=TECHNIQUES, default="auto")

    live = sub.add_parser("live", help="play now and prepare future tracks asynchronously")
    live.add_argument("tracks", nargs="+")
    live.add_argument("--seconds", type=float, help="stop after this many seconds")
    live.add_argument(
        "--technique",
        choices=tuple(value for value in TECHNIQUES if value not in {"auto", *STEM_TECHNIQUES}),
        default="bass_swap",
    )
    live.add_argument(
        "--near-transition",
        action="store_true",
        help="test by starting eight seconds before the first transition",
    )

    stems = sub.add_parser("stems", help="separate vocals and print vocal-activity timelines")
    stems.add_argument("tracks", nargs="+")
    stems.add_argument("--force", action="store_true")

    intelligent = sub.add_parser(
        "intelligence",
        help="detect sections and generate word-level vocal timelines",
    )
    intelligent.add_argument("tracks", nargs="+")
    intelligent.add_argument("--force", action="store_true")
    intelligent.add_argument("--word-model", default="base")
    intelligent.add_argument("--no-words", action="store_true", help="only run section detection")

    audition = sub.add_parser("audition", help="render every effect as a short transition preview")
    audition.add_argument("tracks", nargs="+")
    audition.add_argument("-o", "--output", required=True)
    audition.add_argument("--vocals", action="store_true", help="use neural vocal maps when planning")
    audition.add_argument("--smart", action="store_true", help="rank section- and word-aware transition candidates")
    audition.add_argument("--word-model", default="base", help="faster-whisper model used by --smart")
    audition.add_argument("--technique", choices=TECHNIQUES, default="auto")
    audition.add_argument(
        "--format",
        choices=("mp3", "flac"),
        default="mp3",
        help="preview codec; FLAC avoids another lossy generation",
    )
    audition.add_argument(
        "--selected-only",
        action="store_true",
        help="render only the technique selected for each transition",
    )

    validate = sub.add_parser("validate", help="measure a rendered mix and its transitions")
    validate.add_argument("audio")
    validate.add_argument("--plan")

    mixxx = sub.add_parser(
        "mixxx",
        help="open up to four tracks in Mixxx or export a Mixxx playlist",
    )
    mixxx.add_argument("tracks", nargs="+")
    mixxx.add_argument("--mixxx-path", help="path to the Mixxx executable")
    mixxx.add_argument("--settings-path", help="isolated Mixxx settings directory")
    mixxx.add_argument("--resource-path", help="Mixxx resources or fork checkout")
    mixxx.add_argument("--start-autodj", action="store_true")
    mixxx.add_argument("--developer", action="store_true")
    mixxx.add_argument("--playlist-output", help="also write the ordered set as M3U8")
    mixxx.add_argument("--dry-run", action="store_true", help="print the launch command only")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            report = validate_render(args.audio, args.plan)
            print(json.dumps(report, indent=2))
            return 0 if report["passed"] else 1

        tracks = discover_tracks(args.tracks)
        if args.command == "mixxx":
            playlist = None
            if args.playlist_output:
                playlist = write_mixxx_playlist(tracks, args.playlist_output)
                _progress(f"Wrote {playlist}")
            if len(tracks) > 4:
                if playlist:
                    print(json.dumps({"playlist": str(playlist), "launched": False}, indent=2))
                    return 0
                raise ValueError(
                    "Mixxx can load at most four command-line tracks; add "
                    "--playlist-output for a longer ordered set."
                )
            command = build_mixxx_command(
                tracks,
                executable=args.mixxx_path,
                settings_path=args.settings_path,
                resource_path=args.resource_path,
                start_autodj=args.start_autodj,
                developer=args.developer,
            )
            result: dict[str, object] = {"command": command, "playlist": str(playlist) if playlist else None}
            if args.dry_run:
                result["launched"] = False
            else:
                result.update({"launched": True, "pid": launch_mixxx(command)})
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "prepare":
            report = prepare_library(
                tracks,
                level=args.level,
                transition_bars=args.bars,
                workers=args.workers,
                word_model=args.word_model,
                force=args.force,
                progress=_progress,
            )
            print(json.dumps(report, indent=2))
            return 0 if not report["failures"] else 2
        if args.command == "stems":
            values = [analyze_vocals(path, force=args.force) for path in tracks]
            print(json.dumps([value.__dict__ for value in values], indent=2))
            return 0
        if args.command == "intelligence":
            analyses = analyze_ordered(
                tracks,
                transition_bars=args.bars,
                workers=args.workers,
                progress=_progress,
            )
            _progress("Generating neural vocal-activity maps")
            vocal_maps = {analysis.path: analyze_vocals(analysis.path, force=args.force) for analysis in analyses}
            values = [
                analyze_intelligence(
                    analysis,
                    vocal_maps[analysis.path],
                    word_model=args.word_model,
                    transcribe=not args.no_words,
                    force=args.force,
                )
                for analysis in analyses
            ]
            print(json.dumps([value.to_dict() for value in values], indent=2))
            return 0
        if args.command == "live":
            stream_ordered_live(
                tracks,
                transition_bars=args.bars,
                workers=args.workers,
                max_seconds=args.seconds,
                start_near_first_transition=args.near_transition,
                technique=args.technique,
                progress=_progress,
            )
            return 0

        if args.command == "analyze":
            values = [
                analyze_track(path, transition_bars=args.bars, force=args.force)
                for path in tracks
            ]
            print(json.dumps([value.__dict__ for value in values], indent=2))
            return 0

        analyses = analyze_ordered(
            tracks,
            transition_bars=args.bars,
            workers=args.workers,
            progress=_progress,
        )
        vocal_maps = None
        smart = getattr(args, "smart", False)
        needs_vocals = (
            getattr(args, "vocals", False)
            or getattr(args, "technique", "") in STEM_TECHNIQUES
            or smart
        )
        if needs_vocals:
            _progress("Generating neural vocal-activity maps")
            vocal_maps = {str(path): analyze_vocals(path) for path in tracks}
        intelligence = None
        if smart:
            assert vocal_maps is not None
            _progress("Detecting sections and transcribing word-level vocals")
            intelligence = {
                analysis.path: analyze_intelligence(
                    analysis,
                    vocal_maps[analysis.path],
                    word_model=args.word_model,
                )
                for analysis in analyses
            }
        mix_plan = create_plan(
            analyses,
            vocal_maps=vocal_maps,
            intelligence=intelligence,
            technique=getattr(args, "technique", "auto"),
        )
        if args.command == "audition":
            outputs = render_transition_auditions(
                mix_plan,
                args.output,
                selected_only=args.selected_only,
                output_format=args.format,
            )
            for output in outputs:
                _progress(f"Wrote {output}")
        elif args.command == "plan":
            destination = Path(args.output).expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(mix_plan.to_dict(), indent=2) + "\n")
            _progress(f"Wrote {destination}")
        elif args.command == "render":
            render_mix(
                mix_plan,
                args.output,
                preview_dir=args.previews,
                progress=_progress,
            )
        elif args.command == "stream":
            start = 0.0
            if args.transition is not None:
                if args.transition < 1 or args.transition > len(mix_plan.transitions):
                    raise ValueError(f"Transition must be between 1 and {len(mix_plan.transitions)}")
                start = max(0.0, mix_plan.transitions[args.transition - 1].set_time - 8.0)
            stream_mix(mix_plan, start_seconds=start, max_seconds=args.seconds)
        return 0
    except (ValueError, RuntimeError, OSError) as error:
        print(f"setmix: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
