# SetMix prototype

Before changing planner or transition behavior, read
[`docs/HUMAN_FEEDBACK.md`](docs/HUMAN_FEEDBACK.md). It records listener
preferences, failed and approved references, open acceptance criteria, and the
musical rationale behind current tunables.

SetMix analyzes songs, keeps their supplied order, estimates BPM and musical
key/Camelot position, aligns beats and eight-bar phrases, separates four stems,
and renders or plays staged drum, bass, melodic, and vocal handoffs.

It does not choose songs. A text file or the command-line argument order is the
playlist order.

## Browser UI

The interactive catalog and continuous-mix interface lives in `ui/`. It reads
the local music folder (checking `~/Documents/DJ Music` and then
`~/Desktop/dj-music`) and uses cached SetMix analysis where available. Run it:

```sh
.venv/bin/python ui/server.py
```

Then open `http://localhost:4173`. The prototype lets you search and filter the
catalog, accept a suggested next track, choose any other track, and see every
preparation stage. Press the play button on the current track to start immediately.
In the background, the server runs the same 32-bar smart `stem_phrase` pipeline as
the CLI: track analysis, Demucs vocal/stem separation, faster-whisper word timing,
section-aware candidate ranking, tempo/phase alignment, and the final render. The
browser joins that prepared handoff before its selected phrase and keeps playing
through the next track while the following transition prepares.

The first uncached pair can take several minutes and may download neural models.
Analysis, stems, transcripts, stretched tracks, and finished pair handoffs are all
cached, so repeated pairs become much faster. Override the library location with
`--music-dir /path/to/music` or the `SETMIX_MUSIC_DIR` environment variable.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

FFmpeg and ffplay must be installed and available on `PATH`.

For neural vocal/accompaniment separation:

```sh
.venv/bin/pip install -r requirements-stems.txt
```

For section detection and word-level lyric timestamps:

```sh
.venv/bin/pip install -r requirements-intelligence.txt
```

## Smart transition planning

`--smart` detects phrase-aligned song sections, transcribes the separated vocal
stem with word timestamps, generates multiple cue pairs, and ranks them before
rendering. The first transcription downloads a faster-whisper model; track
sections and transcripts are cached after that.

```sh
scripts/setmix --bars 32 plan first.flac second.flac \
  --smart --technique stem_phrase -o output/plan.json

scripts/setmix --bars 32 audition first.flac second.flac \
  --smart --technique stem_phrase --selected-only -o output/smart-preview
```

Run the intelligence layer without planning or rendering:

```sh
scripts/setmix intelligence first.flac second.flac --word-model base
```

The plan JSON is also the UI contract. `track_intelligence` contains sections,
word timestamps, lyric phrases, language, and confidence; `ranked_candidates`
contains alternate cue pairs with component scores and human-readable reasons;
each selected transition repeats its score, section handoff, and exact cues.

## Try the included three-track demo

```sh
scripts/setmix render examples/house-demo.txt \
  -o output/house-demo.wav \
  --previews output/previews
```

Play the same compiled mix live instead of writing it:

```sh
scripts/setmix stream examples/house-demo.txt
```

The compiled stream supports the same neural planning and effects as rendering:

```sh
scripts/setmix --bars 8 stream first.flac second.flac \
  --vocals --technique auto
```

Start playback as soon as the first track is ready while upcoming tracks are
analyzed and tempo-prepared on background workers:

```sh
scripts/setmix live examples/house-demo.txt
```

For a fixed effect while future tracks prepare asynchronously:

```sh
scripts/setmix live examples/house-demo.txt --technique highpass_out
```

The `live` command preserves the supplied order. It never asks a model to make a
decision in the audio callback; upcoming transitions are prepared while the
current track is playing.

Jump directly to a transition for a quick live audition:

```sh
scripts/setmix stream examples/house-demo.txt --transition 1 --seconds 48
```

Measure peak level, clipping, DC offset, and loudness continuity across every
transition:

```sh
scripts/setmix validate output/house-demo.wav
```

Generate cached vocal timelines and use them to select transition phrases:

```sh
scripts/setmix stems examples/house-demo.txt
scripts/setmix --bars 8 render examples/house-demo.txt \
  --vocals --technique auto -o output/stem-aware.flac
```

Available transition techniques are `bass_swap`, `filter_sweep`, `highpass_out`,
`lowpass_reveal`, `echo_out`, `loop_filter`, `reverb_tail`, `drop_cut`, and
`stem_phrase`. `auto` chooses from the neural vocal-overlap
estimate. The loop is beat-sized; filter movement and echo delay are derived
from the transition's beat grid.

When smart planning finds a confident incoming drop after a sufficient build
window inside the transition, it
can choose `drop_cut`: the outgoing track remains dominant while a filtered,
bass-free preview of the incoming intro plays underneath. The bass/drum weight
changes precisely on the detected drop while mids and highs overlap for a few
beats, preserving continuity instead of making a full-spectrum hard cut.

Render all techniques as short A/B auditions without creating seven full mixes:

```sh
scripts/setmix --bars 8 audition first.flac second.flac \
  --vocals -o output/effect-auditions
```

Create one distinct effect sample per adjacent pair in a longer playlist:

```sh
scripts/setmix --bars 8 audition playlist.txt --technique varied \
  --selected-only -o output/seven-samples
```

For a slower transition that controls each musical layer independently, use 16
or 32 bars and the neural four-stem mixer:

```sh
scripts/setmix --bars 32 audition first.flac second.flac \
  --technique stem_phrase --selected-only -o output/stem-phrase
```

`stem_phrase` aligns eight-bar phrases, establishes the incoming drums early,
finishes the outgoing lyric, swaps bass at the phrase midpoint, trades the
melodic layers under filtering, reveals the incoming singer later, and removes
the outgoing drums last. Stem extraction is cached.

Use any ordered list of files:

```sh
scripts/setmix render \
  "/path/to/first.flac" \
  "/path/to/second.mp3" \
  "/path/to/third.wav" \
  -o output/my-set.wav
```

The prototype currently keeps the first track's tempo for the entire set and
rejects tracks more than 8% away from it. This avoids pretending that a badly
stretched transition is acceptable.

## Outputs

- The rendered WAV or FLAC
- `<mix>.plan.json`, including cue points, transition times, and peak measurement
- Optional short MP3 transition previews
- A validation report for clipping and transition continuity
- Cached analyses and tempo-matched PCM under `.setmix-cache/`

## Current boundary

The base analyzer uses beat, onset, energy, and spectral features. The optional
stem layer runs Hybrid Transformer Demucs locally and produces a 250 ms vocal
activity timeline. The intelligence layer uses faster-whisper locally for lyric
timing and labels phrase-level sections from energy, brightness, percussion, and
vocal density. Analyses, stems, sections, and transcripts are cached locally.
