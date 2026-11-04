"use strict";

const $ = (sel) => document.querySelector(sel);
const messagesEl = $("#messages");
const emptyEl = $("#empty");
const inputEl = $("#input");
const sendBtn = $("#send");
const libraryEl = $("#library");
const dialog = $("#prompt-dialog");

const state = { sessionId: null, busy: false };

/* ---------- utilities ---------- */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

function storage(op, key, value) {
  try {
    if (op === "get") return localStorage.getItem(key);
    if (op === "set") localStorage.setItem(key, value);
    if (op === "del") localStorage.removeItem(key);
  } catch { /* storage unavailable: run without persistence */ }
  return null;
}

function renderMarkdown(text) {
  const wrap = el("div", { class: "md" });
  if (window.marked && window.DOMPurify) {
    wrap.innerHTML = DOMPurify.sanitize(marked.parse(text, { gfm: true, breaks: false }));
  } else {
    wrap.append(el("p", { text, style: "white-space:pre-wrap" }));
  }
  enhanceCodeBlocks(wrap);
  return wrap;
}

function enhanceCodeBlocks(root) {
  for (const pre of root.querySelectorAll("pre")) {
    const code = pre.querySelector("code");
    const lang = (code?.className.match(/language-(\S+)/) || [])[1] || "";
    const box = el("div", { class: "codeblock" });
    const copy = el("button", { class: "btn ghost small", type: "button", text: "Copy" });
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(pre.innerText);
        copy.textContent = "Copied";
      } catch { copy.textContent = "Copy failed"; }
      setTimeout(() => (copy.textContent = "Copy"), 1500);
    });
    const save = el("button", { class: "btn ghost small", type: "button", text: "Save" });
    save.addEventListener("click", () => openSaveDialog(pre.innerText));
    pre.replaceWith(box);
    box.append(el("div", { class: "code-actions" }, el("span", { class: "lang", text: lang }), copy, save), pre);
  }
}

function scrollToBottom(force = false) {
  const nearBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 160;
  if (force || nearBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function truncate(s, n) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/* ---------- session ---------- */

async function ensureSession() {
  if (state.sessionId) return state.sessionId;
  const saved = storage("get", "promptCoachSession");
  if (saved) return (state.sessionId = saved);
  const res = await fetch("/api/session", { method: "POST" });
  state.sessionId = (await res.json()).session_id;
  storage("set", "promptCoachSession", state.sessionId);
  return state.sessionId;
}

async function newChat() {
  if (state.busy) return;
  if (state.sessionId) fetch(`/api/session/${state.sessionId}`, { method: "DELETE" }).catch(() => {});
  storage("del", "promptCoachSession");
  state.sessionId = null;
  messagesEl.replaceChildren(emptyEl);
  emptyEl.hidden = false;
  inputEl.focus();
}

/* ---------- assistant turn rendering ---------- */

class TurnView {
  constructor() {
    this.root = el("div", { class: "msg-assistant" });
    this.typing = el("div", { class: "typing", "aria-label": "Working" }, el("i"), el("i"), el("i"));
    this.root.append(this.typing);
    this.text = null;       // current text segment {node, buf}
    this.thinking = null;   // current thinking segment {body, buf}
    this.tools = new Map(); // tool_use id -> card
    this.renderQueued = false;
    messagesEl.append(this.root);
  }

  _insert(node) {
    this.root.insertBefore(node, this.typing);
    scrollToBottom();
  }

  thinkingStart() {
    const body = el("div", { class: "thinking-body" });
    const det = el("details", { class: "thinking" }, el("summary", { text: "Reasoning" }), body);
    this.thinking = { body, buf: "" };
    this._insert(det);
  }

  thinkingDelta(t) {
    if (!this.thinking) this.thinkingStart();
    this.thinking.buf += t;
    this.thinking.body.textContent = this.thinking.buf;
  }

  textStart() {
    this.text = { node: el("div"), buf: "" };
    this._insert(this.text.node);
  }

  textDelta(t) {
    if (!this.text) this.textStart();
    this.text.buf += t;
    if (!this.renderQueued) {
      this.renderQueued = true;
      requestAnimationFrame(() => {
        this.renderQueued = false;
        this._renderText();
      });
    }
  }

  _renderText() {
    if (!this.text) return;
    this.text.node.replaceChildren(renderMarkdown(this.text.buf));
    scrollToBottom();
  }

  toolPending(id, name) {
    if (this.tools.has(id)) return;
    const card = new ToolCard(name);
    this.tools.set(id, card);
    this._insert(card.root);
  }

  toolCall(id, name, input) {
    this.toolPending(id, name);
    this.tools.get(id).setInput(input);
  }

  toolResult(id, name, ev) {
    this.toolPending(id, name);
    this.tools.get(id).setResult(ev);
    scrollToBottom();
  }

  notice(message, isError = false) {
    this._insert(el("div", { class: "notice" + (isError ? " error" : ""), text: message }));
  }

  done(usage) {
    this._renderText();
    this.typing.remove();
    for (const card of this.tools.values()) card.finishIfPending();
    if (usage && (usage.input_tokens || usage.output_tokens)) {
      const totalIn = usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens;
      this.root.append(el("div", {
        class: "usage",
        text: `${totalIn.toLocaleString()} in (${usage.cache_read_input_tokens.toLocaleString()} cached) · ${usage.output_tokens.toLocaleString()} out`,
        title: "Tokens used by the coach this turn (test runs not included)",
      }));
    }
  }
}

const TOOL_TITLES = {
  run_prompt: "Testing prompt",
  count_tokens: "Counting tokens",
  save_prompt: "Saving to library",
  list_prompts: "Listing library",
  get_prompt: "Opening saved prompt",
};

class ToolCard {
  constructor(name) {
    this.name = name;
    this.sub = el("span", { class: "sub" });
    this.body = el("div", { class: "tool-body" });
    this.root = el("div", { class: "tool running" },
      el("div", { class: "tool-head" },
        el("span", { class: "tool-status", "aria-hidden": "true" }),
        el("span", { class: "title", text: TOOL_TITLES[name] || name }),
        this.sub),
      this.body);
  }

  setInput(input) {
    if (this.name === "run_prompt" && input) {
      const n = Array.isArray(input.test_inputs) ? input.test_inputs.length : 0;
      this.sub.textContent = `${input.label || ""} · ${n} input${n === 1 ? "" : "s"}`;
    } else if (input?.name) {
      this.sub.textContent = input.name;
    }
  }

  setResult(ev) {
    this.root.classList.remove("running");
    this.root.classList.add(ev.is_error ? "error" : "ok");
    const d = ev.data;
    if (this.name === "run_prompt" && d && d.runs) {
      this.sub.textContent = `${d.label} · ${d.runs.length} input${d.runs.length === 1 ? "" : "s"} · ${d.model}`;
      d.runs.forEach((r, i) => this.body.append(runView(r, i)));
    } else if (ev.is_error) {
      this.body.append(el("div", { class: "tool-error", text: ev.content }));
    } else if (this.name === "count_tokens" && d) {
      this.sub.textContent = `${d.input_tokens.toLocaleString()} input tokens on ${d.model}`;
    } else if (this.name === "save_prompt" && d) {
      this.sub.textContent = `${d.name} · v${d.version}`;
    } else if (this.name === "list_prompts") {
      this.sub.textContent = Array.isArray(d) ? `${d.length} saved` : "";
    } else if (this.name === "get_prompt" && d) {
      this.sub.textContent = `${d.name} · v${d.version}`;
    }
  }

  finishIfPending() {
    if (this.root.classList.contains("running")) {
      this.root.classList.remove("running");
      this.root.classList.add("error");
      this.sub.textContent = "not run";
    }
  }
}

function runView(r, i) {
  const failed = !!r.error;
  let meta = failed ? "error" : `${r.output_tokens} tok`;
  if (r.stop_reason === "refusal") meta = "refused";
  else if (r.stop_reason === "max_tokens") meta += " · cut off";
  const body = el("div", { class: "run-body" },
    el("div", {}, el("div", { class: "label", text: "Input" }), el("pre", { text: r.input })),
    el("div", {}, el("div", { class: "label", text: failed ? "Error" : "Output" }),
      el("pre", { text: failed ? r.error : (r.output || "(empty)") })));
  return el("details", { class: "run" + (failed ? " failed" : "") },
    el("summary", {},
      el("span", { class: "n", text: `#${i + 1}` }),
      el("span", { class: "in", text: truncate(r.input.replace(/\s+/g, " "), 120) }),
      el("span", { class: "meta", text: meta })),
    body);
}

/* ---------- sending ---------- */

function setBusy(busy) {
  state.busy = busy;
  sendBtn.disabled = busy;
  $("#new-chat").disabled = busy;
}

async function send(text) {
  if (state.busy || !text.trim()) return;
  emptyEl.hidden = true;
  messagesEl.append(el("div", { class: "msg-user-wrap" }, el("div", { class: "msg-user", text })));
  inputEl.value = "";
  autosize();
  scrollToBottom(true);
  setBusy(true);
  const view = new TurnView();
  let finished = false;

  try {
    const sessionId = await ensureSession();
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: text }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `Request failed (${res.status})`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const ev = JSON.parse(line.slice(6));
        if (ev.type === "done") finished = true;
        handleEvent(view, ev);
      }
    }
    if (!finished) {
      view.notice("The connection closed before the response finished.", true);
      view.done(null);
    }
  } catch (err) {
    view.notice(err.message || String(err), true);
    view.done(null);
  } finally {
    setBusy(false);
    inputEl.focus();
  }
}

function handleEvent(view, ev) {
  switch (ev.type) {
    case "thinking_start": view.thinkingStart(); break;
    case "thinking_delta": view.thinkingDelta(ev.text); break;
    case "text_start": view.textStart(); break;
    case "text_delta": view.textDelta(ev.text); break;
    case "tool_pending": view.toolPending(ev.id, ev.name); break;
    case "tool_call": view.toolCall(ev.id, ev.name, ev.input); break;
    case "tool_result": view.toolResult(ev.id, ev.name, ev); break;
    case "library_changed": loadLibrary(); break;
    case "notice": view.notice(ev.message); break;
    case "error": view.notice(ev.message, true); break;
    case "done": view.done(ev.usage); break;
  }
}

/* ---------- library ---------- */

async function loadLibrary() {
  let items = [];
  try {
    items = await (await fetch("/api/prompts")).json();
  } catch { /* leave empty */ }
  const list = $("#library-list");
  list.replaceChildren(...items.map((p) => el("li", {},
    el("button", { type: "button", onclick: () => openPromptDialog(p.name) },
      el("span", { class: "name", text: p.name }),
      el("span", { class: "meta", text: `v${p.version} · ${new Date(p.saved_at).toLocaleDateString()}` })))));
  $("#library-empty").hidden = items.length > 0;
}

function promptField(label, text) {
  if (!text) return null;
  const copy = el("button", { class: "btn ghost small", type: "button", text: "Copy" });
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(text); copy.textContent = "Copied"; }
    catch { copy.textContent = "Copy failed"; }
    setTimeout(() => (copy.textContent = "Copy"), 1500);
  });
  return el("div", {},
    el("div", { class: "label", style: "display:flex;justify-content:space-between;align-items:center" },
      el("span", { text: label }), copy),
    el("pre", { text }));
}

async function openPromptDialog(name) {
  const res = await fetch(`/api/prompts/${encodeURIComponent(name)}`);
  if (!res.ok) return loadLibrary();
  const p = await res.json();
  const versions = p.history.slice().reverse();
  const select = $("#dialog-version");
  select.hidden = versions.length < 2;
  select.replaceChildren(...versions.map((v) => el("option", { value: v.version, text: `Version ${v.version}` })));

  const show = (v) => {
    $("#dialog-content").replaceChildren(...[
      promptField("System prompt", v.system_prompt),
      promptField("User template", v.user_template),
      v.notes ? el("div", {}, el("div", { class: "label", text: "Notes" }), renderMarkdown(v.notes)) : null,
      el("div", { class: "usage", text: `Saved ${new Date(v.saved_at).toLocaleString()}` }),
    ].filter(Boolean));
  };
  select.onchange = () => show(versions.find((v) => String(v.version) === select.value));
  show(versions[0]);

  $("#dialog-title").textContent = p.name;
  const del = el("button", { class: "btn danger", type: "button", text: "Delete" });
  del.addEventListener("click", async () => {
    if (!confirm(`Delete “${p.name}” and all its versions?`)) return;
    await fetch(`/api/prompts/${encodeURIComponent(p.name)}`, { method: "DELETE" });
    dialog.close();
    loadLibrary();
  });
  const improve = el("button", { class: "btn", type: "button", text: "Improve with coach" });
  improve.addEventListener("click", () => {
    dialog.close();
    inputEl.value = `Help me improve my saved prompt “${p.name}”. Load it from the library first.`;
    autosize();
    inputEl.focus();
    libraryEl.classList.remove("open");
  });
  $("#dialog-actions").replaceChildren(del, el("span", { class: "spacer" }), improve,
    el("button", { class: "btn primary", value: "close", text: "Close" }));
  dialog.showModal();
}

function openSaveDialog(text) {
  $("#dialog-title").textContent = "Save to library";
  $("#dialog-version").hidden = true;
  const name = el("input", { type: "text", placeholder: "Name, e.g. Support bot system prompt", maxlength: "80" });
  const body = el("textarea", {});
  body.value = text;
  const notes = el("input", { type: "text", placeholder: "Notes (optional)" });
  const error = el("div", { class: "tool-error" });
  $("#dialog-content").replaceChildren(
    el("div", {}, el("div", { class: "label", text: "Name" }), name),
    el("div", {}, el("div", { class: "label", text: "System prompt" }), body),
    el("div", {}, el("div", { class: "label", text: "Notes" }), notes),
    error);
  const save = el("button", { class: "btn primary", type: "button", text: "Save" });
  save.addEventListener("click", async () => {
    const res = await fetch("/api/prompts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.value, system_prompt: body.value, notes: notes.value }),
    });
    if (!res.ok) {
      error.textContent = (await res.json().catch(() => ({}))).detail || "Could not save.";
      return;
    }
    dialog.close();
    loadLibrary();
  });
  $("#dialog-actions").replaceChildren(
    el("button", { class: "btn", value: "cancel", text: "Cancel" }), save);
  dialog.showModal();
  name.focus();
}

/* ---------- wiring ---------- */

function autosize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 240) + "px";
}

inputEl.addEventListener("input", autosize);
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send(inputEl.value);
  }
});
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  send(inputEl.value);
});
$("#new-chat").addEventListener("click", newChat);
for (const b of document.querySelectorAll(".starter")) {
  b.addEventListener("click", () => {
    inputEl.value = b.dataset.text;
    autosize();
    inputEl.focus();
    inputEl.setSelectionRange(inputEl.value.length, inputEl.value.length);
  });
}
$("#library-toggle").addEventListener("click", () => {
  const open = libraryEl.classList.toggle("open");
  $("#library-toggle").setAttribute("aria-expanded", String(open));
});
$("#library-close").addEventListener("click", () => libraryEl.classList.remove("open"));

fetch("/api/config").then((r) => r.json()).then((c) => {
  $("#model-chip").textContent = c.agent_model;
}).catch(() => {});
loadLibrary();
// Conversations live in server memory, so a page reload starts a fresh chat view;
// drop the stale id to keep the transcript and server state in sync.
storage("del", "promptCoachSession");
inputEl.focus();
