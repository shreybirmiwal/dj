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
  controller: { connected: false, native: false, name: null, messages: 0 },
  cue: { available: false, channel: null, level: 0.7, device: null },
  libraryView: "all",
  deckOptions: { quantize: true, keyLock: true, slip: false, keySync: true, phraseSync: true },
};

const $ = (selector) => document.querySelector(selector);
const audioPrimary = $("#audioPrimary");
const audioSecondary = $("#audioSecondary");
let currentAudio = audioPrimary;
let transitioning = false;
let joiningHandoff = false;
let resumingCapsule = false;
let mixRequestGeneration = 0;
let audioGraph = null;
let midiAccess = null;
let midiOutput = null;
const automationGains = new WeakMap([[audioPrimary, 1], [audioSecondary, 1]]);
const midiMsb = new Map();
const waveformCache = new Map();
const waveformRequests = new Map();
const waveformLayerCache = new Map();
const lyricsCache = new Map();
const lyricsRequests = new Map();
const lyricsCheckedAt = new Map();
const lyricPollTimers = new Map();
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
  const progress = state.current?.length ? state.elapsed / state.current.length : 0;
  drawTechnicalWaveform($("#heroWave"), state.current, progress, ["#236f9e", "#42c9ad", "#bd61c9"]);
  updateProgress();
}

async function loadWaveform(track) {
  if (!track || waveformCache.has(String(track.id))) return;
  const key = String(track.id);
  if (waveformRequests.has(key)) return waveformRequests.get(key);
  const request = fetch(`/api/waveforms/${encodeURIComponent(key)}`)
    .then(response => response.ok ? response.json() : Promise.reject(new Error(`waveform ${response.status}`)))
    .then(data => {
      waveformCache.set(key, data);
      updatePerformanceConsole();
      return data;
    })
    .catch(error => console.error(error))
    .finally(() => waveformRequests.delete(key));
  waveformRequests.set(key, request);
  return request;
}

function lyricState(track) {
  return track ? lyricsCache.get(String(track.id)) : null;
}

async function loadLyrics(track, { force = false } = {}) {
  if (!track) return null;
  const key = String(track.id);
  const cached = lyricsCache.get(key);
  const checked = lyricsCheckedAt.get(key) || 0;
  if (!force && cached?.status === "ready") return cached;
  if (!force && cached && Date.now() - checked < 5000) return cached;
  if (lyricsRequests.has(key)) return lyricsRequests.get(key);
  const request = fetch(`/api/lyrics/${encodeURIComponent(key)}`, { cache: "no-store" })
    .then(response => response.ok ? response.json() : Promise.reject(new Error(`lyrics ${response.status}`)))
    .then(data => {
      lyricsCache.set(key, data);
      lyricsCheckedAt.set(key, Date.now());
      renderDeckLyrics("A", state.current, logicalSourceSeconds());
      renderDeckLyrics("B", state.queued || state.suggestion, Number((state.queued || state.suggestion)?.cueIn) || 0);
      if (["queued", "working"].includes(data.status)) scheduleLyricPoll(track);
      return data;
    })
    .catch(error => {
      console.error(error);
      const data = { status: "error", message: error.message, phrases: [], words: [] };
      lyricsCache.set(key, data);
      lyricsCheckedAt.set(key, Date.now());
      return data;
    })
    .finally(() => lyricsRequests.delete(key));
  lyricsRequests.set(key, request);
  return request;
}

function scheduleLyricPoll(track) {
  const key = String(track.id);
  if (lyricPollTimers.has(key)) return;
  const timer = window.setTimeout(async () => {
    lyricPollTimers.delete(key);
    const result = await loadLyrics(track, { force: true });
    if (["queued", "working"].includes(result?.status)) scheduleLyricPoll(track);
  }, 1400);
  lyricPollTimers.set(key, timer);
}

async function analyzeLyrics(track) {
  if (!track) return;
  const key = String(track.id);
  lyricsCache.set(key, { status: "queued", message: "Sending track to lyric analysis", progress: 2, phrases: [], words: [] });
  updatePerformanceConsole();
  try {
    const response = await fetch(`/api/lyrics/${encodeURIComponent(key)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wordModel: "base" }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not analyze lyrics");
    lyricsCache.set(key, result);
    lyricsCheckedAt.set(key, Date.now());
    updatePerformanceConsole();
    if (["queued", "working"].includes(result.status)) scheduleLyricPoll(track);
  } catch (error) {
    lyricsCache.set(key, { status: "error", message: error.message, phrases: [], words: [] });
    updatePerformanceConsole();
  }
}

function setTimestampedLyricLine(element, transcript, phrase, second) {
  element.replaceChildren();
  if (!phrase) {
    element.textContent = "—";
    return;
  }
  const words = (transcript.words || []).filter(word => word.start < phrase.end + 0.05 && word.end > phrase.start - 0.05);
  if (!words.length) {
    element.textContent = phrase.text;
    return;
  }
  words.forEach(word => {
    const token = document.createElement("span");
    token.textContent = word.word;
    token.title = `${formatDeckTime(word.start)} · ${Math.round((word.probability || 0) * 100)}%`;
    token.classList.toggle("sung", word.end < second);
    token.classList.toggle("active", word.start <= second && second <= word.end);
    element.append(token);
  });
}

function renderDeckLyrics(deck, track, second) {
  if (!track) return;
  const transcript = lyricState(track);
  const meta = $(`#deck${deck}LyricMeta`);
  const button = $(`#analyzeLyrics${deck}`);
  const previous = $(`#deck${deck}LyricPrevious`);
  const current = $(`#deck${deck}LyricCurrent`);
  const next = $(`#deck${deck}LyricNext`);
  const hint = $(`#deck${deck}LyricHint`);
  $(`#deck${deck}LyricTime`).textContent = `${deck === "B" ? "CUE " : ""}${formatDeckTime(second)}`;
  if (!transcript || transcript.status !== "ready") {
    const working = ["queued", "working"].includes(transcript?.status);
    meta.textContent = working
      ? `${(transcript.stage || "QUEUED").toUpperCase()} · ${transcript.progress || 0}%`
      : transcript?.status === "error" ? "ANALYSIS ERROR" : "NOT ANALYZED";
    button.hidden = working;
    button.textContent = transcript?.status === "error" ? "RETRY" : "ANALYZE";
    previous.textContent = "—";
    current.textContent = working ? (transcript.message || "Building timestamped machine transcript…") : "Run lyric analysis to expose words, phrases, and safe vocal boundaries.";
    next.textContent = "—";
    hint.textContent = working ? "AI PREPROCESSING RUNNING IN BACKGROUND" : "USED BY THE VOCAL HANDOFF PLANNER";
    return;
  }
  button.hidden = true;
  const phrases = transcript.phrases || [];
  let index = phrases.findIndex(phrase => phrase.start <= second && second <= phrase.end);
  if (index < 0) index = phrases.findIndex(phrase => phrase.end >= second);
  if (index < 0) index = Math.max(0, phrases.length - 1);
  const phrase = phrases[index];
  previous.textContent = index > 0 ? phrases[index - 1].text : "—";
  setTimestampedLyricLine(current, transcript, phrase, second);
  next.textContent = index + 1 < phrases.length ? phrases[index + 1].text : "—";
  const confidence = Math.round((transcript.confidence || 0) * 100);
  const source = transcript.source || transcript.model || "AI";
  meta.textContent = `${String(source).toUpperCase()} · ${confidence}% · ${phrases.length} LINES`;
  if (deck === "A") {
    const cleanExit = phrases.find(item => item.end >= second)?.end;
    hint.textContent = cleanExit == null ? "NO LATER LYRIC BOUNDARY" : `NEXT LYRIC EXIT ${formatDeckTime(cleanExit)}`;
  } else {
    const entry = phrases.find(item => item.start >= second)?.start;
    hint.textContent = entry == null ? "NO VOCAL ENTRY AFTER CUE" : `VOCAL ENTRY ${formatDeckTime(entry)} · +${Math.max(0, entry - second).toFixed(1)}S`;
  }
}

function paintTechnicalWaveform(context, width, height, track, waveform, palette, played) {
  const center = height / 2;
  context.fillStyle = "#06080b";
  context.fillRect(0, 0, width, height);

  const duration = Number(waveform?.duration || track.length || 0);
  const phraseSeconds = track.bpm ? (60 / track.bpm) * 32 : 16;
  if (duration > 0) {
    let phraseIndex = 0;
    for (let time = 0; time < duration; time += phraseSeconds) {
      const x = (time / duration) * width;
      const nextX = Math.min(width, ((time + phraseSeconds) / duration) * width);
      context.fillStyle = phraseIndex % 2 ? "rgba(92,115,145,.035)" : "rgba(255,255,255,.012)";
      context.fillRect(x, 0, nextX - x, height);
      context.fillStyle = phraseIndex % 4 === 0 ? "rgba(96,184,228,.34)" : "rgba(255,255,255,.11)";
      context.fillRect(Math.round(x), 0, 1, height);
      phraseIndex += 1;
    }
  }

  for (const [start, end] of waveform?.vocalSegments || []) {
    const x = (start / duration) * width;
    const segmentWidth = ((end - start) / duration) * width;
    context.fillStyle = "rgba(218,77,116,.2)";
    context.fillRect(x, 2, Math.max(1, segmentWidth), 5);
  }

  const bands = waveform.bands;
  const step = width / bands.length;
  for (let index = 0; index < bands.length; index += 1) {
    const [low, mid, high] = bands[index];
    const components = [low, mid, high].map(value => Math.max(0.35, value * center * 2.75));
    let inner = 0;
    const colors = played ? palette : ["#294353", "#31534f", "#40394e"];
    for (let band = 0; band < components.length; band += 1) {
      const outer = Math.min(center - 2, inner + components[band]);
      context.fillStyle = colors[band];
      context.fillRect(index * step, center - outer, Math.max(1, step + 0.35), outer - inner);
      context.fillRect(index * step, center + inner, Math.max(1, step + 0.35), outer - inner);
      inner = outer;
    }
  }

  context.fillStyle = "rgba(255,255,255,.14)";
  context.fillRect(0, center, width, 1);

  const marker = (seconds, color, label) => {
    if (!Number.isFinite(Number(seconds)) || !duration) return;
    const x = Math.max(0, Math.min(width - 1, (Number(seconds) / duration) * width));
    context.fillStyle = color;
    context.fillRect(x, 0, 1.5, height);
    context.font = "bold 8px IBM Plex Mono";
    context.fillText(label, Math.min(width - 28, x + 4), 17);
  };
  marker(track.cueIn, "#49d6b0", "CUE");
  marker(track.cueOut, "#ffad46", "MIX");
  if (track.id === state.current?.id) {
    readHotCues().forEach((seconds, index) => marker(seconds, "#cf6dff", `H${index + 1}`));
  }
}

function waveformLayer(canvas, track, waveform, palette, played) {
  const hotCues = track.id === state.current?.id ? JSON.stringify(readHotCues()) : "";
  const key = [track.id, canvas.width, canvas.height, palette.join("-"), played, hotCues, waveform.bands.length, waveform.vocalSegments?.length || 0].join(":");
  if (waveformLayerCache.has(key)) return waveformLayerCache.get(key);
  const layer = document.createElement("canvas");
  layer.width = canvas.width;
  layer.height = canvas.height;
  paintTechnicalWaveform(layer.getContext("2d"), layer.width, layer.height, track, waveform, palette, played);
  waveformLayerCache.set(key, layer);
  if (waveformLayerCache.size > 48) waveformLayerCache.delete(waveformLayerCache.keys().next().value);
  return layer;
}

function drawTechnicalWaveform(canvas, track, progress, palette) {
  if (!canvas || !track) return;
  const context = canvas.getContext("2d");
  const waveform = waveformCache.get(String(track.id));
  context.clearRect(0, 0, canvas.width, canvas.height);
  if (!waveform?.bands?.length) {
    context.fillStyle = "#06080b";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = "rgba(115,137,158,.38)";
    context.fillRect(0, canvas.height / 2 - 1, canvas.width, 2);
    context.font = "10px IBM Plex Mono";
    context.fillStyle = "#68727e";
    context.fillText("DECODING WAVEFORM…", 12, canvas.height / 2 - 10);
    loadWaveform(track);
    return;
  }
  context.drawImage(waveformLayer(canvas, track, waveform, palette, false), 0, 0);
  const playedWidth = Math.round(Math.max(0, Math.min(1, progress)) * canvas.width);
  if (playedWidth > 0) {
    context.save();
    context.beginPath();
    context.rect(0, 0, playedWidth, canvas.height);
    context.clip();
    context.drawImage(waveformLayer(canvas, track, waveform, palette, true), 0, 0);
    context.restore();
  }
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
  drawTechnicalWaveform($("#heroWave"), current, progress, ["#236f9e", "#42c9ad", "#bd61c9"]);
  $(".deck-a .playhead").style.left = `${progress * 100}%`;
  $(".hero-playhead").style.left = `${progress * 100}%`;
  $("#deckABeatgrid").style.setProperty("--deck-progress", `${progress * 100}%`);

  $("#deckBTitle").textContent = next.title;
  $("#deckBArtist").textContent = next.artist;
  $("#deckBKey").textContent = next.camelot || next.key || "—";
  $("#deckBBpm").textContent = next.bpm ? Number(next.bpm).toFixed(1) : "—";
  $("#deckBLength").textContent = `-${formatDeckTime(next.length || 0)}`;
  const delta = current.bpm && next.bpm ? ((state.masterBpm || current.bpm) / next.bpm - 1) * 100 : 0;
  $("#deckBTempoDelta").textContent = `${delta >= 0 ? "+" : ""}${delta.toFixed(1)}%`;
  drawTechnicalWaveform($("#deckBWave"), next, 0, ["#73519f", "#ad78e1", "#c785b9"]);
  const nextWaveform = waveformCache.get(String(next.id));
  $("#deckBGridInfo").textContent = next.bpm ? `GRID ${Number(next.bpm).toFixed(2)} BPM` : "GRID PENDING";
  $("#deckBVocalInfo").textContent = nextWaveform?.vocalSegments?.length
    ? `VOCALS ${nextWaveform.vocalSegments.length} REGIONS`
    : "VOCALS PENDING";
  loadWaveform(current);
  loadWaveform(next);
  renderDeckLyrics("A", current, logicalSourceSeconds());
  renderDeckLyrics("B", next, Math.max(0, Number(next.cueIn) || 0));
  loadLyrics(current);
  loadLyrics(next);

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

function compatibilityScore(track) {
  if (!state.current || track.id === state.current.id) return 0;
  let score = 30;
  if (track.bpm && state.current.bpm) score += Math.max(0, 35 - Math.abs(track.bpm - state.current.bpm) * 4);
  if (track.camelot && state.current.camelot) {
    const currentNumber = Number.parseInt(state.current.camelot, 10);
    const nextNumber = Number.parseInt(track.camelot, 10);
    const currentMode = state.current.camelot.slice(-1);
    const nextMode = track.camelot.slice(-1);
    const distance = Math.min(Math.abs(currentNumber - nextNumber), 12 - Math.abs(currentNumber - nextNumber));
    if (track.camelot === state.current.camelot) score += 30;
    else if (currentMode === nextMode && distance === 1) score += 24;
    else if (currentNumber === nextNumber && currentMode !== nextMode) score += 20;
  }
  score += Math.max(0, 8 - Math.abs((track.energy || 3) - (state.current.energy || 3)) * 3);
  return Math.max(0, Math.min(99, Math.round(score)));
}

function renderRows() {
  const visible = tracks.filter((track) => {
    const inFilter = state.filter === "All" || track.genre === state.filter;
    const query = state.search.toLowerCase();
    const analyzed = Boolean(track.bpm && track.camelot && track.length);
    const score = compatibilityScore(track);
    const inView = state.libraryView === "all"
      || (state.libraryView === "analyzed" && analyzed)
      || (state.libraryView === "energy" && track.energy >= 4)
      || (state.libraryView === "harmonic" && score >= 78)
      || (state.libraryView === "ready" && analyzed && track.cueIn != null && track.cueOut != null)
      || (state.libraryView === "pending" && !analyzed);
    return inFilter && inView && `${track.title} ${track.artist} ${track.genre}`.toLowerCase().includes(query);
  });
  $("#trackRows").innerHTML = visible.map((track, index) => `
    <tr data-id="${track.id}">
      <td><span class="track-number">${String(index + 1).padStart(2, "0")}</span></td>
      <td><div class="table-title-cell"><div class="cover cover-small cover-${track.cover}">${track.cover === 2 ? '<span class="moon"></span><span class="horizon"></span>' : ""}</div><span><strong>${track.title}</strong><small>${track.artist} · ${track.genre}</small></span></div></td>
      <td>${track.bpm ?? "—"}</td><td><span class="key-cell">${track.camelot ?? "—"}</span></td><td>${energyBars(track.energy, true)}</td>
      <td><span class="match-cell">${track.id === state.current.id ? "MASTER" : `${compatibilityScore(track)}%`}</span></td>
      <td><span class="analysis-cell ${track.bpm && track.camelot ? "ready" : "pending"}"><i></i>${track.bpm && track.camelot ? "READY" : "PENDING"}</span></td>
      <td>${track.length ? formatTime(track.length) : "—"}</td>
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
  loadWaveform(track);
  updatePerformanceConsole();
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
  prefetchLikelyNext(track);
  makeWaveform();
  renderRows();
}

function loadAudio(track, audio = currentAudio) {
  const target = new URL(track.mediaUrl, window.location.href).href;
  if (audio.src !== target) {
    audio.src = track.mediaUrl;
    audio.load();
  }
  const rate = state.masterBpm && track.bpm ? state.masterBpm / track.bpm : 1;
  audio.playbackRate = Math.max(0.5, Math.min(2, rate));
  audio.preservesPitch = true;
}

function setPlaybackState(playing, label) {
  state.playing = playing;
  $("#playbackToggle").classList.toggle("playing", playing);
  $("#deckAPlay").classList.toggle("playing", playing);
  $("#deckAPlay .transport-icon").textContent = playing ? "Ⅱ" : "▶";
  $("#playbackToggle").setAttribute("aria-label", playing ? "Pause continuous mix" : "Play continuous mix");
  $("#outputStatus").innerHTML = `<i></i> ${label}`;
  sendMidiFeedback([0x90, 0x0b, playing ? 0x7f : 0]);
}

function nativeDesktopApi() {
  return window.pywebview?.api || null;
}

function updateCueControls(message = null) {
  document.querySelectorAll("[data-channel-cue]").forEach(button => {
    button.classList.toggle("active", state.cue.channel === button.dataset.channelCue);
  });
  $("#masterCue").classList.toggle("active", state.cue.channel === "A");
  $("#cueRouteStatus").textContent = message || (state.cue.available
    ? (state.cue.channel ? `DECK ${state.cue.channel} → USB 3/4` : "USB 3/4 READY")
    : "CONNECT FLX4 USB");
}

async function stopHeadphoneCue() {
  const api = nativeDesktopApi();
  if (api) await api.stop_headphone_cue();
  state.cue.channel = null;
  updateCueControls();
}

async function toggleHeadphoneCue(channel) {
  const api = nativeDesktopApi();
  if (!api) {
    updateCueControls("NATIVE APP REQUIRED");
    return;
  }
  if (state.cue.channel === channel) {
    await stopHeadphoneCue();
    return;
  }
  const track = channel === "A" ? state.current : (state.queued || state.suggestion);
  if (!track) return;
  const offset = channel === "A" ? logicalSourceSeconds() : Math.max(0, Number(track.cueIn) || 0);
  updateCueControls("ROUTING CUE…");
  const result = await api.start_headphone_cue(String(track.id), offset, state.cue.level);
  if (!result.ok) {
    state.cue.channel = null;
    state.cue.available = false;
    updateCueControls(result.error || "CUE ROUTE FAILED");
    return;
  }
  state.cue.available = true;
  state.cue.channel = channel;
  state.cue.device = result.device;
  updateCueControls();
}

async function refreshNativeCueOutput() {
  const api = nativeDesktopApi();
  if (!api) return;
  const result = await api.list_audio_outputs();
  state.cue.available = Boolean(result.ok && result.available);
  state.cue.device = result.device || null;
  updateCueControls();
}

async function setHeadphoneLevel(value) {
  state.cue.level = Math.max(0, Math.min(1, Number(value)));
  $("#headphoneLevel").value = state.cue.level;
  $("#headphoneLevel").style.setProperty("--knob", state.cue.level);
  const api = nativeDesktopApi();
  if (api) await api.set_headphone_level(state.cue.level);
}

function sendMidiFeedback(data) {
  if (midiOutput) {
    midiOutput.send(data);
    return;
  }
  const api = nativeDesktopApi();
  if (api && state.controller.native) api.send_midi(data).catch(() => {});
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
    incoming.playbackRate = 1;
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
  if (active.result.capsule) {
    const liveDeck = currentAudio === audioPrimary ? audioSecondary : audioPrimary;
    loadAudio(active.nextTrack, liveDeck);
  }
  setPlaybackState(true, "Continuous smart output");
  prepareSmartMix(state.suggestion);
}

async function resumeOriginalAfterCapsule(force = false) {
  const active = state.activeHandoff;
  if (!active?.promoted || !active.result.capsule || resumingCapsule) return;
  const remaining = Math.max(0, Number(active.result.duration || currentAudio.duration) - currentAudio.currentTime);
  if (!force && remaining > 0.65) return;
  resumingCapsule = true;
  const capsule = currentAudio;
  const live = currentAudio === audioPrimary ? audioSecondary : audioPrimary;
  try {
    loadAudio(active.nextTrack, live);
    await waitForMetadata(live);
    const sourceSecond = logicalSourceSeconds();
    live.currentTime = Math.max(0, Math.min(sourceSecond, live.duration || sourceSecond));
    setAutomationGain(live, 0);
    await live.play();
    const started = performance.now();
    const fade = (now) => {
      if (!state.playing) {
        live.pause();
        resumingCapsule = false;
        return;
      }
      const position = Math.min(1, (now - started) / (JOIN_FADE_SECONDS * 1000));
      setAutomationGain(capsule, Math.cos(position * Math.PI / 2));
      setAutomationGain(live, Math.sin(position * Math.PI / 2));
      if (position < 1 && !capsule.ended) {
        requestAnimationFrame(fade);
        return;
      }
      capsule.pause();
      currentAudio = live;
      setAutomationGain(capsule, 0);
      setAutomationGain(live, 1);
      state.activeHandoff = null;
      state.elapsed = live.currentTime;
      resumingCapsule = false;
      setPlaybackState(true, "Continuous live deck output");
      updateProgress();
    };
    requestAnimationFrame(fade);
  } catch (error) {
    resumingCapsule = false;
    console.error(error);
    setPlaybackState(false, "Could not resume the live incoming deck");
  }
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

function beatJump(beats) {
  const secondsPerBeat = 60 / Math.max(1, Number(state.current?.bpm) || 120);
  const target = Math.max(0, Math.min(currentAudio.duration || state.current.length || Infinity, currentAudio.currentTime + Number(beats) * secondsPerBeat));
  currentAudio.currentTime = target;
  state.elapsed = logicalSourceSeconds();
  updateProgress();
}

function toggleDeckOption(name, button) {
  state.deckOptions[name] = !state.deckOptions[name];
  button.classList.toggle("active", state.deckOptions[name]);
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
  } else if (channel === 6 && controller === 0x0d) {
    setHeadphoneLevel(value);
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
    if (note === 0x54) toggleHeadphoneCue(channel === 0 ? "A" : "B");
  }
  if (channel === 6 && note === 0x63) toggleHeadphoneCue("A");
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
  const nativeApi = nativeDesktopApi();
  if (nativeApi) {
    try {
      $("#hardwareStatus").textContent = "CONNECTING NATIVE MIDI…";
      const result = await nativeApi.connect_controller();
      if (!result.ok) throw new Error(result.error || "DDJ-FLX4 not detected");
      state.controller.connected = true;
      state.controller.native = true;
      state.controller.name = result.name;
      button.classList.remove("unsupported");
      button.classList.add("connected");
      $("#hardwareName").textContent = result.name;
      $("#hardwareStatus").textContent = result.output ? "NATIVE MIDI I/O · READY" : "NATIVE MIDI INPUT · READY";
      setPlaybackState(state.playing, state.playing ? "FLX4 control active" : "FLX4 ready");
      await refreshAudioOutputs();
      await refreshNativeCueOutput();
      return;
    } catch (error) {
      state.controller.connected = false;
      button.classList.remove("connected");
      button.classList.add("unsupported");
      $("#hardwareStatus").textContent = "NOT DETECTED · CHECK USB";
      console.error(error);
      return;
    }
  }
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
    } else if (nativeDesktopApi() && !deviceId) {
      $("#hardwareStatus").textContent = state.controller.connected ? "NATIVE MIDI · SYSTEM AUDIO" : "SYSTEM AUDIO";
      return;
    } else {
      throw new Error("Audio output selection requires Chrome 110+");
    }
    $("#hardwareStatus").textContent = deviceId ? "MIDI + USB AUDIO ROUTED" : "MIDI CONNECTED · SYSTEM AUDIO";
  } catch (error) {
    $("#hardwareStatus").textContent = "AUDIO ROUTE NEEDS CHROME";
    console.error(error);
  }
}

async function initializeDesktopRuntime() {
  const api = nativeDesktopApi();
  if (!api) return;
  state.controller.native = true;
  document.body.classList.add("desktop-runtime");
  try {
    const info = await api.runtime_info();
    $("#runtimeBadge").textContent = `${String(info.platform).toUpperCase()} DESKTOP`;
    await refreshNativeCueOutput();
    const devices = await api.list_midi_devices();
    if (devices.ok && devices.flx4Inputs?.length) {
      await connectController();
    } else {
      $("#hardwareStatus").textContent = "NATIVE MIDI · AWAITING USB";
    }
  } catch (error) {
    $("#hardwareStatus").textContent = "NATIVE BRIDGE ERROR";
    console.error(error);
  }
}

async function pollNativeController() {
  const api = nativeDesktopApi();
  if (!api || pollNativeController.running) return;
  pollNativeController.running = true;
  try {
    const devices = await api.list_midi_devices();
    const detected = Boolean(devices.ok && devices.flx4Inputs?.length);
    if (detected && !state.controller.connected) {
      await connectController();
    } else if (!detected && state.controller.connected) {
      state.controller.connected = false;
      await stopHeadphoneCue();
      state.cue.available = false;
      $("#connectController").classList.remove("connected");
      $("#connectController").classList.add("unsupported");
      $("#hardwareStatus").textContent = "NATIVE MIDI · AWAITING USB";
    }
  } catch (error) {
    console.error(error);
  } finally {
    pollNativeController.running = false;
  }
}
pollNativeController.running = false;

window.addEventListener("setmix-midi", event => handleMidiMessage({ data: event.detail }));
window.addEventListener("pywebviewready", initializeDesktopRuntime);

$("#trackRows").addEventListener("click", (event) => {
  const button = event.target.closest("[data-mix-id]");
  if (button) {
    queueTrack(tracks.find(track => String(track.id) === button.dataset.mixId));
    return;
  }
  const row = event.target.closest("tr[data-id]");
  if (!row) return;
  document.querySelectorAll("#trackRows tr").forEach(item => item.classList.toggle("focused", item === row));
  const track = tracks.find(item => String(item.id) === row.dataset.id);
  if (track && track.id !== state.current.id) setSuggestion(track);
});
$("#trackRows").addEventListener("dblclick", event => {
  const row = event.target.closest("tr[data-id]");
  const track = row && tracks.find(item => String(item.id) === row.dataset.id);
  if (track) queueTrack(track);
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
$("#deckBCue").addEventListener("click", () => toggleHeadphoneCue("B"));
$("#deckBLoad").addEventListener("click", () => queueTrack(state.suggestion));
$("#approveNext").addEventListener("click", () => queueTrack(state.suggestion));
$("#rejectNext").addEventListener("click", () => cycleSuggestion(1));
$("#forceMix").addEventListener("click", forceSmartMixNow);
$("#connectController").addEventListener("click", connectController);
$("#audioOutput").addEventListener("change", event => selectAudioOutput(event.target.value));
$("#loopToggle").addEventListener("click", () => setLoopEnabled(!state.loop.enabled));
$("#lyricsViewToggle").addEventListener("click", event => {
  const expanded = $(".performance-console").classList.toggle("lyrics-expanded");
  event.currentTarget.classList.toggle("active", expanded);
});
$("#analyzeLyricsA").addEventListener("click", () => analyzeLyrics(state.current));
$("#analyzeLyricsB").addEventListener("click", () => analyzeLyrics(state.queued || state.suggestion));
document.querySelectorAll("[data-beat-jump]").forEach(button => button.addEventListener("click", () => beatJump(button.dataset.beatJump)));
$("#quantizeToggle").addEventListener("click", event => toggleDeckOption("quantize", event.currentTarget));
$("#keyLockToggle").addEventListener("click", event => toggleDeckOption("keyLock", event.currentTarget));
$("#slipToggle").addEventListener("click", event => toggleDeckOption("slip", event.currentTarget));
$("#keySyncToggle").addEventListener("click", event => toggleDeckOption("keySync", event.currentTarget));
$("#phraseSyncToggle").addEventListener("click", event => toggleDeckOption("phraseSync", event.currentTarget));
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
document.querySelectorAll("[data-channel-cue]").forEach(button => button.addEventListener("click", () => toggleHeadphoneCue(button.dataset.channelCue)));
$("#masterCue").addEventListener("click", () => toggleHeadphoneCue("A"));
$("#headphoneLevel").addEventListener("input", event => setHeadphoneLevel(event.target.value));
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
$(".collection-tree").addEventListener("click", event => {
  const button = event.target.closest("[data-library-view]");
  if (!button) return;
  state.libraryView = button.dataset.libraryView;
  document.querySelectorAll(".collection-tree [data-library-view]").forEach(item => item.classList.toggle("active", item === button));
  renderRows();
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
  resumeOriginalAfterCapsule();
  updateProgress();
}, 1000);

setInterval(pollNativeController, 3000);

function handleAudioEnded(audio) {
  if (currentAudio !== audio) return;
  if (state.activeHandoff?.promoted && state.activeHandoff.result.capsule) {
    resumeOriginalAfterCapsule(true);
    return;
  }
  playNextImmediately();
}

audioPrimary.addEventListener("timeupdate", () => { if (currentAudio === audioPrimary) resumeOriginalAfterCapsule(); });
audioSecondary.addEventListener("timeupdate", () => { if (currentAudio === audioSecondary) resumeOriginalAfterCapsule(); });
audioPrimary.addEventListener("ended", () => handleAudioEnded(audioPrimary));
audioSecondary.addEventListener("ended", () => handleAudioEnded(audioSecondary));

window.addEventListener("resize", makeWaveform);

function bestNextTrack(current, excludedId = null) {
  return likelyNextTracks(current, excludedId, 1)[0];
}

function likelyNextTracks(current, excludedId = null, limit = 3) {
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
  }).slice(0, limit);
}

function prefetchLikelyNext(current) {
  if (!current) return;
  const candidates = likelyNextTracks(current, null, 3);
  candidates.forEach(track => {
    loadWaveform(track);
    loadLyrics(track);
  });
  fetch("/api/prefetch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fromId: current.id, trackIds: candidates.map(track => track.id) }),
  }).catch(error => console.debug("prefetch unavailable", error));
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
  const analyzed = tracks.filter(track => track.bpm && track.camelot).length;
  $("#analysisCount").textContent = `${analyzed} / ${tracks.length} READY`;
  $("#treeAllCount").textContent = tracks.length;
  $("#treeAnalyzedCount").textContent = analyzed;
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
    prefetchLikelyNext(firstAnalyzed);
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
