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
gain/EQ/filter/faders, crossfader, and master level are implemented. MIDI ports
are detected at launch and polled for USB hot-plug.

Audio currently follows the single macOS system output. Independent FLX4 master
and headphone cue buses require a native multi-channel audio backend and remain
to be implemented. The mappings are covered by automated tests, but final feel,
jog direction, LED behavior, and USB reconnect should also be validated on the
physical controller.
