/* weather.slippylabs.com — consensus forecast front end.
 *
 * All data comes from /api/. Temperatures and wind arrive in F and mph
 * throughout; the C toggle converts at render time, so switching units costs
 * no refetch. Every value that reaches the DOM goes through textContent — a
 * place name is attacker-supplied via the hash, so none of it may be markup.
 */
(function () {
  "use strict";

  const $ = (s, r) => (r || document).querySelector(s);
  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const ADMIN_HOST = "https://admin.slippylabs.com";
  const ACCOUNT_SLUG = "weather";
  const RECENTS_KEY = "slippy-weather-recents";
  const UNITS_KEY = "slippy-weather-units";
  const EVENT_KEY = "slippy-weather-event";
  const MAX_RECENTS = 6;

  // Verdict thresholds. Applied to the event window when one is set.
  const RULES = {
    rain: { good: 20, watch: 45 },
    wind: { good: 12, watch: 20 },
    hum:  { good: 65, watch: 80 },
  };

  const state = {
    loc: null,       // {name, lat, lon, tz, country, admin1, label}
    data: null,
    units: localStorage.getItem(UNITS_KEY) === "c" ? "c" : "f",
    event: null,     // {date, from, to, label}
    pending: null,   // abort token for the in-flight forecast request
  };

  /* ---------- formatting ---------- */

  // Dates are plain YYYY-MM-DD in the *location's* calendar. Never feed them
  // to new Date(iso) -- that parses as UTC and slides a day backwards west of
  // Greenwich. Build from parts so the label matches the forecast's own day.
  function parseDay(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  function fmtDay(iso) { const d = parseDay(iso); return MON[d.getMonth()] + " " + d.getDate(); }

  function temp(f) {
    if (f == null) return "—";
    return (state.units === "c" ? Math.round((f - 32) * 5 / 9) : Math.round(f)) + "°";
  }
  function speed(mph) {
    if (mph == null) return "—";
    return state.units === "c"
      ? Math.round(mph * 1.609) + " km/h"
      : Math.round(mph) + " mph";
  }
  function hourLabel(h) {
    if (h === 0) return "12am";
    if (h === 12) return "noon";
    return (h % 12 === 0 ? 12 : h % 12) + (h < 12 ? "am" : "pm");
  }
  function fmtClock(hhmm) {
    if (!hhmm) return "";
    const [h, m] = hhmm.split(":").map(Number);
    const ampm = h < 12 ? "am" : "pm";
    return (h % 12 === 0 ? 12 : h % 12) + ":" + String(m).padStart(2, "0") + ampm;
  }
  function fmtTime(iso) {
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }
  // "first → last"; a source can report ok with nothing in it, so read the end
  // of the range defensively rather than letting one blank source throw out of
  // render() and blank the page.
  function coverageEnd(coverage) {
    if (typeof coverage !== "string") return null;
    const end = coverage.split("→")[1];
    return end ? end.trim() : null;
  }

  function grade(v, r) { return v == null ? null : v <= r.good ? "good" : v <= r.watch ? "watch" : "concern"; }
  function gradeTemp(t) { return t == null ? null : (t >= 62 && t <= 82) ? "good" : (t >= 50 && t <= 90) ? "watch" : "concern"; }
  function avgOf(rows, k) {
    const v = rows.map(r => r[k]).filter(x => x != null);
    return v.length ? Math.round(v.reduce((a, b) => a + b, 0) / v.length) : null;
  }

  /* ---------- persistence ---------- */

  function readJSON(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch (e) { return fallback; }
  }
  function recents() { const r = readJSON(RECENTS_KEY, []); return Array.isArray(r) ? r : []; }
  function rememberPlace(loc) {
    const key = l => `${l.lat.toFixed(2)},${l.lon.toFixed(2)}`;
    const list = recents().filter(l => key(l) !== key(loc));
    list.unshift({ name: loc.name, lat: loc.lat, lon: loc.lon, tz: loc.tz,
                   country: loc.country, admin1: loc.admin1, label: loc.label });
    try { localStorage.setItem(RECENTS_KEY, JSON.stringify(list.slice(0, MAX_RECENTS))); } catch (e) {}
    renderRecents();
  }

  /* ---------- the account's chosen place ----------
     Signed-in visitors get their current place stored against their account,
     which is the only way slippylabs.com's home-page strip can know where to
     show weather for -- localStorage here is unreadable from that origin. It
     is one place name and its coordinates, nothing else, and the page says so
     rather than doing it silently. Signed-out visitors are untouched: recents
     stay in this browser exactly as before. */

  let accountUser = null;

  async function initAccount() {
    try {
      const res = await fetch(`${ADMIN_HOST}/whoami`, {
        credentials: "include", headers: { Accept: "application/json" },
      });
      const d = await res.json();
      accountUser = d && d.authenticated ? d.username : null;
    } catch (e) {
      accountUser = null;   // admin console down: everything else still works
    }
    if (!accountUser) return;
    showAccountNote();
    // The page restores a place from the hash or from recents without waiting
    // for this check, so by now select() may already have run and skipped the
    // save. Catch that case up.
    if (state.loc) saveAccountPlace(state.loc);
  }

  function showAccountNote() {
    const el = $("#account-note");
    if (!el) return;
    el.textContent = "Signed in as " + accountUser +
      " — this place is saved to your account so it shows on the Slippy Labs home page.";
    el.hidden = false;
  }

  async function saveAccountPlace(loc) {
    if (!accountUser || !loc) return;
    try {
      await fetch(`${ADMIN_HOST}/api/saves/${ACCOUNT_SLUG}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ save_data: {
          v: 1,
          updated: new Date().toISOString().slice(0, 16),
          location: { name: loc.label || loc.name, lat: loc.lat, lon: loc.lon,
                      tz: loc.tz || "UTC", country: loc.country || null,
                      admin1: loc.admin1 || null },
        } }),
      });
    } catch (e) { /* the forecast itself does not depend on this */ }
  }

  /* ---------- hash routing ---------- */
  // #lat,lon/Name[/YYYY-MM-DD]  -- the trailing day is which card is open.

  function writeHash(openDate) {
    if (!state.loc) return;
    const parts = [`${state.loc.lat},${state.loc.lon}`, encodeURIComponent(state.loc.name)];
    if (openDate) parts.push(openDate);
    const h = "#" + parts.join("/");
    if (location.hash !== h) history.replaceState(null, "", h);
  }

  function readHash() {
    const raw = decodeURIComponent(location.hash.replace(/^#/, ""));
    if (!raw) return null;
    const parts = raw.split("/");
    const [lat, lon] = (parts[0] || "").split(",").map(Number);
    if (!isFinite(lat) || !isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
    return { lat, lon, name: parts[1] ? decodeURIComponent(parts[1]) : "", openDate: parts[2] || null };
  }

  /* ---------- API ---------- */

  async function api(path, params) {
    const q = new URLSearchParams(params || {}).toString();
    const r = await fetch(path + (q ? "?" + q : ""));
    if (!r.ok) {
      let msg = "HTTP " + r.status;
      try { msg = (await r.json()).error || msg; } catch (e) {}
      throw new Error(msg);
    }
    return r.json();
  }

  /* ---------- location search ---------- */

  const searchInput = $("#loc-search");
  const resultsEl = $("#loc-results");
  let searchSeq = 0;

  function closeResults() {
    resultsEl.classList.add("hidden");
    resultsEl.textContent = "";
    searchInput.setAttribute("aria-expanded", "false");
  }

  async function doSearch(q) {
    // Each keystroke bumps the sequence; a slower earlier response that lands
    // after a newer one must not overwrite the newer results.
    const seq = ++searchSeq;
    if (q.trim().length < 2) { closeResults(); return; }
    let hits;
    try { hits = (await api("/api/geocode", { q })).results || []; }
    catch (e) { return; }
    if (seq !== searchSeq) return;
    resultsEl.textContent = "";
    if (!hits.length) {
      const li = document.createElement("li");
      li.className = "wx-result-empty";
      li.textContent = "No place by that name.";
      resultsEl.appendChild(li);
    } else {
      hits.forEach(h => {
        const li = document.createElement("li");
        li.className = "wx-result";
        li.setAttribute("role", "option");
        li.tabIndex = 0;
        const n = document.createElement("span");
        n.className = "wx-result-name";
        n.textContent = h.name;
        const sub = document.createElement("span");
        sub.className = "wx-result-sub";
        sub.textContent = [h.admin1, h.country].filter(Boolean).join(", ");
        li.append(n, sub);
        const pick = () => { closeResults(); searchInput.value = ""; select(h); };
        li.addEventListener("click", pick);
        li.addEventListener("keydown", e => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
        });
        resultsEl.appendChild(li);
      });
    }
    resultsEl.classList.remove("hidden");
    searchInput.setAttribute("aria-expanded", "true");
  }

  let searchTimer = null;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = searchInput.value;
    searchTimer = setTimeout(() => doSearch(q), 250);
  });
  searchInput.addEventListener("keydown", e => {
    if (e.key === "Escape") closeResults();
    if (e.key === "ArrowDown") {
      const first = resultsEl.querySelector(".wx-result");
      if (first) { e.preventDefault(); first.focus(); }
    }
    if (e.key === "Enter") {
      e.preventDefault();
      const first = resultsEl.querySelector(".wx-result");
      if (first) first.click();
    }
  });
  document.addEventListener("click", e => {
    if (!e.target.closest(".wx-search-wrap")) closeResults();
  });

  /* ---------- geolocation ---------- */

  $("#geo-btn").addEventListener("click", () => {
    if (!navigator.geolocation) { setStatus("This browser has no location support.", true); return; }
    setStatus("Asking your browser where you are…");
    navigator.geolocation.getCurrentPosition(async pos => {
      try {
        const loc = await api("/api/reverse", { lat: pos.coords.latitude, lon: pos.coords.longitude });
        select(loc);
      } catch (e) { setStatus("Couldn't identify that spot (" + e.message + ").", true); }
    }, err => {
      setStatus(err.code === err.PERMISSION_DENIED
        ? "Location permission denied — search for a place instead."
        : "Couldn't get a location fix.", true);
    }, { timeout: 15000, maximumAge: 600000 });
  });

  /* ---------- recents ---------- */

  function renderRecents() {
    const wrap = $("#recents");
    const list = recents();
    wrap.textContent = "";
    if (!list.length) { wrap.hidden = true; return; }
    const label = document.createElement("span");
    label.className = "wx-recents-label";
    label.textContent = "Recent";
    wrap.appendChild(label);
    list.forEach(l => {
      const b = document.createElement("button");
      b.className = "wx-recent-btn";
      b.type = "button";
      b.textContent = l.name;
      b.title = l.label || l.name;
      b.addEventListener("click", () => select(l));
      wrap.appendChild(b);
    });
    wrap.hidden = false;
  }

  /* ---------- units ---------- */

  function setUnits(u) {
    state.units = u;
    try { localStorage.setItem(UNITS_KEY, u); } catch (e) {}
    $("#unit-f").classList.toggle("active", u === "f");
    $("#unit-c").classList.toggle("active", u === "c");
    $("#unit-f").setAttribute("aria-pressed", String(u === "f"));
    $("#unit-c").setAttribute("aria-pressed", String(u === "c"));
    if (state.data) render(state.data);
  }
  $("#unit-f").addEventListener("click", () => setUnits("f"));
  $("#unit-c").addEventListener("click", () => setUnits("c"));

  /* ---------- event planner ---------- */

  const planner = $("#planner");

  function fillHourSelects() {
    [$("#plan-from"), $("#plan-to")].forEach(sel => {
      sel.textContent = "";
      for (let h = 0; h < 24; h++) {
        const o = document.createElement("option");
        o.value = String(h);
        o.textContent = hourLabel(h);
        sel.appendChild(o);
      }
    });
  }

  function fillDaySelect() {
    const sel = $("#plan-date");
    sel.textContent = "";
    (state.data ? state.data.days : []).forEach(d => {
      const o = document.createElement("option");
      o.value = d;
      o.textContent = DOW[parseDay(d).getDay()] + " " + fmtDay(d);
      sel.appendChild(o);
    });
  }

  function readPlanner() {
    const from = Number($("#plan-from").value);
    let to = Number($("#plan-to").value);
    // A window that ends before it starts is a slip, not an overnight event:
    // clamp rather than silently producing an empty hour range.
    if (to < from) { to = from; $("#plan-to").value = String(from); }
    return { date: $("#plan-date").value, from, to, label: $("#plan-label").value.trim() };
  }

  function applyPlanner() {
    state.event = readPlanner();
    try { localStorage.setItem(EVENT_KEY, JSON.stringify(state.event)); } catch (e) {}
    if (state.data) render(state.data);
  }

  ["#plan-date", "#plan-from", "#plan-to"].forEach(s =>
    $(s).addEventListener("change", applyPlanner));
  $("#plan-label").addEventListener("input", applyPlanner);

  $("#plan-btn").addEventListener("click", () => {
    const open = planner.classList.contains("hidden");
    planner.classList.toggle("hidden", !open);
    $("#plan-btn").setAttribute("aria-expanded", String(open));
    if (open && !state.event) applyPlanner();
  });

  $("#plan-clear").addEventListener("click", () => {
    state.event = null;
    try { localStorage.removeItem(EVENT_KEY); } catch (e) {}
    $("#plan-label").value = "";
    if (state.data) render(state.data);
  });

  function restoreEvent() {
    const e = readJSON(EVENT_KEY, null);
    if (!e || !state.data) return;
    // A saved event only survives if its day is still in range -- forecasts
    // roll forward daily, so yesterday's pick is simply gone.
    if (!state.data.days.includes(e.date)) return;
    state.event = e;
    $("#plan-date").value = e.date;
    $("#plan-from").value = String(e.from);
    $("#plan-to").value = String(e.to);
    $("#plan-label").value = e.label || "";
    planner.classList.remove("hidden");
    $("#plan-btn").setAttribute("aria-expanded", "true");
  }

  /* ---------- status ---------- */

  function setStatus(msg, isErr) {
    const el = $("#status");
    el.textContent = msg;
    el.classList.toggle("wx-err", !!isErr);
  }

  /* ---------- load ---------- */

  async function select(loc) {
    state.loc = loc;
    state.data = null;
    closeResults();
    $("#place-head").hidden = false;
    $("#place-name").textContent = loc.label || loc.name;
    writeHash(null);
    rememberPlace(loc);
    saveAccountPlace(loc);
    $("#days").textContent = "";
    $("#method").classList.add("hidden");
    setStatus("Querying nine forecast sources for " + (loc.name || "that spot") + "… this can take a few seconds the first time.");

    const token = {};
    state.pending = token;
    try {
      const data = await api("/api/forecast", {
        lat: loc.lat, lon: loc.lon, name: loc.name, tz: loc.tz,
        country: loc.country || "", admin1: loc.admin1 || "",
      });
      if (state.pending !== token) return;  // a newer place was picked meanwhile
      state.data = data;
      fillDaySelect();
      restoreEvent();
      render(data);
    } catch (e) {
      if (state.pending !== token) return;
      setStatus("Couldn't load that forecast (" + e.message + ").", true);
    }
  }

  /* ---------- render ---------- */

  function render(data) {
    const days = $("#days");
    days.textContent = "";
    const tpl = $("#day-tpl");
    const okCount = data.sources.filter(s => s.ok).length;
    const ev = state.event;
    let deepLinked = null;
    const wantOpen = (readHash() || {}).openDate;

    data.days.forEach(date => {
      const s = data.summary[date];
      if (!s) return;
      const node = tpl.content.firstElementChild.cloneNode(true);
      const dt = parseDay(date);
      const isKey = !!ev && ev.date === date;

      if (isKey) {
        node.classList.add("is-key");
        const tag = document.createElement("span");
        tag.className = "wx-key-tag";
        tag.textContent = ev.label || "Your event";
        $(".wx-when", node).appendChild(tag);
      }

      $(".wx-dow", node).textContent = DOW[dt.getDay()];
      $(".wx-date", node).textContent = fmtDay(date);
      $(".wx-hi", node).textContent = temp(s.hi);
      $(".wx-lo", node).textContent = temp(s.lo);
      $(".wx-cond", node).textContent = s.cond || "";
      $(".wx-hum", node).textContent = s.hum == null ? "—" : s.hum + "%";
      $(".wx-wind", node).textContent = s.wind == null ? "—" : speed(s.wind) + (s.wdir ? " " + s.wdir : "");

      const popEl = $(".wx-pop-num", node);
      popEl.textContent = s.pop == null ? "—" : s.pop + "%";
      popEl.classList.add(s.pop == null ? "wx-na" : s.pop <= 20 ? "wx-good" : s.pop <= 45 ? "wx-watch" : "wx-concern");

      const band = $(".wx-band", node), fill = $(".wx-fill", node), tick = $(".wx-tick", node);
      if (s.pop == null) {
        $(".wx-bar", node).classList.add("wx-bar-empty");
      } else {
        if (s.pop_min != null && s.pop_max != null) {
          band.style.left = s.pop_min + "%";
          band.style.width = Math.max(1, s.pop_max - s.pop_min) + "%";
        }
        tick.style.left = s.pop + "%";
        requestAnimationFrame(() => { fill.style.width = s.pop + "%"; });
      }

      const meta = $(".wx-meta", node);
      if (s.pop_n) {
        const weighted = s.pop_plain != null && s.weights && Object.keys(s.weights).length > 1;
        meta.textContent = `Average of ${s.pop_n} source${s.pop_n === 1 ? "" : "s"}`
          + (weighted && s.pop_plain !== s.pop ? ` (weighted; a flat average would be ${s.pop_plain}%)` : "")
          + (s.pop_min != null ? ` · they range ${s.pop_min}–${s.pop_max}%` : "");
      } else {
        meta.textContent = "No source publishes a rain chance this far out yet.";
      }

      /* ---- verdict, only for the planned day ---- */
      if (isKey) {
        const win = s.hourly.filter(h => h.h >= ev.from && h.h <= ev.to && h.pop != null);
        // Rain in the couple of hours before matters as much as rain during:
        // guests arrive wet either way. Read the lead-in, then the window.
        const lead = s.hourly.filter(h => h.h >= Math.max(0, ev.from - 2) && h.h <= ev.to && h.pop != null);
        const hourlyBasis = win.length > 0;
        const f = hourlyBasis
          ? { rain: Math.max(...lead.map(h => h.pop)), wind: avgOf(win, "wind") ?? s.wind,
              hum: avgOf(win, "hum") ?? s.hum, temp: avgOf(win, "temp") ?? s.hi }
          : { rain: s.pop, wind: s.wind, hum: s.hum, temp: s.hi };
        const g = { rain: grade(f.rain, RULES.rain), wind: grade(f.wind, RULES.wind),
                    hum: grade(f.hum, RULES.hum), temp: gradeTemp(f.temp) };
        const names = { rain: "rain", wind: "wind", hum: "humidity", temp: "temperature" };
        const concerns = Object.keys(g).filter(k => g[k] === "concern").map(k => names[k]);
        const watches = Object.keys(g).filter(k => g[k] === "watch").map(k => names[k]);
        const known = Object.values(g).filter(Boolean).length;

        let tone = "good", text = "Looking good.";
        if (!known) { tone = "na"; text = "Not enough detail this far out to call it."; }
        else if (concerns.length) { tone = "concern"; text = "Watch the " + concerns.join(" and ") + "."; }
        else if (watches.length) { tone = "watch"; text = "Mostly fine — keep an eye on the " + watches.join(" and ") + "."; }

        const v = $(".wx-verdict", node);
        v.hidden = false;
        v.classList.add("wx-" + tone);
        $(".wx-verdict-label", node).textContent = (ev.label || "Your window") + ", "
          + hourLabel(ev.from) + "–" + hourLabel(ev.to) + " — ";
        $(".wx-verdict-text", node).textContent = text;

        const ul = $(".wx-factors", v);
        [["rain", f.rain == null ? null : f.rain + "%", "rain"],
         ["wind", f.wind == null ? null : speed(f.wind), "wind"],
         ["hum", f.hum == null ? null : f.hum + "%", "humidity"],
         ["temp", f.temp == null ? null : temp(f.temp), "temp"]].forEach(([k, val, label]) => {
          if (val == null) return;
          const li = document.createElement("li");
          li.className = "wx-factor wx-" + (g[k] || "na");
          const a = document.createElement("span"); a.className = "wx-factor-label"; a.textContent = label;
          const b = document.createElement("span"); b.className = "wx-factor-val"; b.textContent = val;
          li.append(a, b);
          ul.appendChild(li);
        });

        if (s.sun && s.sun.sunset) {
          const sunEl = $(".wx-sun", v);
          sunEl.hidden = false;
          sunEl.textContent = "Sunrise " + fmtClock(s.sun.sunrise) + " · sunset " + fmtClock(s.sun.sunset)
            + (s.sun.golden_start ? " · golden hour from " + fmtClock(s.sun.golden_start) : "");
        }
        const basis = document.createElement("p");
        basis.className = "wx-verdict-basis";
        basis.textContent = hourlyBasis
          ? "Read from the hourly forecasts across that window."
          : "No hourly detail this far out — read from the whole-day figures.";
        v.appendChild(basis);
      }

      /* ---- hourly strip ---- */
      const hours = $(".wx-hours", node);
      s.hourly.forEach(h => {
        const b = document.createElement("div");
        const inWindow = isKey && h.h >= ev.from && h.h <= ev.to;
        b.className = "wx-hour" + (inWindow ? " wx-in-window" : "") + (h.pop == null ? " wx-hour-na" : "");
        b.style.setProperty("--pop", (h.pop == null ? 0 : h.pop) + "%");
        b.title = hourLabel(h.h) + (h.pop == null ? " — no data" : ` — ${h.pop}% rain`)
          + (h.temp == null ? "" : `, ${temp(h.temp)}`);
        if (isKey && (h.h === ev.from || h.h === ev.to)) {
          const l = document.createElement("span");
          l.className = "wx-hour-edge";
          b.appendChild(l);
        }
        hours.appendChild(b);
      });
      if (isKey) {
        const cap = document.createElement("p");
        cap.className = "wx-window-cap";
        const sw = document.createElement("span");
        sw.className = "wx-swatch";
        cap.appendChild(sw);
        cap.appendChild(document.createTextNode(
          (ev.label || "Your event") + ", " + hourLabel(ev.from) + "–" + hourLabel(ev.to)));
        $(".wx-hour-axis", node).after(cap);
      }
      if (!s.hourly_available) {
        const p = document.createElement("p");
        p.className = "wx-meta";
        p.textContent = "No source has published hourly detail this far out yet.";
        hours.replaceWith(p);
        const axis = $(".wx-hour-axis", node);
        if (axis) axis.remove();
      }

      /* ---- per-source breakdown ---- */
      const ul = $(".wx-per-source", node);
      data.sources.forEach(src => {
        const li = document.createElement("li");
        const d = src.daily && src.daily[date];
        const n = document.createElement("span");
        n.className = "wx-src-name";
        // Built as nodes, not markup: every other field on this page goes
        // through textContent, and this one line used to be the exception --
        // src.url and src.name interpolated raw into innerHTML.
        n.appendChild(document.createTextNode(src.name));
        const w = s.weights && s.weights[src.id];
        if (w != null && w !== 1) {
          const ws = document.createElement("span");
          ws.className = "wx-w";
          ws.title = "weight in the headline average";
          ws.textContent = "×" + w;
          n.appendChild(ws);
        }
        if (src.note) {
          const small = document.createElement("small");
          small.textContent = src.note;
          n.appendChild(small);
        }
        if (src.ok && d && (d.hum != null || d.wind != null)) {
          const vit = document.createElement("span");
          vit.className = "wx-vit";
          vit.textContent = (d.hum != null ? d.hum + "% humidity" : "")
            + (d.hum != null && d.wind != null ? " · " : "")
            + (d.wind != null ? speed(d.wind) + (d.wdir ? " " + d.wdir : "") : "");
          n.appendChild(vit);
        }
        const t = document.createElement("span"); t.className = "wx-src-t";
        const p = document.createElement("span"); p.className = "wx-src-p";
        if (!src.ok) { li.className = "wx-off"; p.textContent = "unavailable here"; p.classList.add("wx-na"); }
        else if (!d) { li.className = "wx-off"; p.textContent = "not this far out"; p.classList.add("wx-na"); }
        else {
          t.textContent = d.hi != null ? temp(d.hi) + " / " + temp(d.lo) : "";
          if (d.pop == null) { p.textContent = "no rain %"; p.classList.add("wx-na"); }
          else p.textContent = d.pop + "%";
        }
        li.append(n, t, p);
        ul.appendChild(li);
      });

      /* ---- expand / collapse ---- */
      const detail = $(".wx-detail", node);
      const toggle = (scroll) => {
        const open = detail.hidden;
        detail.hidden = !open;
        node.setAttribute("aria-expanded", String(open));
        writeHash(open ? date : null);
        if (open && scroll !== false) {
          requestAnimationFrame(() => node.scrollIntoView({ block: "nearest", behavior: "smooth" }));
        }
      };
      // The element id is "d2026-09-06" while the shareable hash segment is
      // the bare date, so the browser's own anchor jump never fires -- the day
      // opened where it was and stayed off-screen on a phone. Scrolled to once
      // by hand, after every day has been appended.
      node.id = "d" + date;
      if (wantOpen === date) { toggle(false); deepLinked = node; }
      node.addEventListener("click", e => { if (!detail.contains(e.target)) toggle(); });
      node.addEventListener("keydown", e => {
        if ((e.key === "Enter" || e.key === " ") && !detail.contains(e.target)) { e.preventDefault(); toggle(); }
      });
      days.appendChild(node);
    });

    if (deepLinked) requestAnimationFrame(() => deepLinked.scrollIntoView({ block: "start" }));

    /* ---- source roll-call ---- */
    const ul = $("#sources");
    ul.textContent = "";
    data.sources.forEach(src => {
      const li = document.createElement("li");
      if (!src.ok) li.className = "wx-off";
      const a = document.createElement("a");
      a.href = src.url || "#";
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = src.name;
      li.appendChild(a);
      const small = document.createElement("small");
      const through = coverageEnd(src.coverage);
      if (!src.ok) small.textContent = "not available for this location";
      else if (through) small.textContent = "through " + fmtDay(through);
      if (small.textContent) li.appendChild(small);
      ul.appendChild(li);
    });
    $("#method").classList.remove("hidden");
    $("#method-copy").textContent =
      `For ${data.location.label}, each source below is asked for its own forecast and the daily `
      + `chance of rain is averaged across whichever of them reach that day. Tap a day for the `
      + `hour-by-hour picture and every source's own number.`;

    const cacheNote = data.cache && data.cache.age_s > 90
      ? ` · collected ${Math.round(data.cache.age_s / 60)} min ago`
      : "";
    setStatus(`${okCount} of ${data.sources.length} sources reporting · updated ${fmtTime(data.generated_at)}${cacheNote}`);
  }

  /* ---------- boot ---------- */

  fillHourSelects();
  $("#plan-from").value = "16";
  $("#plan-to").value = "18";
  setUnits(state.units);
  renderRecents();
  // Not awaited: a forecast must never wait on the account check. If a place
  // is restored before this resolves, initAccount() saves it once it knows who
  // is signed in.
  initAccount();

  const fromHash = readHash();
  if (fromHash) {
    // The name in the hash is only a readability nicety; the reverse lookup is
    // authoritative and gives the tz, which the forecast genuinely needs.
    api("/api/reverse", { lat: fromHash.lat, lon: fromHash.lon })
      .then(select)
      .catch(() => select({ name: fromHash.name || "That spot", lat: fromHash.lat, lon: fromHash.lon,
                            tz: "UTC", label: fromHash.name || "That spot" }));
  } else {
    const last = recents()[0];
    if (last) select(last);
  }
})();
