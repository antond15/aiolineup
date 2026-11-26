"use strict";
// AIOLineup admin UI. No framework, no build step. All upstream data is
// inserted with textContent / attributes only, never innerHTML.

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// replaceChildren() turns null into the text "null"; drop empty children first.
function fill(el, ...children) {
  el.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

// Icons come from one same-origin sprite (static/icons.svg).
function icon(name, cls = "") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", `ico ${cls}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", `/static/icons.svg#i-${name}`);
  svg.append(use);
  return svg;
}
const btn = (iconName, label, props = {}) => h("button", props, iconName ? icon(iconName) : null, label ? h("span", { text: label }) : null);

class ApiError extends Error {}

// `raw` is a body that is already JSON text (a backup file), sent as it is.
async function api(method, path, body, raw) {
  const opts = { method, headers: { "X-Requested-With": "aiolineup" }, credentials: "same-origin" };
  if (body !== undefined || raw !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = raw !== undefined ? raw : JSON.stringify(body);
  }
  const res = await fetch("/api" + path, opts);
  let data = {};
  try { data = await res.json(); } catch { /* empty */ }
  if (res.status === 401 && path !== "/login") { showLogin(); throw new ApiError("Login required."); }
  if (!res.ok) throw new ApiError(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, ms = 2500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), ms);
}

function fail(err) { if (!(err instanceof ApiError && err.message === "Login required.")) toast(err.message || String(err), 4000); }

const fmtBytes = (n) => {
  if (!n && n !== 0) return "–";
  const u = ["B", "KiB", "MiB", "GiB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
};
const fmtAgo = (ts) => {
  if (!ts) return "never";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 0) return "in " + fmtDur(-s);
  return fmtDur(s) + " ago";
};
const fmtDur = (s) => s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`;
const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "never";
const fmtNum = (n) => (n === undefined || n === null ? "–" : Number(n).toLocaleString());
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); }
  catch { window.prompt("Copy this URL:", text); }
}

// A state is always an icon plus words, never colour alone.
const state = (kind, text, title) => h("span", { class: `state ${kind}`, title }, icon({ ok: "check", warn: "alert", err: "alert", none: "minus" }[kind] || "minus"), h("span", { text }));
// An icon-only state whose words show in a tooltip on hover, focus or tap. The
// tooltip is position: fixed so a scrolling table cannot clip it.
const statusTip = (kind, lines) => {
  lines = lines.filter(Boolean);
  const tip = h("span", { class: "tip", "aria-hidden": "true" }, lines.map((t) => h("span", { text: t })));
  const hide = () => tip.classList.remove("open");
  const show = () => {
    const r = el.getBoundingClientRect();
    tip.style.maxWidth = `${Math.min(320, innerWidth - 32)}px`;
    tip.style.top = `${r.top + r.height / 2}px`;
    tip.classList.add("open");
    // Right of the icon when it fits, else left of it, never off screen.
    const w = tip.offsetWidth;
    const left = r.right + 8 + w <= innerWidth - 16 ? r.right + 8 : Math.max(16, r.left - 8 - w);
    tip.style.left = `${left}px`;
  };
  const el = h("span", { class: `state tipper ${kind}`, tabindex: 0, role: "img", "aria-label": lines.map((t) => t.replace(/\.$/, "")).join(". "),
    onmouseenter: show, onclick: show, onmouseleave: () => document.activeElement !== el && hide(), onfocus: show, onblur: hide,
    onkeydown: (e) => e.key === "Escape" && hide() },
  icon({ ok: "check", warn: "alert", err: "alert", none: "minus" }[kind] || "minus"), tip);
  return el;
};
addEventListener("scroll", () => document.querySelectorAll(".tip.open").forEach((t) => t.classList.remove("open")), true);
// A static placeholder while a page's first data loads (the UI has no motion).
const loading = (text = "Loading…") => h("div", { class: "panel loading", role: "status", text });
const banner = (kind, text) => h("div", { class: `banner ${kind}`, role: kind === "err" ? "alert" : null }, icon("alert"), h("div", { text }));

// ------------------------------------------------------------------ shell

let pollTimer = null;
function setJob(job) {
  const el = $("#job");
  if (el.textContent !== (job || "Idle")) el.textContent = job || "Idle"; // a status region re-announces every write
  el.classList.toggle("busy", !!job);
}

function showLogin() {
  clearInterval(pollTimer);
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login input[name=username]").focus();
}

$("#login").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#login-error").textContent = "";
  try {
    await api("POST", "/login", { username: f.get("username"), password: f.get("password") });
    e.target.reset();
    $("#login").hidden = true;
    route();
  } catch (err) { $("#login-error").textContent = err.message; }
});

const views = {};
function route() {
  clearInterval(pollTimer);
  const m = location.hash.match(/^#\/(\w+)(?:\/(\w+))?/) || [];
  const name = m[1] || "overview";
  if (name === "epg") { location.replace("#/channels"); return; } // the guide page became My channels
  const view = views[name] || views.overview;
  document.querySelectorAll(".nav a").forEach((a) => {
    const on = a.dataset.view === (views[name] ? name : "overview");
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  $("#app").hidden = false;
  // Each visit renders into a new <main>, so a page still loading after the
  // user moved on writes into a detached element instead of the current page.
  const old = $("#main");
  const main = old.cloneNode(false);
  main.append(loading());
  old.replaceWith(main);
  window.scrollTo(0, 0);
  view(main, m[2]).catch((e) => {
    if (!main.isConnected) return;
    fail(e);
    const l = main.querySelector(".loading");
    if (l) l.textContent = "This page could not load. Reload to try again.";
  });
}
window.addEventListener("hashchange", route);

// A failed poll never stops the next one; a 401 stops it through showLogin().
// A page that finished loading after the user left it starts no poll.
function poll(main, fn, ms = 3000) {
  if (!main.isConnected) return;
  clearInterval(pollTimer);
  pollTimer = setInterval(() => fn().catch(() => {}), ms);
}

const pageHead = (title, ...actions) => h("div", { class: "page-head" }, h("h1", { text: title }), actions.length ? h("div", { class: "actions" }, ...actions) : null);

// ------------------------------------------------------------------ overview

views.overview = async (main) => {
  const revealed = new Set();
  let last = null, lastJson = "";

  const feedRow = (label, url) => h("div", { class: "feed" },
    h("span", { class: "name", text: label }),
    revealed.has(label) ? h("code", { text: url }) : h("code", { class: "hidden-url", text: "••••••••••••••••••••" }),
    btn(revealed.has(label) ? "eyeoff" : "eye", revealed.has(label) ? "Hide" : "Show", {
      "aria-label": `${revealed.has(label) ? "Hide" : "Show"} ${label} URL`,
      onclick: () => {
        if (revealed.has(label)) revealed.delete(label); else revealed.add(label);
        draw(last);
        const again = [...main.querySelectorAll("button")].find((b) => b.getAttribute("aria-label")?.endsWith(` ${label} URL`) && b.getAttribute("aria-label").startsWith(revealed.has(label) ? "Hide" : "Show"));
        if (again) again.focus();
      } }),
    btn("copy", "Copy", { "aria-label": `Copy ${label} URL`, onclick: () => copy(url) }));

  const stat = (title, rows) => h("section", { class: "panel stat" }, h("h3", { text: title }),
    h("dl", {}, ...rows.map(([k, v, sub]) => h("div", { class: "row" }, h("dt", { text: k }), h("dd", {}, v, sub ? h("small", { text: sub }) : null)))));

  const refreshAll = () => api("POST", "/refresh").then(() => { toast("Refresh queued"); return load(true); }).catch(fail);

  function draw(d) {
    const enabled = d.sources.filter((s) => s.enabled);
    const lastOk = Math.max(0, ...d.sources.map((s) => s.last_success || 0));
    const errors = d.sources.filter((s) => s.enabled && s.last_error && !(s.last_success > s.last_attempt - 1 && s.last_error.startsWith("Skipped")));
    const epg = d.epg || {};
    const refreshBtn = btn("refresh", "Refresh all", { onclick: refreshAll });
    const next = d.next_refresh ? `Next automatic refresh ${fmtAgo(d.next_refresh)}` : "Automatic refresh is off";
    let health;
    if (errors.length) {
      health = h("section", { class: "health bad", "aria-label": "Health" },
        h("div", { class: "head" }, icon("alert"), h("span", { text: `${errors.length} source${errors.length === 1 ? "" : "s"} failing` })),
        ...errors.map((s) => h("div", { class: "fail" },
          h("div", { class: "what" }, h("b", { text: `${s.name} (${s.kind.toUpperCase()})` }), h("span", { text: s.last_error }),
            h("div", { class: "muted small", text: s.last_success ? `Serving data from ${fmtAgo(s.last_success)}` : "Never fetched successfully" })),
          h("a", { class: "btn", href: `#/sources/${s.id}`, text: "Fix in Sources" }))),
        h("div", { class: "fail" }, h("span", { class: "muted small", text: next }), refreshBtn));
    } else if (!d.sources.length) {
      health = h("section", { class: "health", "aria-label": "Health" },
        h("div", { class: "msg" }, icon("minus"), h("span", { text: "No sources yet. Add an M3U playlist to get started." })),
        h("a", { class: "btn primary", href: "#/sources", text: "Add a source" }));
    } else {
      health = h("section", { class: "health ok", "aria-label": "Health" },
        h("div", { class: "msg" }, icon("check"), h("span", { text: lastOk ? `Feeds up to date · refreshed ${fmtAgo(lastOk)}` : "Waiting for the first refresh" })),
        h("div", { class: "actions" }, refreshBtn));
    }
    fill(main,
      pageHead("Overview"),
      health,
      ...(d.feed_warnings || []).map((w) => banner("warn", w)),
      d.metadata && d.metadata.error ? banner("warn", `Language metadata: ${d.metadata.error}`) : null,
      h("div", { class: "stats" },
        stat("Channels", [
          ["Available", fmtNum(d.available)],
          ["Selected", fmtNum(d.selected), d.selected !== d.selected_available ? `${d.selected - d.selected_available} missing upstream` : null],
          ["Enabled sources", `${enabled.length} of ${d.sources.length}`],
        ]),
        stat("Guide", [
          ["Selected with a guide", epg.selected ? `${epg.covered} of ${epg.selected}` : "–"],
          ["Programmes", fmtNum(epg.programmes)],
          ["Last build", epg.built_at ? fmtAgo(epg.built_at) : "–"],
        ]),
        stat("Output size", [
          ["Playlist", fmtBytes(d.playlist.size_bytes), `${fmtNum(d.playlist.streams ?? 0)} streams`],
          ["Guide", fmtBytes(epg.size_bytes), epg.gz_size_bytes !== undefined ? `${fmtBytes(epg.gz_size_bytes)} gzipped` : null],
        ])),
      h("h2", { text: "Feed URLs" }),
      h("div", { class: "panel feeds" }, feedRow("Playlist (M3U)", d.feeds.playlist), feedRow("Guide (XMLTV)", d.feeds.epg), feedRow("Guide (XMLTV .gz)", d.feeds.epg_gz)),
      h("p", { class: "note", text: "These URLs contain the feed token. Treat them like a password." + (errors.length ? "" : ` ${next}.`) }),
    );
  }

  async function load(force) {
    const d = await api("GET", "/overview");
    setJob(d.job);
    const json = JSON.stringify(d);
    last = d;
    if (force || json !== lastJson) { lastJson = json; draw(d); }
  }

  await load(true);
  poll(main, () => load(false), 5000);
};

// ------------------------------------------------------------------ channels

const chDefaults = { q: "", source: "", category: "", country: "", language: "", status: "all", epg: "" };
const chState = { ...chDefaults };

views.add = async (main) => {
  let offset = 0, total = 0, sources = [];
  const tbody = h("tbody");
  const summary = h("div", { class: "summary" });
  const more = h("div", { class: "more" });
  const chips = h("div", { class: "chips" });
  const selects = {};
  const segs = [];

  try { sources = (await api("GET", "/sources")).sources.filter((s) => s.kind === "m3u"); } catch (e) { fail(e); }

  const search = h("input", { type: "search", placeholder: "Search name or tvg-id", "aria-label": "Search channels by name or tvg-id", value: chState.q,
    oninput: debounce((e) => { chState.q = e.target.value; load(true); }, 250) });
  const mkSelect = (key, label) => {
    const s = h("select", { "aria-label": label, onchange: (e) => { chState[key] = e.target.value; syncControls(); load(true); } }, h("option", { value: "", text: label }));
    selects[key] = s; return s;
  };
  const srcSel = mkSelect("source", "All sources");
  for (const s of sources) srcSel.append(h("option", { value: String(s.id), text: s.name }));
  srcSel.value = chState.source;
  const seg = (key, opts) => {
    const wrap = h("div", { class: "seg", role: "group", "aria-label": key === "status" ? "Selection" : "Guide" });
    const buttons = opts.map(([v, label]) => h("button", { "data-v": v, "aria-pressed": String(chState[key] === v), class: chState[key] === v ? "on" : "", text: label,
      onclick: () => { chState[key] = v; syncControls(); load(true); } }));
    wrap.append(...buttons);
    segs.push({ key, buttons });
    return wrap;
  };

  const activeFilters = () => {
    const out = [];
    if (chState.source) out.push(["source", `Source: ${(sources.find((s) => String(s.id) === chState.source) || {}).name || chState.source}`]);
    for (const k of ["category", "country", "language"]) if (chState[k]) out.push([k, `${k[0].toUpperCase()}${k.slice(1)}: ${chState[k]}`]);
    if (chState.status !== "all") out.push(["status", chState.status === "selected" ? "Selected" : "Not selected"]);
    if (chState.epg) out.push(["epg", chState.epg === "yes" ? "With EPG" : "Without EPG"]);
    return out;
  };

  const toggleBtn = h("button", { class: "filters-toggle", "aria-expanded": "false", onclick: () => {
    const open = filters.classList.toggle("open");
    toggleBtn.setAttribute("aria-expanded", String(open));
  } });
  const filters = h("div", { class: "filters", id: "channel-filters" },
    srcSel, mkSelect("category", "All categories"), mkSelect("country", "All countries"), mkSelect("language", "All languages"),
    seg("status", [["all", "All"], ["selected", "Selected"], ["unselected", "Not selected"]]),
    seg("epg", [["", "Any EPG"], ["yes", "With EPG"], ["no", "Without EPG"]]),
    h("button", { class: "quiet", onclick: () => { Object.assign(chState, chDefaults); syncControls(); load(true); }, text: "Reset filters" }));
  toggleBtn.setAttribute("aria-controls", "channel-filters");

  function syncControls() {
    search.value = chState.q;
    for (const [k, s] of Object.entries(selects)) s.value = chState[k];
    for (const { key, buttons } of segs) for (const b of buttons) {
      const on = b.dataset.v === chState[key];
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    }
    const act = activeFilters();
    fill(toggleBtn, icon("filter"), h("span", { text: act.length ? `Filters (${act.length})` : "Filters" }));
    fill(chips, act.map(([key, label]) => h("span", { class: "chip" }, label,
      h("button", { "aria-label": `Remove filter ${label}`, onclick: () => { chState[key] = chDefaults[key]; syncControls(); load(true); } }, icon("x")))));
    chips.hidden = !act.length;
  }

  fill(main,
    pageHead("Add channels"),
    h("div", { class: "filterbar" },
      h("div", { class: "searchbox" }, icon("search"), search), toggleBtn, filters),
    chips,
    summary,
    h("div", { class: "panel rt rt-ch selectable" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", { class: "c-cb" }, h("span", { class: "sr", text: "Selected" })), h("th", { class: "c-logo" }, h("span", { class: "sr", text: "Logo" })), h("th", { text: "Channel" }), h("th", { text: "Country / language" }), h("th", { class: "hide-md", text: "Categories" }), h("th", { text: "Streams" }), h("th", { text: "EPG", title: "Programme data found in the last guide build" }))),
      tbody)),
    more,
  );
  syncControls();

  const onKey = (e) => { if (e.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) { e.preventDefault(); search.focus(); } };
  if (main.isConnected) { // the user may have left while the sources loaded
    document.addEventListener("keydown", onKey);
    window.addEventListener("hashchange", () => document.removeEventListener("keydown", onKey), { once: true });
  }

  const params = () => new URLSearchParams(Object.entries(chState).filter(([, v]) => v)).toString();

  function fillFacets(facets) {
    for (const [key, facet] of [["category", "categories"], ["country", "countries"], ["language", "languages"]]) {
      const sel = selects[key];
      const label = sel.options[0].text;
      const opts = facets[facet].map(([v, n]) => h("option", { value: v, text: `${v} (${n})` }));
      // A category edit can empty the active filter's value; keep it visible while it filters.
      if (chState[key] && !facets[facet].some(([v]) => v === chState[key])) opts.push(h("option", { value: chState[key], text: `${chState[key]} (0)` }));
      sel.replaceChildren(h("option", { value: "", text: label }), ...opts);
      sel.value = chState[key];
    }
  }

  const renderMore = () => more.replaceChildren(offset < total ? h("button", { onclick: () => load(false), text: `Load more (${total - offset} remaining)` }) : "");

  async function load(reset) {
    if (reset) offset = 0;
    const d = await api("GET", `/channels?${params()}&offset=${offset}&limit=100`).catch(fail);
    if (!d) return;
    total = d.total;
    if (reset) {
      tbody.replaceChildren();
      fillFacets(d.facets);
      for (const m of d.missing) tbody.append(missingRow(m));
      if (!d.items.length && !d.missing.length) tbody.append(h("tr", {}, h("td", { colspan: 7, class: "empty", text: d.available ? "No channels match these filters." : "No channels yet. Add or refresh an M3U source." })));
    }
    for (const it of d.items) tbody.append(row(it));
    offset += d.items.length;
    renderSummary(d.selected);
    renderMore();
  }

  function renderSummary(selected) {
    const filtered = Object.entries(chState).some(([k, v]) => v && !(k === "status" && v === "all"));
    fill(summary,
      h("span", {}, h("b", { text: fmtNum(total) }), filtered ? " matching" : " channels"),
      h("span", {}, h("b", { text: fmtNum(selected) }), " selected"),
      h("span", { class: "spacer" }),
      filtered && total ? btn("plus", `Select all ${total} matching`, { onclick: () => bulk("select") }) : null,
      filtered && total ? btn("minus", `Deselect all ${total} matching`, { onclick: () => bulk("deselect") }) : null,
    );
  }

  async function bulk(action) {
    if (total > 100 && !confirm(`${action === "select" ? "Select" : "Deselect"} ${total} channels?`)) return;
    const d = await api("POST", "/selection", { filter: chState, action }).catch(fail);
    if (d) { toast(`${d.changed} channel(s) ${action}ed`); load(true); }
  }

  async function toggle(it, tr, cb) {
    const want = cb.checked;
    try {
      const d = await api("POST", "/selection", want ? { select: [it.key] } : { deselect: [it.key] });
      it.selected = want;
      it.number = null;
      tr.classList.toggle("selected", want);
      renderSummary(d.selected);
      // A new selection gets the next free number; show it on the row.
      if (want) {
        const one = await api("GET", `/channel?key=${encodeURIComponent(it.key)}`).catch(() => null);
        if (one && cb.checked) it.number = one.number;
      }
      numberTag(tr, it);
    } catch (e) { cb.checked = !want; fail(e); }
  }

  // The channel number of a selected row, a small tag under the name.
  function numberTag(tr, it) {
    const cell = tr.querySelector(".c-name");
    cell.querySelector(".chnum")?.remove();
    if (it.selected && it.number) cell.querySelector(".chname").after(h("span", { class: "chnum", text: `Number ${it.number}` }));
  }

  function row(it) {
    const cb = h("input", { type: "checkbox", checked: it.selected, "aria-label": `Select ${it.name}` });
    const tr = h("tr", { class: it.selected ? "selected" : "" });
    cb.addEventListener("change", () => toggle(it, tr, cb));
    // Tapping anywhere on the row toggles it, except the controls inside it.
    tr.addEventListener("click", (e) => {
      if (e.target.closest("button, input, a") || String(getSelection()).length) return;
      cb.checked = !cb.checked;
      toggle(it, tr, cb);
    });
    const logo = it.logo ? h("img", { class: "logo", src: it.logo, loading: "lazy", referrerpolicy: "no-referrer", alt: "", onerror: (e) => e.target.remove() }) : "";
    const streamsText = h("span", { class: "muted", text: `${it.streams} ${it.streams === 1 ? "stream" : "streams"}` });
    const epg = it.epg ? state("ok", "Guide", `${it.epg.epg_id} (${it.epg.programmes} programmes)`)
      : it.selected ? state("warn", "No guide") : h("span", { class: "muted", text: "–" });
    // Every shown category is sent to the playlist; an own list says it was edited.
    const catTags = [...it.categories.map((c) => h("span", { class: "tag", text: c })),
      it.category_edited ? h("span", { class: "tag edited", text: "edited" }) : null].filter(Boolean);
    const plainTags = [...it.countries, ...it.languages].map((c) => h("span", { class: "tag", text: c }));
    const tags = [...plainTags, ...catTags].map((t) => t.cloneNode(true));
    const tagCell = (cls, label, els) => h("td", { class: cls, "data-label": label }, els.length ? h("div", { class: "tags" }, ...els) : null);
    tr.append(
      h("td", { class: "c-cb" }, cb),
      h("td", { class: "c-logo" }, logo),
      h("td", { class: "c-name" }, h("div", { class: "chname", text: it.name }), h("div", { class: "chid", text: it.tvg_id || "no tvg-id" }),
        // Phone cards show every tag in one line under the name instead of the two captioned cells.
        tags.length ? h("div", { class: "tags ph-tags" }, ...tags) : null),
      tagCell("c-tags", "Country / language", plainTags),
      tagCell("c-cats hide-md", "Categories", catTags),
      h("td", { class: "c-streams" }, streamsText),
      h("td", { class: "c-epg" }, epg),
    );
    numberTag(tr, it);
    return tr;
  }

  function missingRow(m) {
    const cb = h("input", { type: "checkbox", checked: true, "aria-label": `Selected: ${m.name} (missing upstream)` });
    const tr = h("tr", { class: "selected missing" },
      h("td", { class: "c-cb" }, cb),
      h("td", { colspan: 6 }, h("div", { class: "chname", text: m.name }), h("span", { class: "tag miss", text: "Missing from all enabled sources. Kept selected, returns automatically." })));
    cb.addEventListener("change", async () => {
      try { await api("POST", "/selection", { deselect: [m.key] }); tr.remove(); } catch (e) { cb.checked = true; fail(e); }
    });
    return tr;
  }

  await load(true);
};

// ------------------------------------------------------------------ sources

views.sources = async (main, targetId) => {
  // The 4 s poll rebuilds the tables. The add form and rows being edited are
  // kept as the same nodes so typed values survive a rebuild.
  const editing = new Map(); // source id -> its edit row
  let form = null, formOpen = false, last = null, lastJson = "", scrolled = false;

  const draw = () => {
    const d = last;
    const section = (kind, title, help) => {
      const list = d.sources.filter((s) => s.kind === kind);
      return [
        h("h2", { text: title }),
        h("p", { class: "note", text: help }),
        h("div", { class: "panel rt rt-src scroll" }, h("table", {},
          h("thead", {}, h("tr", {}, ...["On", "Name / URL", "Priority", "Status", "Items", "In use", "Size", "Actions"].map((t) => h("th", { text: t })))),
          h("tbody", {}, list.length ? list.map(sourceRow) : h("tr", {}, h("td", { colspan: 8, class: "empty", text: "No sources." }))))),
      ];
    };
    form ??= addForm();
    for (const id of editing.keys()) if (!d.sources.some((s) => s.id === id)) editing.delete(id);
    fill(main,
      pageHead("Sources", h("button", { class: "primary", "aria-expanded": String(formOpen), "aria-controls": "add-source", onclick: () => { formOpen = !formOpen; draw(); if (formOpen) form.querySelector("input[name=name]").focus(); } },
        icon(formOpen ? "x" : "plus"), h("span", { text: formOpen ? "Close" : "Add source" }))),
      formOpen ? form : null,
      ...section("m3u", "M3U playlists", "Channels from all enabled playlists are merged. Lower priority numbers come first when ordering stream alternatives."),
      ...section("xmltv", "XMLTV guides", "Guides may be plain or .gz. Each selected channel takes its programmes from the first guide (lowest priority number) that has data for it."),
    );
    if (targetId && !scrolled) {
      const t = main.querySelector(`[data-source-id="${targetId}"]`);
      if (t) { scrolled = true; t.scrollIntoView({ block: "center" }); }
    }
  };

  const render = async () => {
    const d = await api("GET", "/sources");
    setJob(d.job);
    const json = JSON.stringify(d);
    // Keep the DOM (keyboard focus, an open status tooltip) when nothing changed and focus is inside the page.
    const keep = json === lastJson && ((main.contains(document.activeElement) && document.activeElement !== document.body) || !!main.querySelector(".tip.open"));
    last = d; lastJson = json;
    if (!keep) draw();
  };

  const sourceRow = (s) => {
    if (editing.has(s.id)) return editing.get(s.id);
    const tr = h("tr", { "data-source-id": s.id, class: [String(s.id) === targetId ? "target" : "", s.channels_used === 0 ? "unused" : ""].join(" ").trim() || null });
    const status = s.pending ? statusTip("none", ["Queued…"])
      : s.last_error ? statusTip(s.last_error.startsWith("Skipped") ? "warn" : "err",
        [s.last_error, fmtDate(s.last_attempt), s.last_success ? `Using data from ${fmtAgo(s.last_success)}` : null])
      : s.last_success ? statusTip("ok", [`OK · ${fmtAgo(s.last_success)}`, fmtDate(s.last_success)])
      : statusTip("none", ["Never fetched"]);
    const unit = s.kind === "m3u" ? "playlist" : "guide";
    const inUse = s.channels_used === null
      ? h("span", { class: "muted", title: `Disabled ${unit}`, text: "—" })
      : s.channels_used === 0 ? state("warn", "0", `No selected channel uses this ${unit}`)
      : h("span", { title: `Selected channels using this ${unit}`, text: fmtNum(s.channels_used) });
    const en = h("input", { type: "checkbox", checked: !!s.enabled, "aria-label": `Enabled: ${s.name}`, onchange: (e) => api("PUT", `/sources/${s.id}`, { enabled: e.target.checked }).then(render).catch(fail) });
    tr.append(
      h("td", { class: "c-on" }, h("label", { class: "chk" }, en, h("span", { class: "enabled-text", "aria-hidden": "true", text: "Enabled" }))),
      h("td", { class: "c-name" }, h("div", { class: "chname", text: s.name }), h("div", { class: "chid url-cell", title: s.url, text: s.url })),
      h("td", { class: "c-prio", "data-label": "Priority", text: s.priority }),
      h("td", { class: "c-status", "data-label": "Status" }, status),
      h("td", { class: "c-items", "data-label": "Items", text: fmtNum(s.item_count) }),
      h("td", { class: "c-used", "data-label": "In use" }, inUse),
      h("td", { class: "c-size", "data-label": "Size", text: fmtBytes(s.size_bytes) }),
      h("td", { class: "c-act" }, h("div", { class: "row-actions" },
        btn("refresh", null, { class: "icon", title: "Refresh", "aria-label": `Refresh ${s.name}`, onclick: () => api("POST", `/sources/${s.id}/refresh`).then(() => { toast("Refresh queued"); return render(); }).catch(fail) }),
        btn("edit", null, { class: "icon", title: "Edit", "aria-label": `Edit ${s.name}`, onclick: () => { const er = editRow(s); editing.set(s.id, er); tr.replaceWith(er); er.querySelector("input").focus(); } }),
        btn("trash", null, { class: "icon danger", title: "Delete", "aria-label": `Delete ${s.name}`, onclick: () => confirm(`Delete source “${s.name}”? Selections are kept.`) && api("DELETE", `/sources/${s.id}`).then(render).catch(fail) }))),
    );
    return tr;
  };

  const field = (label, input, hint) => h("label", {}, h("span", { text: label }), input, hint ? h("span", { class: "hint", text: hint }) : null);

  const editRow = (s) => {
    const name = h("input", { value: s.name, maxlength: 100 });
    const url = h("input", { value: s.url, type: "url" });
    const prio = h("input", { type: "number", value: s.priority });
    return h("tr", { class: "edit", "data-source-id": s.id }, h("td", { colspan: 8 }, h("div", { class: "editform" },
      h("div", { class: "formgrid" }, field("Name", name), field("URL", url), field("Priority", prio, "Lower is preferred")),
      h("div", { class: "formactions" },
        h("button", { class: "primary", onclick: () => api("PUT", `/sources/${s.id}`, { name: name.value, url: url.value, priority: Number(prio.value) }).then(() => { editing.delete(s.id); return render(); }).catch(fail), text: "Save" }),
        h("button", { onclick: () => { editing.delete(s.id); draw(); }, text: "Cancel" })))));
  };

  const addForm = () => {
    const kind = h("select", { name: "kind" }, h("option", { value: "m3u", text: "M3U playlist" }), h("option", { value: "xmltv", text: "XMLTV guide" }));
    const name = h("input", { name: "name", placeholder: "e.g. iptv-org US", required: true, maxlength: 100 });
    const url = h("input", { name: "url", placeholder: "https://…", required: true, type: "url" });
    const prio = h("input", { name: "priority", type: "number", value: 100 });
    return h("form", { id: "add-source", class: "panel pad formpanel", onsubmit: async (e) => {
      e.preventDefault();
      try {
        await api("POST", "/sources", { kind: kind.value, name: name.value, url: url.value, priority: Number(prio.value) });
        e.target.reset(); formOpen = false; toast("Source added, fetching…"); render();
      } catch (err) { fail(err); }
    } },
      h("div", { class: "formgrid" }, field("Type", kind), field("Name", name), field("URL", url), field("Priority", prio, "Lower is preferred. Default 100.")),
      h("div", { class: "formactions" },
        h("button", { class: "primary", type: "submit", text: "Add source" }),
        h("button", { type: "button", onclick: () => { formOpen = false; draw(); }, text: "Cancel" })));
  };

  await render();
  // activeElement (unlike :focus) still points at the field while the window is in the background.
  poll(main, async () => { if (!main.contains(document.activeElement) || !document.activeElement.matches("input:not([type=checkbox]), select, textarea")) await render(); }, 4000);
};

// ------------------------------------------------------------------ my channels

const epgState = { filter: "all", q: "" };
const MAX_STREAMS = 8; // AIOStreams offers the first 8 distinct URLs of a channel
const MAX_CATEGORIES = 8; // and keeps the first 8 group-title parts
const epgClock = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const epgNow = (n) => n ? `${epgClock.format(n.start * 1000)}–${epgClock.format(n.stop * 1000)} ${n.title || ""}`.trim() : null;

// One word per row: which group a selected channel belongs to and how it reads.
function epgInfo(c, pending) {
  const dg = c.diagnosis;
  if (!c.available) return { group: "gone", kind: "warn", text: "Not in playlist" };
  if (c.mapping === "") return { group: "off", kind: "none", text: "No guide" };
  if (c.coverage) return { group: "matched" };
  const n = dg ? dg.suggestions.length : 0;
  if (n) return { group: "needs", kind: "warn", text: `${n} ${n === 1 ? "match" : "matches"}` };
  // A rebuild is queued or running: usable data was found, so the row is about to be matched.
  if (dg && pending && dg.found.some((f) => f.usable)) return { group: "needs", kind: "none", text: "Waiting for rebuild" };
  if (dg && dg.found.length) return { group: "needs", kind: "warn", text: "No current data" };
  return { group: "needs", kind: "err", text: "Not found" };
}

views.channels = async (main) => {
  const phone = window.matchMedia("(max-width: 1099.98px)"); // below this the panel is a bottom sheet (same step as the CSS)
  const saved = new Map(); // channel key -> time saved; the row reads "Saved · rebuilding" meanwhile
  let data = null, lastJson = "", openKey = null, panelSeq = 0;
  let panelNumber = null; // set while a panel is open: shows a number saved from the row
  let panelState = null; // while a panel is open: whether it has unsaved overrides, and the channel's name
  let meta = new Map(); // selected channel key -> logo and category from /api/channels
  let categories = []; // category suggestions for the panel's field

  // /api/epg has the lineup and guide state; logos and categories come from the catalogue.
  async function loadMeta() {
    const next = new Map();
    for (let offset = 0; ; offset += 500) {
      const d = await api("GET", `/channels?status=selected&sort=number&limit=500&offset=${offset}`);
      for (const it of d.items) next.set(it.key, it);
      if (!offset) { categories = d.facets.categories.map(([v]) => v); fill(catNames, categories.map((v) => h("option", { value: v }))); }
      if (offset + 500 >= d.total) break;
    }
    meta = next;
  }
  const catNames = h("datalist", { id: "cat-names" });

  const top = h("div");
  const rows = h("div", { class: "lineup-list panel" });
  const aside = h("aside", { class: "lineup-panel", hidden: true, tabindex: "-1" });
  const backdrop = h("div", { class: "lineup-backdrop", hidden: true, onclick: () => closePanel() });
  const split = h("div", { class: "lineup-split" }, rows, aside);
  const segWrap = h("div", { class: "seg", role: "group", "aria-label": "Show channels" });
  const search = h("input", { type: "search", placeholder: "Search name, id or number", "aria-label": "Search My channels by name, id or number", value: epgState.q,
    oninput: debounce((e) => { epgState.q = e.target.value; drawList(); }, 150) });
  const tools = h("div", { class: "filterbar" }, segWrap, h("div", { class: "searchbox" }, icon("search"), search));
  const head = pageHead("My channels", btn("refresh", "Rebuild guide", { onclick: () => api("POST", "/epg/rebuild").then(() => toast("Rebuild queued")).catch(fail) }));
  fill(main, head, loading("Loading your channels…")); // the full page appears with the first data

  // The saved guide choice against what the last build used: Off but still covered, or a
  // chosen id other than the covered one. Automatic cannot be told apart that way, so `saved`
  // (set right after a save) covers it until the build has had time to run.
  const disagrees = (c) => {
    const cov = c.coverage ? c.coverage.epg_id.toLowerCase() : null;
    if (c.mapping === "") return cov !== null;
    // The same id from another guide is a change too.
    if (typeof c.mapping === "string") return cov !== c.mapping.toLowerCase() || (!!c.mapping_source && !!c.coverage && c.coverage.source !== c.mapping_source);
    return false;
  };
  const rebuilding = (c) => saved.has(c.key) || (!!data.epg_pending && disagrees(c));
  const rebuildState = () => h("span", { class: "state none" }, h("span", { class: "spin", "aria-hidden": "true" }), h("span", { text: "Rebuilding" }));

  const byNumber = (a, b) => a.number - b.number || a.key.localeCompare(b.key);
  const groups = () => {
    const g = { needs: [], matched: [], off: [], gone: [] };
    for (const c of data.channels) g[epgInfo(c, data.epg_pending).group].push(c);
    return g;
  };

  function drawTop() {
    const d = data, st = d.stats || {};
    const matched = d.channels.filter((c) => c.coverage).length;
    const enabled = (d.guide_sources || []).filter((s) => s.enabled);
    const parts = [];
    if (st.built_at) {
      parts.push(`${fmtNum(st.programmes)} programmes`, `${fmtBytes(st.size_bytes)} (${fmtBytes(st.gz_size_bytes)} gz)`, `built ${fmtAgo(st.built_at)}`);
    }
    fill(top,
      !d.has_sources ? banner("warn", "No XMLTV sources configured. Add one on the Sources page.") : null,
      ...(d.feed_warnings || []).map((w) => banner("warn", w)),
      st.errors && Object.keys(st.errors).length ? banner("err", "Guide filtering errors: " + Object.values(st.errors).join("; ")) : null,
      ...(d.guide_sources || []).filter((s) => s.enabled && (!s.cached || s.error)).map((s) => banner(s.cached ? "warn" : "err",
        s.cached ? `Guide source "${s.name}": last refresh failed (${s.error}); using the copy from ${fmtAgo(s.last_success)}.`
          : `Guide source "${s.name}" has never been downloaded successfully${s.error ? `: ${s.error}` : " yet"}. It contributes nothing.`)),
      d.channels.length ? h("section", { class: "panel lineup-health", "aria-label": "Guide coverage" },
        h("div", { class: "big", text: `${matched} of ${d.channels.length} matched` }),
        h("div", { class: "muted small lineup-meta" }, ...parts.map((p) => h("span", { text: p })),
          h("a", { href: "#/sources", text: `${enabled.length} ${enabled.length === 1 ? "guide" : "guides"}` }))) : null);
  }

  function drawList() {
    const g = groups();
    const all = data.channels.slice().sort(byNumber);
    const opts = [["all", "All", all.length], ["needs", "Needs guide", g.needs.length], ["gone", "Missing", g.gone.length]];
    if (!segWrap.children.length) segWrap.append(...opts.map(([v, label]) => h("button", { "data-v": v, onclick: () => { epgState.filter = v; drawList(); } }, label, " ", h("span", { class: "n" }))));
    opts.forEach(([v, , n], i) => {
      const b = segWrap.children[i], on = epgState.filter === v;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
      b.lastElementChild.textContent = String(n);
    });
    const active = document.activeElement?.dataset;
    const keep = active?.key, keepNum = active?.numkey;
    const q = epgState.q.trim().toLowerCase();
    const list = (epgState.filter === "all" ? all : g[epgState.filter] || []).filter((c) => !q || String(c.number) === q || [c.name, c.out_id, c.tvg_id].some((s) => (s || "").toLowerCase().includes(q)));
    fill(rows, list.length ? list.map(lineRow)
      : h("div", { class: "empty" }, !all.length ? ["No channels yet. Pick some on ", h("a", { href: "#/add", text: "Add channels" }), "."] : "No channels here."));
    if (keep) rowFor(keep)?.focus();
    else if (keepNum) rows.querySelector(`input[data-numkey="${CSS.escape(keepNum)}"]`)?.focus();
  }

  // One row: the number as its own field, then a button that opens the channel's panel.
  function lineRow(c) {
    const info = epgInfo(c, data.epg_pending);
    const sel = c.key === openKey;
    const m = meta.get(c.key);
    const sub = info.group === "matched" ? `${c.coverage.epg_id} · ${c.coverage.source}` : (c.out_id || c.tvg_id);
    const num = h("input", { type: "text", inputmode: "numeric", class: "numfield", value: String(c.number), maxlength: 4, "data-numkey": c.key, "aria-label": `Number of ${c.name}`,
      title: "Type a number and press Enter",
      onkeydown: (e) => {
        if (e.key === "Enter") { e.preventDefault(); setNumber(c, num); }
        else if (e.key === "Escape") { num.value = String(c.number); e.stopPropagation(); }
      },
      onblur: () => { if (!num.dataset.busy) num.value = String(c.number); } });
    const logo = m && m.logo ? h("img", { class: "logo", src: m.logo, loading: "lazy", referrerpolicy: "no-referrer", alt: "", onerror: (e) => e.target.remove() }) : h("span", { class: "logo none" });
    // The first category, then +N whose title (and hidden text) lists the rest.
    const cats = m ? m.categories : [], rest = cats.slice(1);
    const cat = cats.length ? h("span", { class: "lcats" }, h("span", { class: "tag", text: cats[0] }),
      rest.length ? h("span", { class: "tag plus", text: `+${rest.length}`, title: rest.join(", "), "aria-hidden": "true" }) : null,
      rest.length ? h("span", { class: "sr", text: `, also ${rest.join(", ")}` }) : null) : null;
    return h("div", { class: `lineup-row lrow${sel ? " sel" : ""}` },
      num,
      h("button", { class: "lopen", "data-key": c.key, "aria-expanded": String(sel), onclick: () => openPanel(c) },
        logo,
        h("span", { class: "who" }, h("span", { class: "chname", text: c.name }), h("span", { class: "chid", text: sub })),
        cat,
        rebuilding(c) ? rebuildState() : info.group === "matched" ? state("ok", "Guide", `${c.coverage.epg_id} (${c.coverage.source})`) : info.text ? state(info.kind, info.text) : null,
        icon("chevron", "go")));
  }

  const rowFor = (key) => [...rows.querySelectorAll("button.lopen")].find((b) => b.dataset.key === key);

  // Typing a number and pressing Enter moves the channel; the list re-sorts and the field keeps focus.
  async function setNumber(c, input) {
    const v = input.value.trim();
    if (v === String(c.number)) return;
    if (!/^\d{1,4}$/.test(v) || Number(v) < 1) { input.value = String(c.number); return toast("Number must be a whole number from 1 to 9999.", 4000); }
    input.dataset.busy = "1";
    try { await api("PUT", "/channel/number", { key: c.key, number: Number(v) }); }
    catch (e) { delete input.dataset.busy; input.value = String(c.number); return fail(e); }
    delete input.dataset.busy;
    toast(`${c.name} is now number ${v}`);
    c.number = Number(v);
    input.defaultValue = v; // saved, so the redraw below may replace the field
    if (c.key === openKey && panelNumber) panelNumber(c.number);
    await load(true);
  }

  let saving = false;
  let guideView = null; // "chosen" while the search is open before anything is saved
  let lastGuideSig = "";
  async function save(c, epg_id, source_id = null) {
    if (saving) return;
    saving = true;
    try { await api("PUT", "/epg/mapping", { key: c.key, epg_id, source_id }); } catch (e) { fail(e); return; } finally { saving = false; }
    saved.set(c.key, Date.now());
    guideView = null; // the switch follows the saved choice again
    const row = data.channels.find((x) => x.key === c.key);
    if (row) { row.mapping = epg_id; row.mapping_source = null; }
    toast("Saved. The guide rebuilds in a few seconds.");
    drawList();
    if (openKey === c.key) renderGuide(c, epg_id === null ? "auto" : epg_id === "" ? "off" : "chosen");
    load(true).catch(fail); // epg_pending now says a rebuild is queued
  }

  // A guide channel (suggestion, search result or current): name, id and source, what is on now, Use.
  function candidate(it, c, primary) {
    const now = epgNow(it.now);
    return h("div", { class: "cand" },
      h("div", { class: "main" }, h("div", { class: "chname", text: it.display_name || it.epg_id }), h("div", { class: "chid", text: `${it.epg_id} · ${it.source}` }),
        now ? h("div", { class: "muted small", text: now }) : it.programmes === 0 ? h("div", { class: "muted small", text: "No programmes" }) : null),
      h("button", { class: primary ? "primary" : "", "aria-label": `Use ${it.display_name || it.epg_id} for ${c.name}`, text: "Use", onclick: () => save(c, it.epg_id, it.source_id) }));
  }

  const guideMode = (c) => (c.mapping == null ? "auto" : c.mapping === "" ? "off" : "chosen");

  // The guide section of the panel: a switch Automatic / Chosen / Off that follows the saved
  // choice (not the last build). Automatic and Off save on click; Chosen opens the search and
  // only a Use saves.
  function renderGuide(c, focusMode) {
    const box = $("#pn-guide");
    if (!box) return;
    c = data.channels.find((x) => x.key === c.key) || c;
    const mode = guideMode(c), view = guideView || mode, pend = rebuilding(c);
    lastGuideSig = guideSig(c);
    const dg = c.diagnosis, cov = c.coverage;
    const sugg = view === "auto" && dg ? dg.suggestions : [];
    const shown = new Set(sugg.map((s) => `${s.source_id}|${s.epg_id}`));
    let seq = 0;
    const pick = (v) => {
      if (v === view) return;
      if (v === "chosen") { guideView = "chosen"; return renderGuide(c, "chosen"); }
      if (v === mode) { guideView = null; return renderGuide(c, v); }
      save(c, v === "auto" ? null : "");
    };
    const seg = h("div", { class: "seg fill", role: "group", "aria-label": `Guide for ${c.name}` },
      ...[["auto", "Automatic"], ["chosen", "Chosen"], ["off", "Off"]].map(([v, label]) => h("button", { class: view === v ? "on" : "", "aria-pressed": String(view === v), "data-mode": v, text: label, onclick: () => pick(v) })));
    const results = h("div", { class: "cand-list" });
    const input = h("input", { type: "search", "aria-label": `Search guide channels for ${c.name}`, value: c.name });
    const run = async () => {
      const q = input.value.trim(), mine = ++seq;
      if (q.length < 2) { fill(results); return; }
      const d = await api("GET", `/epg/search?q=${encodeURIComponent(q)}`).catch(fail);
      if (!d || mine !== seq) return;
      const hits = d.results.filter((r) => !shown.has(`${r.source_id}|${r.epg_id}`));
      const exact = d.results.some((r) => r.epg_id.toLowerCase() === q.toLowerCase());
      fill(results, hits.map((r) => candidate(r, c, false)),
        !hits.length ? h("div", { class: "muted small", text: d.results.length ? "Already listed above." : "No guide channels found." }) : null,
        !exact ? h("div", { class: "cand" }, h("div", { class: "main" }, h("div", { class: "chid", text: q })),
          h("button", { "aria-label": `Use ${q} as the guide id for ${c.name}`, text: "Use as id", onclick: () => save(c, q) })) : null);
    };
    input.addEventListener("input", debounce(run, 250));
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); run(); } }); // Enter searches; only "Use as id" saves a typed id
    // What the last build used is stale while a rebuild is due, so it is not shown then.
    const f0 = dg && dg.found[0];
    const ready = dg && dg.found.find((f) => f.usable); // listed with current programmes, not built in yet
    const none = dg && !dg.candidates.length ? "The playlist gives this channel no guide id."
      : ready ? `${ready.source} lists ${ready.epg_id}. It applies after the rebuild.`
      : f0 ? `${f0.source} lists ${f0.epg_id} but has no current programmes.`
      : dg ? `No guide lists ${dg.candidates.join(", ")}.` : null;
    let body = null;
    if (view === "auto" && !pend) {
      body = [
        cov ? h("div", { class: "cand cur" }, h("div", { class: "main" },
          h("div", { class: "muted small", text: "Matched by the playlist id" }), h("div", { class: "chname", text: cov.epg_id }), h("div", { class: "chid", text: cov.source }),
          cov.now ? h("div", { class: "muted small", text: epgNow(cov.now) }) : null)) : none ? h("p", { class: "muted small", text: none }) : null,
        sugg.length ? h("div", {}, h("h4", { text: "Similar names" }), ...sugg.map((s, i) => candidate(s, c, i === 0))) : null,
      ];
    } else if (view === "chosen") {
      body = [
        mode === "chosen" ? h("div", { class: "cand cur" }, h("div", { class: "main" },
          h("div", { class: "muted small", text: "Your choice" }), h("div", { class: "chname", text: c.mapping }),
          c.mapping_source ? h("div", { class: "chid", text: c.mapping_source }) : null,
          !pend && cov && cov.now ? h("div", { class: "muted small", text: epgNow(cov.now) }) : null)) : null,
        h("div", {}, h("h4", { text: mode === "chosen" ? "Change" : "Search guides" }), h("div", { class: "searchbox" }, icon("search"), input), results),
      ];
    }
    fill(box,
      h("h3", { text: "Guide" }),
      seg,
      ...(body || []),
      pend ? h("p", { class: "hint", text: "Applies after the rebuild" }) : null);
    if (focusMode) box.querySelector(`[data-mode="${focusMode}"]`)?.focus();
    if (view === "chosen") run();
  }
  // What the open guide section shows: it is redrawn when this changes, not on every poll.
  const guideSig = (c) => JSON.stringify([c.mapping, c.coverage && [c.coverage.epg_id, c.coverage.now && c.coverage.now.start], rebuilding(c), c.diagnosis && c.diagnosis.suggestions.map((s) => [s.source_id, s.epg_id, s.now && s.now.start]), c.diagnosis && c.diagnosis.found.map((f) => f.usable)]);

  // The panel: one channel, saved field by field. Sections are separate boxes so a save
  // redraws only its own section and keyboard focus stays where it was.
  async function openPanel(c) {
    if (!confirmDiscard()) return;
    const mine = ++panelSeq;
    const det = await api("GET", `/channel?key=${encodeURIComponent(c.key)}`).catch(fail);
    if (!det || mine !== panelSeq) return;
    openKey = c.key;
    const m = () => meta.get(c.key);
    const live = !!c.available;
    const title = h("h2", { id: "lineup-pn-title", text: det.name || c.name });
    const sec = (id, ...kids) => h("section", { class: "pn-sec", id }, ...kids);
    const num = sec("pn-number"), edit = sec("pn-edit"), streams = sec("pn-streams"), guide = sec("pn-guide");
    guide.tabIndex = -1; // takes focus after a guide choice replaces its buttons
    const lock = (btns, on) => btns.forEach((b) => (b.disabled = on));
    const field = (id, label, input, hint) => h("div", { class: "field" }, h("label", { for: id, text: label }), input, hint ? h("p", { class: "hint", text: hint }) : null);
    const refresh = () => Promise.all([load(true), loadMeta().then(drawList)]).catch(fail);
    panelNumber = (n) => { det.number = n; renderNumber(); };
    let overridesDirty = () => false;
    panelState = { dirty: () => overridesDirty(), name: () => det.name || c.name };
    guideView = null;

    function renderNumber() {
      const input = h("input", { type: "text", inputmode: "numeric", id: "pn-num", maxlength: 4, value: String(det.number), class: "numfield wide" });
      const go = async () => {
        const v = input.value.trim();
        if (v === String(det.number)) return;
        if (!/^\d{1,4}$/.test(v) || Number(v) < 1) return toast("Number must be a whole number from 1 to 9999.", 4000);
        lock([input, ok], true);
        try { await api("PUT", "/channel/number", { key: c.key, number: Number(v) }); } catch (e) { lock([input, ok], false); return fail(e); }
        det.number = Number(v); c.number = det.number;
        toast(`Number ${v} saved`);
        renderNumber(); $("#pn-num")?.focus();
        load(true);
      };
      const ok = btn(null, "Save", { class: "primary", onclick: go });
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
      fill(num, field("pn-num", "Channel number", h("div", { class: "inline" }, input, ok)));
    }

    // Overrides: name, logo and categories are edited freely and saved together by one Save;
    // nothing here saves on its own. `cats` is the picker's staged list; it only becomes the
    // channel's own list once the admin changes it.
    const sameList = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
    function renderOverrides() {
      const m1 = m();
      const known = live && !!m1; // without the channel's categories, nothing is shown or sent for them
      const sv = m1 ? { own: !!m1.category_edited, list: m1.categories || [] } : { own: false, list: [] };
      const source = m1 ? m1.source_categories || [] : [];
      let cats = { own: sv.own, list: (sv.own ? sv.list : source).slice() };
      const savedName = det.name_edited ? det.name : "", savedLogo = det.logo_edited ? det.logo : "";
      const name = h("input", { type: "text", id: "pn-name", maxlength: 200, value: savedName, placeholder: det.source_name || "Name", autocomplete: "off" });
      const logo = h("input", { type: "url", id: "pn-logo", maxlength: 2048, value: savedLogo, placeholder: det.source_logo || "https://…", autocomplete: "off" });
      const unsaved = h("span", { class: "muted small", role: "status" });
      const catsDirty = () => known && (cats.own !== sv.own || (cats.own && !sameList(cats.list, sv.list)));
      overridesDirty = () => name.value.trim() !== savedName || logo.value.trim() !== savedLogo || catsDirty();
      const sync = () => { unsaved.textContent = overridesDirty() ? "Unsaved changes" : ""; };

      // Category picker: chips and the typing field share one input-looking box.
      const chipList = h("ul", { class: "chips pn-chips", role: "list", "aria-label": `Categories of ${det.name || c.name}` });
      const input = h("input", { type: "text", id: "pn-catin", list: "cat-names", maxlength: 60, autocomplete: "off", "aria-label": "Add a category" });
      const base = () => (cats.own ? cats.list : cats.list.slice(0, MAX_CATEGORIES)); // what a change starts from
      const note = h("p", { class: "muted small" });
      const useSource = btn(null, "Use source", { class: "link", onclick: () => { cats = { own: false, list: source.slice() }; drawCats(-1); } });
      const drawCats = (focusAt) => {
        const full = base().length >= MAX_CATEGORIES;
        const last = cats.list.length === 1; // the last chip cannot be removed
        fill(chipList, cats.list.map((v, i) => h("li", { class: `chip${cats.own ? "" : " src"}` }, h("span", { text: v }),
          last ? null : btn("x", null, { "aria-label": `Remove category ${v}`, title: "Remove", "data-i": String(i), onclick: () => remove(v, i) }))));
        input.disabled = full;
        input.placeholder = full ? `${MAX_CATEGORIES} categories, the most` : "Type or pick a category";
        useSource.hidden = !cats.own;
        note.textContent = !cats.own && cats.list.length > MAX_CATEGORIES ? `Only the first ${MAX_CATEGORIES} are sent.` : "";
        note.hidden = !note.textContent;
        sync();
        if (focusAt !== undefined) {
          const xs = [...chipList.querySelectorAll("button")];
          // After a removal: the chip now at that place, else the field. After an add or a
          // reset (-1): the field, or the last chip's × once the field is full.
          (xs.find((b) => Number(b.dataset.i) === focusAt) || (input.disabled ? xs[xs.length - 1] : input))?.focus();
        }
      };
      const change = (list, focusAt) => { cats = { own: true, list }; drawCats(focusAt); };
      const add = () => {
        const v = input.value.trim();
        if (!v) return;
        if (/[;|"]/.test(v)) return toast("A category cannot contain ; | or \".", 4000);
        if (base().some((x) => x.toLowerCase() === v.toLowerCase())) { toast("This channel already has that category.", 4000); return input.select(); }
        input.value = "";
        change([...base(), v], -1);
      };
      // By name: an unedited channel's chips can list more than the 8 that are sent.
      const remove = (v, i) => { const next = base().filter((x) => x.toLowerCase() !== v.toLowerCase()); if (next.length) change(next, i); };
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); add(); }
        else if (e.key === "Backspace" && !input.value && cats.list.length > 1) { e.preventDefault(); remove(cats.list[cats.list.length - 1], cats.list.length - 1); }
      });
      // Picking a suggestion from the list adds it; typing waits for Enter.
      input.addEventListener("input", (e) => { if ((!e.inputType || e.inputType === "insertReplacementText") && categories.includes(input.value)) add(); });
      const box = h("div", { class: "catbox", onclick: (e) => { if (e.target === box || e.target === chipList) input.focus(); } }, chipList, input);

      const save = btn(null, "Save", { class: "primary", id: "pn-save", onclick: async () => {
        const lv = logo.value.trim();
        if (lv && lv !== savedLogo && !/^https:\/\//i.test(lv)) return toast("Logo must be an https:// URL.", 4000);
        const body = { key: c.key, name: name.value, logo: logo.value };
        if (known) body.categories = cats.own ? cats.list : null;
        const ctrls = [...edit.querySelectorAll("button, input")];
        lock(ctrls, true);
        let r;
        try { r = await api("PUT", "/channel/edit", body); } catch (e) { lock(ctrls, false); return fail(e); }
        Object.assign(det, { name: r.name, name_edited: r.name_edited, logo: r.logo, logo_edited: r.logo_edited });
        if (r.categories && meta.get(c.key)) Object.assign(meta.get(c.key), { categories: r.categories, feed_categories: r.feed_categories, source_categories: r.source_categories, category_edited: r.category_edited });
        title.textContent = det.name || c.name;
        toast("Overrides saved");
        renderOverrides();
        $("#pn-save")?.focus();
        refresh();
      } });
      for (const i of [name, logo]) {
        i.addEventListener("input", sync);
        i.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save.click(); } });
      }
      drawCats();
      fill(edit,
        h("h3", { text: "Overrides" }),
        field("pn-name", "Name", name),
        field("pn-logo", "Logo URL", logo),
        known ? h("div", { class: "field" }, h("div", { class: "field-head" }, h("label", { for: "pn-catin", text: "Categories" }), useSource), box, note) : null,
        h("div", { class: "pn-save" }, save, unsaved));
    }

    function renderStreams(focus) {
      const list = det.feed_streams;
      let distinct = 0, divider = -1;
      list.forEach((s, i) => { if (s.on && !s.url_repeat && ++distinct === MAX_STREAMS) divider = i; });
      const send = async (next, msg, focusSig, focusDir) => {
        const bs = [...streams.querySelectorAll("button, input")];
        lock(bs, true);
        try { await api("PUT", "/channel/streams", { key: c.key, streams: next }); }
        catch (e) { fail(e); const again = await api("GET", `/channel?key=${encodeURIComponent(c.key)}`).catch(() => null); if (again) Object.assign(det, again); return renderStreams(); }
        const again = await api("GET", `/channel?key=${encodeURIComponent(c.key)}`).catch(fail);
        if (again) Object.assign(det, again);
        toast(msg);
        renderStreams([focusSig, focusDir]);
      };
      const choices = (l) => l.map((s) => ({ sig: s.sig, on: s.on }));
      const move = (i, d) => {
        const next = list.slice();
        [next[i], next[i + d]] = [next[i + d], next[i]];
        send(choices(next), "Stream order saved", list[i].sig, d < 0 ? "up" : "down");
      };
      const flip = (i, on) => send(choices(list.map((s, j) => (j === i ? { ...s, on } : s))), on ? "Stream switched on" : "Stream switched off", list[i].sig, "on");
      const items = [];
      list.forEach((s, i) => {
        const label = `${s.source}, stream ${i + 1}`;
        items.push(h("div", { class: `stream-row${s.on ? "" : " off"}` },
          h("label", { class: "chk onoff" }, h("input", { type: "checkbox", checked: s.on, "data-sig": s.sig, "data-dir": "on", "aria-label": `${s.on ? "On" : "Off"}: ${label}`, onchange: (e) => flip(i, e.target.checked) })),
          h("div", { class: "info" }, h("div", { class: "src", text: s.source }), h("div", { class: "chid one", title: s.url, text: s.url }),
            s.directives.length ? h("div", { class: "chid one", title: s.directives.join("  "), text: s.directives.join("  ") }) : null,
            s.url_repeat ? h("div", {}, state("none", "Same URL as an earlier stream, ignored by AIOStreams")) : null),
          h("div", { class: "moves", role: "group", "aria-label": `Order of ${label}` },
            btn("up", null, { class: "icon", "data-sig": s.sig, "data-dir": "up", title: "Move up", "aria-label": `Move ${label} up`, disabled: i === 0, onclick: () => move(i, -1) }),
            btn("chevron", null, { class: "icon", "data-sig": s.sig, "data-dir": "down", title: "Move down", "aria-label": `Move ${label} down`, disabled: i === list.length - 1, onclick: () => move(i, 1) }))));
        if (i === divider && i < list.length - 1) items.push(h("div", { class: "stream-divider", role: "note" }, icon("alert"), h("span", { text: `AIOStreams uses only the first ${MAX_STREAMS} streams. The ones below are not offered.` })));
      });
      fill(streams,
        h("h3", { text: "Streams" }),
        h("p", { class: "hint", text: list.length ? "Sent in this order." : "No stream from an enabled playlist." }),
        h("div", { class: "stream-list" }, ...items),
        det.streams_edited ? h("div", {}, btn(null, "Use source order", { onclick: () => send(null, "Using the source order") })) : null);
      if (focus) {
        const [sig, dir] = focus;
        const pick = (d) => [...streams.querySelectorAll("[data-sig]")].find((x) => x.dataset.sig === sig && x.dataset.dir === d && !x.disabled);
        (pick(dir) || pick(dir === "up" ? "down" : "up") || pick("on"))?.focus();
      }
    }

    function renderFoot() {
      return h("div", { class: "pn-foot" }, btn("trash", "Remove channel", { class: "danger", onclick: async () => {
        if (!confirm(`Remove “${det.name || c.name}”? Its number and stream choices are forgotten.`)) return;
        try { await api("POST", "/selection", { deselect: [c.key] }); } catch (e) { return fail(e); }
        toast("Removed from My channels");
        closePanel(true);
        await refresh();
        (rows.querySelector("button.lopen") || search).focus(); // the removed row is gone
      } }));
    }

    renderNumber(); renderOverrides();
    if (live) renderStreams();
    fill(aside,
      h("div", { class: "pn-head" },
        h("div", { class: "who" }, title, h("div", { class: "chid", text: c.out_id || c.tvg_id })),
        btn("x", null, { class: "quiet", "aria-label": "Close", onclick: () => closePanel() })),
      live ? null : h("p", { class: "banner-line" }, state("warn", "Missing from all enabled sources. Kept selected, returns automatically.")),
      num, edit, live ? streams : null, live ? guide : null, renderFoot());
    if (live) renderGuide(c);
    aside.setAttribute("aria-labelledby", "lineup-pn-title");
    if (phone.matches) { aside.setAttribute("role", "dialog"); aside.setAttribute("aria-modal", "true"); document.documentElement.classList.add("sheet-open"); }
    else { aside.removeAttribute("role"); aside.removeAttribute("aria-modal"); }
    aside.hidden = false;
    backdrop.hidden = !phone.matches;
    split.classList.add("open");
    document.addEventListener("keydown", onDocKey);
    drawList();
    aside.scrollTop = 0;
    aside.focus({ preventScroll: !phone.matches });
  }

  // Esc closes the panel from anywhere while it is open.
  function onDocKey(e) { if (e.key === "Escape") closePanel(); }
  // While the sheet is modal, focus that leaves it is pulled back in.
  function onFocusIn(e) { if (phone.matches && !aside.hidden && !aside.contains(e.target)) aside.focus(); }
  document.addEventListener("focusin", onFocusIn);
  // The panel's mode is fixed when it opens; with unsaved overrides it stays as it is unless the admin agrees.
  const onModeChange = () => { if (openKey && confirmDiscard()) closePanel(true); }; // the panel's mode is fixed when it opens
  phone.addEventListener("change", onModeChange);
  const teardown = () => { document.removeEventListener("keydown", onDocKey); document.documentElement.classList.remove("sheet-open"); };
  window.addEventListener("hashchange", () => { document.removeEventListener("focusin", onFocusIn); phone.removeEventListener("change", onModeChange); }, { once: true });

  // Unsaved overrides are only dropped after the admin agrees; Cancel keeps panel and edits.
  function confirmDiscard() {
    return !(openKey && panelState && panelState.dirty()) || confirm(`Discard unsaved changes to ${panelState.name()}?`);
  }

  function closePanel(force) {
    if (!force && !confirmDiscard()) return;
    const key = openKey;
    openKey = null;
    panelState = null;
    panelSeq++;
    aside.hidden = true;
    backdrop.hidden = true;
    split.classList.remove("open");
    teardown();
    drawList();
    if (key) rowFor(key)?.focus();
  }

  aside.addEventListener("keydown", (e) => {
    if (e.key !== "Tab" || !phone.matches) return;
    const f = [...aside.querySelectorAll("button, input")].filter((x) => !x.disabled);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === aside)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  window.addEventListener("hashchange", teardown, { once: true });

  async function load(force) {
    const d = await api("GET", "/epg");
    setJob(d.job);
    const json = JSON.stringify(d);
    const changed = force === true || json !== lastJson;
    if (!data) fill(main, head, top, tools, split, backdrop, catNames);
    data = d;
    // A number being typed is not replaced under the user's hands; the next poll redraws.
    const a = document.activeElement;
    if (a && a.classList.contains("numfield") && rows.contains(a) && a.value !== a.defaultValue) return;
    lastJson = json;
    // A saved row stays "rebuilding" until the build has had time to finish.
    let cleared = false;
    for (const [k, t] of saved) if (!d.job && Date.now() - t > 7000) { saved.delete(k); cleared = true; if (k === openKey) renderGuide({ key: k }); }
    if (changed || cleared) { drawTop(); drawList(); }
    const open = openKey && d.channels.find((x) => x.key === openKey);
    // Not while the admin is typing in the guide search: the next poll tries again.
    const typing = document.activeElement && $("#pn-guide")?.contains(document.activeElement) && document.activeElement.tagName === "INPUT";
    if (open && $("#pn-guide") && !typing && guideSig(open) !== lastGuideSig) renderGuide(open);
  }

  await loadMeta().catch(fail);
  await load();
  poll(main, load, 4000);
};

// ------------------------------------------------------------------ settings

const MAX_BACKUP_BYTES = 12 * 1024 * 1024;

// Export is a plain download; import checks the file on the server first, shows what it
// holds, and only replaces everything after the admin confirms.
function backupPanel(main) {
  const review = h("div", { class: "backup-review", hidden: true });
  let latest = 0; // a slower, older pick must not replace a newer one
  const picker = h("input", { type: "file", accept: ".json,application/json", hidden: true, onchange: async () => {
    const file = picker.files[0];
    picker.value = "";
    if (!file) return;
    const mine = ++latest;
    review.hidden = true; // the earlier file can no longer be confirmed
    fill(review);
    if (file.size > MAX_BACKUP_BYTES) return showReview(null, "This file is larger than 12 MiB, so it is not a backup.");
    try {
      const text = await file.text();
      const summary = await api("POST", "/backup/import?check=1", undefined, text);
      if (mine === latest) showReview({ text, summary });
    } catch (err) { if (mine === latest) showReview(null, err.message || String(err)); }
  } });
  const noteLine = (text) => h("p", { class: "hint", text });
  function showReview(found, error) {
    if (!found) {
      fill(review, banner("err", error));
      review.hidden = false;
      return;
    }
    const { text, summary } = found, n = summary.counts;
    const when = new Date(summary.exported_at);
    const buttons = [];
    const message = h("div", { "aria-live": "polite" });
    const replace = btn(null, "Replace with backup", { class: "danger", onclick: async () => {
      buttons.forEach((b) => (b.disabled = true));
      let done;
      try {
        done = await api("POST", "/backup/import", undefined, text);
      } catch (err) {
        fill(message, banner("err", err.message || String(err)));
        buttons.forEach((b) => (b.disabled = false));
        return;
      }
      // Imported: never offer the replace again, even if showing the new settings fails.
      toast(`Backup imported. Refreshing ${done.refreshing} ${done.refreshing === 1 ? "source" : "sources"}…`);
      try { await views.settings(main); } catch (err) { fail(err); }
    } });
    const cancel = btn(null, "Cancel", { onclick: () => { review.hidden = true; fill(review); } });
    buttons.push(replace, cancel);
    fill(review,
      h("p", { class: "review-title", text: `Backup from ${Number.isNaN(when.getTime()) ? summary.exported_at : when.toLocaleString()}` }),
      h("dl", { class: "envlist" },
        ...[["Sources", n.sources], ["Channels", n.channels], ["Guide mappings", n.guide_mappings], ["Settings", n.settings]]
          .map(([k, v]) => h("div", { class: "row" }, h("dt", { text: k }), h("dd", { class: "num", text: fmtNum(v) })))),
      noteLine("Importing replaces all current sources, channels and settings. A refresh of all sources starts right after."),
      message,
      h("div", { class: "actions" }, replace, cancel));
    review.hidden = false;
    replace.focus();
  }
  return h("div", {}, h("h2", { text: "Backup" }),
    h("div", { class: "panel pad backup" },
      h("p", { text: "Save your sources, channels, guide mappings and settings as one file, or restore them from one." }),
      noteLine("The file contains your source URLs, including any logins in them. Keep it private."),
      h("div", { class: "actions" },
        h("a", { class: "btn", href: "/api/backup", download: "" }, icon("download"), h("span", { text: "Export backup" })),
        btn("upload", "Import backup…", { type: "button", onclick: () => picker.click() }),
        picker),
      review));
}

views.settings = async (main) => {
  const d = await api("GET", "/settings");
  const s = d.settings, env = d.env;
  const f = {
    epg_days: h("input", { id: "f-days", type: "number", min: 0, max: 60, value: s.epg_days }),
    epg_past_hours: h("input", { id: "f-past", type: "number", min: 0, max: 168, value: s.epg_past_hours }),
    enrich_languages: h("input", { type: "checkbox", checked: s.enrich_languages }),
    metadata_url: h("input", { id: "f-meta", type: "url", value: s.metadata_url }),
    playlist_epg_header: h("input", { type: "checkbox", checked: s.playlist_epg_header }),
  };
  const field = (label, input, hint, id) => h("div", { class: "field" }, h("label", { for: id, text: label }), input, hint ? h("p", { class: "hint", text: hint }) : null);
  const check = (label, input, hint) => h("div", { class: "field" }, h("label", { class: "chk" }, input, h("span", { text: label })), hint ? h("p", { class: "hint", text: hint }) : null);
  fill(main,
    pageHead("Settings"),
    h("div", { class: "settings" },
      h("form", { class: "panel pad", onsubmit: async (e) => {
        e.preventDefault();
        try {
          await api("PUT", "/settings", {
            epg_days: Number(f.epg_days.value), epg_past_hours: Number(f.epg_past_hours.value),
            enrich_languages: f.enrich_languages.checked, metadata_url: f.metadata_url.value,
            playlist_epg_header: f.playlist_epg_header.checked,
          });
          toast("Saved");
        } catch (err) { fail(err); }
      } },
        h("div", { class: "settings" },
          field("EPG days ahead", f.epg_days, "Programmes starting later are dropped. 0 keeps everything.", "f-days"),
          field("EPG hours of history", f.epg_past_hours, "Programmes that ended longer ago are dropped.", "f-past"),
          check("Look up language metadata", f.enrich_languages, "Reads channel languages from the iptv-org API (feeds.json) for filtering and tvg-language."),
          field("Metadata URL", f.metadata_url, null, "f-meta"),
          check("Advertise guide in playlist", f.playlist_epg_header, env.public_url ? "Adds x-tvg-url to the playlist header. Only needed by players that discover guides from the playlist." : "Requires PUBLIC_URL to be set."),
          h("div", {}, h("button", { class: "primary", type: "submit", text: "Save settings" })))),
      backupPanel(main),
      h("div", {}, h("h2", { text: "Environment" }),
        h("dl", { class: "panel pad envlist" },
          ...[["Admin user", env.admin_user], ["Public URL", env.public_url || "(from request headers)"], ["Refresh interval", env.refresh_seconds ? fmtDur(env.refresh_seconds) : "disabled"],
            ["Request timeout", `${env.request_timeout}s`], ["M3U size limit", `${env.m3u_max_mb} MiB`], ["EPG download limit", `${env.epg_max_download_mb} MiB`],
            ["EPG decompressed limit", `${env.epg_max_decompressed_mb} MiB`], ["Private network sources", env.allow_private_sources ? "allowed" : "blocked"]]
            .map(([k, v]) => h("div", { class: "row" }, h("dt", { text: k }), h("dd", { text: v }))))),
      h("div", {}, btn("logout", "Sign out", { onclick: () => api("POST", "/logout").then(showLogin).catch(fail) }))),
  );
};

route();
