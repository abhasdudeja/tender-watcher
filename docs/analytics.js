/* Anonymous visit and usage counting with GoatCounter (https://www.goatcounter.com): no cookies, no personal data.
   Nothing is loaded or sent unless SITE below holds a GoatCounter site code, and never for visitors who send
   Do Not Track or Global Privacy Control. app.js calls window.twTrack("event/name") for a few UI actions
   (view switch, source filter, map clicks, a search was used). Events carry fixed names only: never the search
   text, organisation names or anything else the visitor typed. Visits are counted against the page path alone,
   without the ?q=... filter part of the address. A visitor can also opt out with
   localStorage.setItem("skipgc", "t") in the browser console (GoatCounter's own switch). */
(function () {
  "use strict";

  var SITE = "amdudeja";   // https://amdudeja.goatcounter.com ; set to "" to count nothing

  window.twTrack = function () {};
  if (!/^[a-z0-9][a-z0-9-]{1,30}$/.test(SITE)) return;
  if (navigator.doNotTrack === "1" || window.doNotTrack === "1" || navigator.globalPrivacyControl === true) return;

  var queue = [], loaded = false;
  function send(name) {
    try { window.goatcounter.count({ path: name, title: name, event: true }); } catch (e) { /* analytics must never break the page */ }
  }
  window.twTrack = function (name) {
    if (typeof name !== "string" || !/^[A-Za-z0-9\/_.-]{1,60}$/.test(name)) return;
    if (loaded) send(name);
    else if (queue.length < 50) queue.push(name);
  };

  function showNote() { var n = document.getElementById("privacyNote"); if (n) n.hidden = false; }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", showNote); else showNote();

  window.goatcounter = { endpoint: "https://" + SITE + ".goatcounter.com/count", path: location.pathname || "/" };
  var s = document.createElement("script");
  s.async = true;
  s.src = "https://gc.zgo.at/count.js";
  s.onload = function () {
    loaded = true;
    // the page view is counted by count.js itself on load; events queued meanwhile go out after it
    while (queue.length) send(queue.shift());
  };
  s.onerror = function () { queue.length = 0; };   // blocked (ad blocker, offline): drop quietly
  document.head.appendChild(s);
})();
