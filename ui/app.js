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
};

const $ = (selector) => document.querySelector(selector);
const audioPrimary = $("#audioPrimary");
const audioSecondary = $("#audioSecondary");
let currentAudio = audioPrimary;
let transitioning = false;
let joiningHandoff = false;
let mixRequestGeneration = 0;
const JOIN_FADE_SECONDS = 0.35;
const formatTime = (seconds) => {
  const value = Math.max(0, Math.floor(seconds));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
};

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
  $("#playbackToggle").setAttribute("aria-label", playing ? "Pause continuous mix" : "Play continuous mix");
  $("#outputStatus").innerHTML = `<i></i> ${label}`;
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
    incoming.volume = 0;
    await incoming.play();
    setPlaybackState(true, `Full smart mix ready for ${prepared.nextTrack.title}`);
    const started = performance.now();
    const fade = (now) => {
      if (!state.playing) {
        outgoing.volume = 1;
        incoming.pause();
        incoming.volume = 1;
        joiningHandoff = false;
        return;
      }
      const position = Math.min(1, (now - started) / (JOIN_FADE_SECONDS * 1000));
      outgoing.volume = Math.cos(position * Math.PI / 2);
      incoming.volume = Math.sin(position * Math.PI / 2);
      if (position < 1) {
        requestAnimationFrame(fade);
        return;
      }
      outgoing.pause();
      outgoing.volume = 1;
      incoming.volume = 1;
      currentAudio = incoming;
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
    incoming.volume = 1;
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
