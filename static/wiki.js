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
  const { counts } = state.index || { counts: {} };
  const parts = [];
  if (counts.meta) parts.push(`${counts.meta} meta`);
  if (counts.concepts) parts.push(`${counts.concepts} concepts`);
  if (counts.entities) parts.push(`${counts.entities} entities`);
  if (counts.batches) parts.push(`${counts.batches} batches`);
  if (counts.sources) parts.push(`${counts.sources} sources`);
  $("#wiki-counts").textContent = parts.join(" · ");
}

function _renderList(listEl, items, makePath, makeMeta) {
  const current = currentPath();
  listEl.innerHTML = "";
  items.forEach((item) => {
    const li = document.createElement("li");
    const a = document.createElement("a");
    const path = makePath(item);
    a.href = `/wiki/${path}`;
    a.dataset.path = path;
    const title = document.createElement("span");
    title.textContent = item.title;
    a.appendChild(title);
    if (makeMeta) {
      const meta = document.createElement("span");
      meta.className = "item-meta";
      meta.textContent = makeMeta(item);
      a.appendChild(meta);
    }
    if (current === path) a.classList.add("current");
    a.addEventListener("click", interceptLink);
    li.appendChild(a);
    listEl.appendChild(li);
  });
}

function renderSidebar() {
  if (!state.index) return;
  _renderList(
    $("#wiki-meta-list"),
    state.index.metas || [],
    (m) => `meta/${m.slug}`,
    (m) => `${m.concept_count}`,
  );
  _renderList(
    $("#wiki-concept-list"),
    state.index.concepts || [],
    (c) => `concepts/${c.slug}`,
    (c) => `${c.source_count}`,
  );
  _renderList(
    $("#wiki-entity-list"),
    state.index.entities || [],
    (e) => `entities/${e.slug}`,
    (e) => `${e.mentions}`,
  );
  _renderList(
    $("#wiki-batch-list"),
    state.index.batches || [],
    (b) => `batches/${b.slug}`,
    null,
  );
  _renderList(
    $("#wiki-source-list"),
    state.index.sources || [],
    (s) => `sources/${s.slug}`,
    (s) => s.date || "",
  );
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

  if (path === "map") {
    await renderMap(article);
    $$(".wiki-list a").forEach((a) => a.classList.remove("current"));
    return;
  }

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

// ---------- map ----------

const MAP_VIEWBOX = 220; // viewBox is 0..220 (we map projection [-1,1] -> [PAD, VIEWBOX-PAD])
const MAP_PAD = 8;
const RATING_RADIUS = [3, 4, 5, 7, 9, 11]; // index = rating
const CONCEPT_RADIUS = 7;
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 12;
const DRAG_THRESHOLD = 5; // pixels of movement before treating mousedown as drag

const mapState = {
  zoom: 1,
  panX: 0,       // offset in viewBox units from default-centered
  panY: 0,
  isDragging: false,
  dragStartClient: null,
  dragHasMoved: false,
  data: null,
};

function dateToOrdinal(d) {
  // "3/18/26" -> sortable number, "2/25/26" < "3/18/26"
  if (!d) return 0;
  const m = d.match(/^(\d+)\/(\d+)\/(\d+)$/);
  if (!m) return 0;
  const yy = parseInt(m[3], 10) + 2000;
  return yy * 10000 + parseInt(m[1], 10) * 100 + parseInt(m[2], 10);
}

function lerpColor(t) {
  // t in [0, 1] -> cream to terracotta
  // cream rgb(233, 220, 198), terracotta rgb(179, 90, 35)
  const cream = [233, 220, 198];
  const terra = [179, 90, 35];
  const r = Math.round(cream[0] + (terra[0] - cream[0]) * t);
  const g = Math.round(cream[1] + (terra[1] - cream[1]) * t);
  const b = Math.round(cream[2] + (terra[2] - cream[2]) * t);
  return `rgb(${r}, ${g}, ${b})`;
}

async function renderMap(container) {
  document.title = "Map — popcorn wiki";
  // reset zoom/pan when entering the map
  mapState.zoom = 1;
  mapState.panX = 0;
  mapState.panY = 0;
  container.innerHTML = `
    <div class="wiki-map-wrap">
      <div class="wiki-map-header">
        <h1>Map</h1>
        <div class="legend">
          <span>older</span>
          <span class="legend-gradient"></span>
          <span>newer</span>
          <span>· size = rating · scroll to zoom · drag to pan · click points to open · click empty space for ideas</span>
        </div>
      </div>
      <div class="wiki-map-controls">
        <label><input type="checkbox" id="map-toggle-concepts" checked> Show concept labels</label>
        <label><input type="checkbox" id="map-toggle-time" checked> Color by date</label>
      </div>
      <div class="wiki-map-svg-wrap" id="map-svg-wrap">
        <div class="wiki-empty">Computing projection…</div>
      </div>
      <div id="idea-panel"></div>
    </div>
  `;

  let data;
  try {
    data = await api("/api/wiki/map");
  } catch (err) {
    $("#map-svg-wrap").innerHTML = `<div class="wiki-empty">Failed to load: ${err.message}</div>`;
    return;
  }
  if (!data.points || data.points.length === 0) {
    $("#map-svg-wrap").innerHTML = '<div class="wiki-empty">No projection data. Build the wiki first.</div>';
    return;
  }

  mapState.data = data;
  drawMap(data);
  bindMapInteractions();

  $("#map-toggle-concepts").addEventListener("change", () => drawMap(data));
  $("#map-toggle-time").addEventListener("change", () => drawMap(data));
}

function viewBoxString() {
  const size = MAP_VIEWBOX / mapState.zoom;
  const cx = MAP_VIEWBOX / 2 + mapState.panX;
  const cy = MAP_VIEWBOX / 2 + mapState.panY;
  return `${cx - size / 2} ${cy - size / 2} ${size} ${size}`;
}

function updateViewBox() {
  const svg = $("#map-svg");
  if (svg) svg.setAttribute("viewBox", viewBoxString());
  const zl = $("#map-zoom-level");
  if (zl) zl.textContent = `${mapState.zoom.toFixed(1)}×`;
}

function eventToSvgCoords(e) {
  const svg = $("#map-svg");
  if (!svg) return [0, 0];
  const rect = svg.getBoundingClientRect();
  const fx = (e.clientX - rect.left) / rect.width;
  const fy = (e.clientY - rect.top) / rect.height;
  const size = MAP_VIEWBOX / mapState.zoom;
  const cx = MAP_VIEWBOX / 2 + mapState.panX;
  const cy = MAP_VIEWBOX / 2 + mapState.panY;
  return [cx - size / 2 + fx * size, cy - size / 2 + fy * size];
}

function setZoom(newZoom, pivotClientX, pivotClientY) {
  newZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, newZoom));
  const svg = $("#map-svg");
  if (!svg) {
    mapState.zoom = newZoom;
    return;
  }
  // Compute the SVG-coord point under the pivot before zoom change
  const rect = svg.getBoundingClientRect();
  const fx = (pivotClientX - rect.left) / rect.width;
  const fy = (pivotClientY - rect.top) / rect.height;
  const oldSize = MAP_VIEWBOX / mapState.zoom;
  const oldCx = MAP_VIEWBOX / 2 + mapState.panX;
  const oldCy = MAP_VIEWBOX / 2 + mapState.panY;
  const px = oldCx - oldSize / 2 + fx * oldSize;
  const py = oldCy - oldSize / 2 + fy * oldSize;
  // Adjust pan so (px, py) remains under the pivot after zoom
  mapState.zoom = newZoom;
  const newSize = MAP_VIEWBOX / newZoom;
  mapState.panX = px + newSize / 2 - fx * newSize - MAP_VIEWBOX / 2;
  mapState.panY = py + newSize / 2 - fy * newSize - MAP_VIEWBOX / 2;
  updateViewBox();
}

function resetZoom() {
  mapState.zoom = 1;
  mapState.panX = 0;
  mapState.panY = 0;
  updateViewBox();
}

function bindMapInteractions() {
  const wrap = $("#map-svg-wrap");
  if (!wrap) return;

  // Wheel = zoom toward cursor
  wrap.addEventListener("wheel", (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;
    setZoom(mapState.zoom * factor, e.clientX, e.clientY);
  }, { passive: false });

  // Drag = pan; click on bg = idea discovery
  wrap.addEventListener("mousedown", (e) => {
    // Ignore mousedown on points so point clicks still navigate
    if (e.target.classList && e.target.classList.contains("map-point")) return;
    mapState.isDragging = true;
    mapState.dragHasMoved = false;
    mapState.dragStartClient = { x: e.clientX, y: e.clientY };
    wrap.classList.add("dragging");
  });

  window.addEventListener("mousemove", (e) => {
    if (!mapState.isDragging) return;
    const dx = e.clientX - mapState.dragStartClient.x;
    const dy = e.clientY - mapState.dragStartClient.y;
    if (!mapState.dragHasMoved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    mapState.dragHasMoved = true;
    const svg = $("#map-svg");
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const scale = (MAP_VIEWBOX / mapState.zoom) / rect.width;
    mapState.panX -= dx * scale;
    mapState.panY -= dy * scale;
    mapState.dragStartClient = { x: e.clientX, y: e.clientY };
    updateViewBox();
  });

  window.addEventListener("mouseup", (e) => {
    if (!mapState.isDragging) return;
    const wasMove = mapState.dragHasMoved;
    mapState.isDragging = false;
    wrap.classList.remove("dragging");
    if (wasMove) return;
    // Clean click on empty space → idea discovery
    // Only fire if the event target was the svg bg or the wrap
    const t = e.target;
    if (t.classList && t.classList.contains("map-point")) return;
    const [svgX, svgY] = eventToSvgCoords(e);
    const projX = (svgX - MAP_PAD) / (MAP_VIEWBOX / 2 - MAP_PAD) - 1;
    const projY = (svgY - MAP_PAD) / (MAP_VIEWBOX / 2 - MAP_PAD) - 1;
    requestIdeas(projX, projY, e.clientX, e.clientY);
  });
}

function drawMap(data) {
  const showConcepts = $("#map-toggle-concepts").checked;
  const colorByTime = $("#map-toggle-time").checked;

  const entries = data.points.filter((p) => p.kind === "entry");
  const concepts = data.points.filter((p) => p.kind === "concept");

  // Date range for color mapping
  const ordinals = entries.map((p) => dateToOrdinal(p.date)).filter((o) => o > 0);
  const minD = Math.min(...ordinals);
  const maxD = Math.max(...ordinals);

  const xy = (p) => [
    (p.x + 1) * (MAP_VIEWBOX / 2 - MAP_PAD) + MAP_PAD,
    (p.y + 1) * (MAP_VIEWBOX / 2 - MAP_PAD) + MAP_PAD,
  ];

  const svgParts = [
    `<svg viewBox="${viewBoxString()}" xmlns="http://www.w3.org/2000/svg" id="map-svg" preserveAspectRatio="xMidYMid meet">`,
    `<rect x="-1000" y="-1000" width="3000" height="3000" fill="transparent" id="map-bg"/>`,
  ];

  // Entries
  for (const p of entries) {
    const [cx, cy] = xy(p);
    const r = RATING_RADIUS[Math.min(p.rating || 0, 5)] || RATING_RADIUS[0];
    let fill = "#9aa";
    if (colorByTime && minD < maxD) {
      const t = (dateToOrdinal(p.date) - minD) / (maxD - minD);
      fill = lerpColor(t);
    } else if (!colorByTime) {
      fill = p.rating >= 3 ? "#b35a23" : "#c8aa8a";
    }
    const stroke = p.private ? "#a13d2f" : "#1f1d17";
    svgParts.push(
      `<circle class="map-point map-point-entry" cx="${cx.toFixed(2)}" cy="${cy.toFixed(2)}" r="${r}" ` +
      `fill="${fill}" stroke="${stroke}" stroke-width="0.5" stroke-opacity="0.4" ` +
      `data-kind="entry" data-slug="${escapeAttr(p.slug)}" data-title="${escapeAttr(p.title)}" ` +
      `data-date="${escapeAttr(p.date)}" data-rating="${p.rating}" data-type="${escapeAttr(p.type)}"/>`
    );
  }

  // Concept centroids
  for (const p of concepts) {
    const [cx, cy] = xy(p);
    svgParts.push(
      `<circle class="map-point map-point-concept" cx="${cx.toFixed(2)}" cy="${cy.toFixed(2)}" r="${CONCEPT_RADIUS}" ` +
      `data-kind="concept" data-slug="${escapeAttr(p.slug)}" data-title="${escapeAttr(p.title)}"/>`
    );
    if (showConcepts) {
      svgParts.push(
        `<text class="map-concept-label" x="${cx.toFixed(2)}" y="${(cy + CONCEPT_RADIUS + 7).toFixed(2)}">${escapeText(p.title)}</text>`
      );
    }
  }

  svgParts.push("</svg>");
  svgParts.push('<div class="map-tooltip" id="map-tooltip"></div>');
  svgParts.push(`
    <div class="map-zoom-controls">
      <button id="map-zoom-in" title="Zoom in">+</button>
      <span class="zoom-level" id="map-zoom-level">${mapState.zoom.toFixed(1)}×</span>
      <button id="map-zoom-out" title="Zoom out">−</button>
      <button id="map-zoom-reset" title="Reset view">⌂</button>
    </div>
  `);

  $("#map-svg-wrap").innerHTML = svgParts.join("");
  $("#map-zoom-in").addEventListener("click", () => {
    const r = $("#map-svg-wrap").getBoundingClientRect();
    setZoom(mapState.zoom * 1.4, r.left + r.width / 2, r.top + r.height / 2);
  });
  $("#map-zoom-out").addEventListener("click", () => {
    const r = $("#map-svg-wrap").getBoundingClientRect();
    setZoom(mapState.zoom / 1.4, r.left + r.width / 2, r.top + r.height / 2);
  });
  $("#map-zoom-reset").addEventListener("click", () => resetZoom());

  // Bind interactions
  const svg = $("#map-svg");
  const tooltip = $("#map-tooltip");
  const wrap = $("#map-svg-wrap");

  $$(".map-point", svg).forEach((pt) => {
    pt.addEventListener("mouseenter", (e) => {
      const title = pt.dataset.title;
      const kind = pt.dataset.kind;
      let meta = "";
      if (kind === "entry") {
        const rating = "🍿".repeat(parseInt(pt.dataset.rating || "0"));
        meta = `${rating} · ${pt.dataset.date} · ${pt.dataset.type}`;
      } else {
        meta = "concept";
      }
      tooltip.innerHTML =
        `<div class="tooltip-title">${escapeText(title)}</div>` +
        `<div class="tooltip-meta">${escapeText(meta)}</div>`;
      tooltip.classList.add("visible");
      positionTooltip(tooltip, e, wrap);
    });
    pt.addEventListener("mousemove", (e) => positionTooltip(tooltip, e, wrap));
    pt.addEventListener("mouseleave", () => tooltip.classList.remove("visible"));
    pt.addEventListener("click", (e) => {
      e.stopPropagation();
      const kind = pt.dataset.kind;
      const slug = pt.dataset.slug;
      if (!slug) return;
      navigate(`${kind === "entry" ? "sources" : "concepts"}/${slug}`, true);
    });
  });

  // Note: empty-space click + drag are handled by bindMapInteractions on the wrap.
}

function positionTooltip(tooltip, e, wrap) {
  const rect = wrap.getBoundingClientRect();
  const x = e.clientX - rect.left + 14;
  const y = e.clientY - rect.top + 8;
  tooltip.style.left = `${x}px`;
  tooltip.style.top = `${y}px`;
}

function escapeText(s) {
  return String(s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function escapeAttr(s) {
  return escapeText(s);
}

async function requestIdeas(px, py, screenX, screenY) {
  const panel = $("#idea-panel");
  panel.innerHTML = `
    <div class="idea-panel">
      <h3>Ideas for this empty patch</h3>
      <div class="idea-meta">click at (${px.toFixed(2)}, ${py.toFixed(2)})</div>
      <div class="idea-loading">Asking Claude…</div>
    </div>
  `;
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });

  try {
    const res = await fetch("/api/wiki/ideas", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x: px, y: py, k: 6 }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    renderIdeas(data, px, py);
  } catch (err) {
    panel.innerHTML = `<div class="idea-panel"><h3>Ideas for this empty patch</h3>
      <div class="idea-loading">Failed: ${err.message}</div></div>`;
  }
}

function renderIdeas(data, px, py) {
  const panel = $("#idea-panel");
  const ideas = data.ideas || [];
  const nearestTitles = (data.nearest || []).map((n) => n.title).slice(0, 5).join(" · ");

  let html = `<div class="idea-panel">
    <h3>Ideas for this empty patch</h3>
    <div class="idea-meta">click at (${px.toFixed(2)}, ${py.toFixed(2)}) · nearest: ${escapeText(nearestTitles)}</div>`;

  if (ideas.length === 0) {
    html += `<div class="idea-loading">Claude returned no suggestions.</div>`;
  } else {
    for (const idea of ideas) {
      const q = encodeURIComponent(idea.search_query || idea.title || "");
      html += `<div class="idea-suggestion">
        <div class="idea-title">${escapeText(idea.title || "")}</div>
        <div class="idea-why">${escapeText(idea.why || "")}</div>
        ${q ? `<a class="idea-search" href="https://www.google.com/search?q=${q}" target="_blank" rel="noopener">search →</a>` : ""}
      </div>`;
    }
  }
  html += `</div>`;
  panel.innerHTML = html;
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
