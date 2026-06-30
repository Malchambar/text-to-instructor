// Chat panel: ask about the current page (and optionally the web). The thread
// persists across page changes (and reloads, via localStorage); the page context
// is supplied server-side from the most recent narrate.

(function () {
  const $ = (id) => document.getElementById(id);
  const log = $("chat-log");
  const form = $("chat-form");
  const input = $("chat-input");
  const send = $("chat-send");
  const engineSel = $("chat-engine");
  const webBox = $("chat-web");
  const clearBtn = $("chat-clear");
  const saveBtn = $("chat-save");

  const KEY = "t2i_chat";
  let messages = [];

  // Restore persisted thread + settings.
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || "{}");
    messages = saved.messages || [];
    if (saved.engine) engineSel.value = saved.engine;
    if (typeof saved.web === "boolean") webBox.checked = saved.web;
  } catch (e) {}

  function persist() {
    localStorage.setItem(
      KEY,
      JSON.stringify({ messages, engine: engineSel.value, web: webBox.checked })
    );
  }

  function bubble(role, content, cls) {
    const div = document.createElement("div");
    div.className = `msg ${role}${cls ? " " + cls : ""}`;
    div.textContent = content;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }

  function render() {
    log.innerHTML = "";
    for (const m of messages) bubble(m.role, m.content);
  }

  async function sendMessage() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    messages.push({ role: "user", content: text });
    bubble("user", text);
    persist();

    send.disabled = true;
    const thinking = bubble("assistant", "…thinking", "thinking");
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages,
          engine: engineSel.value,
          web: webBox.checked,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `Chat failed (${res.status})`);
      thinking.remove();
      const reply = (data.reply || "").trim() || "(no reply)";
      messages.push({ role: "assistant", content: reply });
      bubble("assistant", reply);
      persist();
    } catch (e) {
      thinking.textContent = `⚠ ${e.message}`;
      thinking.classList.remove("thinking");
    } finally {
      send.disabled = false;
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    sendMessage();
  });

  // Enter sends; Shift+Enter makes a newline.
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  engineSel.addEventListener("change", persist);
  webBox.addEventListener("change", persist);
  clearBtn.addEventListener("click", () => {
    if (messages.length && !confirm("Clear this conversation and start a new one?")) return;
    messages = [];
    render();
    persist();
  });

  // Save the conversation to a Markdown file so you keep your own history.
  saveBtn.addEventListener("click", () => {
    if (!messages.length) return;
    const body = messages
      .map((m) => `**${m.role === "user" ? "You" : "Instructor"}:** ${m.content}`)
      .join("\n\n");
    const blob = new Blob([`# Conversation\n\n${body}\n`], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "conversation.md";
    a.click();
    URL.revokeObjectURL(a.href);
  });

  render();
})();
