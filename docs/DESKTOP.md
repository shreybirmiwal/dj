# SetMix desktop architecture

The desktop build keeps the proven SetMix DSP and neural pipeline behind a
private loopback server, embeds the performance UI in a native window, and owns
controller MIDI through `python-rtmidi`. No public web service or external
browser is involved.

## Processing model

- Library metadata and cached analysis appear immediately.
- Selecting the next track starts the full CLI-equivalent preparation job:
  analysis, stems, word timing, candidate ranking, alignment, and render.
- The current track keeps playing while that work runs.
- A completed handoff is joined at its selected phrase; the rendered incoming
  song becomes the continuous source and the next preparation can begin.
- Existing `.setmix-cache` assets are reused when running from the checkout.

## Hardware status

Native FLX4 input/output MIDI, transport, jog, browse/load, loops, sync, channel
gain/EQ/filter/faders, crossfader, master level, and headphone cue are implemented.
MIDI ports are detected at launch and polled for USB hot-plug.

Master playback follows the macOS output and should be assigned to the FLX4,
which uses USB channels 1/2 for its RCA MASTER OUT. A native PortAudio cue engine
decodes the selected deck with FFmpeg and writes it pre-fader only to FLX4 USB
channels 3/4. This keeps the speakers and headphones independent. The mappings
and channel isolation are covered by automated tests, but final gain, latency,
jog direction, LED behavior, and USB reconnect still need physical-controller
validation.
