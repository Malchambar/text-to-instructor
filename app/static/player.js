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
  segCounter: $("seg-counter"),
  audio: $("audio"),
  autoadvance: $("autoadvance"),
};

let lesson = null;
let cur = 0;
let audioUrls = []; // per-segment audio URL cache (cleared on new lesson / voice change)
let advanceOnPlay = false; // after a paused (content-review) segment, Play goes to next

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

function setStatus(msg, isError = false) {
  els.status.textContent = msg || "";
  els.status.classList.toggle("error", isError);
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
  els.narrate.disabled = true;
  els.stage.classList.add("hidden");
  els.player.classList.add("hidden");
  setStatus("Reading the page in Chrome and writing the lecture… this can take a moment.");

  try {
    const res = await fetch("/api/narrate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        voice: els.voice.value,
        vision: els.vision.value || null,
        writer: els.writer.value || null,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);

    lesson = data;
    audioUrls = [];
    if (!lesson.segments || lesson.segments.length === 0) {
      throw new Error("No narration was produced for this page.");
    }
    setStatus(`${lesson.title || lesson.url} — ${lesson.segments.length} segments`);
    cur = 0;
    els.stage.classList.remove("hidden");
    els.player.classList.remove("hidden");
    loadSegment(0, true);
  } catch (e) {
    setStatus(e.message, true);
  } finally {
    els.narrate.disabled = false;
  }
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
  els.segCounter.textContent = `${i + 1} / ${lesson.segments.length}`;
  els.audio.playbackRate = currentSpeed();

  try {
    const url = await ensureAudio(i);
    if (cur !== i) return; // user moved on while audio was loading
    els.audio.src = url;
    els.audio.playbackRate = currentSpeed();
    if (autoplay) els.audio.play().catch(() => {});
  } catch (e) {
    setStatus(e.message, true);
  }

  // Warm the next couple of segments.
  prefetch(i + 1);
  prefetch(i + 2);
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

els.narrate.addEventListener("click", narrate);
els.playpause.addEventListener("click", togglePlay);
els.prev.addEventListener("click", () => loadSegment(cur - 1, true));
els.next.addEventListener("click", () => loadSegment(cur + 1, true));
els.voice.addEventListener("change", changeVoice);
els.speed.addEventListener("change", () => {
  els.audio.playbackRate = currentSpeed();
});

// Spacebar = play/pause when a lesson is loaded.
document.addEventListener("keydown", (e) => {
  if (e.code === "Space" && lesson && e.target.tagName !== "SELECT") {
    e.preventDefault();
    togglePlay();
  }
});

// Pull defaults from the server settings.
fetch("/api/settings")
  .then((r) => r.json())
  .then((s) => {
    if (s.tts_voice) els.voice.value = s.tts_voice;
    if (s.tts_speed) applySpeedDefault(s.tts_speed);
    if (s.vision_provider) els.vision.value = s.vision_provider;
    if (s.writer_provider) els.writer.value = s.writer_provider;
  })
  .catch(() => {});
