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
    notice: $("notice"), toast: $("toast"), tabs: $("tabs"), stateBar: $("stateBar"), stateInfo: $("stateInfo"), stateName: $("stateName"), stateSub: $("stateSub"), back: $("backToMap"), mapView: $("mapView"), tendersView: $("tendersView"),
    sharedView: $("sharedView"), sharedBody: $("sharedBody"), sharedBack: $("sharedBack"), sharedNote: $("sharedNote"),
  };

  const track = (name) => { if (window.twTrack) window.twTrack(name); };   // analytics.js; fixed event names only, never typed text

  const PAGE = 50;
  let all = [];           // tenders loaded so far
  let sources = {};       // slug -> source meta
  let filtered = [];
  let shown = 0;
  let newOnly = false;     // format 2 only: first seen in the last 2 days
  let showClosed = false;  // include tenders no longer on the portal (st = "closed" | "removed")
  let manifest = null;
  let lazy = false;        // format >= 3 manifest (presets + orgs.json)
  let tab = "map";         // "map" (the landing tab) | "tenders" (the list)
  let shared = null;       // a link to one tender (?t=<source>:<id>&o=<organisation>): { s, id, o }; its page replaces both tabs
  const baseTitle = document.title;
  let listStarted = false; // the Tenders tab has loaded its rows (nothing is fetched for the list while only the map is looked at)
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
  // The address says which tab: ?tab=tenders / ?tab=map; an address with list filters but no tab is an older link to the list.
  const FILTER_KEYS = ["q", "src", "org", "cat", "close", "val", "sort", "new", "closed", "view"];
  function tabFromParams(p) {
    const t = p.get("tab");
    return t === "tenders" || t === "map" ? t : [...p.keys()].some((k) => FILTER_KEYS.includes(k)) ? "tenders" : "map";
  }
  function readState() {
    const p = new URLSearchParams(location.search);
    tab = tabFromParams(p);
    const t = p.get("t") || "", at = t.indexOf(":");
    shared = at > 0 && at < t.length - 1 ? { s: t.slice(0, at), id: t.slice(at + 1), o: p.get("o") || "" } : null;
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
      else view = [...p.keys()].some((k) => !["view", "tab", "t", "o", "ref"].includes(k)) ? "all" : "new";  // old deep links keep meaning "all"
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
  function writeState(push) {
    if (shared) return;               // a shared-tender page keeps its address
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
      if (view !== "new" || rest) { const q = new URLSearchParams(); q.set("view", view); for (const [k, v] of p) q.set(k, v); return setUrl(withTab(q), push); }
    }
    setUrl(withTab(p), push);
  }
  // the Map tab keeps a clean address; the Tenders tab always says so (tab=tenders) and then lists its filters
  function withTab(p) {
    if (tab !== "tenders") return "";
    const q = new URLSearchParams({ tab: "tenders" });
    for (const [k, v] of p) q.set(k, v);
    return q.toString();
  }
  function setUrl(qs, push) { history[push ? "pushState" : "replaceState"](null, "", qs ? "?" + qs : location.pathname); }

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

  // The heading above the list: which state (and portal) it is showing, with the way back to the map
  function updateStateBar() {
    const src = sources[els.source.value];
    els.stateBar.hidden = els.tabs.querySelector('button[data-tab="map"]').hidden && !src;   // no map in this index: only a heading
    els.back.hidden = els.tabs.querySelector('button[data-tab="map"]').hidden;
    els.stateInfo.hidden = !src;
    if (!src) return;
    const host = (() => { try { return new URL(src.base_url).hostname; } catch (e) { return ""; } })();
    els.stateName.textContent = (src.state && mapUI.nameOf(src.state)) || src.name || src.slug;
    els.stateSub.textContent = [src.name, host].filter(Boolean).join(" · ");
  }

  function apply() {
    updateStateBar();
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
    mapUI.sync();
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

  function card(t, open) {
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
        <button data-share title="Send a link to this tender to a colleague">Share</button>
        <span class="id">${esc(t.id)}${t.r ? " · Ref " + esc(t.r) : ""}</span>
      </div>
      <details${open ? " open" : ""}><summary>Details</summary>
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
    el.querySelector("[data-share]").addEventListener("click", () => shareTender(t));
    return el;
  }

  // ---------- sharing one tender ----------
  // The link names the source and tender id (and the organisation, so the page can fetch just that organisation's files):
  //   ?t=<source>:<id>&o=<organisation>&ref=share
  // It carries no personal data and nothing about the sender. The page it opens is built in the browser from the index.
  function tenderLink(t) {
    const q = new URLSearchParams();
    if (t.o) q.set("o", t.o);
    q.set("ref", "share");
    return location.href.split(/[?#]/)[0] + "?t=" + encodeURIComponent(t.s) + ":" + encodeURIComponent(t.id) + "&" + q.toString();
  }
  async function shareTender(t) {
    const url = tenderLink(t);
    const lines = [t.t, t.o + (t.d && !t.st ? " · closes " + fmtDate(t.d) : "") + (t.v != null ? " · " + fmtINR(t.v) : "")];
    track("share/tender");
    if (navigator.share) {            // phones: the system share sheet (WhatsApp, mail, ...)
      try { await navigator.share({ title: t.t, text: lines.join("\n"), url }); return; }
      catch (e) { if (e && e.name === "AbortError") return; }
    }
    const text = lines.join("\n") + "\n" + url;
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); toast("Link copied: paste it in a message"); return; } catch (e) { /* fall through */ }
    }
    window.prompt("Copy this link to the tender:", url);
  }

  // find one tender: in what is already loaded, else in the organisation's files, else in every file of its source
  async function findTender(sh) {
    const have = all.find((r) => r.s === sh.s && r.id === sh.id);
    if (have) return have;
    const src = sources[sh.s];
    if (!src) return null;
    const own = [];
    if (sh.o && orgsData) for (const e of orgEntries(sh.o, sh.s)) own.push(...(e.files || []));
    const tried = new Set();
    for (const group of [own, (src.chunks || []).map((c) => c.file)]) {
      const files = [...new Set(group)].filter((f) => !tried.has(f));
      for (let i = 0; i < files.length; i += POOL) {
        const part = files.slice(i, i + POOL);
        const rows = await Promise.all(part.map((f) => fetchJSON("data/" + f).then((d) => d.tenders || []).catch(() => [])));
        part.forEach((f) => tried.add(f));
        for (const list of rows) { const hit = list.find((r) => r.id === sh.id); if (hit) return hit; }
      }
    }
    return null;
  }
  async function openShared() {
    track("share/open");
    els.sharedNote.textContent = "";
    els.sharedBody.innerHTML = '<div class="empty">Loading the tender…</div>';
    const t = await findTender(shared);
    if (!shared) return;              // the visitor left while it loaded
    if (!t) {
      els.sharedBody.innerHTML = '<div class="empty">This tender is not in the current index. It may be very old, or listed under another source.' +
        '<br><button type="button" id="sharedSearch">Search for ' + esc(shared.id) + "</button></div>";
      $("sharedSearch").addEventListener("click", () => { location.href = location.pathname + "?" + new URLSearchParams({ tab: "tenders", view: "all", q: shared.id }); });
      return;
    }
    document.title = t.t.slice(0, 90) + " · " + baseTitle;
    els.sharedNote.textContent = "Data as of " + fmtDate(manifest.generated) + ". The portal has the latest status.";
    els.sharedBody.replaceChildren(card(t, true));
  }
  // leave the shared page for a tab (the list or the map); Back returns to the shared page
  function leaveShared(to) {
    shared = null;
    document.title = baseTitle;
    els.sharedBody.textContent = "";
    tab = to;
    showTab();
    window.scrollTo(0, 0);
    track("tab/" + to);
    writeState(true);
    if (to === "tenders") startList();
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
    track("view/" + v);
    view = v; fullScope = false;
    updateViewButtons();
    rebuildOptions();
    ensureLoaded();
    apply();
  }

  // ---------- tabs: Map (landing) | Tenders ----------
  function showTab() {
    els.mapView.hidden = tab !== "map" || !!shared;
    els.tendersView.hidden = tab !== "tenders" || !!shared;
    els.sharedView.hidden = !shared;
    for (const b of els.tabs.querySelectorAll("button[data-tab]")) {
      const on = !shared && b.dataset.tab === tab;
      b.setAttribute("aria-selected", on);
      b.classList.toggle("active", on);
      b.tabIndex = on ? 0 : -1;
    }
    mapUI.show(tab === "map" && !shared);
  }
  // first visit to the list: fetch what its view needs (until then only the map's small files were loaded)
  function startList() {
    if (listStarted) return;
    listStarted = true;
    ensureLoaded();
    apply();
  }
  // fromUser: a click on a tab or on the map. It adds a history entry, so Back returns to the other tab.
  function setTab(t, fromUser) {
    if (shared) return leaveShared(t);
    if (t === tab) return;
    tab = t;
    showTab();
    if (fromUser) { track("tab/" + t); window.scrollTo(0, 0); }
    writeState(!!fromUser);
    if (t === "tenders") startList();
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
      mapUI.setStatus(st.sources);
      const bad = st.sources.filter((s) => !s.ok);
      if (bad.length) {
        els.notice.hidden = false;
        els.notice.innerHTML = `Last crawl had problems for: ${esc(bad.map((s) => s.name || s.slug).join(", "))}. Data for those sources may be stale. <a href="status.html">Crawl status</a>`;
      }
    } catch (e) { /* status is optional */ }
  }

  // ---------- India map panel ----------
  // The landing tab. Shown only when the manifest carries per-source "state" fields (ISO 3166-2 code, or "central").
  // Everything it shows is aggregated from the manifest (state / active / stats) and status.json (crawl time);
  // boundaries come from map/india-states.json, fetched when the Map tab is first drawn in "Map" style. A click selects
  // that state's source in the ordinary source filter (selectSource) and opens the Tenders tab, so URL state and lazy
  // chunk loading behave exactly as for the dropdown.
  const mapUI = (function () {
    const pre = (o) => { const r = {}; for (const k of Object.keys(o)) r["IN-" + k] = o[k]; return r; };
    const CODE = pre({ AN: "AN", AP: "AP", AR: "AR", AS: "AS", BR: "BR", CH: "CH", CT: "CG", DL: "DL", DH: "DD", GA: "GA", GJ: "GJ", HR: "HR", HP: "HP", JK: "JK", JH: "JH",
      KA: "KA", KL: "KL", LA: "LA", LD: "LD", MP: "MP", MH: "MH", MN: "MN", ML: "ML", MZ: "MZ", NL: "NL", OR: "OD", PY: "PY", PB: "PB", RJ: "RJ", SK: "SK", TN: "TN",
      TG: "TS", TR: "TR", UP: "UP", UT: "UK", WB: "WB" });                // ISO code -> the abbreviation people use
    const NAMES = pre({ AN: "Andaman & Nicobar Islands", AP: "Andhra Pradesh", AR: "Arunachal Pradesh", AS: "Assam", BR: "Bihar", CH: "Chandigarh", CT: "Chhattisgarh",
      DL: "Delhi", DH: "Dadra & Nagar Haveli and Daman & Diu", GA: "Goa", GJ: "Gujarat", HR: "Haryana", HP: "Himachal Pradesh", JK: "Jammu & Kashmir", JH: "Jharkhand",
      KA: "Karnataka", KL: "Kerala", LA: "Ladakh", LD: "Lakshadweep", MP: "Madhya Pradesh", MH: "Maharashtra", MN: "Manipur", ML: "Meghalaya", MZ: "Mizoram",
      NL: "Nagaland", OR: "Odisha", PY: "Puducherry", PB: "Punjab", RJ: "Rajasthan", SK: "Sikkim", TN: "Tamil Nadu", TG: "Telangana", TR: "Tripura",
      UP: "Uttar Pradesh", UT: "Uttarakhand", WB: "West Bengal" });
    const TILE = pre({ LA: [0, 2], JK: [1, 2], HP: [2, 3], PB: [3, 2], CH: [3, 3], UT: [3, 4], SK: [3, 7], AR: [3, 9], HR: [4, 3], DL: [4, 4], UP: [4, 5], BR: [4, 6], WB: [4, 7],
      AS: [4, 8], NL: [4, 9], RJ: [5, 2], MP: [5, 4], CT: [5, 5], JH: [5, 6], ML: [5, 8], MN: [5, 9], GJ: [6, 1], MH: [6, 3], TG: [6, 5], OR: [6, 6], TR: [6, 8], MZ: [6, 9],
      DH: [7, 1], GA: [7, 2], KA: [7, 3], AP: [7, 5], LD: [8, 1], KL: [8, 3], TN: [8, 4], PY: [9, 5], AN: [9, 8] });   // [row, column]
    const MARKER = pre({ CH: [-32, -18], DL: [-32, 6], DH: [-24, 8], GA: [-22, 6], LD: [-10, 18], PY: [14, 14] });     // marker offsets from the real position
    const BLOCKED = pre({});             // states whose portal cannot be crawled (none now); { IN-XX: reason } to mark one
    const LIMITED = pre({ MZ: "Only the state e-Tender notice board is crawled (a handful of open notices)." });
    const STEPS = { active: [0, 100, 1000, 3000, 6000], new: [0, 20, 100, 300, 800], closing48: [0, 20, 100, 300, 800] };
    const LABEL = { active: "active tenders", new: "new tenders in the last 2 days", closing48: "tenders closing within 48 hours" };
    const plain = (s) => String(s).normalize("NFD").replace(/[̀-ͯ]/g, "");
    const num = (n) => fmtN(n || 0);
    const canHover = matchMedia("(hover: hover) and (pointer: fine)").matches;

    const e = {};          // elements, resolved in init()
    const agg = new Map(); // "IN-KL" | "central" -> { active, new, closing48, orgs, srcs, best }
    const finished = {};   // source slug -> last crawl finish (status.json)
    let ready = false, open = false /* the Map tab is showing */, metric = "active", style = innerWidth < 640 ? "tiles" : "map", selected = null;
    let geo = null, geoState = "idle";   // "idle" | "loading" | "ok" | "error"

    function buildModel() {
      for (const x of manifest.sources) {
        if (!x.state || x.state === "multi" || !(x.enabled || x.active > 0)) continue;   // only sources the source dropdown offers; "multi" = several states, not on the map
        let m = agg.get(x.state);
        if (!m) agg.set(x.state, m = { active: 0, new: 0, closing48: 0, orgs: 0, srcs: [], best: null });
        const st = x.stats || {};
        m.active += x.active || 0; m.new += st.new || 0; m.closing48 += st.closing48 || 0; m.orgs += st.orgs || 0;
        m.srcs.push(x);
      }
      for (const m of agg.values()) {   // the source a click opens: an enabled one with the most active tenders
        const pool = m.srcs.some((x) => x.enabled) ? m.srcs.filter((x) => x.enabled) : m.srcs;
        m.best = pool.reduce((a, b) => ((b.active || 0) > (a.active || 0) ? b : a));
      }
    }

    const nameOf = (k) => k === "central" ? "Central / all-India" : NAMES[k] || (geo && geo.units[k] ? plain(geo.units[k].name) : k);
    const codeOf = (k) => CODE[k] || k.slice(3);
    const kind = (k) => agg.has(k) ? "ok" : BLOCKED[k] ? "blocked" : "none";
    const bucket = (n) => { const t = STEPS[metric]; if (!n) return 0; let b = 1; for (let i = 1; i < t.length; i++) if (n >= t[i]) b = i + 1; return b; };
    const cls = (k) => (kind(k) === "ok" ? "b" + bucket(agg.get(k)[metric]) : kind(k));
    const label = (k) => {
      const m = agg.get(k);
      return m ? `${nameOf(k)}: ${num(m[metric])} ${LABEL[metric]}. Open the list.`
        : `${nameOf(k)}: ${BLOCKED[k] ? "cannot be crawled (CAPTCHA)" : "not on TenderWatch yet"}`;
    };
    const ago = (iso) => {
      const t = iso ? Date.parse(iso) : NaN;
      if (isNaN(t)) return "";
      const h = (Date.now() - t) / 3600000;
      return h < 1 ? "under an hour ago" : h < 48 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} days ago`;
    };
    const hostOf = (u) => { try { return new URL(u).hostname; } catch (err) { return ""; } };
    const setMsg = (t) => { e.msg.textContent = t || ""; };

    // ----- drawing -----
    function drawMap() {
      const U = geo.units, p = [`<svg viewBox="0 0 ${geo.w} ${geo.h}" role="group" aria-label="Map of India by state">`,
        `<defs><pattern id="mapHatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="var(--warn-soft)"/><line x1="0" y1="0" x2="0" y2="6" stroke="var(--warn)" stroke-width="1.6"/></pattern></defs>`];
      const keys = Object.keys(U);
      for (const k of keys) {
        const ok = kind(k) === "ok";
        p.push(`<path class="st ${cls(k)}" data-k="${k}" d="${esc(U[k].d)}" fill-rule="evenodd" tabindex="${ok ? 0 : -1}" role="${ok ? "link" : "img"}" aria-label="${esc(label(k))}"></path>`);
      }
      for (const k of keys) {
        if (U[k].marker) continue;
        const c = cls(k);
        p.push(`<text class="lab ${/^b[345]$/.test(c) ? "dk" : ""}${c === "none" || c === "blocked" ? " mt" : ""}" x="${U[k].cx}" y="${U[k].cy}">${esc(codeOf(k))}</text>`);
      }
      for (const k of keys) {
        const u = U[k]; if (!u.marker) continue;
        const [dx, dy] = MARKER[k] || [-20, 10], x = u.cx + dx, y = u.cy + dy, ok = kind(k) === "ok";
        p.push(`<line x1="${u.cx}" y1="${u.cy}" x2="${x}" y2="${y}" stroke="var(--muted)" stroke-width=".6"/>`);
        p.push(`<circle class="mk ${cls(k)}" data-k="${k}" cx="${x}" cy="${y}" r="6.4" tabindex="${ok ? 0 : -1}" role="${ok ? "link" : "img"}" aria-label="${esc(label(k))}"></circle>`);
        p.push(`<text class="mlab" x="${x + (dx < 0 ? -9 : 9)}" y="${y}" text-anchor="${dx < 0 ? "end" : "start"}" dominant-baseline="central">${esc(codeOf(k))}</text>`);
      }
      p.push("</svg>");
      e.box.className = "mapbox";
      e.box.innerHTML = p.join("");
    }
    function drawTiles() {
      const frag = document.createDocumentFragment();
      for (const [k, [r, c]] of Object.entries(TILE)) {
        const m = agg.get(k), b = document.createElement("button");
        b.type = "button"; b.className = "tile " + cls(k); b.dataset.k = k; b.style.gridRow = r + 1; b.style.gridColumn = c + 1;
        b.setAttribute("role", "listitem"); b.setAttribute("aria-label", label(k));
        b.innerHTML = `<span class="c">${esc(codeOf(k))}</span><span class="n">${m ? num(m[metric]) : BLOCKED[k] ? "!" : "–"}</span>`;
        frag.appendChild(b);
      }
      e.tiles.innerHTML = "";
      e.tiles.appendChild(frag);
    }
    function markSel() {
      for (const el of e.panel.querySelectorAll("[data-k]")) {
        const on = el.dataset.k === selected;
        el.classList.toggle("sel", on);
        if (on) el.setAttribute("aria-current", "true"); else el.removeAttribute("aria-current");
      }
    }
    function render() {
      if (!ready || !open) return;
      e.box.hidden = style !== "map"; e.tiles.hidden = style !== "tiles";
      if (style === "map") {
        if (geo) drawMap();
        else { e.box.className = "mapbox loading"; e.box.textContent = "Loading map…"; ensureGeo(); }
      } else drawTiles();
      for (const b of e.styleG.children) b.setAttribute("aria-pressed", b.dataset.v === style);
      for (const b of e.metricG.children) b.setAttribute("aria-pressed", b.dataset.m === metric);
      const c = agg.get("central");
      if (c) e.central.innerHTML = `Central / all-India · <strong>${num(c[metric])}</strong> ${metric === "active" ? "active" : metric === "new" ? "new" : "closing ≤ 48 h"}`;
      const t = STEPS[metric], f = (n) => fmtN(n);
      const names = ["0", `1–${f(t[1] - 1)}`, `${f(t[1])}–${f(t[2] - 1)}`, `${f(t[2])}–${f(t[3] - 1)}`, `${f(t[3])}–${f(t[4] - 1)}`, `${f(t[4])}+`];
      e.legend.innerHTML = names.map((x, i) => `<span><i class="sw" style="background:var(--b${i})"></i>${x}</span>`).join("") +
        `<span><i class="sw none"></i>not on TenderWatch yet</span>` +
        (Object.keys(BLOCKED).some((k) => !agg.has(k)) ? `<span><i class="sw blocked"></i>blocked by a CAPTCHA</span>` : "");
      markSel();
    }

    // ----- boundary file (lazy) -----
    async function ensureGeo() {
      if (geoState !== "idle") return;
      geoState = "loading";
      try {
        const r = await fetch("map/india-states.json");
        if (!r.ok) throw new Error(r.status);
        const g = await r.json();
        if (!g || !g.units || !(g.w > 0) || !(g.h > 0)) throw new Error("bad geometry");
        geo = g; geoState = "ok";
      } catch (err) {
        geoState = "error"; style = "tiles";
        e.styleG.querySelector('[data-v="map"]').hidden = true;
        setMsg("The map outline could not be loaded, so the tile grid is shown instead.");
      }
      render();
    }

    // ----- tooltip (mouse only) -----
    function tipHtml(k) {
      const m = agg.get(k), n = esc(nameOf(k));
      if (!m) return `<strong>${n}</strong>${BLOCKED[k] ? "Cannot be crawled: " + esc(BLOCKED[k]) : "Not on TenderWatch yet."}`;
      const when = ago(finished[m.best.slug]), host = hostOf(m.best.base_url);
      const foot = [when ? "Crawled " + when : "", host].filter(Boolean).map(esc).join(" · ");
      return `<strong>${n}</strong><div class="row"><span>Active tenders</span><b>${num(m.active)}</b></div><div class="row"><span>New (2 days)</span><b>${num(m.new)}</b></div>` +
        `<div class="row"><span>Closing within 48 h</span><b>${num(m.closing48)}</b></div><div class="row"><span>Organisations</span><b>${num(m.orgs)}</b></div>` +
        (foot || LIMITED[k] ? `<div class="foot">${foot}${LIMITED[k] ? (foot ? "<br>" : "") + esc(LIMITED[k]) : ""}</div>` : "");
    }
    function moveTip(ev) {
      e.tip.style.left = Math.max(8, Math.min(ev.clientX + 16, innerWidth - e.tip.offsetWidth - 8)) + "px";
      e.tip.style.top = Math.max(8, Math.min(ev.clientY + 16, innerHeight - e.tip.offsetHeight - 8)) + "px";
    }
    function showTip(k, ev) { e.tip.innerHTML = tipHtml(k); e.tip.hidden = false; moveTip(ev); }
    function hideTip() { e.tip.hidden = true; }

    // ----- actions -----
    function pick(k) {
      hideTip();
      track("map/state/" + k);   // also counted for states we do not cover yet: shows where the demand is
      const m = agg.get(k);
      if (!m) { setMsg(`${nameOf(k)}: ${BLOCKED[k] || "not on TenderWatch yet."}`); return; }
      if (!selectSource(m.best.slug)) return;
      setMsg("");
      setTab("tenders", true);   // the list, filtered to that state's source
    }
    // called when the Map tab is shown or hidden: nothing is drawn (or fetched) while another tab is up
    function show(v) {
      open = v;
      if (!v) hideTip();
      render();
    }
    // keep the highlighted state in step with the source filter (dropdown, URL, Clear, or a click here)
    function sync() {
      if (!ready) return;
      const cur = sources[els.source.value], key = cur && cur.state ? cur.state : null;
      if (key === selected) return;
      selected = key;
      markSel();
    }

    function init() {
      if (!manifest.sources.some((x) => x.state)) return;   // older index: no map panel at all
      buildModel();
      Object.assign(e, {
        panel: $("mapPanel"), metricG: $("mapMetric"), styleG: $("mapStyle"), hint: $("mapHint"),
        box: $("mapBox"), tiles: $("mapTiles"), central: $("mapCentral"), centralNote: $("mapCentralNote"), msg: $("mapMsg"), legend: $("mapLegend"), tip: $("mapTip"),
      });
      e.central.dataset.k = "central";
      e.central.hidden = e.centralNote.hidden = !agg.has("central");
      e.hint.textContent = canHover ? "Hover a state for details; click opens its tender list." : "Tap a state to open its tender list.";
      e.panel.hidden = false;
      ready = true;

      e.metricG.addEventListener("click", (ev) => { const b = ev.target.closest("button"); if (b) { metric = b.dataset.m; track("map/metric/" + metric); render(); } });
      e.styleG.addEventListener("click", (ev) => { const b = ev.target.closest("button"); if (b && !b.hidden) { style = b.dataset.v; track("map/style/" + style); render(); } });
      e.panel.addEventListener("click", (ev) => { const t = ev.target.closest("[data-k]"); if (t) pick(t.dataset.k); });
      e.panel.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape") {
          hideTip(); setMsg("");
          if (selected) clearSource();
        } else if ((ev.key === "Enter" || ev.key === " ") && ev.target.dataset && ev.target.dataset.k && ev.target.tagName !== "BUTTON") {
          ev.preventDefault(); pick(ev.target.dataset.k);   // SVG states / markers (tiles and the chip are real buttons)
        }
      });
      if (canHover) {
        e.panel.addEventListener("mouseover", (ev) => { const t = ev.target.closest("[data-k]"); if (t) showTip(t.dataset.k, ev); else hideTip(); });
        e.panel.addEventListener("mousemove", (ev) => { if (!e.tip.hidden) moveTip(ev); });
        e.panel.addEventListener("mouseleave", hideTip);
        window.addEventListener("scroll", hideTip, { passive: true });
      }

      sync();
    }
    function setStatus(rows) { for (const s of rows || []) if (s && s.slug) finished[s.slug] = s.finished; }
    return { init, show, sync, setStatus, nameOf };
  })();

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
    loadStatus();
    mapUI.init();
    if (!manifest.sources.some((x) => x.state)) {   // an index without the map data: the list is all there is
      tab = "tenders";
      els.tabs.querySelector('button[data-tab="map"]').hidden = true;
    }
    showTab();
    if (shared) openShared();
    else if (tab === "tenders") startList(); else writeState();
  }

  let timer;
  let searched = false;   // count "a search was used" once per page load, not once per keystroke
  els.q.addEventListener("input", () => {
    if (!searched && els.q.value.trim()) { searched = true; track("search"); }
    clearTimeout(timer); timer = setTimeout(apply, 120);
  });
  for (const k of ["cat", "close", "value", "sort"]) els[k].addEventListener("change", apply);
  els.org.addEventListener("change", () => { ensureLoaded(); apply(); });
  function onSourceChange() {
    if (lazy) {
      const org = els.org.value;
      if (org && orgsData && !orgEntries(org, els.source.value).length) els.org.value = "";   // that source has no such organisation
      updateViewButtons();
      rebuildOptions();
    }
    ensureLoaded(); apply();
  }
  els.source.addEventListener("change", onSourceChange);
  els.source.addEventListener("change", () => { if (els.source.value) track("source/" + els.source.value); });   // the dropdown only; the map reports its own click
  // Same as choosing the source in the dropdown, but from the map: show that source's full list ("All tenders"),
  // drop the organisation filter (search text and the other filters stay).
  function selectSource(slug) {
    if (!sources[slug]) return false;
    const defSort = els.sort.value === defaultSort();
    if (lazy && view !== "all") { view = "all"; fullScope = false; }
    setupSourceFilter(slug);
    if (els.source.value !== slug) return false;
    els.org.value = "";
    if (defSort) els.sort.value = defaultSort();
    onSourceChange();
    return true;
  }
  function clearSource() { els.source.value = ""; onSourceChange(); }
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
  els.back.addEventListener("click", () => setTab("map", true));
  els.tabs.addEventListener("click", (e) => { const b = e.target.closest("button[data-tab]"); if (b) setTab(b.dataset.tab, true); });
  els.tabs.addEventListener("keydown", (e) => {   // arrow keys move between the two tabs
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const t = tab === "map" ? "tenders" : "map", b = els.tabs.querySelector(`button[data-tab="${t}"]`);
    if (b.hidden) return;
    e.preventDefault(); setTab(t, true); b.focus();
  });
  window.addEventListener("popstate", () => {
    if (!manifest) return;
    if (shared || new URLSearchParams(location.search).get("t")) { location.reload(); return; }   // to or from a shared-tender page
    setTab(tabFromParams(new URLSearchParams(location.search)), false);
  });
  els.sharedBack.addEventListener("click", () => leaveShared("tenders"));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || document.activeElement === els.q || /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "")) return;
    e.preventDefault();
    setTab("tenders", true);
    els.q.focus();
  });

  init();
})();
