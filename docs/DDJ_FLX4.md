# DDJ-FLX4 integration

SetMix uses the DDJ-FLX4 as a class-compliant USB audio and MIDI device. There
is no SetMix kernel driver and AlphaTheta does not require a separate FLX4 audio
driver: the standard macOS/Windows audio driver is installed when the controller
is connected over USB.

## Connect

1. Connect the FLX4's computer/device USB-C port directly to the computer.
2. Close rekordbox, Serato, and other software that may already own its MIDI ports.
3. Launch `~/Applications/SetMix.app` or run `scripts/setmix-app`.
4. Open SetMix's hardware check and click **ROUTE MASTER TO DDJ-FLX4**. This
   selects the native CoreAudio device and sends the SetMix master to FLX4 USB
   channels 1/2 and the RCA MASTER OUT. Native cue monitoring uses USB
   channels 3/4 and the FLX4 headphones socket. SetMix detects MIDI and audio
   ports automatically, including when the controller is plugged in after launch.

## First hardware test

Click the **DDJ-FLX4** status button in the SetMix desktop app. The hardware-check
panel verifies native MIDI, the four-channel CoreAudio device, and whether macOS
currently routes system audio to the FLX4. Routing changes the system-wide macOS
output, so SetMix only does it when the route button is clicked. Before playing a track:

1. Turn MASTER LEVEL and HEADPHONES LEVEL down, then raise them slightly.
2. Send the quiet **USB 1/2** test tone. It should be heard only through the RCA
   master path.
3. Send the quiet **USB 3/4** test tone. It should be heard only in headphones.
4. Move a jog wheel or press a CUE button. The MIDI receive counter should move.
5. Load two tracks, press deck B CUE to confirm private preview, then use deck A
   PLAY/PAUSE and LOAD to test a real transition.

The diagnostic tones are capped at a low digital level, but the analog MASTER and
HEADPHONES knobs still control the final listening level.

The browser build remains available at `http://localhost:4173`; use current
Google Chrome for its Web MIDI and explicit output-selection features. The
desktop build owns MIDI natively, so it does not need browser MIDI permission.

The desktop app provides a separate pre-fader headphone bus. Press either channel
`CUE` button to audition that deck without adding it to the master. Deck B starts
at its analyzed cue-in point. The FLX4 HEADPHONES LEVEL control changes the native
cue level. Cue audio is deliberately written only to channels 3/4, leaving RCA
master channels 1/2 untouched.

The RCA outputs are line-level, unbalanced master outputs. Connect them to powered
speakers or an amplifier, not passive speakers. Start with the FLX4 MASTER LEVEL
fully down, then raise it slowly. RCA never carries the isolated cue bus.

## Implemented controls

The mapping follows AlphaTheta's DDJ-FLX4 MIDI message list:

| FLX4 control | SetMix behavior |
| --- | --- |
| Deck 1 PLAY/PAUSE | Play or pause the continuous output |
| Deck 1 CUE | Pause and return the active deck to its cue |
| Deck 2 PLAY/PAUSE | Trigger the prepared smart transition now |
| BEAT SYNC | Toggle phrase-aware smart timing |
| LOOP IN / OUT / 4 BEAT EXIT | Set and toggle the active loop |
| Tempo fader | Adjust live deck tempo over a ±8% range; MT controls pitch lock |
| Jog wheel side | Pitch-bend/nudge while playing; fine seek while paused |
| Jog platter | Touch-and-scrub in vinyl mode, resuming cleanly on release |
| Browse encoder | Move through the candidate library |
| Browse/LOAD | Queue the visible candidate |
| Channel trim, EQ, CFX and faders | Control the 48 kHz Web Audio mixer |
| SMART CFX | Toggle the tempo-friendly filter/echo macro |
| Crossfader | Enter manual override and blend deck channels |
| Master level | Control the Web Audio master gain |
| Channel CUE | Send that deck pre-fader to FLX4 headphones on USB 3/4 |
| MASTER CUE | Monitor the currently playing deck in FLX4 headphones |
| Headphones level | Control native headphone-cue gain |

Moving a channel fader or the crossfader changes the mixer from AI automation to
manual override. Click the `MANUAL HARDWARE OVERRIDE` readout to return ownership
to the smart transition engine.

Lyric highlighting is locked to source position rather than wall-clock time. It
therefore stays aligned when the tempo fader speeds up or slows down playback,
including across a pre-rendered smart handoff. The deck lyric footer shows the
active playback multiplier whenever it is not 1.000×.

Official references:

- [DDJ-FLX4 driver information](https://support.alphatheta.com/en-us/articles/12410664372121)
- [DDJ-FLX4 MIDI message list](https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-FLX4/DDJ-FLX4_MIDI_message_List_E1.pdf)
- [DDJ-FLX4 USB connection and output behavior](https://support.alphatheta.com/en-us/articles/22682056443801)
