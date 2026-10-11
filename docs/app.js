/* TenderWatch static client. Loads the data/index.json manifest, then lazily fetches only the chunk
   files the current view needs (selected source / all sources; closed tenders only on demand). */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const els = {
    q: $("q"), source: $("fSource"), org: $("fOrg"), cat: $("fCat"), close: $("fClose"),
    value: $("fValue"), isNew: $("fNew"), closed: $("fClosed"), clear: $("fClear"), sort: $("sort"),
    list: $("list"), more: $("more"), count: $("count"), meta: $("meta"),
    notice: $("notice"), status: $("status"), toast: $("toast"),
  };

  const PAGE = 50;
  let all = [];           // tenders from index.json
  let sources = {};       // slug -> source meta
  let filtered = [];
  let shown = 0;
  let newOnly = false;
  let showClosed = false;  // include tenders no longer on the portal (st = "closed" | "removed")
  let activeCount = 0;
  let manifest = null;
  const chunkState = new Map();   // chunk file -> "loading" | "done" | "error"
  const POOL = 4;                 // parallel chunk downloads
  let wantOrg = "", wantCat = "";  // URL-state selections applied once their options exist
  let refreshTimer = null;

  // ---------- helpers ----------
  const todayISO = new Date().toISOString().slice(0, 10);
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
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const norm = (s) => String(s || "").toLowerCase();
  const toast = (msg) => { els.toast.textContent = msg; els.toast.classList.add("show"); setTimeout(() => els.toast.classList.remove("show"), 1600); };

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
  function readState() {
    const p = new URLSearchParams(location.search);
    els.q.value = p.get("q") || "";
    els.source.value = p.get("src") || "";
    wantOrg = p.get("org") || "";
    wantCat = p.get("cat") || "";
    els.close.value = p.get("close") || "";
    els.value.value = p.get("val") || "";
    els.sort.value = p.get("sort") || "close";
    newOnly = p.get("new") === "1";
    els.isNew.classList.toggle("active", newOnly);
    showClosed = p.get("closed") === "1";
    els.closed.classList.toggle("active", showClosed);
  }
  function writeState() {
    const p = new URLSearchParams();
    if (els.q.value) p.set("q", els.q.value);
    if (els.source.value) p.set("src", els.source.value);
    if (els.org.value) p.set("org", els.org.value);
    if (els.cat.value) p.set("cat", els.cat.value);
    if (els.close.value) p.set("close", els.close.value);
    if (els.value.value) p.set("val", els.value.value);
    if (els.sort.value !== "close") p.set("sort", els.sort.value);
    if (newOnly) p.set("new", "1");
    if (showClosed) p.set("closed", "1");
    const qs = p.toString();
    history.replaceState(null, "", qs ? "?" + qs : location.pathname);
  }

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

  function apply() {
    const toks = tokens(els.q.value.trim());
    const src = els.source.value, org = els.org.value, cat = els.cat.value;
    const closeDays = els.close.value ? +els.close.value : null;
    let vmin = null, vmax = null;
    if (els.value.value) { const [a, b] = els.value.value.split("-"); vmin = +a; vmax = b ? +b : null; }
    const newCut = new Date(Date.now() - 2 * 86400000).toISOString();

    filtered = all.filter((t) => {
      if (t.st && !showClosed) return false;
      if (src && t.s !== src) return false;
      if (org && t.o !== org) return false;
      if (cat && t.k !== cat) return false;
      if (closeDays != null) { const d = daysUntil(t.d); if (d < 0 || d > closeDays) return false; }
      if (vmin != null) { if (t.v == null || t.v < vmin || (vmax != null && t.v >= vmax)) return false; }
      if (newOnly && !(t.f && t.f > newCut)) return false;
      if (toks.length) {
        const hay = t._hay;
        for (const { neg, term } of toks) {
          const hit = hay.includes(term);
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

    shown = 0;
    els.list.innerHTML = "";
    renderMore();
    const base = showClosed ? all.length : activeCount;
    const nClosed = all.length - activeCount;
    const prog = loadProgress();
    const loading = prog.pending ? ` · loading… ${Math.round(100 * prog.done / Math.max(prog.total, 1))}%` : "";
    els.count.textContent = (filtered.length === base
      ? (showClosed ? `${activeCount.toLocaleString("en-IN")} active + ${nClosed.toLocaleString("en-IN")} closed/removed tenders`
                    : `${activeCount.toLocaleString("en-IN")} active tenders`)
      : `${filtered.length.toLocaleString("en-IN")} of ${base.toLocaleString("en-IN")} tenders`) + loading;
    writeState();
  }

  function renderMore() {
    const slice = filtered.slice(shown, shown + PAGE);
    const frag = document.createDocumentFragment();
    for (const t of slice) frag.appendChild(card(t));
    els.list.appendChild(frag);
    shown += slice.length;
    els.more.hidden = shown >= filtered.length;
    if (!filtered.length) els.list.innerHTML = loadProgress().pending
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

  function setupSourceFilter(index) {
    const srcs = index.sources.filter((x) => x.active > 0 || x.enabled);
    els.source.innerHTML = '<option value="">All sources</option>' +
      srcs.map((x) => `<option value="${esc(x.slug)}">${esc(x.name)} (${x.active.toLocaleString("en-IN")})</option>`).join("");
  }

  // org / category choices come from what has been loaded so far; the current selection is kept
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

  // ---------- lazy chunk loading ----------
  const scopeSources = () => (manifest ? manifest.sources.filter((x) => !els.source.value || x.slug === els.source.value) : []);
  function neededChunks() {
    const out = [];
    for (const x of scopeSources()) for (const c of x.chunks || []) if (c.kind === "live" || showClosed) out.push(c);
    return out;
  }
  function loadProgress() {
    const need = neededChunks();
    const total = need.reduce((n, c) => n + c.rows, 0);
    const done = need.reduce((n, c) => n + (chunkState.get(c.file) === "done" ? c.rows : 0), 0);
    return { total, done, pending: need.some((c) => { const st = chunkState.get(c.file); return st !== "done" && st !== "error"; }) };
  }
  function ingest(rows) {
    for (const t of rows) {
      t._hay = norm([t.t, t.o, t.c, t.r, t.id, t.k, t.pc, t.l, t.a, t.z].join(" | "));
      all.push(t);
      if (!t.st) activeCount++;
    }
  }
  function scheduleRefresh() {   // many chunks can land in quick succession: re-render at most ~5x a second
    if (refreshTimer) return;
    refreshTimer = setTimeout(() => { refreshTimer = null; refreshFilterOptions(); apply(); }, 200);
  }
  async function loadChunk(c) {
    chunkState.set(c.file, "loading");
    try {
      const r = await fetch("data/" + c.file, { cache: "no-cache" });
      if (!r.ok) throw new Error(r.status);
      ingest((await r.json()).tenders);
      chunkState.set(c.file, "done");
    } catch (e) {
      chunkState.set(c.file, "error");
      els.notice.hidden = false;
      els.notice.textContent = `Could not load some data (${c.file}). Showing what loaded; reload to retry.`;
    }
    scheduleRefresh();
  }
  // Fetch whatever the current view needs and has not been fetched, POOL files at a time, smallest scope first.
  function ensureLoaded() {
    const queue = neededChunks().filter((c) => !chunkState.has(c.file));
    const worker = async () => { while (queue.length) await loadChunk(queue.shift()); };
    for (let i = 0; i < Math.min(POOL, queue.length); i++) worker();
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
  async function init() {
    try {
      const r = await fetch("data/index.json", { cache: "no-cache" });
      manifest = await r.json();
    } catch (e) {
      els.meta.textContent = "Could not load data/index.json";
      return;
    }
    for (const x of manifest.sources) sources[x.slug] = x;
    const totalActive = manifest.sources.reduce((n, x) => n + (x.active || 0), 0);
    if (Array.isArray(manifest.tenders)) {   // old single-file index: everything is inline
      ingest(manifest.tenders);
      for (const x of manifest.sources) for (const c of x.chunks || []) chunkState.set(c.file, "done");
    }
    els.meta.textContent = `Updated ${fmtDate(manifest.generated)} · ${totalActive.toLocaleString("en-IN")} active tenders`;
    setupSourceFilter(manifest);
    readState();
    refreshFilterOptions();
    apply();
    ensureLoaded();
    loadStatus();
  }

  let timer;
  els.q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(apply, 120); });
  for (const k of ["org", "cat", "close", "value", "sort"]) els[k].addEventListener("change", apply);
  els.source.addEventListener("change", () => { ensureLoaded(); apply(); });
  els.isNew.addEventListener("click", () => { newOnly = !newOnly; els.isNew.classList.toggle("active", newOnly); apply(); });
  els.closed.addEventListener("click", () => { showClosed = !showClosed; els.closed.classList.toggle("active", showClosed); ensureLoaded(); apply(); });
  els.clear.addEventListener("click", () => {
    els.q.value = ""; for (const k of ["source", "org", "cat", "close", "value"]) els[k].value = "";
    newOnly = false; els.isNew.classList.remove("active");
    showClosed = false; els.closed.classList.remove("active"); apply(); els.q.focus();
  });
  els.more.addEventListener("click", renderMore);
  document.addEventListener("keydown", (e) => { if (e.key === "/" && document.activeElement !== els.q) { e.preventDefault(); els.q.focus(); } });

  init();
})();
