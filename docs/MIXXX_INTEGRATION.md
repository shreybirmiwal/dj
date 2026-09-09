# Mixxx integration direction

## Decision

SetMix is not currently built on Mixxx. It owns its analysis, transition DSP,
browser/native UI, FLX4 MIDI mapping, and macOS master/headphone audio routing.

Mixxx should become SetMix's performance runtime, while SetMix remains the AI
planner. Mixxx already solves the hard, latency-sensitive parts: deck playback,
scratching, sync, effects, controller feedback, recording, library management,
and multi-output audio. Rebuilding all of that in Python and Web Audio would
consume the project without improving the AI.

The upstream source fork is <https://github.com/shreybirmiwal/mixxx>. It is kept
separate because Mixxx is a large C++/Qt GPL project and SetMix is a Python
sidecar. This repository's `mixxx-integration` branch contains the SetMix half.

## What works now

The bridge uses Mixxx's documented command-line interface. It can load up to four
tracks into Mixxx's virtual decks and export any longer ordered set as an M3U8
playlist:

```sh
scripts/setmix mixxx first.flac second.mp3

scripts/setmix mixxx examples/house-demo.txt \
  --playlist-output output/house-demo.m3u8
```

On macOS the bridge finds `/Applications/Mixxx.app` automatically. Override it
with `--mixxx-path` or `SETMIX_MIXXX_PATH`. `--dry-run` prints the exact launch
command without starting Mixxx.

This is deliberately a launch/export bridge, not fake automation. Mixxx 2.5 has
an internal Control system and JavaScript controller mappings, but no supported
external IPC, OSC, or WebSocket API. Its controller JavaScript is not a general
network plugin system.

## Target architecture

```text
SetMix Python sidecar                    Mixxx fork (C++ / Qt)
---------------------                   --------------------
library intelligence       local IPC    track/deck loader
next-track ranking       ------------>  real-time deck engine
phrase/cue plan           <------------  state subscriptions
stem preparation                        FLX4 + audio routing
transition intent                       effects + recording
```

The fork should add one small, local-only control adapter rather than embedding
models in the audio process:

1. A Unix-domain socket on macOS/Linux (named pipe on Windows), protected by a
   per-launch token and an allowlist of commands.
2. JSON messages for `load_track`, setting/reading named Mixxx Controls, cue and
   beat-grid metadata, and state subscriptions.
3. All requests posted to the appropriate Qt/engine thread. Model inference,
   file I/O, and socket work must never run in Mixxx's real-time audio callback.
4. SetMix sends high-level transition intent ahead of time; Mixxx performs the
   sample-accurate playback and exposes manual override at all times.

Start with track loading and observable deck state. Then add cue/hotcue writes,
tempo/sync, EQ/filter/fader automation, and finally four-stem controls. Mixxx 2.6
is the relevant base for stem-file playback; 2.5.6 is the conservative baseline
for validating FLX4 and audio behavior.

## Product boundary

“AI-native” should mean the intelligence owns preparation and proposes or
executes musical intent, not that a model runs inside an audio callback. The
human can always touch the controller and take over. A useful first product is:

- SetMix ranks the next tracks and explains why.
- It chooses phrase-aligned transition cues and prepares stems ahead of time.
- Mixxx executes on proven decks with the existing FLX4 mapping.
- Manual movement suspends automation; SetMix can resume only explicitly.

Mixxx is GPLv2-or-later. Distributing a modified Mixxx build requires satisfying
its source and license obligations. The exact packaging boundary between that
fork and the SetMix sidecar should be reviewed before shipping.
