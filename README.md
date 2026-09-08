# SetMix prototype

SetMix analyzes songs, keeps their supplied order, estimates BPM and musical
key/Camelot position, aligns beats and eight-bar phrases, separates four stems,
and renders or plays staged drum, bass, melodic, and vocal handoffs.

It does not choose songs. A text file or the command-line argument order is the
playlist order.

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
`lowpass_reveal`, `echo_out`, `loop_filter`, `reverb_tail`, and `stem_phrase`. `auto` chooses from the neural vocal-overlap
estimate. The loop is beat-sized; filter movement and echo delay are derived
from the transition's beat grid.

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
stem layer runs Hybrid Transformer Demucs locally, caches vocals/accompaniment,
and produces a 250 ms vocal-activity timeline. It does not upload music.
