// popcorn — frontend
"use strict";

const state = {
  entries: new Map(),   // id -> entry data
  cards: new Map(),     // id -> DOM element
  pollers: new Map(),   // id -> timeout handle
  saveTimers: new Map(),// id -> timeout handle for debounced save
  session: null,        // { id, name, entry_count, ... }
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ---------- API ----------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} — ${body}`);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

// ---------- helpers ----------

function debounceSave(id, fn) {
  clearTimeout(state.saveTimers.get(id));
  state.saveTimers.set(id, setTimeout(fn, 800));
}

function showToast(msg, ms = 2200) {
  const toast = $("#toast");
  toast.textContent = msg;
  toast.classList.remove("hidden");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toast.classList.add("hidden"), ms);
}

function setEmpty() {
  const isEmpty = state.entries.size === 0;
  $("#empty-state").classList.toggle("hidden", !isEmpty);
}

// ---------- rendering ----------

function makeCard(entry) {
  const tpl = $("#entry-template");
  const card = tpl.content.firstElementChild.cloneNode(true);
  card.dataset.id = entry.id;

  // Open the paste section by default if the entry needs content.
  const needsContent =
    entry.type === "twitter" ||
    entry.status === "error" ||
    !entry.fetched_content;
  if (needsContent) {
    $(".entry-paste", card).open = true;
  }

  hydrateCard(card, entry);
  bindCard(card);
  return card;
}

function hydrateCard(card, entry) {
  card.dataset.status = entry.status;
  card.dataset.type = entry.type;

  $(".status-label", card).textContent = entry.status;
  const urlEl = $(".entry-url", card);
  urlEl.href = entry.url;
  urlEl.textContent = entry.url;

  const typeSel = $(".entry-type", card);
  if (typeSel.value !== entry.type) typeSel.value = entry.type;

  // extracted text
  const extracted = $(".extracted", card);
  extracted.textContent = entry.fetched_content || (entry.fetch_error || "(no content)");

  // summary
  const summaryEl = $(".summary-text", card);
  if (entry.status === "error" && entry.fetch_error) {
    summaryEl.dataset.error = entry.fetch_error;
    summaryEl.textContent = "";
  } else {
    summaryEl.textContent = entry.summary || "";
  }

  // title
  const titleInput = $(".title-input", card);
  if (document.activeElement !== titleInput) titleInput.value = entry.title || "";

  // title suggestions
  const sugWrap = $(".title-suggestions", card);
  sugWrap.innerHTML = "";
  (entry.title_suggestions || []).forEach((t) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "title-chip";
    chip.textContent = t;
    chip.addEventListener("click", () => {
      titleInput.value = t;
      patch(entry.id, { title: t });
    });
    sugWrap.appendChild(chip);
  });

  // rating
  $$(".rating-btn", card).forEach((btn) => {
    btn.classList.toggle("active", Number(btn.dataset.rating) === entry.rating);
  });

  // private
  $(".private-input", card).checked = !!entry.private;

  // notes
  const notesInput = $(".notes-input", card);
  if (document.activeElement !== notesInput) notesInput.value = entry.notes || "";

  // attached image thumbnail
  const thumbWrap = $(".paste-thumb", card);
  const thumbImg = $(".paste-thumb img", card);
  if (entry.attached_image_filename) {
    thumbImg.src = `/api/entry/${entry.id}/image?v=${encodeURIComponent(entry.attached_image_filename)}`;
    thumbWrap.classList.remove("hidden");
  } else {
    thumbImg.removeAttribute("src");
    thumbWrap.classList.add("hidden");
  }

  // paste textarea — don't overwrite if user is editing it
  const pasteText = $(".paste-text", card);
  if (document.activeElement !== pasteText) {
    pasteText.value = entry.fetched_content || "";
  }

  // chat
  renderChat(card, entry.chat_history || []);
}

function renderChat(card, history) {
  const wrap = $(".chat-history", card);
  wrap.innerHTML = "";
  history.forEach((t) => {
    const el = document.createElement("div");
    el.className = `chat-turn ${t.role}`;
    el.textContent = t.content;
    wrap.appendChild(el);
  });
  wrap.scrollTop = wrap.scrollHeight;
}

function bindCard(card) {
  const id = card.dataset.id;

  $(".entry-type", card).addEventListener("change", (e) => {
    patch(id, { type: e.target.value });
  });

  $(".title-input", card).addEventListener("input", (e) => {
    debounceSave(id, () => patch(id, { title: e.target.value }));
  });

  $$(".rating-btn", card).forEach((btn) => {
    btn.addEventListener("click", () => {
      const r = Number(btn.dataset.rating);
      patch(id, { rating: r });
    });
  });

  $(".private-input", card).addEventListener("change", (e) => {
    patch(id, { private: e.target.checked });
  });

  $(".notes-input", card).addEventListener("input", (e) => {
    debounceSave(id, () => patch(id, { notes: e.target.value }));
  });

  bindPasteUI(card);

  $(".chat-form", card).addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $(".chat-input", card);
    const msg = input.value.trim();
    if (!msg) return;
    input.value = "";
    input.disabled = true;
    try {
      // optimistic append
      const entry = state.entries.get(id);
      const tempHistory = [...(entry.chat_history || []), { role: "user", content: msg }];
      renderChat(card, tempHistory);
      const tempEl = document.createElement("div");
      tempEl.className = "chat-turn assistant";
      tempEl.textContent = "...";
      $(".chat-history", card).appendChild(tempEl);

      const res = await api(`/api/entry/${id}/chat`, {
        method: "POST",
        body: JSON.stringify({ message: msg }),
      });
      entry.chat_history = res.chat_history;
      state.entries.set(id, entry);
      renderChat(card, entry.chat_history);
    } catch (err) {
      showToast(`Chat failed: ${err.message}`);
    } finally {
      input.disabled = false;
      input.focus();
    }
  });
}

// ---------- paste / image OCR ----------

function bindPasteUI(card) {
  const id = card.dataset.id;
  const drop = $(".paste-drop", card);
  const fileInput = $(".paste-file", card);
  const textArea = $(".paste-text", card);
  const processBtn = $(".paste-process", card);
  const statusEl = $(".paste-status", card);

  const setStatus = (msg) => {
    statusEl.textContent = msg || "";
  };

  drop.addEventListener("click", () => fileInput.click());

  drop.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.classList.add("dragover");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("dragover"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("dragover");
    const file = e.dataTransfer.files[0];
    if (file && file.type.startsWith("image/")) {
      uploadImage(id, file, setStatus, textArea);
    }
  });

  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    if (file) uploadImage(id, file, setStatus, textArea);
    fileInput.value = "";
  });

  // paste from clipboard (image OR text into the textarea)
  textArea.addEventListener("paste", (e) => {
    const items = (e.clipboardData || window.clipboardData).items;
    for (const item of items) {
      if (item.kind === "file" && item.type.startsWith("image/")) {
        e.preventDefault();
        const file = item.getAsFile();
        if (file) uploadImage(id, file, setStatus, textArea);
        return;
      }
    }
    // otherwise let default text paste happen
  });

  processBtn.addEventListener("click", async () => {
    const text = textArea.value.trim();
    if (!text) {
      setStatus("Add some text first.");
      return;
    }
    processBtn.disabled = true;
    setStatus("Summarizing...");
    try {
      const updated = await api(`/api/entry/${id}/content`, {
        method: "POST",
        body: JSON.stringify({ text }),
      });
      state.entries.set(id, updated);
      hydrateCard(card, updated);
      setStatus("Done.");
      setTimeout(() => setStatus(""), 1500);
    } catch (err) {
      setStatus("");
      showToast(`Process failed: ${err.message}`);
    } finally {
      processBtn.disabled = false;
    }
  });
}

async function uploadImage(entryId, file, setStatus, textArea) {
  setStatus("Reading image...");
  const fd = new FormData();
  fd.append("image", file, file.name || "screenshot.png");
  try {
    const res = await fetch(`/api/entry/${entryId}/extract-image`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      throw new Error(`${res.status} — ${body}`);
    }
    const data = await res.json();
    textArea.value = data.text || "";
    // refresh thumbnail by re-fetching entry
    const entry = await api(`/api/entry/${entryId}`);
    state.entries.set(entryId, entry);
    const card = state.cards.get(entryId);
    if (card) {
      const thumbWrap = $(".paste-thumb", card);
      const thumbImg = $(".paste-thumb img", card);
      thumbImg.src = `/api/entry/${entryId}/image?v=${Date.now()}`;
      thumbWrap.classList.remove("hidden");
    }
    setStatus("Text extracted. Edit if needed, then click Process.");
  } catch (err) {
    setStatus("");
    showToast(`Image read failed: ${err.message}`);
  }
}

// ---------- mutations ----------

async function patch(id, fields) {
  try {
    const updated = await api(`/api/entry/${id}`, {
      method: "PUT",
      body: JSON.stringify(fields),
    });
    state.entries.set(id, updated);
    // selectively re-render bits that depend on changed fields without nuking focus
    const card = state.cards.get(id);
    if (!card) return;
    // type → may toggle the twitter view + iframe
    if ("type" in fields) {
      card.dataset.type = updated.type;
    }
    // rating buttons
    if ("rating" in fields) {
      $$(".rating-btn", card).forEach((b) => {
        b.classList.toggle("active", Number(b.dataset.rating) === updated.rating);
      });
    }
    if ("private" in fields) {
      $(".private-input", card).checked = !!updated.private;
    }
  } catch (err) {
    showToast(`Save failed: ${err.message}`);
  }
}

// ---------- polling ----------

function pollEntry(id) {
  if (state.pollers.has(id)) return;
  const tick = async () => {
    try {
      const entry = await api(`/api/entry/${id}`);
      state.entries.set(id, entry);
      const card = state.cards.get(id);
      if (card) hydrateCard(card, entry);
      if (entry.status === "ready" || entry.status === "error") {
        state.pollers.delete(id);
        return;
      }
    } catch (err) {
      // entry may have been deleted; stop polling
      state.pollers.delete(id);
      return;
    }
    state.pollers.set(id, setTimeout(tick, 1500));
  };
  state.pollers.set(id, setTimeout(tick, 800));
}

// ---------- entry lifecycle ----------

function appendEntry(entry) {
  const card = makeCard(entry);
  state.entries.set(entry.id, entry);
  state.cards.set(entry.id, card);
  $("#entries").appendChild(card);
  setEmpty();
  if (entry.status !== "ready" && entry.status !== "error") {
    pollEntry(entry.id);
  }
}

// ---------- top-level actions ----------

async function submitBatch() {
  const ta = $("#urls-input");
  const raw = ta.value.trim();
  if (!raw) return;
  const urls = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  if (urls.length === 0) return;

  const btn = $("#submit-batch");
  btn.disabled = true;
  btn.textContent = "Ingesting...";
  try {
    const res = await api("/api/batch", {
      method: "POST",
      body: JSON.stringify({ urls }),
    });
    // for each new entry, fetch full and render
    for (const stub of res.entries) {
      const entry = await api(`/api/entry/${stub.id}`);
      appendEntry(entry);
    }
    ta.value = "";
    showToast(`Added ${res.entries.length} entr${res.entries.length === 1 ? "y" : "ies"}`);
  } catch (err) {
    showToast(`Submit failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Submit";
  }
}

async function exportText() {
  try {
    const text = await api("/api/export/text");
    await navigator.clipboard.writeText(text);
    showToast("Copied plain text to clipboard");
  } catch (err) {
    showToast(`Export failed: ${err.message}`);
  }
}

async function exportJson() {
  try {
    const data = await api("/api/export/json");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const today = new Date().toISOString().slice(0, 10);
    a.href = url;
    a.download = `popcorn-${today}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast("Downloaded JSON");
  } catch (err) {
    showToast(`Export failed: ${err.message}`);
  }
}

// ---------- sessions ----------

function clearEntriesUI() {
  state.entries.clear();
  state.cards.clear();
  state.pollers.forEach(clearTimeout);
  state.pollers.clear();
  state.saveTimers.forEach(clearTimeout);
  state.saveTimers.clear();
  $("#entries").innerHTML = "";
}

async function loadCurrentSession() {
  const data = await api("/api/sessions");
  state.session = data.sessions.find((s) => s.id === data.current_id) || null;
  renderSessionName();
}

function renderSessionName() {
  const nameEl = $("#session-name");
  if (state.session) {
    nameEl.textContent = state.session.name;
    nameEl.title = `${state.session.entry_count} entr${state.session.entry_count === 1 ? "y" : "ies"} · click to rename`;
  } else {
    nameEl.textContent = "—";
  }
}

async function openDropdown() {
  const menu = $("#sessions-menu");
  if (!menu.classList.contains("hidden")) {
    menu.classList.add("hidden");
    return;
  }
  menu.innerHTML = '<div class="dropdown-empty">Loading...</div>';
  menu.classList.remove("hidden");
  try {
    const data = await api("/api/sessions");
    menu.innerHTML = "";
    if (data.sessions.length === 0) {
      menu.innerHTML = '<div class="dropdown-empty">No saved sessions.</div>';
      return;
    }
    data.sessions.forEach((s) => {
      const row = document.createElement("div");
      row.className = "dropdown-item" + (s.id === data.current_id ? " current" : "");

      const name = document.createElement("span");
      name.className = "name";
      name.textContent = s.name;
      row.appendChild(name);

      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = `${s.entry_count}`;
      row.appendChild(meta);

      const del = document.createElement("button");
      del.className = "delete";
      del.textContent = "✕";
      del.title = "Delete this session and its entries";
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`Delete session "${s.name}" and its ${s.entry_count} entries?`)) return;
        try {
          await api(`/api/sessions/${s.id}`, { method: "DELETE" });
          await loadCurrentSession();
          await loadEntries();
          await openDropdown(); // refresh the menu
          await openDropdown();
          showToast(`Deleted "${s.name}"`);
        } catch (err) {
          showToast(`Delete failed: ${err.message}`);
        }
      });
      row.appendChild(del);

      row.addEventListener("click", async () => {
        if (s.id === data.current_id) {
          menu.classList.add("hidden");
          return;
        }
        try {
          await api(`/api/sessions/${s.id}/open`, { method: "POST" });
          menu.classList.add("hidden");
          await loadCurrentSession();
          await loadEntries();
          showToast(`Opened "${s.name}"`);
        } catch (err) {
          showToast(`Open failed: ${err.message}`);
        }
      });

      menu.appendChild(row);
    });
  } catch (err) {
    menu.innerHTML = `<div class="dropdown-empty">Error: ${err.message}</div>`;
  }
}

async function loadEntries() {
  clearEntriesUI();
  try {
    const res = await api("/api/entries");
    res.entries.forEach(appendEntry);
  } catch (err) {
    showToast(`Failed to load entries: ${err.message}`);
  }
  setEmpty();
}

async function newSession() {
  try {
    const created = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ name: null }),
    });
    state.session = created;
    renderSessionName();
    await loadEntries();
    showToast(`New session: ${created.name}`);
  } catch (err) {
    showToast(`New session failed: ${err.message}`);
  }
}

async function renameCurrentSession() {
  if (!state.session) return;
  const name = prompt("Name this session:", state.session.name);
  if (!name || name.trim() === "") return;
  try {
    const updated = await api(`/api/sessions/${state.session.id}`, {
      method: "PUT",
      body: JSON.stringify({ name: name.trim() }),
    });
    state.session = updated;
    renderSessionName();
    showToast(`Renamed to "${updated.name}"`);
  } catch (err) {
    showToast(`Rename failed: ${err.message}`);
  }
}

// ---------- init ----------

async function init() {
  $("#submit-batch").addEventListener("click", submitBatch);
  $("#urls-input").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      submitBatch();
    }
  });
  $("#export-text").addEventListener("click", exportText);
  $("#export-json").addEventListener("click", exportJson);
  $("#open-session").addEventListener("click", openDropdown);
  $("#new-session").addEventListener("click", newSession);
  $("#save-session").addEventListener("click", renameCurrentSession);
  $("#session-name").addEventListener("click", renameCurrentSession);

  // close dropdown when clicking outside
  document.addEventListener("click", (e) => {
    const dropdown = $("#open-dropdown");
    if (!dropdown.contains(e.target)) {
      $("#sessions-menu").classList.add("hidden");
    }
  });

  await loadCurrentSession();
  await loadEntries();
}

document.addEventListener("DOMContentLoaded", init);
