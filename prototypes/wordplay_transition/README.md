# Isolated wordplay-transition prototype

This experiment renders a lyric-to-lyric transition without changing the main
SetMix package. It uses manually supplied phrase timestamps so the first test
measures whether the musical idea works, independently of automatic lyric
transcription.

The included presets cover three wordplay pairs:

- Taio Cruz, Dynamite -> Lady Gaga, Just Dance ("dance" handoff)
- Justin Bieber, Baby -> Jay Sean, Down ("down" handoff)
- Nicki Minaj, Starships -> Daft Punk, One More Time

The intended arrangement is `aligned-swap`:

1. Song one plays normally through the matching lyric.
2. Its matching word or phrase locks into a steady beat-sized loop.
3. Song two's instrumental pre-hook begins underneath that loop.
4. When song two reaches the same lyric, all song-one stems are hard-cut and
   the song-two vocal enters at full strength.

The Starships preset also keeps the earlier experimental A/B variants:

- `clean`: the second matching phrase enters immediately after the first.
- `phrase-loop`: the outgoing "one more time" fragment loops for two bars.
- `word-roll`: the last word rolls four times before the incoming drop.
- `hype-build`: accelerating loops, a riser, and a one-beat vacuum before the drop.

Demucs stems and rendered audio stay inside this directory's ignored `cache/`
and `output/` folders.

## Render the included test

From the repository root:

```sh
.venv/bin/python prototypes/wordplay_transition/wordplay_demo.py \
  prototypes/wordplay_transition/presets/starships-one-more-time.json
```

Substitute `dynamite-just-dance.json` or `baby-down.json` to render the other
pairs. Render the corrected arrangement explicitly with `--variant aligned-swap`.

The first run separates both tracks into four stems and can take several
minutes. Later runs reuse the isolated cache. Render a single variant with
`--variant clean`, `--variant phrase-loop`, or `--variant word-roll`.

## Try another pair

Copy the preset and edit these timestamps:

- `left_context_start`: where the audition begins in the outgoing track.
- `left_phrase_start` / `left_phrase_end`: the outgoing lyric line.
- `right_phrase_start`: where the matching incoming lyric begins.
- Each variant's `loop_start` / `loop_end`: the vocal fragment to repeat.

The prototype deliberately requires manual timestamps. Automatic word-level
matching should only be added after these auditions establish that the effect
is worth productizing.
