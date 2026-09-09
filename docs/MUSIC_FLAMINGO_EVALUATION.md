# Music Flamingo DJ annotation evaluation

Date: 2026-09-08

## Question

Can `nvidia/music-flamingo-hf` improve SetMix by preprocessing tracks into
timestamped sections, vocal phrases, drops, safe transition windows, protected
singalong regions, and reusable instrumental loops?

## Setup

- NVIDIA Music Flamingo preview model (8B), Hugging Face Transformers
- One L40S 45 GB GPU rented through Lium
- Three full-song probes: Give Me Everything, I Love It, Trust Issues
- Two focused 60-second probes: the late Give Me Everything hook and the
  opening of Trust Issues
- Total Lium spend: approximately $0.11

The raw outputs are saved under `output/music-flamingo-probe-2026-09-08/`.

## Result

Music Flamingo recognized broad musical semantics in places. It estimated the
tested tempos reasonably and recognized repeated hooks and vocal intensity.
That makes it potentially useful as a coarse, offline semantic critic.

It was not reliable enough to create the engine's timing map:

- Every response violated the requested JSON schema and was rejected.
- Whole-song outputs stopped early or invented highly regular short sections.
- Focused outputs used inconsistent or impossible time scales.
- The model quoted/transcribed lyrics despite an explicit instruction not to.
- Some genre, arrangement, and event descriptions were incorrect.
- Confidence values did not reflect the observed uncertainty.

## Decision

Do not let Music Flamingo directly set cue points, beat grids, fades, loops, or
stem handoff times. Keep deterministic DSP, beat tracking, separated-vocal
activity, and word timestamps as the timing authority.

If we revisit this model, use it only after SetMix proposes a small set of
beat-snapped candidate windows. Ask the model to rank or label whole windows
with coarse categories such as `protect_hook`, `calm`, `build`, or
`reusable_bed`; reject malformed output, clamp every decision to detected beat
and phrase boundaries, and fall back to the existing scorer.

Before adopting it, evaluate candidate ranking on a labeled benchmark built
from human feedback. The key metric is whether it chooses the preferred window,
not whether its prose sounds plausible.
