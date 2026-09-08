# DDJ-FLX4 integration

SetMix uses the DDJ-FLX4 as a class-compliant USB audio and MIDI device. There
is no SetMix kernel driver and AlphaTheta does not require a separate FLX4 audio
driver: the standard macOS/Windows audio driver is installed when the controller
is connected over USB.

## Connect

1. Connect the FLX4's computer/device USB-C port directly to the computer.
2. Close rekordbox, Serato, and other software that may already own its MIDI ports.
3. Open SetMix at `http://localhost:4173` in current Google Chrome.
4. Press **DDJ-FLX4 / Connect hardware** and allow MIDI access.
5. Choose **DDJ-FLX4** under **Audio out**. MASTER and headphones then use the
   controller's USB audio interface when the browser exposes it.

The Codex in-app browser can display the performance interface, but hardware
MIDI and explicit audio-output selection require browser support. Use Chrome if
the hardware badge says `OPEN IN CHROME FOR WEB MIDI`.

## Implemented controls

The mapping follows AlphaTheta's DDJ-FLX4 MIDI message list:

| FLX4 control | SetMix behavior |
| --- | --- |
| Deck 1 PLAY/PAUSE | Play or pause the continuous output |
| Deck 1 CUE | Pause and return the active deck to its cue |
| Deck 2 PLAY/PAUSE | Trigger the prepared smart transition now |
| BEAT SYNC | Toggle phrase-aware smart timing |
| LOOP IN / OUT / 4 BEAT EXIT | Set and toggle the active loop |
| Jog wheel | Fine seek on the active deck |
| Browse encoder | Move through the candidate library |
| Browse/LOAD | Queue the visible candidate |
| Channel trim, EQ, filter and faders | Control the Web Audio mixer |
| Crossfader | Enter manual override and blend deck channels |
| Master level | Control the Web Audio master gain |

Moving a channel fader or the crossfader changes the mixer from AI automation to
manual override. Click the `MANUAL HARDWARE OVERRIDE` readout to return ownership
to the smart transition engine.

Official references:

- [DDJ-FLX4 driver information](https://support.alphatheta.com/en-us/articles/12410664372121)
- [DDJ-FLX4 MIDI message list](https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-FLX4/DDJ-FLX4_MIDI_message_List_E1.pdf)

