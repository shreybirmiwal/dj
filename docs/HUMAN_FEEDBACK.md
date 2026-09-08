# SetMix human feedback and tuning rationale

Last updated: 2026-09-08

This is the durable listening record for SetMix. Re-read it before changing the
planner, scorer, transition choreography, or defaults. It distinguishes what the
listener actually said from our engineering interpretation so an inference is
never mistaken for a confirmed preference.

## Rules for using this document

- **Human feedback** is the source of truth. Preserve its meaning even when the
  implementation changes.
- **Engineering inference** is a hypothesis to test, not a settled preference.
- Do not call a transition fixed until the listener has heard the new render.
- Prefer rules that generalize across songs. Never hard-code a title, artist, or
  timestamp to make one demo pass.
- Keep failed renders. They are regression examples and explain why a tunable
  exists.
- Record the actual transition event being judged: lyric exit, bass swap, drum
  takeover, drop, or incoming vocal entrance. A single generic crossfade time is
  not enough.

## Listening north star

The transition should feel like the new song was already becoming inevitable
before the listener consciously notices the switch. Long overlaps are welcome
when stems keep the arrangement clean. A successful transition preserves lyrical
sentences, aligns rhythmic phase and phrases, maintains energy, and gives the next
song an intentional entrance rather than merely crossfading two masters.

## Confirmed preferences

### Song choice and workflow

- The human supplies the songs and their order. SetMix mixes them; it does not
  need to choose music yet.
- Do not rely on Rekordbox Automix. Planning and rendering should be ours and
  explainable as timed actions.
- The eventual live experience should generate asynchronously: while one song
  plays, analyze and render candidates for the next supplied song.
- The user should be able to change upcoming songs before the transition is
  committed.

### Rhythm and structure

- Matching BPM alone is not beat matching. Beat transients/phase must line up.
- Phrases matter. Prefer 8/16/32-bar structure and section-aware entrances.
- Tempo can move inside a song; use a local tempo map instead of one global BPM.
- When beat confidence is poor, avoid a long exposed drum mash. Prefer a
  technique that masks uncertainty or uses stems to remove competing drums.

### Arrangement

- Longer overlaps generally sound better than abrupt changes, especially when
  stems allow drums, bass, instruments, and vocals to move independently.
- Avoid two lead singers at once unless an intentional mashup is requested.
- Do not cut a word or unfinished lyrical thought.
- Avoid a hard transition followed by an empty/dead passage. The incoming song
  should already have useful energy or an intentional build ready.
- A transition should get to a meaningful musical point without making the
  listener wait through an uninteresting passage.
- Low-pass, high-pass, EQ swaps, loops, echo, and reverb are useful only when
  they support a musical event. Effects are not substitutes for phrasing.
- Output must remain centered stereo; a one-sided/left-only result is a failure.

### Quality and development

- Improvements must generalize rather than special-case the demo under review.
- Render several candidates and score/listen before keeping the first plan.
- Human reactions such as “too abrupt,” “too mashed,” “dead,” or “good” should
  become explicit acceptance criteria and regression tests.

## Positive reference

### Long four-stem phrase transition

- Reference: `output/five-harmonic-four-stem-demos-v5/transition-05-stem_phrase.mp3`
- Human feedback: “THIS IS REALLY GOOD” and specifically identified transition
  05 as a strong result.
- What likely worked: a long phrase-aligned overlap, continuous drum correction,
  staged stem handoff, separated singers, and gradual bass/instrument movement.
- Preserve: continuity and the feeling that the transition is unfolding rather
  than switching.

## Failure history and lessons

### Early prototypes

- Human feedback: transitions sounded like basic crossfades with insufficient
  overlap.
- Human feedback: some examples mashed both songs together and did not beat
  match convincingly.
- Human feedback: Safe and Sound did not beat match.
- Engineering response: phrase alignment, isolated-drum phase/drift correction,
  four-stem choreography, and 32-bar demonstrations.
- Remaining lesson: objective BPM/key compatibility cannot replace isolated-drum
  and listening QA.

### Preview dead air

- Human feedback: transitions sometimes changed hard and then felt dead.
- Engineering finding: preview padding produced literal zero-valued tail audio in
  at least one render.
- Response: previews now stop at the incoming track's active end rather than
  padding unavailable context with silence.
- General lesson: distinguish intended breakdowns from generated silence.

### Incoming-song skip

- Engineering finding from smart planning: the scorer was willing to jump 92
  seconds into an incoming song to obtain a better word boundary.
- Response: incoming candidate search was limited to the first four phrases and
  playlist-position weight was increased.
- General lesson: a technically clean transition is not useful if it silently
  discards the identity and progression of the next song.

### Badtameez Dil -> Mo Bamba

- Failed render: `output/smart-random/03-bollywood-trap/transition-01-echo_out.mp3`
- Failed render: `output/drop-cut-bollywood-trap/transition-01-drop_cut.mp3`
- Failed render: `output/drop-cut-bollywood-trap-v2/transition-01-drop_cut.mp3`
- Human feedback 1: the incoming intro partly worked, but Badtameez Dil should
  give way on the Mo Bamba drop.
- Human feedback 2: the first drop implementation became “just a hard cut.”
- Human feedback 3 (current): the swap is still too sudden and Badtameez Dil is
  cut mid-word.
- Current status: **OPEN / high priority**.
- Engineering diagnosis:
  - The planner correctly found the incoming drop, but interpreted “cut on the
    drop” as a full-spectrum dominance switch.
  - Multiband smoothing removed the waveform discontinuity but did not solve the
    lyrical discontinuity.
  - Candidate word scoring currently evaluates the nominal outgoing vocal exit
    near 42% of the transition. A `drop_cut` may change dominance at a different
    time, so it can score a safe word boundary that is irrelevant to the actual
    cut.
- Required general behavior:
  - Tease the incoming intro without taking over.
  - Determine the actual outgoing vocal-removal time from the chosen technique.
  - Move the drop alignment, loop the intro, or remove only instrumental layers
    until the outgoing word/phrase has finished.
  - Change rhythmic and bass weight on the drop, but let vocals/melodic tails
    finish naturally.
- Acceptance criteria:
  - No outgoing word is truncated or perceptually interrupted.
  - Mo Bamba's drop remains recognizable and impactful.
  - No full-spectrum stop/start sensation.
  - No extended lead-vocal collision.

### Trust Issues -> We Still Don't Trust You

- Failed/insufficient renders:
  - `output/trust-songs-smart-v2/transition-01-drop_cut.mp3`
  - `output/trust-songs-smart-v3/transition-01-echo_out.mp3`
- Best reference so far:
  - `output/trust-songs-smart-v4/transition-01-echo_out.mp3`
- Human feedback 1: the initial transition was too harsh.
- Human feedback 2 (current): Trust Issues has a chill, repeatable beat that can
  continue underneath for a longer build; when the first song becomes quiet for
  a moment, introduce the vocals of song two.
- Human feedback 3: “this one is good” for the v4 `transition-01-echo_out.mp3`.
- Human feedback 4: there is a small clash in the underlying beats after the
  perceived transition.
- Current status: **GOOD, MINOR ISSUE / needs revised listening pass**.
- Behavior to preserve:
  - Reject the early impact/drop interpretation for this pair.
  - Start the incoming material after its early energy jump rather than allowing
    the jump to punch through during the blend.
  - Use a gradual echo/filter handoff with no broadband stop/start sensation.
  - Do not echo the outgoing kick/bass after incoming rhythmic ownership is
    established.
- Engineering interpretation to test:
  - This pair needs an **instrumental-bed / vocal-pocket** archetype, not a
    generic stereo filter or impact cut.
  - Separate or loop a stable instrumental phrase from Trust Issues.
  - Keep that restrained beat as the continuity bed while elements of the second
    song appear.
  - Detect a real outgoing vocal phrase ending/quiet pocket.
  - Introduce the second song's vocal on that pocket, aligned to a phrase/downbeat.
  - Bass and drum ownership can change independently from vocal ownership.
- Acceptance criteria:
  - The repeated chill groove is audible long enough to establish continuity.
  - Incoming vocals do not arrive over an active outgoing line.
  - Vocal entrance sounds prompted by the quiet pocket, not by an arbitrary
    percentage of transition duration.
  - No sudden broadband level or spectral jump.
- Note: v4 is directionally approved but retains a small late beat clash. The
  instrumental-bed/vocal-pocket idea remains a future alternate arrangement.

## Current tunables and why they exist

These values describe the current implementation, not eternal truths. Change
them only with a listening reason and update this table.

| Area | Current value | Why | Known limitation |
|---|---:|---|---|
| Preferred long audition | 32 bars | Human preferred long, layered transitions | Should become adaptive by section availability |
| Phrase unit | 8 bars / 32 beats | Aligns common pop, hip-hop, and dance phrasing | Odd meters and irregular arrangements need another model |
| Maximum global tempo move | 8% | Avoid obviously damaged time stretching | Compatible half/double-time tracks may need explicit handling |
| Local tempo sampling | Every 8 bars | Tracks gradual live tempo movement | Sudden tempo changes need denser anchors |
| Vocal activity resolution | 250 ms | Cheap cached planning signal | Word timestamps must override it near lyric cuts |
| Incoming search | First 4 complete phrases | Prevents skipping deep into the next song | May miss a uniquely clean later intro edit |
| Outgoing search | Last 8 complete candidates | Preserves more of the outgoing track | Long transitions can still begin too early |
| Stem outgoing vocal fade | 24%-42% | Finishes outgoing singer before midpoint | Fixed percentages ignore actual word endings |
| Stem incoming vocal reveal | 48%-68% | Creates a vocal-free pocket | Trust feedback requires event-driven entrance |
| Stem bass swap | 38%-62% | Keeps two bass lines apart | Must react to drops and breakdowns |
| Stem drum blend | 4%-96% | Provides long rhythmic continuity | Unsafe when either beat grid is uncertain |
| Drop eligibility | 32%-80% of transition | Requires an audible setup before impact | Does not yet require a lyric-safe actual cut time |
| Ideal drop location | 52%, width 22% | Favors a balanced build/drop arc | Some genres need earlier or later payoff |
| Drop low-end exit | Final beat before drop | Creates tension and clears bass | Can feel abrupt without stem/vocal independence |
| Drop low-end entrance | First quarter-beat after drop | Preserves impact | Needs local loudness compensation |
| Drop mid overlap | 2 beats | Avoids full-spectrum hard cut | Can still truncate a vocal phrase |
| Drop high overlap | 4 beats | Retains ambience and continuity | Dense vocals can still clash |
| Drum drift correction | 4 eight-bar blocks, R² >= 0.90 | Applies warp only to reliable measured drift | Syncopated patterns can confuse correlation |
| Track loudness target | Approximately -15 dB RMS analysis target | Reduces obvious song-to-song level changes | Local section energy still varies |
| Long-transition solo space | 8 bars when the track permits | Prevents back-to-back transitions and cue rewinds | Very short tracks may have to mix again immediately |
| Long-stem energy floor | 66% of interpolated 75th-percentile section RMS, max 3.5x lift | Supports unexpectedly sparse handoff centers without flattening the whole song | Still needs human listening for audible pumping or stem noise |
| Output limiter | Approximately -1.5 dBFS ceiling | Leaves room for MP3 inter-sample overshoot | Limiting cannot repair a poor arrangement |

### Candidate score weights

Current default/impact values in `setmix/intelligence.py` are technique-aware.
Long `stem_phrase` blends instead use 0.19 vocal safety, 0.15 word boundaries,
0.24 transition energy floor, and zero drop-opportunity weight: a landmark that
would justify an impact cut must not pull a gradual blend into two breakdowns.

| Signal | Weight | Rationale |
|---|---:|---|
| Vocal safety | 0.16 | Avoid simultaneous lead lines |
| Word boundaries | 0.14 | Finish lyrics cleanly |
| Section compatibility | 0.13 | Prefer musically sensible handoffs |
| Early-energy stability | 0.10 | Avoid surprise rises early in gradual blends |
| Energy continuity | 0.08 | Avoid holes and unplanned jumps |
| Beat confidence | 0.08 | Expose drums only when the grid is trustworthy |
| Harmonic compatibility | 0.06 | Reduce clashes without overvaluing imperfect key detection |
| Playlist position | 0.06 | Avoid skipping too far into the incoming song |
| Tempo compatibility | 0.04 | Penalize difficult stretches |
| Drop opportunity | 0.15 | Reward a usable, sufficiently prepared incoming drop |

Known scoring defect: word-boundary timestamps are evaluated using fixed stem
handoff percentages. They must become **technique-specific event times**. For
example, score the actual drop dominance event for `drop_cut`, and score the
detected vocal-pocket entrance for the future instrumental-bed technique.

### Ten-song randomized pop/EDM set
- Render: `output/ten-song-random-2026-09-08/full-mix-v5.mp3`
- Status: IMPROVED, NEEDS LISTENING
- General rules tested:
  - A track must never rewind to an earlier phrase after it has entered.
  - Reserve eight solo bars between long handoffs when duration permits.
  - Score the quietest point of gradual blends, not merely the difference
    between their starting sections.
  - Smoothly support sparse transition centers; do not normalize whole songs.
- Objective QA:
  - 24:35 duration, nine 16-bar four-stem transitions.
  - Zero clipped samples, -0.67 dBFS decoded MP3 peak, -15.56 dBFS RMS.
  - All transition loudness ranges below 8 dB; worst measured range 7.77 dB.
- Human acceptance result: pending.

### Technique research: loop bridges and harmonic protection
- References: Pioneer DJ genre-mixing guide, pro-technique guide, and harmonic
  mixing interviews (reviewed 2026-09-08).
- Status: IMPROVED, NEEDS LISTENING
- General rules tested:
  - When two arrangements are busy, use a simple repeated drum phrase as a
    temporary third deck instead of mashing both masters together.
  - Outgoing vocals leave before the loop becomes dominant; destination vocals
    enter only after its drums and bass are established.
  - Camelot-compatible songs may share melodic material. Incompatible pairs
    keep rhythmic continuity but clear the old melody before revealing the new
    one, with a tighter bass swap.
- Objective QA:
  - 18 engine tests pass, including rhythmic-bed continuity and a synthetic
    incompatible-melody overlap regression.
  - Real auditions contain no decoded NaNs, infinities, clipping, or detected
    half-second regions below -40 dBFS.
- Human acceptance result: pending for both audition renders.

## Next implementation priorities

1. Make every transition technique publish an event schedule: outgoing vocal
   exit, incoming vocal entry, bass ownership, drum ownership, and full musical
   ownership.
2. Score word safety at those real event times, not at global percentages.
3. Add phrase-end protection: if an event lands inside a word or lyrical phrase,
   move the event, loop the instrumental, or keep the outgoing vocal stem alive.
4. Add an `instrumental_bed` archetype with phrase-safe looping and quiet-pocket
   vocal entry for the trust-song feedback.
5. Re-render Badtameez Dil -> Mo Bamba and obtain explicit human approval. Keep
   the approved trust-song v4 as a regression reference.
6. Later, store ratings and descriptors in a machine-readable preference model;
   do not treat the current hand-authored rules as trained personalization.

## Feedback update template

Append new feedback using this structure:

```text
### Track A -> Track B (or general behavior)
- Render:
- Human feedback:
- Status: OPEN / IMPROVED, NEEDS LISTENING / APPROVED
- Engineering hypothesis:
- General rule being tested:
- Tunables changed:
- Objective QA:
- Human acceptance result:
```
