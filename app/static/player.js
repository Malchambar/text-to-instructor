// Player: gets the narration script up front, then renders each segment's audio
// lazily (with a little prefetch ahead) so playback starts fast. Shows each
// segment's diagram, stops at content-review questions, and can switch voice live.

const $ = (id) => document.getElementById(id);

const els = {
  narrate: $("narrate"),
  voice: $("voice"),
  speed: $("speed"),
  vision: $("vision"),
  writer: $("writer"),
  status: $("status"),
  stage: $("stage"),
  diagram: $("diagram"),
  caption: $("caption"),
  showcard: $("showcard"),
  transcript: $("transcript"),
  pausenote: $("pausenote"),
  player: $("player"),
  prev: $("prev"),
  next: $("next"),
  playpause: $("playpause"),
  seek: $("seek"),
  curTime: $("cur-time"),
  durTime: $("dur-time"),
  segCounter: $("seg-counter"),
  audio: $("audio"),
  autoadvance: $("autoadvance"),
  theme: $("theme"),
  storage: $("storage"),
  hint: $("hint"),
};

// Show how much local disk this session's generated audio/diagrams occupy
// (cleared on exit). Empty when nothing is cached yet.
async function refreshStorage() {
  try {
    const r = await fetch("/api/storage");
    const d = await r.json();
    els.storage.textContent = d.files ? `🗄 ${d.human}` : "";
    els.storage.title = d.files
      ? `${d.files} file(s) cached locally this session — cleared automatically when the app exits`
      : "";
  } catch (e) {}
}
refreshStorage();
setInterval(refreshStorage, 8000);

let lesson = null;
let cur = 0;
let audioUrls = []; // per-segment audio URL cache (cleared on new lesson / voice change)
let advanceOnPlay = false; // after a paused (content-review) segment, Play goes to next
let narrateController = null; // AbortController while a lesson is being prepared
let scrubbing = false; // user is dragging the seek bar (don't let timeupdate fight it)

// Back-button: if we're more than this many seconds into the current segment,
// the first press restarts it; pressing again near the start jumps to the previous.
const PREV_RESTART_THRESHOLD = 2.5;

const isTyping = (t) =>
  t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.tagName === "SELECT" || t.isContentEditable);

// Always return a finite, positive playback rate (the dropdown can be blank).
function currentSpeed() {
  const v = parseFloat(els.speed.value);
  return Number.isFinite(v) && v > 0 ? v : 1.0;
}

// Select the dropdown option matching a numeric speed, else default to 1.0x.
function applySpeedDefault(v) {
  const match = [...els.speed.options].find((o) => parseFloat(o.value) === parseFloat(v));
  els.speed.value = match ? match.value : "1.0";
}

function setStatus(msg, isError = false, loading = false) {
  els.status.classList.toggle("error", isError);
  els.status.textContent = "";
  if (loading) {
    const sp = document.createElement("span");
    sp.className = "spinner";
    els.status.appendChild(sp);
  }
  els.status.appendChild(document.createTextNode(msg || ""));
}

// While a lesson is building, poll the server for its current stage and narrate
// it to the user (Reading the page → Writing the lesson → …).
let progressTimer = null;
async function pollProgress() {
  try {
    const r = await fetch("/api/progress");
    const p = await r.json();
    if (p.label) setStatus(p.label, false, true);
  } catch (e) {}
}
function startProgress() {
  stopProgress();
  progressTimer = setInterval(pollProgress, 450);
}
function stopProgress() {
  if (progressTimer) clearInterval(progressTimer);
  progressTimer = null;
}

// mm:ss for the seek-bar time labels.
function fmtTime(s) {
  if (!Number.isFinite(s) || s < 0) s = 0;
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

// Paint the played portion of the scrub bar (left of the thumb) in the accent colour.
function paintSeek() {
  const max = parseFloat(els.seek.max) || 1;
  const pct = Math.max(0, Math.min(100, (parseFloat(els.seek.value) / max) * 100));
  els.seek.style.background =
    `linear-gradient(to right, var(--accent) 0%, var(--accent) ${pct}%, var(--border-2) ${pct}%, var(--border-2) 100%)`;
}

// Reset the scrub bar to the start of a fresh segment.
function resetSeek() {
  els.seek.value = 0;
  els.seek.max = 100;
  els.curTime.textContent = "0:00";
  els.durTime.textContent = "0:00";
  paintSeek();
}

// Render (server-side, cached) and cache the URL for one segment's audio.
async function ensureAudio(i) {
  if (i < 0 || !lesson || i >= lesson.segments.length) return null;
  if (audioUrls[i]) return audioUrls[i];
  const res = await fetch(`/api/segment_audio/${i}`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || `Audio failed (${res.status})`);
  audioUrls[i] = `/audio/${data.audio_path}`;
  return audioUrls[i];
}

// Warm upcoming segments in the background so they're ready when we get there.
function prefetch(i) {
  ensureAudio(i).catch(() => {});
}

async function narrate() {
  narrateController = new AbortController();
  els.narrate.textContent = "■ Stop";
  els.stage.classList.add("hidden");
  els.player.classList.add("hidden");
  setStatus("Starting…", false, true);
  startProgress(); // narrate each build stage as the server reports it

  try {
    const res = await fetch("/api/narrate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        voice: els.voice.value,
        vision: els.vision.value || null,
        writer: els.writer.value || null,
      }),
      signal: narrateController.signal,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);

    stopProgress();
    lesson = data;
    audioUrls = [];
    if (!lesson.segments || lesson.segments.length === 0) {
      throw new Error("No narration was produced for this page.");
    }
    cur = 0;
    els.stage.classList.remove("hidden");
    els.player.classList.remove("hidden");
    els.hint.classList.remove("hidden");
    // "Save diagrams" export disabled (see index.html); preserved for opt-in builds:
    // els.savediagrams.classList.toggle("hidden", !(lesson.diagrams && lesson.diagrams.length));
    setStatus("Preparing the voice…", false, true);
    await loadSegment(0, true); // awaits the first segment's audio synthesis
    setStatus(`${lesson.title || lesson.url} — ${lesson.segments.length} segments`);
    refreshStorage();
  } catch (e) {
    setStatus(e.name === "AbortError" ? "Stopped." : e.message, e.name !== "AbortError");
  } finally {
    stopProgress();
    narrateController = null;
    els.narrate.textContent = "▶ Teach this page";
  }
}

function stopNarrate() {
  if (narrateController) narrateController.abort(); // ends the wait on our side
  fetch("/api/stop", { method: "POST" }).catch(() => {}); // kills engine subprocesses
}

function diagramFor(seg) {
  if (seg.image_idx === null || seg.image_idx === undefined) return null;
  return (lesson.diagrams || []).find((d) => d.idx === seg.image_idx) || null;
}

async function loadSegment(i, autoplay) {
  if (!lesson || i < 0 || i >= lesson.segments.length) return;
  cur = i;
  advanceOnPlay = false;
  const seg = lesson.segments[i];

  const dia = diagramFor(seg);
  const show = (seg.show || "").trim();
  if (dia) {
    els.diagram.src = `/diagrams/${dia.png_path}`;
    els.caption.textContent = dia.alt || dia.context || "";
    els.diagram.classList.remove("hidden");
  } else {
    els.diagram.classList.add("hidden");
    els.caption.textContent = "";
  }
  // When there's no diagram but the segment has on-screen text (an example,
  // scenario, definition...), show it as a card so it's visible, not just spoken.
  if (!dia && show) {
    els.showcard.textContent = show;
    els.showcard.classList.remove("hidden");
  } else {
    els.showcard.classList.add("hidden");
  }

  els.transcript.textContent = seg.speak;
  els.pausenote.classList.toggle("hidden", !seg.pause);

  // Subtle fade as each segment comes up.
  els.stage.classList.remove("fade");
  void els.stage.offsetWidth;
  els.stage.classList.add("fade");
  els.segCounter.textContent = `${i + 1} / ${lesson.segments.length}`;
  resetSeek();
  els.audio.playbackRate = currentSpeed();

  try {
    const url = await ensureAudio(i);
    if (cur !== i) return; // user moved on while audio was loading
    els.audio.src = url;
    els.audio.playbackRate = currentSpeed();
    if (autoplay) els.audio.play().catch(() => {});
    refreshStorage(); // a new segment's audio was just cached to disk
  } catch (e) {
    setStatus(e.message, true);
  }

  // Warm the next couple of segments.
  prefetch(i + 1);
  prefetch(i + 2);
}

// Media-player back button: first press restarts the current segment; pressing
// again while still near the start steps back to the previous segment (which then
// plays from its start, so rapid presses walk backwards segment by segment).
function goPrev() {
  if (cur > 0 && els.audio.currentTime <= PREV_RESTART_THRESHOLD) {
    loadSegment(cur - 1, true);
  } else {
    els.audio.currentTime = 0;
    if (els.audio.paused) els.audio.play().catch(() => {});
  }
}

function togglePlay() {
  // After a content-review pause, Play continues to the next segment.
  if (advanceOnPlay && els.audio.paused) {
    advanceOnPlay = false;
    loadSegment(cur + 1, true);
    return;
  }
  if (els.audio.paused) els.audio.play().catch(() => {});
  else els.audio.pause();
}

async function changeVoice() {
  if (!lesson) return; // nothing loaded yet; dropdown applies at next Narrate
  const wasPlaying = !els.audio.paused;
  els.audio.pause();
  setStatus("Switching voice…");
  try {
    const res = await fetch("/api/revoice", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice: els.voice.value }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Re-voice failed (${res.status})`);
    audioUrls = []; // force re-fetch in the new voice
    setStatus(`${lesson.title || lesson.url} — ${lesson.segments.length} segments`);
    loadSegment(cur, wasPlaying);
  } catch (e) {
    setStatus(e.message, true);
  }
}

els.audio.addEventListener("ended", () => {
  const seg = lesson && lesson.segments[cur];
  if (!seg) return;
  const hasNext = cur < lesson.segments.length - 1;
  if (seg.pause && hasNext) {
    // Content-review question: don't auto-advance; let the next Play continue.
    advanceOnPlay = true;
  } else if (els.autoadvance.checked && hasNext) {
    loadSegment(cur + 1, true);
  }
});
els.audio.addEventListener("play", () => (els.playpause.textContent = "⏸"));
els.audio.addEventListener("pause", () => (els.playpause.textContent = "▶"));

// Keep the scrub bar and time labels in sync with playback.
els.audio.addEventListener("loadedmetadata", () => {
  const d = els.audio.duration;
  els.seek.max = Number.isFinite(d) && d > 0 ? d : 100;
  els.durTime.textContent = fmtTime(d);
  paintSeek();
});
els.audio.addEventListener("timeupdate", () => {
  if (scrubbing) return;
  els.seek.value = els.audio.currentTime;
  els.curTime.textContent = fmtTime(els.audio.currentTime);
  paintSeek();
});

// Drag to scrub/rewind within the current segment.
els.seek.addEventListener("input", () => {
  scrubbing = true;
  els.curTime.textContent = fmtTime(parseFloat(els.seek.value));
  paintSeek();
});
els.seek.addEventListener("change", () => {
  els.audio.currentTime = parseFloat(els.seek.value) || 0;
  scrubbing = false;
});

els.narrate.addEventListener("click", () => (narrateController ? stopNarrate() : narrate()));
// "Save diagrams" export disabled (see index.html); preserved for opt-in builds:
// els.savediagrams.addEventListener("click", () => (window.location.href = "/api/diagrams.zip"));
els.playpause.addEventListener("click", togglePlay);

// Light / dark theme toggle (persisted; applied early in <head> to avoid a flash).
function applyThemeIcon() {
  const light = document.documentElement.getAttribute("data-theme") === "light";
  els.theme.textContent = light ? "☀️" : "🌙";
}
els.theme.addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("t2i_theme", next); } catch (e) {}
  applyThemeIcon();
});
applyThemeIcon();
els.prev.addEventListener("click", goPrev);
els.next.addEventListener("click", () => loadSegment(cur + 1, true));
els.voice.addEventListener("change", changeVoice);
els.speed.addEventListener("change", () => {
  els.audio.playbackRate = currentSpeed();
  savePrefs();
});

// Persist the user's selections to a local file so they survive restarts.
function savePrefs() {
  fetch("/api/preferences", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      voice: els.voice.value,
      speed: currentSpeed(),
      vision: els.vision.value,
      writer: els.writer.value,
      auto_advance: els.autoadvance.checked,
    }),
  }).catch(() => {});
}

els.voice.addEventListener("change", savePrefs);
els.vision.addEventListener("change", savePrefs);
els.writer.addEventListener("change", savePrefs);
els.autoadvance.addEventListener("change", savePrefs);

// Spacebar = play/pause when a lesson is loaded — but never while typing (e.g. chat).
document.addEventListener("keydown", (e) => {
  if (e.code === "Space" && lesson && !isTyping(e.target)) {
    e.preventDefault();
    togglePlay();
  }
});

// Restore the user's saved preferences (falls back to server defaults).
fetch("/api/preferences")
  .then((r) => r.json())
  .then((p) => {
    if (p.voice) els.voice.value = p.voice;
    if (p.speed) applySpeedDefault(p.speed);
    if (p.vision) els.vision.value = p.vision;
    if (p.writer) els.writer.value = p.writer;
    if (typeof p.auto_advance === "boolean") els.autoadvance.checked = p.auto_advance;
  })
  .catch(() => {});
