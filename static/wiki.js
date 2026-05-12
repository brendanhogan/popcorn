// popcorn — wiki viewer
"use strict";

const state = {
  index: null, // { concepts: [], sources: [], counts: {} }
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function showToast(msg, ms = 2200) {
  const toast = $("#toast");
  toast.textContent = msg;
  toast.classList.remove("hidden");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toast.classList.add("hidden"), ms);
}

// ---------- routing ----------

function currentPath() {
  // /wiki -> "index"; /wiki/concepts/foo -> "concepts/foo"
  const p = window.location.pathname;
  if (p === "/wiki" || p === "/wiki/" || p === "/wiki/index") return "index";
  const m = p.match(/^\/wiki\/(.+)$/);
  return m ? m[1] : "index";
}

function navigate(path, push = true) {
  if (push) {
    history.pushState({ path }, "", `/wiki/${path}`);
  }
  loadPage(path);
}

// ---------- API ----------

async function api(url) {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} — ${body || res.statusText}`);
  }
  return res.json();
}

// ---------- index / sidebar ----------

async function loadIndex() {
  try {
    state.index = await api("/api/wiki/index");
    renderSidebar();
    renderCounts();
  } catch (err) {
    showToast(`Failed to load index: ${err.message}`);
  }
}

function renderCounts() {
  const { counts } = state.index || { counts: { concepts: 0, sources: 0 } };
  $("#wiki-counts").textContent = `${counts.concepts} concepts · ${counts.sources} sources`;
}

function renderSidebar() {
  if (!state.index) return;
  const current = currentPath();

  const conceptList = $("#wiki-concept-list");
  conceptList.innerHTML = "";
  state.index.concepts.forEach((c) => {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = `/wiki/concepts/${c.slug}`;
    a.dataset.path = `concepts/${c.slug}`;
    const title = document.createElement("span");
    title.textContent = c.title;
    const meta = document.createElement("span");
    meta.className = "item-meta";
    meta.textContent = String(c.source_count);
    a.appendChild(title);
    a.appendChild(meta);
    if (current === `concepts/${c.slug}`) a.classList.add("current");
    a.addEventListener("click", interceptLink);
    li.appendChild(a);
    conceptList.appendChild(li);
  });

  const sourceList = $("#wiki-source-list");
  sourceList.innerHTML = "";
  state.index.sources.forEach((s) => {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = `/wiki/sources/${s.slug}`;
    a.dataset.path = `sources/${s.slug}`;
    const title = document.createElement("span");
    title.textContent = s.title;
    const meta = document.createElement("span");
    meta.className = "item-meta";
    meta.textContent = s.date || "";
    a.appendChild(title);
    a.appendChild(meta);
    if (current === `sources/${s.slug}`) a.classList.add("current");
    a.addEventListener("click", interceptLink);
    li.appendChild(a);
    sourceList.appendChild(li);
  });
}

function applyFilter(query) {
  const q = query.trim().toLowerCase();
  for (const list of ["#wiki-concept-list", "#wiki-source-list"]) {
    $$(`${list} li`).forEach((li) => {
      const text = li.textContent.toLowerCase();
      li.classList.toggle("hidden", q !== "" && !text.includes(q));
    });
  }
}

// ---------- page rendering ----------

async function loadPage(path) {
  const article = $("#wiki-article");
  article.innerHTML = '<div class="wiki-empty">Loading…</div>';

  try {
    const data = await api(`/api/wiki/page?path=${encodeURIComponent(path)}`);
    document.title = `${data.title} — popcorn wiki`;
    article.innerHTML = data.html;

    if (data.backlinks && data.backlinks.length > 0) {
      const bl = document.createElement("div");
      bl.className = "wiki-backlinks";
      bl.innerHTML = '<div class="wiki-backlinks-title">Linked from</div><ul></ul>';
      const ul = bl.querySelector("ul");
      data.backlinks.forEach((b) => {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.href = `/wiki/${b.path}`;
        a.textContent = b.title;
        a.dataset.path = b.path;
        a.addEventListener("click", interceptLink);
        li.appendChild(a);
        ul.appendChild(li);
      });
      article.appendChild(bl);
    }

    // Intercept wiki-link clicks within the article
    $$(".wiki-link", article).forEach((a) => {
      if (a.tagName !== "A" || a.classList.contains("broken")) return;
      a.addEventListener("click", interceptLink);
      // sync dataset.path with href
      const m = a.getAttribute("href").match(/^\/wiki\/(.+)$/);
      if (m) a.dataset.path = m[1];
    });

    // Update sidebar highlight
    $$(".wiki-list a").forEach((a) => {
      a.classList.toggle("current", a.dataset.path === path);
    });
  } catch (err) {
    article.innerHTML = `<div class="wiki-empty">${err.message}</div>`;
  }
}

function interceptLink(e) {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
  const path = e.currentTarget.dataset.path;
  if (!path) return;
  e.preventDefault();
  navigate(path, true);
}

// ---------- rebuild ----------

async function rebuild() {
  const btn = $("#wiki-rebuild");
  if (!confirm("Rebuild the wiki? Takes ~15-30s and costs ~$0.30. The current pages stay live until the rebuild finishes.")) return;
  btn.disabled = true;
  btn.textContent = "Building...";
  try {
    const res = await fetch("/api/wiki/build", { method: "POST" });
    if (!res.ok) throw new Error(await res.text());
    showToast("Rebuild started. Refresh in 30s.");

    // Poll status
    const poll = async () => {
      const status = await api("/api/wiki/build/status");
      if (!status.running) {
        btn.disabled = false;
        btn.textContent = "Rebuild";
        if (status.last_error) {
          showToast(`Rebuild error: ${status.last_error}`, 6000);
        } else {
          showToast("Rebuild complete. Reloading…");
          setTimeout(() => window.location.reload(), 800);
        }
        return;
      }
      setTimeout(poll, 2000);
    };
    setTimeout(poll, 2000);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "Rebuild";
    showToast(`Rebuild failed: ${err.message}`);
  }
}

// ---------- init ----------

window.addEventListener("popstate", () => loadPage(currentPath()));

async function init() {
  $("#wiki-rebuild").addEventListener("click", rebuild);
  $("#wiki-search").addEventListener("input", (e) => applyFilter(e.target.value));

  await loadIndex();
  await loadPage(currentPath());
}

document.addEventListener("DOMContentLoaded", init);
