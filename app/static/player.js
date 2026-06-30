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
  restart: $("restart"),
  seek: $("seek"),
  curTime: $("cur-time"),
  durTime: $("dur-time"),
  segCounter: $("seg-counter"),
  audio: $("audio"),
  autoadvance: $("autoadvance"),
  theme: $("theme"),
  storage: $("storage"),
  hint: $("hint"),
  stepMode: $("step-mode"),
  jumpBtn: $("jump-step"),
  slidePrev: $("slide-prev"),
  slideNext: $("slide-next"),
  slideDots: $("slide-dots"),
  videoOverlay: $("video-overlay"),
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
let slideDias = []; // step-mode: the current segment's image group (slideshow)
let slideIdx = 0;
let slideManual = false; // user clicked the slideshow arrows — stop auto-advancing

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
        step_mode: els.stepMode ? els.stepMode.value : "auto",
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

// The image group for a segment: step-mode segments carry image_idxs (a
// slideshow); older/freeform segments carry a single image_idx.
function segImages(seg) {
  const idxs =
    seg.image_idxs && seg.image_idxs.length
      ? seg.image_idxs
      : seg.image_idx !== null && seg.image_idx !== undefined
      ? [seg.image_idx]
      : [];
  const byIdx = {};
  (lesson.diagrams || []).forEach((d) => (byIdx[d.idx] = d));
  return idxs.map((i) => byIdx[i]).filter(Boolean);
}

function stepLabel(seg) {
  if (seg && seg.step_idx != null && lesson.steps && lesson.steps[seg.step_idx]) {
    const st = lesson.steps[seg.step_idx];
    return `${st.number} ${st.title}`.trim();
  }
  return "";
}

function renderDots() {
  if (!els.slideDots) return;
  els.slideDots.innerHTML = "";
  if (slideDias.length < 2) return;
  slideDias.forEach((_, i) => {
    const dot = document.createElement("span");
    dot.className = "dot" + (i === slideIdx ? " on" : "");
    els.slideDots.appendChild(dot);
  });
}

function showSlide(k) {
  if (!slideDias.length) return;
  slideIdx = Math.max(0, Math.min(slideDias.length - 1, k));
  const d = slideDias[slideIdx];
  els.diagram.src = `/diagrams/${d.png_path}`;
  const seg = lesson.segments[cur];
  const label = stepLabel(seg) || d.alt || d.context || "";
  els.caption.textContent =
    slideDias.length > 1 ? `${label}  ·  ${slideIdx + 1}/${slideDias.length}` : label;
  // Video posters get a "watch on the original page" overlay instead of a still.
  if (els.videoOverlay) {
    if (d.is_video) {
      els.videoOverlay.dataset.anchor = d.anchor || "";
      els.videoOverlay.classList.remove("hidden");
    } else {
      els.videoOverlay.classList.add("hidden");
    }
  }
  renderDots();
}

function esc(s) {
  return String(s).replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

// Render a text-only "show" card with light visual structure. The writer puts a
// list under a header line ending in ":" and separates any closing summary with
// a BLANK LINE. So: split into blank-line blocks; in a block, a ":" first line
// is the title and EVERY other line is a bullet (we don't try to guess which
// "items" are really sentences — they're all list items). Later blocks are
// closing notes; a lone sentence with no list stays a plain paragraph.
function renderShow(text) {
  const ul = (items) =>
    "<ul>" + items.map((i) => `<li>${esc(i)}</li>`).join("") + "</ul>";
  const blocks = String(text)
    .split(/\n\s*\n/)
    .map((b) => b.trim())
    .filter(Boolean);
  let html = "";
  blocks.forEach((block, bi) => {
    const lines = block
      .split(/\r?\n/)
      .map((l) => l.replace(/^[-•*]\s+/, "").trim())
      .filter(Boolean);
    if (!lines.length) return;
    const header = lines[0].endsWith(":") ? lines[0] : null;
    const items = header ? lines.slice(1) : lines;
    if (header) {
      html += `<div class="show-title">${esc(header)}</div>`;
      if (items.length) html += ul(items);
    } else if (bi === 0 && lines.length === 1) {
      html += `<p class="show-plain">${esc(lines[0])}</p>`;
    } else if (bi === 0) {
      html += ul(lines); // first block, multiple lines, no header => a list
    } else {
      lines.forEach((l) => (html += `<p class="show-note">${esc(l)}</p>`));
    }
  });
  return html || `<p class="show-plain">${esc(text)}</p>`;
}

async function loadSegment(i, autoplay) {
  if (!lesson || i < 0 || i >= lesson.segments.length) return;
  cur = i;
  advanceOnPlay = false;
  const seg = lesson.segments[i];

  const dias = segImages(seg);
  slideDias = dias;
  slideIdx = 0;
  slideManual = false;
  const show = (seg.show || "").trim();
  if (dias.length) {
    showSlide(0);
    els.diagram.classList.remove("hidden");
    els.showcard.classList.add("hidden");
  } else {
    els.diagram.classList.add("hidden");
    els.caption.textContent = "";
    if (els.videoOverlay) els.videoOverlay.classList.add("hidden"); // text slide: no video overlay
    // No diagram but on-screen text (example/scenario/definition): show a card.
    if (show) {
      els.showcard.innerHTML = renderShow(show);
      els.showcard.classList.remove("hidden");
    } else {
      els.showcard.classList.add("hidden");
    }
  }
  // Slideshow arrows/dots only when a step has multiple images.
  const multi = dias.length > 1;
  if (els.slidePrev) els.slidePrev.classList.toggle("hidden", !multi);
  if (els.slideNext) els.slideNext.classList.toggle("hidden", !multi);
  if (els.slideDots) els.slideDots.classList.toggle("hidden", !multi);
  // "Open this step on the page" — only step-mode segments carry an anchor.
  if (els.jumpBtn) {
    if (seg.source_anchor) {
      els.jumpBtn.dataset.anchor = seg.source_anchor;
      els.jumpBtn.classList.remove("hidden");
    } else {
      els.jumpBtn.classList.add("hidden");
    }
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
  // Step-mode slideshow: spread the step's images across its audio so they
  // auto-advance with the narration (unless the user is flipping manually).
  if (!slideManual && slideDias.length > 1) {
    const d = els.audio.duration;
    if (Number.isFinite(d) && d > 0) {
      const k = Math.min(
        slideDias.length - 1,
        Math.floor((els.audio.currentTime / d) * slideDias.length)
      );
      if (k !== slideIdx) showSlide(k);
    }
  }
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

// Start the whole lesson over: go to segment 1, paused, and wait for Play.
function restartLesson() {
  if (!lesson || !(lesson.segments || []).length) return;
  advanceOnPlay = false;
  els.audio.pause(); // fires "pause" -> playpause shows ▶
  loadSegment(0, false); // load the first segment without auto-playing
}
els.restart.addEventListener("click", restartLesson);

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

// Step-mode slideshow arrows: manual flip pauses auto-advance for this segment.
if (els.slidePrev)
  els.slidePrev.addEventListener("click", () => {
    slideManual = true;
    showSlide(slideIdx - 1);
  });
if (els.slideNext)
  els.slideNext.addEventListener("click", () => {
    slideManual = true;
    showSlide(slideIdx + 1);
  });

// "Open this step on the page" — scroll the existing lesson tab to the step.
if (els.jumpBtn)
  els.jumpBtn.addEventListener("click", () => {
    const anchor = els.jumpBtn.dataset.anchor;
    if (!anchor) return;
    fetch("/api/open_step", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ anchor }),
    }).catch(() => {});
  });

// "Watch this video on the original page" — bring the source tab to the front
// (and scroll to the video if we captured an anchor). It plays in the source
// page, never in this player.
if (els.videoOverlay)
  els.videoOverlay.addEventListener("click", () => {
    fetch("/api/open_step", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ anchor: els.videoOverlay.dataset.anchor || "" }),
    }).catch(() => {});
  });

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
      step_mode: els.stepMode ? els.stepMode.value : "auto",
    }),
  }).catch(() => {});
}

els.voice.addEventListener("change", savePrefs);
els.vision.addEventListener("change", savePrefs);
els.writer.addEventListener("change", savePrefs);
els.autoadvance.addEventListener("change", savePrefs);
if (els.stepMode) els.stepMode.addEventListener("change", savePrefs);

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
    if (p.step_mode && els.stepMode) els.stepMode.value = p.step_mode;
  })
  .catch(() => {});
