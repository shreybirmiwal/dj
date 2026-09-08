const demoTracks = [
  { id: 1, title: "Apricots", artist: "Bicep", bpm: 129, key: "C maj", camelot: "8B", energy: 4, length: 268, genre: "Electronic", cover: 2, match: 96, technique: "Bass swap" },
  { id: 2, title: "Innerbloom", artist: "RÜFÜS DU SOL", bpm: 122, key: "D min", camelot: "7A", energy: 3, length: 557, genre: "Electronic", cover: 3, match: 88, technique: "Filter sweep" },
  { id: 3, title: "Glue", artist: "Bicep", bpm: 128, key: "A min", camelot: "8A", energy: 4, length: 342, genre: "Electronic", cover: 1, match: 94, technique: "Bass swap" },
  { id: 4, title: "Losing It", artist: "FISHER", bpm: 125, key: "F min", camelot: "4A", energy: 5, length: 248, genre: "House", cover: 4, match: 91, technique: "Loop filter" },
  { id: 5, title: "Opal (Four Tet Remix)", artist: "Bicep", bpm: 126, key: "E min", camelot: "9A", energy: 4, length: 491, genre: "Electronic", cover: 5, match: 93, technique: "Stem phrase" },
  { id: 6, title: "Archangel", artist: "Burial", bpm: 134, key: "G min", camelot: "6A", energy: 3, length: 240, genre: "UK Garage", cover: 6, match: 84, technique: "Echo out" },
  { id: 7, title: "Atlas", artist: "Lane 8", bpm: 123, key: "B min", camelot: "10A", energy: 3, length: 365, genre: "House", cover: 7, match: 87, technique: "Lowpass reveal" },
  { id: 8, title: "Kerala", artist: "Bonobo", bpm: 114, key: "C min", camelot: "5A", energy: 2, length: 237, genre: "Downtempo", cover: 8, match: 81, technique: "Reverb tail" },
];

let tracks = demoTracks;

const state = {
  current: demoTracks[2],
  suggestion: demoTracks[0],
  queued: null,
  elapsed: 134,
  playing: false,
  filter: "All",
  search: "",
  sessionSeconds: 6504,
  masterBpm: null,
  mixJob: null,
  preparedHandoff: null,
  activeHandoff: null,
  loop: { beats: 4, enabled: false, start: 0, end: 0 },
  mixer: {
    manual: false, crossfader: 0.5, master: 1,
    trimA: 0.82, trimB: 0.82, faderA: 1, faderB: 1,
    highA: 0.5, highB: 0.5, midA: 0.5, midB: 0.5,
    lowA: 0.5, lowB: 0.5, filterA: 0.5, filterB: 0.5,
  },
  controller: { connected: false, name: null, messages: 0 },
};

const $ = (selector) => document.querySelector(selector);
const audioPrimary = $("#audioPrimary");
const audioSecondary = $("#audioSecondary");
let currentAudio = audioPrimary;
let transitioning = false;
let joiningHandoff = false;
let mixRequestGeneration = 0;
let audioGraph = null;
let midiAccess = null;
let midiOutput = null;
const automationGains = new WeakMap([[audioPrimary, 1], [audioSecondary, 1]]);
const midiMsb = new Map();
const JOIN_FADE_SECONDS = 0.35;
const formatTime = (seconds) => {
  const value = Math.max(0, Math.floor(seconds));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
};

const formatDeckTime = (seconds) => {
  const value = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(value / 60);
  const wholeSeconds = Math.floor(value % 60);
  const milliseconds = Math.floor((value % 1) * 1000);
  return `${String(minutes).padStart(2, "0")}:${String(wholeSeconds).padStart(2, "0")}.${String(milliseconds).padStart(3, "0")}`;
};

function ensureAudioGraph() {
  if (audioGraph || !(window.AudioContext || window.webkitAudioContext)) return audioGraph;
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  const context = new AudioContextClass();
  const master = context.createGain();
  master.connect(context.destination);
  const createChannel = (audio) => {
    const source = context.createMediaElementSource(audio);
    const highpass = context.createBiquadFilter(); highpass.type = "highpass"; highpass.frequency.value = 20;
    const low = context.createBiquadFilter(); low.type = "lowshelf"; low.frequency.value = 180;
    const mid = context.createBiquadFilter(); mid.type = "peaking"; mid.frequency.value = 1200; mid.Q.value = 0.8;
    const high = context.createBiquadFilter(); high.type = "highshelf"; high.frequency.value = 6000;
    const lowpass = context.createBiquadFilter(); lowpass.type = "lowpass"; lowpass.frequency.value = 20000;
    const gain = context.createGain();
    source.connect(highpass).connect(low).connect(mid).connect(high).connect(lowpass).connect(gain).connect(master);
    return { audio, highpass, low, mid, high, lowpass, gain };
  };
  audioGraph = { context, master, channels: new Map([[audioPrimary, createChannel(audioPrimary)], [audioSecondary, createChannel(audioSecondary)]]) };
  applyMixer();
  return audioGraph;
}

function eqGain(value) {
  return value < 0.5 ? (value - 0.5) * 36 : (value - 0.5) * 12;
}

function setAutomationGain(audio, value) {
  automationGains.set(audio, Math.max(0, Math.min(1, value)));
  applyMixer();
}

function applyMixer() {
  const mix = state.mixer;
  const active = currentAudio;
  const standby = currentAudio === audioPrimary ? audioSecondary : audioPrimary;
  const crossA = mix.manual ? Math.min(1, Math.SQRT2 * Math.cos(mix.crossfader * Math.PI / 2)) : 1;
  const crossB = mix.manual ? Math.min(1, Math.SQRT2 * Math.sin(mix.crossfader * Math.PI / 2)) : 1;
  const update = (audio, channelName, cross) => {
    const channel = audioGraph?.channels.get(audio);
    const suffix = channelName;
    const auto = automationGains.get(audio) ?? 1;
    const fader = mix.manual ? mix[`fader${suffix}`] : 1;
    const trim = Math.min(1.4, mix[`trim${suffix}`] / 0.82);
    const finalGain = auto * cross * fader * trim * mix.master;
    if (!channel) {
      audio.volume = Math.max(0, Math.min(1, finalGain));
      return;
    }
    const now = audioGraph.context.currentTime;
    channel.gain.gain.setTargetAtTime(finalGain, now, 0.012);
    channel.low.gain.setTargetAtTime(eqGain(mix[`low${suffix}`]), now, 0.02);
    channel.mid.gain.setTargetAtTime(eqGain(mix[`mid${suffix}`]), now, 0.02);
    channel.high.gain.setTargetAtTime(eqGain(mix[`high${suffix}`]), now, 0.02);
    const color = mix[`filter${suffix}`];
    channel.highpass.frequency.setTargetAtTime(color < 0.5 ? 20 + (0.5 - color) * 3600 : 20, now, 0.02);
    channel.lowpass.frequency.setTargetAtTime(color > 0.5 ? 20000 - (color - 0.5) * 36000 : 20000, now, 0.02);
  };
  update(active, "A", crossA);
  update(standby, "B", crossB);
  if (audioGraph) audioGraph.master.gain.setTargetAtTime(1, audioGraph.context.currentTime, 0.015);
  $("#mixerMode").textContent = mix.manual ? "MANUAL HARDWARE OVERRIDE" : "AUTOMATION OWNS MIX";
  $("#mixerMode").parentElement.classList.toggle("manual", mix.manual);
}

function makeWaveform() {
  const waveform = $("#waveform");
  waveform.innerHTML = "";
  const bars = Math.max(52, Math.min(100, Math.floor(waveform.clientWidth / 5)));
  for (let i = 0; i < bars; i += 1) {
    const bar = document.createElement("i");
    bar.className = "wave-bar";
    const harmonic = Math.sin(i * 0.74) * 8 + Math.sin(i * 0.19 + 1) * 6;
    bar.style.height = `${Math.max(7, 24 + harmonic + ((i * 13) % 17))}px`;
    bar.dataset.position = i / (bars - 1);
    waveform.appendChild(bar);
  }
  updateProgress();
}

function waveformSeed(track) {
  return [...`${track?.id || "deck"}${track?.title || ""}`].reduce((value, character) => (value * 31 + character.charCodeAt(0)) >>> 0, 2166136261);
}

function drawTechnicalWaveform(canvas, track, progress, palette) {
  if (!canvas || !track) return;
  const context = canvas.getContext("2d");
  const { width, height } = canvas;
  const center = height / 2;
  const seed = waveformSeed(track);
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#080a0d";
  context.fillRect(0, 0, width, height);
  const bars = 260;
  const barWidth = width / bars;
  for (let index = 0; index < bars; index += 1) {
    const position = index / (bars - 1);
    const pseudo = Math.abs(Math.sin(index * 0.217 + seed * 0.0001) * Math.cos(index * 0.071 + seed * 0.00003));
    const phrase = 0.48 + 0.52 * Math.abs(Math.sin(index * 0.031 + seed));
    const amplitude = 10 + pseudo * phrase * center * 0.88;
    const played = position <= progress;
    const gradient = context.createLinearGradient(0, center - amplitude, 0, center + amplitude);
    gradient.addColorStop(0, played ? palette[0] : "#25343a");
    gradient.addColorStop(0.45, played ? palette[1] : "#2b3b42");
    gradient.addColorStop(0.55, played ? palette[2] : "#302e3b");
    gradient.addColorStop(1, played ? palette[0] : "#25343a");
    context.fillStyle = gradient;
    context.fillRect(index * barWidth, center - amplitude, Math.max(1, barWidth - 1), amplitude * 2);
  }
  context.fillStyle = "rgba(255,255,255,.08)";
  context.fillRect(0, center, width, 1);
}

function currentMixResult() {
  return state.preparedHandoff?.result || state.activeHandoff?.result || null;
}

function updatePerformanceConsole() {
  const current = state.current;
  const next = state.queued || state.suggestion;
  if (!current || !next) return;
  $("#deckATitle").textContent = current.title;
  $("#deckAArtist").textContent = current.artist;
  $("#deckAKey").textContent = current.camelot || current.key || "—";
  $("#deckABpm").textContent = current.bpm ? Number(current.bpm).toFixed(1) : "—";
  $("#deckATime").textContent = formatDeckTime(state.elapsed);
  $("#deckARemaining").textContent = `-${formatDeckTime((current.length || 0) - state.elapsed)}`;
  const beat = current.bpm ? Math.floor(state.elapsed / (60 / current.bpm)) : 0;
  $("#deckAPhrase").textContent = `PHRASE ${String(Math.floor(beat / 32) + 1).padStart(2, "0")} / BAR ${Math.floor((beat % 32) / 4) + 1}.${(beat % 4) + 1}`;
  const progress = current.length ? Math.max(0, Math.min(1, state.elapsed / current.length)) : 0;
  drawTechnicalWaveform($("#deckAWave"), current, progress, ["#2a9ac1", "#55d8ff", "#9974d1"]);
  $(".deck-a .playhead").style.left = `${progress * 100}%`;
  $("#deckABeatgrid").style.setProperty("--deck-progress", `${progress * 100}%`);

  $("#deckBTitle").textContent = next.title;
  $("#deckBArtist").textContent = next.artist;
  $("#deckBKey").textContent = next.camelot || next.key || "—";
  $("#deckBBpm").textContent = next.bpm ? Number(next.bpm).toFixed(1) : "—";
  $("#deckBLength").textContent = `-${formatDeckTime(next.length || 0)}`;
  const delta = current.bpm && next.bpm ? ((state.masterBpm || current.bpm) / next.bpm - 1) * 100 : 0;
  $("#deckBTempoDelta").textContent = `${delta >= 0 ? "+" : ""}${delta.toFixed(1)}%`;
  drawTechnicalWaveform($("#deckBWave"), next, 0, ["#73519f", "#ad78e1", "#c785b9"]);

  const job = state.mixJob;
  const result = currentMixResult();
  const progressValue = job?.progress || (result ? 100 : 0);
  $("#renderProgressBar").style.width = `${progressValue}%`;
  $("#renderProgressText").textContent = job ? `${(job.stage || "queued").toUpperCase()} / ${progressValue}%` : result ? "HANDOFF ARMED / CACHE READY" : "ENGINE IDLE";
  $("#deckBState").textContent = job ? `${(job.stage || "queued").toUpperCase()} / ${progressValue}%` : result ? "READY / QUANTIZED" : "STANDBY / ANALYSIS";
  const stages = ["analysis", "vocals", "intelligence", "planning", "rendering"];
  const stageIndex = job ? Math.max(0, stages.indexOf(job.stage)) : result ? stages.length : -1;
  $("#pipelineStages").querySelectorAll("span").forEach((node, index) => {
    node.classList.toggle("active", index === stageIndex);
    node.classList.toggle("done", index < stageIndex || Boolean(result));
  });
  $("#pipelineStages").querySelectorAll("i").forEach((node, index) => node.classList.toggle("done", index < stageIndex || Boolean(result)));
  $("#aiTechnique").textContent = (result?.technique || job?.options?.technique || "stem_phrase").toUpperCase();
  $("#aiConfidence").textContent = result?.score == null ? "--%" : `${Math.round(Number(result.score) * (Number(result.score) <= 1 ? 100 : 1))}%`;
  $("#aiSections").textContent = result ? `${result.fromSection || "phrase"} → ${result.toSection || "phrase"}`.toUpperCase() : "-- → --";
  const vocalOverlap = result?.transition?.vocal_overlap;
  $("#aiVocalRisk").textContent = vocalOverlap == null ? "--" : vocalOverlap < 0.15 ? "LOW" : vocalOverlap < 0.4 ? "MED" : "CONTROLLED";
  $("#masterClock").textContent = formatDeckTime(state.elapsed);
  updateHotCuePads();
}

function hotCueStorageKey() {
  return `setmix:hotcues:${state.current?.id || "none"}`;
}

function readHotCues() {
  try { return JSON.parse(localStorage.getItem(hotCueStorageKey()) || "[]"); } catch { return []; }
}

function updateHotCuePads() {
  const cues = readHotCues();
  $("#hotCuePads").querySelectorAll("[data-hotcue]").forEach((button) => {
    const position = cues[Number(button.dataset.hotcue)];
    button.classList.toggle("set", Number.isFinite(position));
    button.querySelector("span").textContent = Number.isFinite(position) ? formatTime(position) : "HOT CUE";
  });
}

function updateProgress() {
  const progress = state.current.length ? state.elapsed / state.current.length : 0;
  document.querySelectorAll(".wave-bar").forEach((bar) => {
    bar.classList.toggle("played", Number(bar.dataset.position) <= progress);
  });
  $("#elapsed").textContent = formatTime(state.elapsed);
  $("#remaining").textContent = `−${formatTime(state.current.length - state.elapsed)}`;
  const handoff = state.activeHandoff?.result;
  const transitionAt = handoff && !state.activeHandoff.promoted
    ? (handoff.handoff_start + handoff.transition_offset) * handoff.transition.tempo_ratio_from
    : Number(state.current.cueOut) || Math.max(0, state.current.length - 32);
  const transitionIn = transitionAt - state.elapsed;
  if (!$("#smartToggle").checked) {
    $("#transitionStatus").textContent = "Manual timing enabled";
    $("#transitionDetail").textContent = state.queued ? "Your next track is ready" : "Choose a track from the catalog";
  } else if (state.mixJob && !state.preparedHandoff) {
    $("#transitionStatus").textContent = "Preparing full smart mix";
    $("#transitionDetail").textContent = `${state.mixJob.message} · music keeps playing`;
    $("#technique").textContent = `${state.mixJob.stage || "queued"} ${state.mixJob.progress || 0}%`;
  } else if (state.preparedHandoff) {
    $("#transitionStatus").textContent = "Neural transition ready";
    $("#transitionDetail").textContent = transitionIn > 0
      ? `Joining rendered mix in ${formatTime(Math.max(0, transitionIn - 8))}`
      : "Joining the phrase-aligned handoff now";
  } else if (handoff && !state.activeHandoff.promoted && currentAudio.currentTime < handoff.incoming_offset) {
    const untilMix = handoff.transition_offset - currentAudio.currentTime;
    $("#transitionStatus").textContent = untilMix > 0 ? "Full transition armed" : "Stem transition in progress";
    $("#transitionDetail").textContent = untilMix > 0
      ? `Best mix window in ${formatTime(untilMix)}`
      : `${state.activeHandoff.nextTrack.title} is mixing in now`;
  } else if (transitionIn > 0 && !transitioning) {
    $("#transitionStatus").textContent = "Smart transition armed";
    $("#transitionDetail").textContent = state.queued
      ? `Best mix window in ${formatTime(transitionIn)}`
      : "Choose a next track so SetMix can prepare it";
  } else {
    $("#transitionStatus").textContent = "Smart transition available";
    $("#transitionDetail").textContent = "Choose a track from the catalog";
  }
  updatePerformanceConsole();
}

function energyBars(level, mini = false) {
  return `<span class="${mini ? "mini-energy" : "energy-bars"}">${[1,2,3,4,5].map(n => `<i class="${n <= level ? (mini ? "on" : "") : "off"}"></i>`).join("")}</span>`;
}

function renderRows() {
  const visible = tracks.filter((track) => {
    const inFilter = state.filter === "All" || track.genre === state.filter;
    const query = state.search.toLowerCase();
    return inFilter && `${track.title} ${track.artist}`.toLowerCase().includes(query);
  });
  $("#trackRows").innerHTML = visible.map((track, index) => `
    <tr data-id="${track.id}">
      <td><span class="track-number">${String(index + 1).padStart(2, "0")}</span></td>
      <td><div class="table-title-cell"><div class="cover cover-small cover-${track.cover}">${track.cover === 2 ? '<span class="moon"></span><span class="horizon"></span>' : ""}</div><span><strong>${track.title}</strong><small>${track.artist} · ${track.genre}</small></span></div></td>
      <td>${track.bpm ?? "—"}</td><td>${track.camelot ?? "—"}</td><td>${energyBars(track.energy, true)}</td><td>${track.length ? formatTime(track.length) : "—"}</td>
      <td><button class="mix-next-button ${state.queued?.id === track.id ? "selected" : ""}" data-mix-id="${track.id}">${state.queued?.id === track.id ? "Queued ✓" : "Mix next"}</button></td>
    </tr>`).join("");
  $("#emptyState").hidden = visible.length > 0;
}

function setSuggestion(track) {
  state.suggestion = track;
  $("#suggestedTitle").textContent = track.title;
  $("#suggestedArtist").textContent = track.artist;
  $("#suggestedGenre").textContent = track.genre;
  $("#matchValue").textContent = track.match;
  $("#matchRing").style.background = `radial-gradient(circle at center,var(--surface-2) 57%,transparent 59%),conic-gradient(var(--lime) 0 ${track.match}%,#333239 ${track.match}%)`;
  $("#suggestedCover").className = `cover cover-medium cover-${track.cover}`;
  $("#suggestedCover").innerHTML = track.cover === 2 ? '<span class="moon"></span><span class="horizon"></span>' : "";
  const bpmDiff = track.bpm - state.current.bpm;
  $("#tempoReason").textContent = track.bpm && state.current.bpm
    ? `${bpmDiff >= 0 ? "+" : ""}${Math.round(bpmDiff)} BPM · ${Math.abs(bpmDiff) < 3 ? "natural lift" : "tempo matched"}`
    : "Analysis pending";
  $("#keyReason").textContent = state.current.key && track.key ? `${state.current.key} → ${track.key}` : "Analysis pending";
  $("#energyReason").textContent = track.energy > state.current.energy ? "Smooth build" : track.energy === state.current.energy ? "Energy locked" : "Controlled release";
  $("#technique").textContent = track.technique;
  const button = $("#acceptSuggestion");
  button.classList.toggle("queued", state.queued?.id === track.id);
  button.querySelector("span").textContent = state.queued?.id === track.id ? "Locked in next" : "Mix this next";
}

function queueTrack(track, showToast = true) {
  if (!track || track.id === state.current.id) return;
  state.queued = track;
  $("#queueCount").textContent = "1";
  setSuggestion(track);
  renderRows();
  updateProgress();
  if (showToast) {
    const toast = $("#toast");
    toast.querySelector("strong").textContent = `${track.title} is up next`;
    toast.classList.add("show");
    clearTimeout(window.toastTimer);
    window.toastTimer = setTimeout(() => toast.classList.remove("show"), 3200);
  }
  if ($("#smartToggle").checked) prepareSmartMix(track);
}

function renderCurrentTrack(track, { preserveQueue = false } = {}) {
  const previous = state.current;
  state.current = track;
  state.elapsed = 0;
  if (!preserveQueue) {
    state.queued = null;
    $("#queueCount").textContent = "0";
  }
  $("#currentTitle").textContent = track.title;
  $("#currentArtist").textContent = track.artist;
  $("#currentBpm").textContent = track.bpm ?? "—";
  $("#currentKey").textContent = track.key ?? "—";
  $("#currentCover").className = `cover cover-large cover-${track.cover}`;
  $("#currentCover").innerHTML = track.cover === 2 ? '<span class="moon"></span><span class="horizon"></span>' : "";
  $("#currentEnergy").outerHTML = energyBars(track.energy).replace('<span class="energy-bars"', '<strong class="energy-bars" id="currentEnergy"').replace('</span>', '</strong>');
  const next = bestNextTrack(track, previous?.id);
  setSuggestion(next);
  makeWaveform();
  renderRows();
}

function loadAudio(track, audio = currentAudio) {
  const target = new URL(track.mediaUrl, window.location.href).href;
  if (audio.src !== target) {
    audio.src = track.mediaUrl;
    audio.load();
  }
}

function setPlaybackState(playing, label) {
  state.playing = playing;
  $("#playbackToggle").classList.toggle("playing", playing);
  $("#deckAPlay").classList.toggle("playing", playing);
  $("#deckAPlay .transport-icon").textContent = playing ? "Ⅱ" : "▶";
  $("#playbackToggle").setAttribute("aria-label", playing ? "Pause continuous mix" : "Play continuous mix");
  $("#outputStatus").innerHTML = `<i></i> ${label}`;
  if (midiOutput) midiOutput.send([0x90, 0x0b, playing ? 0x7f : 0]);
}

function waitForMetadata(audio) {
  if (audio.readyState >= 1) return Promise.resolve();
  return new Promise((resolve, reject) => {
    audio.addEventListener("loadedmetadata", resolve, { once: true });
    audio.addEventListener("error", () => reject(new Error("Could not load this audio file")), { once: true });
  });
}

async function togglePlayback() {
  if (state.playing) {
    audioPrimary.pause();
    audioSecondary.pause();
    setPlaybackState(false, "Paused");
    return;
  }
  try {
    ensureAudioGraph();
    if (audioGraph?.context.state === "suspended") await audioGraph.context.resume();
    if (!currentAudio.src) loadAudio(state.current);
    await currentAudio.play();
    setPlaybackState(true, state.activeHandoff ? "Playing the rendered smart mix" : "Streaming while the full mix prepares");
    if (!state.mixJob && !state.preparedHandoff && !state.activeHandoff) {
      prepareSmartMix(state.queued || state.suggestion);
    }
  } catch (error) {
    setPlaybackState(false, "Could not play this format");
    console.error(error);
  }
}

async function prepareSmartMix(next) {
  if (!next || next.id === state.current.id) return;
  const generation = ++mixRequestGeneration;
  state.preparedHandoff = null;
  state.mixJob = { status: "queued", message: "Sending the pair to the mix engine", progress: 2 };
  updateProgress();
  try {
    const response = await fetch("/api/mixes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        fromId: state.current.id,
        toId: next.id,
        bars: 32,
        smart: true,
        technique: "stem_phrase",
        wordModel: "base",
        targetBpm: state.masterBpm,
      }),
    });
    const job = await response.json();
    if (!response.ok) throw new Error(job.error || "Could not start the smart mix");
    if (generation !== mixRequestGeneration) return;
    state.mixJob = job;
    if (job.status === "ready") {
      acceptPreparedJob(job, next, generation);
      return;
    }
    pollMixJob(job.id, next, generation);
  } catch (error) {
    if (generation !== mixRequestGeneration) return;
    state.mixJob = null;
    $("#transitionStatus").textContent = "Smart preparation failed";
    $("#transitionDetail").textContent = error.message;
    setPlaybackState(state.playing, state.playing ? "Playing original audio" : "Ready to play");
    console.error(error);
  }
}

async function pollMixJob(jobId, next, generation) {
  while (generation === mixRequestGeneration) {
    await new Promise(resolve => setTimeout(resolve, 1200));
    try {
      const response = await fetch(`/api/mixes/${jobId}`, { cache: "no-store" });
      if (!response.ok) throw new Error("Lost contact with the mix job");
      const job = await response.json();
      if (generation !== mixRequestGeneration) return;
      state.mixJob = job;
      updateProgress();
      if (job.status === "ready") {
        acceptPreparedJob(job, next, generation);
        return;
      }
      if (job.status === "error") throw new Error(job.message || "Mix preparation failed");
    } catch (error) {
      if (generation !== mixRequestGeneration) return;
      state.mixJob = null;
      $("#transitionStatus").textContent = "Smart preparation failed";
      $("#transitionDetail").textContent = error.message;
      console.error(error);
      return;
    }
  }
}

function acceptPreparedJob(job, next, generation) {
  if (generation !== mixRequestGeneration || job.result.fromId !== state.current.id) return;
  state.mixJob = null;
  state.preparedHandoff = { result: job.result, nextTrack: next };
  if (job.result.score != null) {
    const match = Math.round(Number(job.result.score) * (Number(job.result.score) <= 1 ? 100 : 1));
    next.match = Math.max(0, Math.min(99, match));
    setSuggestion(next);
  }
  // setSuggestion also refreshes the pill from catalogue metadata, so apply
  // the actual renderer-selected technique after it.
  $("#technique").textContent = job.result.technique.replaceAll("_", " ");
  updateProgress();
  maybeJoinPreparedHandoff();
}

function logicalSourceSeconds() {
  const active = state.activeHandoff;
  if (!active) return currentAudio.currentTime || state.elapsed;
  const result = active.result;
  if (!active.promoted) {
    return (result.handoff_start + currentAudio.currentTime) * result.transition.tempo_ratio_from;
  }
  const stretched = result.incoming_consumed + Math.max(0, currentAudio.currentTime - result.incoming_offset);
  return stretched * result.transition.tempo_ratio_to;
}

async function maybeJoinPreparedHandoff() {
  if (!state.playing || !state.preparedHandoff || joiningHandoff) return;
  const prepared = state.preparedHandoff;
  const result = prepared.result;
  const stretchedPosition = logicalSourceSeconds() / result.transition.tempo_ratio_from;
  if (stretchedPosition + 0.15 < result.handoff_start) return;
  if (stretchedPosition > result.handoff_start + result.incoming_offset) {
    state.preparedHandoff = null;
    $("#transitionStatus").textContent = "Prepared after this mix window";
    $("#transitionDetail").textContent = "The next safe phrase will be used at track end";
    return;
  }
  joiningHandoff = true;
  const outgoing = currentAudio;
  const incoming = currentAudio === audioPrimary ? audioSecondary : audioPrimary;
  try {
    incoming.src = result.mediaUrl;
    incoming.load();
    await waitForMetadata(incoming);
    incoming.currentTime = Math.max(0, stretchedPosition - result.handoff_start);
    setAutomationGain(incoming, 0);
    await incoming.play();
    setPlaybackState(true, `Full smart mix ready for ${prepared.nextTrack.title}`);
    const started = performance.now();
    const fade = (now) => {
      if (!state.playing) {
        setAutomationGain(outgoing, 1);
        incoming.pause();
        setAutomationGain(incoming, 1);
        joiningHandoff = false;
        return;
      }
      const position = Math.min(1, (now - started) / (JOIN_FADE_SECONDS * 1000));
      setAutomationGain(outgoing, Math.cos(position * Math.PI / 2));
      setAutomationGain(incoming, Math.sin(position * Math.PI / 2));
      if (position < 1) {
        requestAnimationFrame(fade);
        return;
      }
      outgoing.pause();
      currentAudio = incoming;
      setAutomationGain(outgoing, 0);
      setAutomationGain(incoming, 1);
      state.activeHandoff = { ...prepared, promoted: false };
      state.preparedHandoff = null;
      joiningHandoff = false;
      setPlaybackState(true, "Playing the rendered smart mix");
      updateProgress();
    };
    requestAnimationFrame(fade);
  } catch (error) {
    joiningHandoff = false;
    incoming.pause();
    setAutomationGain(incoming, 1);
    setPlaybackState(state.playing, "Could not join the prepared mix");
    console.error(error);
  }
}

function promoteIncomingTrack() {
  const active = state.activeHandoff;
  if (!active || active.promoted) return;
  active.promoted = true;
  renderCurrentTrack(active.nextTrack);
  state.elapsed = logicalSourceSeconds();
  setPlaybackState(true, "Continuous smart output");
  prepareSmartMix(state.suggestion);
}

async function playNextImmediately() {
  if (state.preparedHandoff) {
    await maybeJoinPreparedHandoff();
    if (state.activeHandoff) return;
  }
  const next = state.queued || state.suggestion;
  if (!next) return;
  currentAudio.pause();
  currentAudio = currentAudio === audioPrimary ? audioSecondary : audioPrimary;
  setAutomationGain(audioPrimary, currentAudio === audioPrimary ? 1 : 0);
  setAutomationGain(audioSecondary, currentAudio === audioSecondary ? 1 : 0);
  renderCurrentTrack(next);
  state.activeHandoff = null;
  loadAudio(next);
  try {
    await currentAudio.play();
    setPlaybackState(true, "Streaming while the next full mix prepares");
    prepareSmartMix(state.suggestion);
  } catch (error) {
    setPlaybackState(false, "Could not play this format");
  }
}

function setLoopEnabled(enabled, start = null, end = null) {
  const beatSeconds = 60 / (state.masterBpm || state.current.bpm || 120);
  if (start != null) state.loop.start = Math.max(0, start);
  if (end != null) state.loop.end = Math.max(state.loop.start + 0.05, end);
  if (!state.loop.end || state.loop.end <= state.loop.start) {
    state.loop.start = Math.floor((currentAudio.currentTime || 0) / beatSeconds) * beatSeconds;
    state.loop.end = state.loop.start + beatSeconds * state.loop.beats;
  }
  state.loop.enabled = enabled;
  $("#loopToggle").classList.toggle("active", enabled);
  $("#loopToggle").textContent = enabled ? "EXIT" : "LOOP";
}

function cueCurrentDeck() {
  currentAudio.pause();
  currentAudio.currentTime = 0;
  state.elapsed = 0;
  setPlaybackState(false, "Cue point ready");
  updateProgress();
}

async function forceSmartMixNow() {
  const prepared = state.preparedHandoff;
  if (!prepared) {
    await playNextImmediately();
    return;
  }
  const outgoing = currentAudio;
  const incoming = currentAudio === audioPrimary ? audioSecondary : audioPrimary;
  const result = prepared.result;
  outgoing.pause();
  incoming.src = result.mediaUrl;
  incoming.load();
  await waitForMetadata(incoming);
  incoming.currentTime = Math.max(0, result.transition_offset);
  currentAudio = incoming;
  setAutomationGain(outgoing, 0);
  setAutomationGain(incoming, 1);
  state.activeHandoff = { ...prepared, promoted: false };
  state.preparedHandoff = null;
  await incoming.play();
  setPlaybackState(true, "Smart transition triggered from hardware");
  updateProgress();
}

function cycleSuggestion(direction) {
  if (!tracks.length) return;
  const start = tracks.findIndex(track => track.id === state.suggestion.id);
  let index = start;
  for (let attempts = 0; attempts < tracks.length; attempts += 1) {
    index = (index + direction + tracks.length) % tracks.length;
    if (tracks[index].id !== state.current.id) {
      setSuggestion(tracks[index]);
      updatePerformanceConsole();
      return;
    }
  }
}

function setMixerValue(name, value, { manual = false } = {}) {
  if (!(name in state.mixer)) return;
  state.mixer[name] = Math.max(0, Math.min(1, value));
  if (manual) state.mixer.manual = true;
  const input = $(`#${name}`);
  if (input) {
    input.value = state.mixer[name];
    input.style.setProperty("--knob", state.mixer[name]);
  }
  applyMixer();
}

function mapMidi14(channel, controller, value) {
  if (channel <= 1) {
    const suffix = channel === 0 ? "A" : "B";
    const mappings = { 0x04: `trim${suffix}`, 0x07: `high${suffix}`, 0x0b: `mid${suffix}`, 0x0f: `low${suffix}`, 0x13: `fader${suffix}` };
    const filterController = channel === 0 ? 0x17 : 0x18;
    const name = controller === filterController ? `filter${suffix}` : mappings[controller];
    if (name) setMixerValue(name, value, { manual: name.startsWith("fader") });
  } else if (channel === 6 && controller === 0x1f) {
    setMixerValue("crossfader", value, { manual: true });
  } else if (channel === 6 && controller === 0x08) {
    setMixerValue("master", value);
  }
}

function processMidiControl(channel, controller, value) {
  if (channel === 6 && controller === 0x40) {
    cycleSuggestion(value <= 0x3f ? Math.max(1, value) : -Math.max(1, 0x80 - value));
    return;
  }
  if (channel <= 1 && [0x21, 0x22, 0x23].includes(controller)) {
    if (channel === 0) {
      const delta = value - 0x40;
      currentAudio.currentTime = Math.max(0, Math.min(currentAudio.duration || Infinity, currentAudio.currentTime + delta * 0.035));
    }
    return;
  }
  if (controller < 0x20) {
    midiMsb.set(`${channel}:${controller}`, value);
    return;
  }
  const base = controller - 0x20;
  const key = `${channel}:${base}`;
  if (!midiMsb.has(key)) return;
  const normalized = ((midiMsb.get(key) << 7) | value) / 16383;
  mapMidi14(channel, base, normalized);
}

function processMidiNote(channel, note, velocity) {
  if (!velocity) return;
  if (channel <= 1) {
    if (note === 0x0b) channel === 0 ? togglePlayback() : forceSmartMixNow();
    if (note === 0x0c && channel === 0) cueCurrentDeck();
    if (note === 0x58) {
      $("#smartToggle").checked = !$("#smartToggle").checked;
      $("#deckASync").classList.toggle("active", $("#smartToggle").checked);
      $("#deckBSync").classList.toggle("active", $("#smartToggle").checked);
      updateProgress();
    }
    if (note === 0x10 && channel === 0) {
      state.loop.start = currentAudio.currentTime;
      state.loop.end = 0;
    }
    if (note === 0x11 && channel === 0) setLoopEnabled(true, state.loop.start, currentAudio.currentTime);
    if (note === 0x4d && channel === 0) setLoopEnabled(!state.loop.enabled);
  }
  if (channel === 6 && [0x41, 0x46, 0x47].includes(note)) queueTrack(state.suggestion);
}

function handleMidiMessage(event) {
  const [status, data1, data2] = event.data;
  const type = status & 0xf0;
  const channel = status & 0x0f;
  state.controller.messages += 1;
  $("#hardwareStatus").textContent = `MIDI ACTIVE · ${state.controller.messages} RX`;
  if (type === 0x90 || type === 0x80) processMidiNote(channel, data1, type === 0x80 ? 0 : data2);
  if (type === 0xb0) processMidiControl(channel, data1, data2);
}

function attachMidiDevices() {
  const candidates = [...midiAccess.inputs.values()].filter(port => /DDJ[- ]?FLX4|Pioneer DJ/i.test(port.name));
  const outputs = [...midiAccess.outputs.values()].filter(port => /DDJ[- ]?FLX4|Pioneer DJ/i.test(port.name));
  document.querySelectorAll(".hardware-button").forEach(button => button.classList.toggle("connected", candidates.length > 0));
  if (!candidates.length) {
    state.controller.connected = false;
    $("#hardwareStatus").textContent = "NOT DETECTED · CHECK USB";
    return;
  }
  candidates.forEach(port => { port.onmidimessage = handleMidiMessage; });
  midiOutput = outputs[0] || null;
  state.controller.connected = true;
  state.controller.name = candidates[0].name;
  $("#hardwareName").textContent = candidates[0].name;
  $("#hardwareStatus").textContent = "MIDI CONNECTED · READY";
  setPlaybackState(state.playing, state.playing ? "FLX4 control active" : "FLX4 ready");
}

async function refreshAudioOutputs() {
  const select = $("#audioOutput");
  if (!navigator.mediaDevices?.enumerateDevices) return;
  const devices = (await navigator.mediaDevices.enumerateDevices()).filter(device => device.kind === "audiooutput");
  select.innerHTML = '<option value="">SYSTEM DEFAULT</option>' + devices.map((device, index) => `<option value="${device.deviceId}">${device.label || `AUDIO OUTPUT ${index + 1}`}</option>`).join("");
  const flx4 = devices.find(device => /DDJ[- ]?FLX4|Pioneer DJ/i.test(device.label));
  if (flx4) select.value = flx4.deviceId;
}

async function connectController() {
  const button = $("#connectController");
  if (!navigator.requestMIDIAccess) {
    button.classList.add("unsupported");
    $("#hardwareStatus").textContent = "OPEN IN CHROME FOR WEB MIDI";
    await refreshAudioOutputs();
    return;
  }
  try {
    $("#hardwareStatus").textContent = "REQUESTING MIDI ACCESS…";
    midiAccess = await navigator.requestMIDIAccess({ sysex: false });
    midiAccess.onstatechange = attachMidiDevices;
    attachMidiDevices();
    await refreshAudioOutputs();
    $("#audioOutput").dispatchEvent(new Event("change"));
  } catch (error) {
    button.classList.add("unsupported");
    $("#hardwareStatus").textContent = "MIDI PERMISSION DENIED";
    console.error(error);
  }
}

async function selectAudioOutput(deviceId) {
  try {
    const graph = ensureAudioGraph();
    if (graph?.context.setSinkId) {
      await graph.context.setSinkId(deviceId || "");
    } else if (audioPrimary.setSinkId) {
      await Promise.all([audioPrimary.setSinkId(deviceId), audioSecondary.setSinkId(deviceId)]);
    } else {
      throw new Error("Audio output selection requires Chrome 110+");
    }
    $("#hardwareStatus").textContent = deviceId ? "MIDI + USB AUDIO ROUTED" : "MIDI CONNECTED · SYSTEM AUDIO";
  } catch (error) {
    $("#hardwareStatus").textContent = "AUDIO ROUTE NEEDS CHROME";
    console.error(error);
  }
}

$("#trackRows").addEventListener("click", (event) => {
  const button = event.target.closest("[data-mix-id]");
  if (!button) return;
  queueTrack(tracks.find(track => String(track.id) === button.dataset.mixId));
});

$("#acceptSuggestion").addEventListener("click", () => queueTrack(state.suggestion));
$("#anotherSuggestion").addEventListener("click", () => {
  const options = tracks.filter(track => track.id !== state.current.id && track.id !== state.suggestion.id);
  setSuggestion(options[Math.floor(Math.random() * options.length)]);
});

$("#filterRow").addEventListener("click", (event) => {
  const filter = event.target.closest("[data-filter]");
  if (!filter) return;
  state.filter = filter.dataset.filter;
  document.querySelectorAll(".filter").forEach(button => button.classList.toggle("active", button === filter));
  renderRows();
});

$("#searchInput").addEventListener("input", (event) => { state.search = event.target.value; renderRows(); });
$("#heartButton").addEventListener("click", (event) => event.currentTarget.classList.toggle("liked"));
$("#playbackToggle").addEventListener("click", togglePlayback);
$("#deckAPlay").addEventListener("click", togglePlayback);
$("#deckACue").addEventListener("click", cueCurrentDeck);
$("#deckBCue").addEventListener("click", () => queueTrack(state.suggestion, false));
$("#deckBLoad").addEventListener("click", () => queueTrack(state.suggestion));
$("#approveNext").addEventListener("click", () => queueTrack(state.suggestion));
$("#rejectNext").addEventListener("click", () => cycleSuggestion(1));
$("#forceMix").addEventListener("click", forceSmartMixNow);
$("#connectController").addEventListener("click", connectController);
$("#audioOutput").addEventListener("change", event => selectAudioOutput(event.target.value));
$("#loopToggle").addEventListener("click", () => setLoopEnabled(!state.loop.enabled));
document.querySelectorAll("[data-loop-change]").forEach(button => button.addEventListener("click", () => {
  const sizes = [0.25, 0.5, 1, 2, 4, 8, 16, 32];
  const current = sizes.indexOf(state.loop.beats);
  state.loop.beats = sizes[Math.max(0, Math.min(sizes.length - 1, current + Number(button.dataset.loopChange)))];
  $("#loopSize").textContent = state.loop.beats;
  if (state.loop.enabled) {
    const beatSeconds = 60 / Math.max(1, Number(state.current?.bpm) || 120);
    setLoopEnabled(true, state.loop.start, state.loop.start + beatSeconds * state.loop.beats);
  }
}));
$("#hotCuePads").addEventListener("click", event => {
  const button = event.target.closest("[data-hotcue]");
  if (!button) return;
  const index = Number(button.dataset.hotcue);
  const cues = readHotCues();
  if (event.altKey || event.shiftKey) {
    cues[index] = null;
  } else if (Number.isFinite(cues[index])) {
    currentAudio.currentTime = Math.max(0, cues[index]);
  } else {
    cues[index] = currentAudio.currentTime || state.elapsed;
  }
  localStorage.setItem(hotCueStorageKey(), JSON.stringify(cues));
  updateHotCuePads();
});
document.querySelectorAll(".stem-toggles button").forEach(button => button.addEventListener("click", () => button.classList.toggle("active")));
document.querySelectorAll(".pro-mixer input[type=range]").forEach(input => {
  input.style.setProperty("--knob", input.value);
  input.addEventListener("input", () => setMixerValue(input.id, Number(input.value), { manual: input.id.startsWith("fader") || input.id === "crossfader" }));
});
document.querySelectorAll("[data-channel-cue]").forEach(button => button.addEventListener("click", () => button.classList.toggle("active")));
$("#mixerMode").parentElement.addEventListener("click", () => {
  state.mixer.manual = false;
  applyMixer();
});
[$("#deckASync"), $("#deckBSync")].forEach(button => button.addEventListener("click", () => {
  $("#smartToggle").checked = !$("#smartToggle").checked;
  $("#deckASync").classList.toggle("active", $("#smartToggle").checked);
  $("#deckBSync").classList.toggle("active", $("#smartToggle").checked);
  if ($("#smartToggle").checked && !state.mixJob && !state.preparedHandoff) prepareSmartMix(state.queued || state.suggestion);
  updateProgress();
}));
$("#smartToggle").addEventListener("change", () => {
  if ($("#smartToggle").checked && !state.mixJob && !state.preparedHandoff && !state.activeHandoff) {
    prepareSmartMix(state.queued || state.suggestion);
  }
  updateProgress();
});
$("#waveform").addEventListener("click", (event) => {
  const rect = event.currentTarget.getBoundingClientRect();
  state.elapsed = Math.round(((event.clientX - rect.left) / rect.width) * state.current.length);
  if (currentAudio.readyState >= 1) {
    const active = state.activeHandoff;
    if (!active) {
      currentAudio.currentTime = state.elapsed;
    } else if (active.promoted) {
      currentAudio.currentTime = Math.max(
        active.result.incoming_offset,
        active.result.incoming_offset + state.elapsed / active.result.transition.tempo_ratio_to - active.result.incoming_consumed,
      );
    } else {
      currentAudio.currentTime = Math.max(
        0,
        state.elapsed / active.result.transition.tempo_ratio_from - active.result.handoff_start,
      );
    }
  }
  updateProgress();
});

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault(); $("#searchInput").focus();
  }
});

setInterval(() => {
  state.sessionSeconds += 1;
  const hours = Math.floor(state.sessionSeconds / 3600);
  const minutes = Math.floor((state.sessionSeconds % 3600) / 60);
  const seconds = state.sessionSeconds % 60;
  $("#sessionTime").textContent = [hours, minutes, seconds].map(v => String(v).padStart(2, "0")).join(":");
  if (!state.playing) return;
  if (state.loop.enabled && currentAudio.currentTime >= state.loop.end) {
    currentAudio.currentTime = state.loop.start;
  }
  state.elapsed = logicalSourceSeconds();
  maybeJoinPreparedHandoff();
  const active = state.activeHandoff;
  transitioning = Boolean(active && !active.promoted && currentAudio.currentTime >= active.result.transition_offset);
  if (active && !active.promoted && currentAudio.currentTime >= active.result.incoming_offset) {
    promoteIncomingTrack();
  }
  updateProgress();
}, 1000);

audioPrimary.addEventListener("ended", () => { if (currentAudio === audioPrimary) playNextImmediately(); });
audioSecondary.addEventListener("ended", () => { if (currentAudio === audioSecondary) playNextImmediately(); });

window.addEventListener("resize", makeWaveform);

function bestNextTrack(current, excludedId = null) {
  const currentIdentity = `${current.title}|${current.artist}`.toLowerCase();
  const options = tracks.filter(track =>
    track.id !== current.id &&
    track.id !== excludedId &&
    `${track.title}|${track.artist}`.toLowerCase() !== currentIdentity
  );
  return options.sort((left, right) => {
    const leftDistance = current.bpm && left.bpm ? Math.abs(current.bpm - left.bpm) : 999;
    const rightDistance = current.bpm && right.bpm ? Math.abs(current.bpm - right.bpm) : 999;
    return leftDistance - rightDistance || right.match - left.match;
  })[0] || options[0];
}

function renderFilters() {
  const counts = tracks.reduce((result, track) => {
    result[track.genre] = (result[track.genre] || 0) + 1;
    return result;
  }, {});
  const popular = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 4);
  $("#filterRow").innerHTML = `
    <button class="filter active" data-filter="All">All tracks <span>${tracks.length}</span></button>
    ${popular.map(([genre, count]) => `<button class="filter" data-filter="${genre}">${genre} <span>${count}</span></button>`).join("")}
    <button class="filter-icon" aria-label="More filters">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M7 12h10M10 17h4"/></svg>
    </button>`;
}

async function loadCatalog() {
  try {
    const response = await fetch("/api/catalog", { cache: "no-store" });
    if (!response.ok) throw new Error(`catalog request failed: ${response.status}`);
    const catalog = await response.json();
    if (!catalog.tracks?.length) throw new Error("music folder contains no supported audio files");
    tracks = catalog.tracks;
    state.filter = "All";
    state.search = "";
    state.queued = null;
    state.mixJob = null;
    state.preparedHandoff = null;
    state.activeHandoff = null;
    $("#searchInput").value = "";
    const shortenedSource = catalog.source.replace(/^\/Users\/[^/]+/, "~");
    $("#catalogSource").textContent = `${catalog.count} tracks · ${shortenedSource}`;
    renderFilters();
    renderRows();

    const firstAnalyzed = tracks.find(track => track.cueOut && track.bpm && track.length)
      || tracks.find(track => track.bpm && track.length)
      || tracks[0];
    state.current = firstAnalyzed;
    state.masterBpm = Number(firstAnalyzed.bpm) || null;
    state.elapsed = 0;
    $("#currentTitle").textContent = firstAnalyzed.title;
    $("#currentArtist").textContent = firstAnalyzed.artist;
    $("#currentBpm").textContent = firstAnalyzed.bpm ?? "—";
    $("#currentKey").textContent = firstAnalyzed.key ?? "—";
    $("#currentCover").className = `cover cover-large cover-${firstAnalyzed.cover}`;
    $("#currentCover").innerHTML = firstAnalyzed.cover === 2 ? '<span class="moon"></span><span class="horizon"></span>' : "";
    $("#currentEnergy").outerHTML = energyBars(firstAnalyzed.energy).replace('<span class="energy-bars"', '<strong class="energy-bars" id="currentEnergy"').replace('</span>', '</strong>');
    const next = bestNextTrack(firstAnalyzed);
    setSuggestion(next);
    loadAudio(firstAnalyzed);
    setPlaybackState(false, "Ready to play");
    makeWaveform();
  } catch (error) {
    $("#catalogSource").textContent = "Local library unavailable — start with python3 ui/server.py";
    console.error(error);
    renderFilters();
    renderRows();
    makeWaveform();
    setSuggestion(state.suggestion);
  }
}

loadCatalog();
