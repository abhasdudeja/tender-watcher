/* TenderWatch static client.
   Format 3 (data/index.json "format" >= 3): the first paint needs only index.json + orgs.json (small); rows come
   from the small preset files (new / closing soon / recently closed) or, on demand, from the chunk files of the
   selected organisation / source, and from every live chunk only for the "All tenders" view.
   Format 2 (older manifest): lazily fetches the chunk files of the selected source / all sources as before. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const els = {
    q: $("q"), source: $("fSource"), org: $("fOrg"), cat: $("fCat"), close: $("fClose"),
    value: $("fValue"), isNew: $("fNew"), closed: $("fClosed"), clear: $("fClear"), sort: $("sort"),
    list: $("list"), more: $("more"), count: $("count"), meta: $("meta"), views: $("views"), hint: $("hint"),
    notice: $("notice"), status: $("status"), toast: $("toast"),
  };

  const PAGE = 50;
  let all = [];           // tenders loaded so far
  let sources = {};       // slug -> source meta
  let filtered = [];
  let shown = 0;
  let newOnly = false;     // format 2 only: first seen in the last 2 days
  let showClosed = false;  // include tenders no longer on the portal (st = "closed" | "removed")
  let manifest = null;
  let lazy = false;        // format >= 3 manifest (presets + orgs.json)
  let view = "new";        // "new" | "closing" | "all" (format 3 only; format 2 is always "all")
  let fullScope = false;   // preset view whose capped file the user chose to widen: load the chunks and apply the rule
  let orgsData = null;     // orgs.json (format 3)
  let P = {};              // manifest.presets
  const orgByName = new Map();    // org name -> [orgs.json entries] (one per source it appears in)
  const fileMeta = new Map();     // data file -> { file, rows, kind: "live" | "closed" | "preset", bit }
  const rowIndex = new Map();     // "source\u0001id" -> position in `all` (format 3 de-duplication)
  const chunkState = new Map();   // data file -> "queued" | "loading" | "done" | "error"
  const POOL = 4;                 // parallel downloads
  let queue = [], running = 0;
  const VIEW_BIT = { new: 1, closing: 2, closed_recent: 4 };   // which preset(s) a row came from (row._v)
  let wantSrc = "", wantOrg = "", wantCat = "";  // URL-state selections applied once their options exist
  let refreshTimer = null, lastRefresh = 0, optionsDirty = false;
  const collator = new Intl.Collator("en");

  // ---------- helpers ----------
  const todayISO = new Date().toISOString().slice(0, 10);
  // preset rules, evaluated client-side when a preset file is only a capped sample (in IST, like the portals' dates)
  const istNow = () => new Date(Date.now() + 19800000);
  const istDay = (offsetDays) => new Date(istNow().getTime() + offsetDays * 86400000).toISOString().slice(0, 10);
  const fmtDate = (s) => {
    if (!s) return "—";
    const d = new Date(s.length === 10 ? s + "T00:00" : s);
    if (isNaN(d)) return s;
    const day = d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
    return s.length > 10 ? day + ", " + d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : day;
  };
  const daysUntil = (s) => {
    if (!s) return Infinity;
    return Math.ceil((new Date(s.slice(0, 10) + "T00:00") - new Date(todayISO + "T00:00")) / 86400000);
  };
  const fmtINR = (v) => {
    if (v == null) return "";
    if (v >= 1e7) return "₹" + (v / 1e7).toFixed(v % 1e7 ? 2 : 0) + " Cr";
    if (v >= 1e5) return "₹" + (v / 1e5).toFixed(v % 1e5 ? 2 : 0) + " L";
    return "₹" + v.toLocaleString("en-IN");
  };
  const fmtN = (n) => Number(n).toLocaleString("en-IN");
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const norm = (s) => String(s || "").toLowerCase();
  const toast = (msg) => { els.toast.textContent = msg; els.toast.classList.add("show"); setTimeout(() => els.toast.classList.remove("show"), 1600); };
  // search string of a row, built the first time the row is actually searched (keeps it off the first paint)
  const hay = (t) => t._hay || (t._hay = norm([t.t, t.o, t.c, t.r, t.id, t.k, t.pc, t.l, t.a, t.z].join(" | ")));

  function portalLinks(t) {
    const src = sources[t.s] || {};
    const base = src.base_url || "#";
    if (src.type === "gepnic") {
      // GePNIC deep links expire with the session; land on the portal home, which has
      // a "Search with ID/Title/Reference no" box. We copy the ID for the user.
      return { home: base.replace(/\/app.*$/, "/app"), hint: "Paste the Tender ID in the portal's search box" };
    }
    return { home: base, hint: "" };
  }

  // ---------- URL state ----------
  const VIEWS = ["new", "closing", "all"];
  function readState() {
    const p = new URLSearchParams(location.search);
    els.q.value = p.get("q") || "";
    wantSrc = p.get("src") || "";
    wantOrg = p.get("org") || "";
    wantCat = p.get("cat") || "";
    els.close.value = p.get("close") || "";
    els.value.value = p.get("val") || "";
    els.sort.value = p.get("sort") || "close";
    showClosed = p.get("closed") === "1";
    els.closed.classList.toggle("active", showClosed);
    els.closed.setAttribute("aria-pressed", showClosed);
    if (lazy) {
      const v = p.get("view");
      if (VIEWS.includes(v)) view = v;
      else if (p.get("new") === "1") view = "new";                          // old "New" chip -> the New view
      else view = [...p.keys()].some((k) => k !== "view") ? "all" : "new";  // old deep links keep meaning "all"
      newOnly = false;
    } else {
      view = "all";
      newOnly = p.get("new") === "1";
      els.isNew.classList.toggle("active", newOnly);
      els.isNew.setAttribute("aria-pressed", newOnly);
    }
    if (!p.get("sort")) els.sort.value = defaultSort();
  }
  // the New view reads best newest-first; every other view keeps "closing soonest"
  function defaultSort() { return lazy && view === "new" ? "pub" : "close"; }
  function writeState() {
    const p = new URLSearchParams();
    if (els.q.value) p.set("q", els.q.value);
    if (els.source.value) p.set("src", els.source.value);
    if (els.org.value) p.set("org", els.org.value);
    if (els.cat.value) p.set("cat", els.cat.value);
    if (els.close.value) p.set("close", els.close.value);
    if (els.value.value) p.set("val", els.value.value);
    if (els.sort.value !== defaultSort()) p.set("sort", els.sort.value);
    if (newOnly) p.set("new", "1");
    if (showClosed) p.set("closed", "1");
    if (lazy) {
      // the default view is left out of an otherwise empty URL; next to other params it is written
      // explicitly, because a URL with params but no view is an old-style link and means "all"
      const rest = p.toString();
      if (view !== "new" || rest) { const q = new URLSearchParams(); q.set("view", view); for (const [k, v] of p) q.set(k, v); return setUrl(q.toString()); }
    }
    setUrl(p.toString());
  }
  function setUrl(qs) { history.replaceState(null, "", qs ? "?" + qs : location.pathname); }

  // ---------- filtering ----------
  function tokens(q) {
    // supports quoted phrases and -negation:  "electric bus" kozhikode -road
    const out = [];
    const re = /(-?)"([^"]+)"|(-?)(\S+)/g;
    let m;
    while ((m = re.exec(q))) {
      const neg = (m[1] || m[3]) === "-";
      const term = (m[2] || m[4] || "").toLowerCase();
      if (term) out.push({ neg, term });
    }
    return out;
  }

  const orgEntries = (org, src) => (orgByName.get(org) || []).filter((e) => !src || e.s === src);
  const presetDays = (k) => (P[k] && P[k].days) || (k === "closed_recent" ? 14 : 7);

  // Which parts of the current preset view can only be answered from chunk files: a preset file is a capped
  // sample ("truncated" = number of matching tenders), so a source / organisation / "show all" needs the rule applied to chunk rows.
  function presetFlags() {
    const widen = !!(els.source.value || els.org.value || fullScope);
    const pv = lazy && view !== "all" ? P[view] : null, pc = P.closed_recent;
    const fullLive = !!(pv && pv.truncated && widen);
    const fullClosed = !!(pv && showClosed && pc && pc.truncated && widen);
    return { fullLive, fullClosed, sample: !!(pv && pv.truncated && !fullLive) };
  }

  // Totals of the current scope (view + source + organisation) according to the manifest; null where unknown.
  function scopeExpected() {
    const src = els.source.value, org = els.org.value;
    let live = null, closed = null;
    if (view === "all") {
      if (org && orgsData) {
        const es = orgEntries(org, src);
        live = es.reduce((n, e) => n + (e.n || 0), 0);
        closed = es.reduce((n, e) => n + (e.c || 0), 0);
      } else {
        const ss = scopeSources();
        live = ss.reduce((n, x) => n + (x.active || 0), 0);
        closed = ss.reduce((n, x) => n + (x.inactive || 0), 0);
      }
    } else if (!org) {
      const f = presetFlags(), st = src ? (sources[src] || {}).stats : null, pc = P.closed_recent;
      if (src) {
        live = st ? st[view] : null;
        closed = !showClosed ? 0 : st ? st.closed_recent : null;
      } else {
        live = f.sample ? P[view].rows : (P[view] ? (P[view].truncated || P[view].rows) : null);
        closed = !showClosed ? 0 : !pc ? null : (f.fullLive ? (pc.truncated || pc.rows) : pc.rows);
      }
    }
    return { live, closed };
  }

  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  function describe(live, closed) {
    const L = fmtN(live), C = fmtN(closed);
    const narrowed = !!(els.source.value || els.org.value);
    if (view === "all") return showClosed ? `${L} active + ${C} closed/removed tenders` : `${L} active tenders`;
    const what = view === "new" ? `new tenders in the last ${plural(presetDays("new"), "day")}`
      : `tenders closing in the next ${plural(presetDays("closing"), "day")}`;
    const f = presetFlags();
    const base = f.sample ? `Showing the ${view === "new" ? "newest" : "soonest-closing"} ${L} of ${fmtN(P[view].truncated)} ${what}` : `${L} ${what}`;
    if (!showClosed) return base;
    const pc = P.closed_recent, capped = pc && pc.truncated && !f.fullClosed && !narrowed;   // closed sample only
    return `${base} + ${capped ? C + " of " + fmtN(pc.truncated) : C} closed${narrowed || f.fullClosed ? "/removed" : " in the last " + plural(presetDays("closed_recent"), "day")}`;
  }

  function apply() {
    const toks = tokens(els.q.value.trim());
    const src = els.source.value, org = els.org.value, cat = els.cat.value;
    const closeDays = els.close.value ? +els.close.value : null;
    let vmin = null, vmax = null;
    if (els.value.value) { const [a, b] = els.value.value.split("-"); vmin = +a; vmax = b ? +b : null; }
    const newCut = new Date(Date.now() - 2 * 86400000).toISOString();
    const vbit = lazy ? (VIEW_BIT[view] || 0) : 0;   // 0 = All tenders
    const narrowSO = !!(src || org);
    const flags = presetFlags();
    const ruleNew = istDay(-presetDays("new")), nowIST = istNow(), ruleNow = nowIST.toISOString().slice(0, 16),
      ruleEnd = new Date(nowIST.getTime() + presetDays("closing") * 86400000).toISOString().slice(0, 16),
      ruleDrop = new Date(Date.now() - presetDays("closed_recent") * 86400000).toISOString().slice(0, 19);
    const rule = (t) => {   // the preset's own rule, for rows that came from chunks
      if (t.st) return !!t.i && t.i.slice(0, 19) >= ruleDrop;
      if (view === "new") return !!t.p && t.p.slice(0, 10) >= ruleNew;
      return !!t.d && t.d >= ruleNow && t.d <= ruleEnd;
    };
    let liveN = 0, closedN = 0;   // rows of the current scope that are loaded so far

    filtered = all.filter((t) => {
      if (t.st) {
        if (!showClosed) return false;
        // preset views: closed rows come from the "recently closed" preset, or (capped preset) from closed chunks by its rule
        if (vbit && !((t._v & 4) || (flags.fullClosed && rule(t)))) return false;
      } else if (vbit && !((t._v & vbit) || (flags.fullLive && rule(t)))) return false;
      if (src && t.s !== src) return false;
      if (org && t.o !== org) return false;
      if (t.st) closedN++; else liveN++;
      if (cat && t.k !== cat) return false;
      if (closeDays != null) { const d = daysUntil(t.d); if (d < 0 || d > closeDays) return false; }
      if (vmin != null) { if (t.v == null || t.v < vmin || (vmax != null && t.v >= vmax)) return false; }
      if (newOnly && !(t.f && t.f > newCut)) return false;
      if (toks.length) {
        const h = hay(t);
        for (const { neg, term } of toks) {
          const hit = h.includes(term);
          if (neg ? hit : !hit) return false;
        }
      }
      return true;
    });

    const s = els.sort.value;
    const cmp = {
      close: (a, b) => (a.d || "9999").localeCompare(b.d || "9999"),
      pub: (a, b) => (b.p || "").localeCompare(a.p || ""),
      value: (a, b) => (b.v || 0) - (a.v || 0),
      org: (a, b) => (a.o || "").localeCompare(b.o || "") || (a.d || "").localeCompare(b.d || ""),
    }[s];
    // closed / removed tenders always sink below live ones
    filtered.sort((a, b) => (a.st ? 1 : 0) - (b.st ? 1 : 0) || cmp(a, b));

    const prog = loadProgress();
    shown = 0;
    els.list.innerHTML = "";
    renderMore(prog.pending);
    const loading = prog.pending ? ` · loading… ${Math.round(100 * prog.done / Math.max(prog.total, 1))}%` : "";
    const narrowed = !!(toks.length || cat || closeDays != null || vmin != null || newOnly);
    let txt;
    if (!lazy) {
      const activeCount = all.reduce((n, t) => n + (t.st ? 0 : 1), 0);
      const base = showClosed ? all.length : activeCount;
      txt = (filtered.length === base
        ? (showClosed ? `${fmtN(activeCount)} active + ${fmtN(all.length - activeCount)} closed/removed tenders` : `${fmtN(activeCount)} active tenders`)
        : `${fmtN(filtered.length)} of ${fmtN(base)} tenders`) + loading;
    } else {
      // totals come from the manifest; once the whole scope is loaded the loaded rows are the truth
      const exp = scopeExpected();
      const live = (!prog.pending || exp.live == null) ? liveN : exp.live;
      const closed = (!prog.pending || exp.closed == null) ? closedN : exp.closed;
      const total = live + (showClosed ? closed : 0);
      if (prog.pending && exp.live == null) txt = "Loading tenders…" + loading;
      else if (narrowed && filtered.length !== total) txt = `${fmtN(filtered.length)} of ${fmtN(total)} tenders` + loading;
      else txt = describe(live, closed) + loading;
    }
    els.count.textContent = txt;
    renderHint(prog.pending, !!(toks.length || cat || closeDays != null || vmin != null || narrowSO));
    writeState();
  }

  // In New / Closing soon only that preset is searched; offer the full search when it comes up (nearly) empty.
  // A capped preset file also gets a button that widens the view to every matching tender (loads the chunks).
  function renderHint(pending, narrowed) {
    const f = presetFlags();
    const few = lazy && view !== "all" && !pending && narrowed && filtered.length < 5;
    const capped = lazy && view !== "all" && f.sample && !pending;
    els.hint.hidden = !(few || capped);
    if (els.hint.hidden) return;
    const days = view === "new" ? `published in the last ${plural(presetDays("new"), "day")}` : `closing in the next ${plural(presetDays("closing"), "day")}`;
    let h = "";
    if (capped) h += `<span>Only the ${view === "new" ? "newest" : "soonest-closing"} ${fmtN(P[view].rows)} are listed.</span> <button type="button" data-full>Show all ${fmtN(P[view].truncated)}</button> `;
    if (few) h += `<span>Only searching tenders ${days}.</span> <button type="button" data-all>Search all tenders</button>`;
    els.hint.innerHTML = h;
  }

  function renderMore(pending) {
    const slice = filtered.slice(shown, shown + PAGE);
    const frag = document.createDocumentFragment();
    for (const t of slice) frag.appendChild(card(t));
    els.list.appendChild(frag);
    shown += slice.length;
    els.more.hidden = shown >= filtered.length;
    if (!filtered.length) els.list.innerHTML = (pending === undefined ? loadProgress().pending : pending)
      ? '<div class="empty">Loading tenders…</div>'
      : '<div class="empty">No tenders match. Try fewer words or clear the filters.</div>';
  }

  function card(t) {
    const d = daysUntil(t.d);
    const gone = !!t.st;
    const soon = !gone && d <= 3 ? " soon" : "";
    const dueTxt = gone ? (t.st === "closed" ? "closed" : "removed from portal before its deadline")
      : d < 0 ? "closed" : d === 0 ? "closes today" : d === 1 ? "closes tomorrow" : `closes in ${d} days`;
    const links = portalLinks(t);
    const tags = [];
    if (t.k) tags.push(t.k);
    if (t.pc) tags.push(t.pc);
    if (t.l) tags.push(t.l);
    const srcName = (sources[t.s] || {}).name || t.s;
    const el = document.createElement("div");
    el.className = "card" + (gone ? " gone" : "");
    el.innerHTML = `
      <p class="title">${gone ? `<span class="badge">${t.st === "closed" ? "Closed" : "Removed"}</span>` : ""}${esc(t.t)}</p>
      <div class="org">${esc(t.o)}${t.c && t.c !== t.o ? ' · <span title="' + esc(t.c) + '">' + esc(t.c.split("||").slice(1).join(" › ")) + "</span>" : ""}</div>
      <div class="tags">${tags.map((x) => `<span>${esc(x)}</span>`).join("")}<span style="background:transparent;color:var(--muted)">${esc(srcName)}</span></div>
      <div class="when">
        <span class="d${soon}">${fmtDate(t.d)}</span>
        <span class="${soon ? "soon" : ""}">${dueTxt}</span>
        ${t.v != null ? `<span class="val">${fmtINR(t.v)}${t.e != null ? " · EMD " + fmtINR(t.e) : ""}</span>` : ""}
      </div>
      <div class="actions">
        ${gone ? "" : `<a class="btn primary" href="${esc(links.home)}" target="_blank" rel="noopener" data-copy="${esc(t.id)}">Open portal ↗</a>`}
        <button data-copy="${esc(t.id)}">Copy Tender ID</button>
        <span class="id">${esc(t.id)}${t.r ? " · Ref " + esc(t.r) : ""}</span>
      </div>
      <details><summary>Details</summary>
        <dl>
          <dt>Published</dt><dd>${fmtDate(t.p)}</dd>
          <dt>Closing</dt><dd>${fmtDate(t.d)}</dd>
          <dt>Bid opening</dt><dd>${fmtDate(t.b)}</dd>
          ${t.a ? `<dt>Inviting authority</dt><dd>${esc(t.a)}</dd>` : ""}
          ${t.z ? `<dt>Pincode</dt><dd>${esc(t.z)}</dd>` : ""}
          <dt>Organisation chain</dt><dd>${esc((t.c || t.o).split("||").join(" › "))}</dd>
          <dt>First seen here</dt><dd>${fmtDate(t.f)}</dd>
          ${gone && t.i ? `<dt>No longer listed since</dt><dd>${fmtDate(t.i)}</dd>` : ""}
          ${links.hint && !gone ? `<dt>How to open</dt><dd>${esc(links.hint)}</dd>` : ""}
        </dl>
      </details>`;
    el.querySelectorAll("[data-copy]").forEach((b) =>
      b.addEventListener("click", () => {
        const id = b.getAttribute("data-copy");
        if (navigator.clipboard) navigator.clipboard.writeText(id).then(() => toast("Tender ID copied: " + id)).catch(() => {});
      })
    );
    return el;
  }

  // ---------- populate filters ----------
  function fillSelect(sel, values, keepFirst = true) {
    const first = keepFirst ? sel.options[0].outerHTML : "";
    sel.innerHTML = first + values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  }
  const optHTML = (value, n) => `<option value="${esc(value)}">${esc(value)}${n == null ? "" : " (" + fmtN(n) + ")"}</option>`;

  // Source choices with the row count of the current view (manifest numbers only; no row data needed).
  function setupSourceFilter(cur) {
    const srcs = manifest.sources.filter((x) => x.active > 0 || x.enabled);
    const items = [];
    for (const x of srcs) {
      let n = x.active;
      if (lazy && view !== "all") {
        n = x.stats ? (x.stats[view] || 0) + (showClosed ? x.stats.closed_recent || 0 : 0) : null;
        if (n === 0 && x.slug !== cur) continue;
      }
      items.push(`<option value="${esc(x.slug)}">${esc(x.name)}${n == null ? "" : " (" + fmtN(n) + ")"}</option>`);
    }
    els.source.innerHTML = '<option value="">All sources</option>' + items.join("");
    els.source.value = cur;
  }

  // (a capped preset is a sample, so it cannot give honest counts)
  const presetLoaded = () => lazy && view !== "all" && P[view] && !P[view].truncated && chunkState.get(P[view].file) === "done";
  // org / category counts of the rows in the current preset view (only used once its preset file is in)
  function viewCounts() {
    const src = els.source.value, bit = VIEW_BIT[view];
    const orgs = new Map(), cats = new Map();
    for (const t of all) {
      if (src && t.s !== src) continue;
      if (t.st ? !(showClosed && (t._v & 4)) : !(t._v & bit)) continue;
      orgs.set(t.o, (orgs.get(t.o) || 0) + 1);
      if (t.k) cats.set(t.k, (cats.get(t.k) || 0) + 1);
    }
    return { orgs, cats };
  }

  // format 3: organisation and category choices come from orgs.json, so they exist before any row is loaded
  function rebuildOrgOptions(counts) {
    const cur = els.org.value || wantOrg, src = els.source.value;
    const items = new Map();   // name -> count shown (null = unknown)
    if (counts) {
      for (const [o, n] of counts.orgs) items.set(o, n);
    } else {
      const withCounts = view === "all";
      for (const [name, es] of orgByName) {
        let n = 0, hit = false;
        for (const e of es) {
          if (src && e.s !== src) continue;
          hit = true;
          n += (e.n || 0) + (showClosed ? e.c || 0 : 0);
        }
        if (hit && (n > 0 || name === cur || !withCounts)) items.set(name, withCounts ? n : null);
      }
    }
    if (cur && !items.has(cur)) items.set(cur, counts ? 0 : null);
    const names = [...items.keys()].sort(collator.compare);
    els.org.innerHTML = '<option value="">All organisations</option>' + names.map((o) => optHTML(o, items.get(o))).join("");
    els.org.value = cur;
    wantOrg = "";
  }
  function rebuildCatOptions(counts) {
    const cur = els.cat.value || wantCat;
    const items = new Map();
    if (counts) {
      for (const [k, n] of [...counts.cats].sort((a, b) => b[1] - a[1])) items.set(k, n);
    } else {
      for (const c of orgsData.categories || []) items.set(c.k, view === "all" ? c.n : null);
    }
    if (cur && !items.has(cur)) items.set(cur, counts ? 0 : null);
    els.cat.innerHTML = els.cat.options[0].outerHTML + [...items].map(([k, n]) => optHTML(k, n)).join("");
    els.cat.value = cur;
    wantCat = "";
  }
  function rebuildOptions() {
    if (!lazy || !orgsData) { refreshFilterOptions(); return; }
    setupSourceFilter(els.source.value);
    const counts = presetLoaded() ? viewCounts() : null;
    rebuildOrgOptions(counts);
    rebuildCatOptions(counts);
  }

  // format 2 (or no orgs.json): org / category choices come from what has been loaded so far
  function refreshFilterOptions() {
    const curOrg = els.org.value || wantOrg, curCat = els.cat.value || wantCat;
    const orgCounts = {};   // active tenders per org; orgs that only have closed ones are still listed (0)
    for (const t of all) orgCounts[t.o] = (orgCounts[t.o] || 0) + (t.st ? 0 : 1);
    if (curOrg && !(curOrg in orgCounts)) orgCounts[curOrg] = 0;
    const orgs = Object.keys(orgCounts).sort((a, b) => a.localeCompare(b));
    els.org.innerHTML = '<option value="">All organisations</option>' +
      orgs.map((o) => `<option value="${esc(o)}">${esc(o)} (${orgCounts[o]})</option>`).join("");
    els.org.value = curOrg;
    const cats = new Set(all.map((t) => t.k).filter(Boolean));
    if (curCat) cats.add(curCat);
    fillSelect(els.cat, [...cats].sort());
    els.cat.value = curCat;
    wantOrg = wantCat = "";
  }

  // ---------- view switcher (format 3) ----------
  function viewCount(k) {
    const src = els.source.value;
    if (k === "all") return scopeSources().reduce((n, x) => n + (x.active || 0), 0);
    if (src) { const st = (sources[src] || {}).stats; return st ? st[k] || 0 : null; }
    return P[k] ? P[k].truncated || P[k].rows : null;
  }
  function updateViewButtons() {
    for (const b of els.views.querySelectorAll("button[data-view]")) {
      const k = b.dataset.view, n = viewCount(k);
      const label = k === "new" ? `New (${presetDays("new")}d)` : k === "closing" ? `Closing soon (${presetDays("closing")}d)` : "All tenders";
      b.innerHTML = esc(label) + (n == null ? "" : ` <span class="n">${fmtN(n)}</span>`);
      b.setAttribute("aria-pressed", k === view);
      b.classList.toggle("active", k === view);
    }
  }
  function setView(v) {
    if (v === view) return;
    view = v; fullScope = false;
    updateViewButtons();
    rebuildOptions();
    ensureLoaded();
    apply();
  }

  // ---------- lazy file loading ----------
  const scopeSources = () => (manifest ? manifest.sources.filter((x) => !els.source.value || x.slug === els.source.value) : []);
  const metaOf = (f) => fileMeta.get(f) || { file: f, rows: 1, kind: /-closed-\d+\.json$/.test(f) ? "closed" : "live" };

  // chunk files of the selected organisation (its own files only) or of the selected source / all sources
  function scopeChunkFiles(wantLive, wantClosed) {
    const org = els.org.value, out = [];
    const ok = (m) => (m.kind === "closed" ? wantClosed : wantLive);
    if (org && orgsData) {
      for (const e of orgEntries(org, els.source.value)) for (const f of e.files || []) { const m = metaOf(f); if (ok(m)) out.push(m); }
    } else {
      for (const x of scopeSources()) for (const c of x.chunks || []) { const m = metaOf(c.file); if (ok(m)) out.push(m); }
    }
    return out;
  }
  function neededFiles() {
    if (!lazy) {
      const out = [];
      for (const x of scopeSources()) for (const c of x.chunks || []) if (c.kind === "live" || showClosed) out.push(metaOf(c.file));
      return out;
    }
    let out = [];
    if (view === "all") {
      out = scopeChunkFiles(true, showClosed);     // every live chunk unless a source / organisation narrows it
    } else {
      if (fileMeta.has((P[view] || {}).file)) out.push(fileMeta.get(P[view].file));
      if (showClosed && P.closed_recent && fileMeta.has(P.closed_recent.file)) out.push(fileMeta.get(P.closed_recent.file));
      // a capped preset file is only a sample: a source / organisation / "show all" needs that scope's chunks
      const f = presetFlags();
      if (f.fullLive || f.fullClosed) out.push(...scopeChunkFiles(f.fullLive, f.fullClosed));
    }
    return [...new Map(out.map((m) => [m.file, m])).values()];
  }
  function loadProgress() {
    const need = neededFiles();
    const total = need.reduce((n, m) => n + m.rows, 0);
    const done = need.reduce((n, m) => n + (chunkState.get(m.file) === "done" ? m.rows : 0), 0);
    return { total, done, pending: need.some((m) => { const st = chunkState.get(m.file); return st !== "done" && st !== "error"; }) };
  }
  function ingest(rows, bit) {
    if (!lazy) { for (const t of rows) all.push(t); return; }
    // de-duplicate by (source, id): a preset row and the same row from a chunk are one tender; the chunk row wins
    for (const t of rows) {
      const key = t.s + "\u0001" + t.id;
      const pos = rowIndex.get(key);
      if (pos === undefined) {
        t._v = bit; t._pre = bit > 0;
        rowIndex.set(key, all.length);
        all.push(t);
      } else {
        const old = all[pos];
        if (bit) old._v |= bit;
        else if (old._pre) { t._v = old._v; t._pre = false; all[pos] = t; }
      }
    }
  }
  function scheduleRefresh() {   // many files can land in quick succession: re-render at most ~5x a second
    if (refreshTimer) return;
    refreshTimer = setTimeout(() => {
      refreshTimer = null; lastRefresh = Date.now();
      if (!lazy || optionsDirty) { optionsDirty = false; rebuildOptions(); }
      apply();
    }, Math.max(0, 200 - (Date.now() - lastRefresh)));
  }
  async function loadFile(m) {
    chunkState.set(m.file, "loading");
    try {
      const r = await fetch("data/" + m.file, { cache: "no-cache" });
      if (!r.ok) throw new Error(r.status);
      ingest((await r.json()).tenders, m.bit || 0);
      chunkState.set(m.file, "done");
      if (m.kind === "preset") optionsDirty = true;   // org / category / source counts now come from this preset's rows
    } catch (e) {
      chunkState.set(m.file, "error");
      els.notice.hidden = false;
      els.notice.textContent = `Could not load some data (${m.file}). Showing what loaded; reload to retry.`;
    }
    scheduleRefresh();
  }
  function pump() {
    while (running < POOL && queue.length) {
      const m = queue.shift();
      running++;
      loadFile(m).finally(() => { running--; pump(); });
    }
  }
  // Fetch whatever the current view needs and has not been fetched, POOL files at a time. Files still waiting
  // from an earlier scope are dropped first so that the new scope is not stuck behind them.
  function ensureLoaded() {
    for (const m of queue) chunkState.delete(m.file);
    queue = neededFiles().filter((m) => !chunkState.has(m.file));
    for (const m of queue) chunkState.set(m.file, "queued");
    pump();
    scheduleRefresh();
  }

  // ---------- status ----------
  async function loadStatus() {
    try {
      const r = await fetch("data/status.json", { cache: "no-cache" });
      if (!r.ok) return;
      const st = await r.json();
      const rows = st.sources.map((s) => {
        const ok = s.ok ? "✓" : "✗";
        const when = s.finished ? fmtDate(s.finished) : "—";
        const errs = (s.errors || []).length ? ` · ${s.errors.length} error(s): ${esc(s.errors[0])}` : "";
        return `<tr><td>${ok}</td><td>${esc(s.name || s.slug)}</td><td>${when}</td><td>${s.listed ?? "—"} listed, ${s.new ?? "—"} new, ${s.details_fetched ?? "—"} details${errs}</td></tr>`;
      }).join("");
      els.status.innerHTML = `<strong>Crawl status</strong><table>${rows}</table>`;
      const bad = st.sources.filter((s) => !s.ok);
      if (bad.length) {
        els.notice.hidden = false;
        els.notice.textContent = `Last crawl had problems for: ${bad.map((s) => s.name || s.slug).join(", ")}. Data for those sources may be stale.`;
      }
    } catch (e) { /* status is optional */ }
  }

  // ---------- init ----------
  async function fetchJSON(url) {
    const r = await fetch(url, { cache: "no-cache" });
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }
  async function init() {
    // index.json and orgs.json are small and independent: fetch them together (orgs.json only exists from format 3 on)
    const orgsReq = fetchJSON("data/orgs.json").catch(() => null);
    try {
      manifest = await fetchJSON("data/index.json");
    } catch (e) {
      els.meta.textContent = "Could not load data/index.json";
      return;
    }
    for (const x of manifest.sources) {
      sources[x.slug] = x;
      for (const c of x.chunks || []) fileMeta.set(c.file, { file: c.file, rows: c.rows || 1, kind: c.kind });
    }
    P = manifest.presets || {};
    lazy = (manifest.format || 0) >= 3 && !!manifest.presets;
    if (lazy) {
      for (const k of Object.keys(VIEW_BIT)) if (P[k] && P[k].file) fileMeta.set(P[k].file, { file: P[k].file, rows: P[k].rows || 1, kind: "preset", bit: VIEW_BIT[k] });
      orgsData = await orgsReq;
      if (orgsData && Array.isArray(orgsData.orgs)) {
        for (const e of orgsData.orgs) { if (!orgByName.has(e.o)) orgByName.set(e.o, []); orgByName.get(e.o).push(e); }
      } else orgsData = null;
    }
    const totalActive = manifest.sources.reduce((n, x) => n + (x.active || 0), 0);
    if (Array.isArray(manifest.tenders)) {   // old single-file index: everything is inline
      ingest(manifest.tenders, 0);
      for (const x of manifest.sources) for (const c of x.chunks || []) chunkState.set(c.file, "done");
    }
    els.meta.textContent = `Updated ${fmtDate(manifest.generated)} · ${fmtN(totalActive)} active tenders`;
    els.views.hidden = !lazy;
    els.isNew.hidden = lazy;
    readState();
    if (!lazy) {
      setupSourceFilter(manifest.sources.some((x) => x.slug === wantSrc) ? wantSrc : "");
      refreshFilterOptions();
    } else {
      setupSourceFilter(manifest.sources.some((x) => x.slug === wantSrc) ? wantSrc : "");
      if (!orgsData) refreshFilterOptions(); else { rebuildOrgOptions(null); rebuildCatOptions(null); }
      updateViewButtons();
    }
    apply();
    ensureLoaded();
    loadStatus();
  }

  let timer;
  els.q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(apply, 120); });
  for (const k of ["cat", "close", "value", "sort"]) els[k].addEventListener("change", apply);
  els.org.addEventListener("change", () => { ensureLoaded(); apply(); });
  els.source.addEventListener("change", () => {
    if (lazy) {
      const org = els.org.value;
      if (org && orgsData && !orgEntries(org, els.source.value).length) els.org.value = "";   // that source has no such organisation
      updateViewButtons();
      rebuildOptions();
    }
    ensureLoaded(); apply();
  });
  els.isNew.addEventListener("click", () => { newOnly = !newOnly; els.isNew.classList.toggle("active", newOnly); els.isNew.setAttribute("aria-pressed", newOnly); apply(); });
  els.closed.addEventListener("click", () => {
    showClosed = !showClosed; els.closed.classList.toggle("active", showClosed); els.closed.setAttribute("aria-pressed", showClosed);
    if (lazy) rebuildOptions();
    ensureLoaded(); apply();
  });
  els.views.addEventListener("click", (e) => { const b = e.target.closest("button[data-view]"); if (b) setView(b.dataset.view); });
  els.hint.addEventListener("click", (e) => {
    if (e.target.closest("button[data-all]")) setView("all");
    else if (e.target.closest("button[data-full]")) { fullScope = true; ensureLoaded(); apply(); }
  });
  els.clear.addEventListener("click", () => {
    els.q.value = ""; for (const k of ["source", "org", "cat", "close", "value"]) els[k].value = "";
    newOnly = false; els.isNew.classList.remove("active"); els.isNew.setAttribute("aria-pressed", "false");
    showClosed = false; els.closed.classList.remove("active"); els.closed.setAttribute("aria-pressed", "false");
    if (lazy) { updateViewButtons(); rebuildOptions(); }
    ensureLoaded(); apply(); els.q.focus();
  });
  els.more.addEventListener("click", () => renderMore());
  document.addEventListener("keydown", (e) => { if (e.key === "/" && document.activeElement !== els.q) { e.preventDefault(); els.q.focus(); } });

  init();
})();
