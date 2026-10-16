#!/usr/bin/env python3
"""weather.slippylabs.com — HTTP front for the multi-source collector.

The static page is served by nginx; this only answers /api/. Collecting a
cold location means nine upstream conversations and takes several seconds, so
everything here is built around not doing that twice:

  fresh  (< TTL)        -> serve from disk
  stale  (< STALE_MAX)  -> serve from disk *and* refresh behind the response
  cold                  -> collect synchronously, one caller at a time

The per-key lock matters as much as the cache: without it, two people opening
the same city at once each start their own nine-source collection.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict

from flask import Flask, jsonify, request

import collect as c

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get("WEATHER_CACHE_DIR", os.path.join(APP_DIR, "cache"))

TTL = 30 * 60           # a forecast younger than this is served as-is
STALE_MAX = 6 * 60 * 60  # older than this and we make the caller wait
MAX_ENTRIES = 400        # cache directory cap, oldest evicted first

# Cold collections per IP. Warm hits are unmetered — the point is purely to
# stop one client driving the AccuWeather scrape in a loop.
COLD_PER_IP = 12
COLD_WINDOW = 300

# Collections running at once, across every location and both the synchronous
# and background paths. Each one is a 9-thread pool of upstream HTTP waits;
# without a cap, many distinct new locations at once meant unbounded threads
# and memory. Callers over the cap get the stale copy, a 202 "pending" (/api/now)
# or a 503 with Retry-After (/api/forecast) -- never a queue.
COLD_MAX = 3
_cold_slots = threading.BoundedSemaphore(COLD_MAX)

# Geocoder lookups: answers are cached (LRU + TTL) and only cache misses spend
# the per-IP budget, so a normal search session never notices it.
GEO_TTL = 24 * 60 * 60
GEO_MAX = 2000
GEO_PER_IP = 120          # uncached /api/geocode lookups per window
REV_PER_IP = 30           # uncached /api/reverse lookups per window
GEO_WINDOW = 300

NAME_MAX = 80
LABEL_MAX = 60

os.makedirs(CACHE_DIR, exist_ok=True)

app = Flask(__name__)

# Collector failures go to stderr, which systemd puts in the journal
# (journalctl -u weather). Under gunicorn, reuse its error handlers so the
# lines share its format; standalone (tests, app.py) fall back to basicConfig.
log = logging.getLogger("weather")
_gerr = logging.getLogger("gunicorn.error")
if _gerr.handlers:
    log.handlers = _gerr.handlers
    log.setLevel(logging.INFO)
    log.propagate = False
else:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# slippylabs.com's home page reads /api/now for its signed-in status strip, so
# this API is now called from a different origin. The rule is the same suffix
# check admin-console uses for /whoami: any https origin whose host is
# slippylabs.com or ends in .slippylabs.com. Suffix matching is safe as written
# -- "weather.slippylabs.com.evil.com" does not end in ".slippylabs.com", and
# neither does "evilslippylabs.com"; a host with a port matches neither.
#
# Credentials are deliberately NOT allowed. This endpoint serves public
# forecast data and knows nothing about accounts, so there is no reason to let
# a page send cookies to it.
def _cors_origin_allowed(origin):
    origin = (origin or "").rstrip("/").lower()
    if not origin.startswith("https://"):
        return False
    host = origin[len("https://"):]
    if "/" in host or "@" in host:
        return False
    return host == "slippylabs.com" or host.endswith(".slippylabs.com")


@app.after_request
def _add_cors(resp):
    origin = request.headers.get("Origin", "")
    if _cors_origin_allowed(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Accept, Content-Type"
        resp.headers["Access-Control-Max-Age"] = "600"
    # Always, even when the origin was refused: caches key on this header, and
    # omitting it lets one origin's answer be replayed to another.
    resp.headers["Vary"] = "Origin"
    return resp

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_refreshing: set[str] = set()
_ip_hits: OrderedDict[str, list[float]] = OrderedDict()
_ip_guard = threading.Lock()


def _lock_for(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


# Rounding to 2dp is ~1.1 km, far finer than any of these models resolve, so
# it collapses the near-identical requests two people in one town produce
# without ever merging genuinely different places.
def _key(lat, lon):
    return f"{round(lat, 2):+07.2f}_{round(lon, 2):+08.2f}".replace("+", "p").replace("-", "m")


def _path(key):
    return os.path.join(CACHE_DIR, f"{key}.json")


def _read(key):
    try:
        with open(_path(key)) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 — a corrupt or missing entry is just a miss
        return None


def _write(key, payload):
    tmp = _path(key) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, _path(key))
    _evict()


def _evict():
    try:
        files = [os.path.join(CACHE_DIR, n) for n in os.listdir(CACHE_DIR) if n.endswith(".json")]
        if len(files) <= MAX_ENTRIES:
            return
        for path in sorted(files, key=os.path.getmtime)[:len(files) - MAX_ENTRIES]:
            os.unlink(path)
    except OSError:
        pass


def _age(payload):
    return time.time() - payload.get("cached_at", 0)


def _trusted(payload):
    """True when the entry's tz was derived by this server, not by a caller.

    Entries written before 2026-10-16 carry whatever tz their first visitor
    sent; they are served but treated as due a refresh, which re-derives it.
    """
    return ((payload or {}).get("location") or {}).get("tz_source") == "server"


def _resolve_tz(lat, lon, prev, client_tz):
    """(tz, source) for a collection. The server's own lookup wins; a cached
    server-derived tz is reused so a refresh costs no extra call; a caller's tz
    is only a fallback when the lookup fails, and only if it is a real zone."""
    if _trusted(prev):
        return prev["location"]["tz"], "server"
    try:
        return c.lookup_tz(lat, lon), "server"
    except Exception as e:  # noqa: BLE001
        log.warning("tz lookup failed for %.2f,%.2f: %s: %s", lat, lon, type(e).__name__, e)
    if client_tz and _tz_ok(client_tz):
        return client_tz, "client"
    return "UTC", "default"


def _collect_into_cache(lat, lon, key, client_tz=None, prev=None):
    """Collect one ~1 km cell. What goes in the shared cache is the forecast for
    the cell's centre and nothing a visitor supplied: no name, no country, and a
    tz the server derived. Labels are attached per request in _served()."""
    clat, clon = round(lat, 2), round(lon, 2)
    tz, tz_source = _resolve_tz(clat, clon, prev, client_tz)
    loc = c.Loc(name=f"{clat:.2f}, {clon:.2f}", lat=clat, lon=clon, tz=tz)
    t0 = time.time()
    payload = c.collect(loc)
    srcs = payload.get("sources") or []
    bad = [x["id"] for x in srcs if not x.get("ok")]
    log.info("collected %s (tz %s/%s) in %.1fs: %d/%d sources ok%s", key, tz, tz_source,
             time.time() - t0, len(srcs) - len(bad), len(srcs),
             f", failed: {','.join(bad)}" if bad else "")
    payload["location"] = {"lat": clat, "lon": clon, "tz": tz, "tz_source": tz_source}
    payload["cached_at"] = time.time()
    _write(key, payload)
    return payload


def _refresh_async(lat, lon, key, client_tz=None, prev=None):
    """Warm the entry behind an already-sent response. Returns False when it
    could not start (already refreshing, or every collection slot is busy)."""
    with _locks_guard:
        if key in _refreshing:
            return True
        if not _cold_slots.acquire(blocking=False):
            return False
        _refreshing.add(key)

    def run():
        try:
            with _lock_for(key):
                _collect_into_cache(lat, lon, key, client_tz, prev)
        except Exception:  # noqa: BLE001 — the stale copy already went out
            log.exception("background refresh failed for %s", key)
        finally:
            _cold_slots.release()
            with _locks_guard:
                _refreshing.discard(key)

    threading.Thread(target=run, daemon=True).start()
    return True


_LOOPBACK = {"127.0.0.1", "::1"}


def _client_ip():
    """The visitor's address, as nginx determined it.

    nginx resolves the real client from CF-Connecting-IP (only when the peer is
    a Cloudflare edge, conf.d/cloudflare-realip.conf) and *overwrites*
    X-Real-IP with it, so that header cannot be forged through the proxy.
    X-Forwarded-For is never read: its leftmost entry is whatever the client
    sent, which made the per-IP budget trivially bypassable. X-Real-IP is only
    honoured when the peer is loopback (i.e. nginx); anything else uses the
    socket address.
    """
    peer = request.remote_addr or "?"
    if peer in _LOOPBACK:
        real = (request.headers.get("X-Real-IP") or "").strip()
        if real:
            return real[:64]
    return peer


def _budget_ok(table, limit, window):
    """Sliding-window per-IP counter; records a hit when it says yes."""
    now = time.time()
    ip = _client_ip()
    with _ip_guard:
        hits = [t for t in table.get(ip, []) if now - t < window]
        if len(hits) >= limit:
            table[ip] = hits
            return False
        hits.append(now)
        table[ip] = hits
        table.move_to_end(ip)
        while len(table) > 4096:
            table.popitem(last=False)
    return True


def _cold_budget_ok():
    return _budget_ok(_ip_hits, COLD_PER_IP, COLD_WINDOW)


_geo_hits: OrderedDict[str, list[float]] = OrderedDict()
_rev_hits: OrderedDict[str, list[float]] = OrderedDict()


class _TTLCache:
    """Tiny thread-safe LRU with a TTL, for geocoder answers."""

    def __init__(self, maxsize, ttl):
        self.maxsize, self.ttl = maxsize, ttl
        self._d: OrderedDict = OrderedDict()
        self._g = threading.Lock()

    def get(self, key):
        with self._g:
            hit = self._d.get(key)
            if hit is None:
                return None
            if time.time() - hit[0] > self.ttl:
                del self._d[key]
                return None
            self._d.move_to_end(key)
            return hit[1]

    def put(self, key, value):
        with self._g:
            self._d[key] = (time.time(), value)
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)


_geo_cache = _TTLCache(GEO_MAX, GEO_TTL)
_rev_cache = _TTLCache(GEO_MAX, GEO_TTL)


def _float(name, lo, hi):
    raw = request.args.get(name)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return v


_TZ_OK = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}$")


@app.get("/api/geocode")
def api_geocode():
    q = (request.args.get("q") or "").strip()[:120]
    if len(q) < 2:
        return jsonify({"results": []})
    ck = " ".join(q.lower().split())
    cached = _geo_cache.get(ck)
    if cached is not None:
        return jsonify({"results": cached})
    if not _budget_ok(_geo_hits, GEO_PER_IP, GEO_WINDOW):
        return jsonify({"error": "Too many searches from this address just now. "
                                 "Give it a few minutes."}), 429
    try:
        hits = c.geocode(q, count=8)
    except Exception as e:  # noqa: BLE001
        log.warning("geocode failed for %r: %s: %s", q, type(e).__name__, e)
        return jsonify({"error": f"lookup failed: {type(e).__name__}"}), 502
    results = [h.as_dict() for h in hits]
    _geo_cache.put(ck, results)
    return jsonify({"results": results})


@app.get("/api/reverse")
def api_reverse():
    try:
        lat, lon = _float("lat", -90, 90), _float("lon", -180, 180)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # ~110 m cells: the name and tz cannot differ meaningfully inside one, and
    # the answer is re-stamped with the caller's own coordinates below.
    ck = (round(lat, 3), round(lon, 3))
    got = _rev_cache.get(ck)
    if got is None:
        if not _budget_ok(_rev_hits, REV_PER_IP, GEO_WINDOW):
            return jsonify({"error": "Too many location lookups from this address just now. "
                                     "Give it a few minutes."}), 429
    try:
        if got is None:
            got = c.reverse_geocode(lat, lon)
            _rev_cache.put(ck, got)
        return jsonify(c.Loc(name=got.name, lat=round(lat, 4), lon=round(lon, 4), tz=got.tz,
                             country=got.country, admin1=got.admin1).as_dict())
    except Exception as e:  # noqa: BLE001
        log.warning("reverse geocode failed for %.2f,%.2f: %s: %s", lat, lon, type(e).__name__, e)
        return jsonify({"error": f"lookup failed: {type(e).__name__}"}), 502


def _clean(raw, limit):
    """A display label from the query string: control/format characters
    dropped, whitespace collapsed, length-capped. The page renders these with
    textContent, so this is about keeping them sane, not about HTML."""
    if not raw:
        return None
    s = "".join(ch for ch in str(raw) if not unicodedata.category(ch).startswith("C"))
    s = " ".join(s.split())[:limit].strip()
    return s or None


def _labels(lat, lon):
    """Per-request naming. Never stored: a cell is shared by everyone in it, so
    whatever one visitor calls it must not become everyone else's label."""
    name = _clean(request.args.get("name"), NAME_MAX) or f"{lat:.3f}, {lon:.3f}"
    country = _clean(request.args.get("country"), LABEL_MAX)
    admin1 = _clean(request.args.get("admin1"), LABEL_MAX)
    return {"name": name, "country": country, "admin1": admin1,
            "label": ", ".join(x for x in (name, admin1, country) if x)}


def _client_tz():
    tz = (request.args.get("tz") or "").strip()[:64]
    return tz if tz and _TZ_OK.match(tz) and _tz_ok(tz) else None


def _payload_tz(payload):
    tz = ((payload or {}).get("location") or {}).get("tz") or "UTC"
    return tz if _tz_ok(tz) else "UTC"


@app.get("/api/forecast")
def api_forecast():
    try:
        lat, lon = _float("lat", -90, 90), _float("lon", -180, 180)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    labels = _labels(lat, lon)
    client_tz = _client_tz()
    key = _key(lat, lon)

    cached = _read(key)
    if cached:
        age = _age(cached)
        if age < TTL and _trusted(cached):
            return _served(cached, "fresh", labels)
        if age < STALE_MAX:
            _refresh_async(lat, lon, key, client_tz, cached)
            return _served(cached, "stale", labels)

    if not _cold_budget_ok():
        if cached:
            return _served(cached, "stale", labels)
        return jsonify({"error": "Too many new locations from this address just now. "
                                 "Give it a few minutes."}), 429

    lock = _lock_for(key)
    with lock:
        # Someone else may have collected this exact place while we waited.
        again = _read(key)
        if again and _age(again) < TTL and _trusted(again):
            return _served(again, "fresh", labels)
        if not _cold_slots.acquire(blocking=False):
            if again or cached:
                return _served(again or cached, "stale", labels)
            resp = jsonify({"error": "The forecast service is busy collecting other "
                                     "places. Try again in a few seconds."})
            resp.headers["Retry-After"] = "10"
            return resp, 503
        try:
            return _served(_collect_into_cache(lat, lon, key, client_tz, again or cached),
                           "cold", labels)
        except Exception as e:  # noqa: BLE001
            log.exception("cold collection failed for %s", key)
            if cached:
                return _served(cached, "stale", labels)
            return jsonify({"error": f"collection failed: {type(e).__name__}"}), 502
        finally:
            _cold_slots.release()


def _served(payload, state, labels):
    body = dict(payload)
    stored = payload.get("location") or {}
    body["location"] = {"lat": stored.get("lat"), "lon": stored.get("lon"),
                        "tz": _payload_tz(payload), **labels}
    body["cache"] = {"state": state, "age_s": int(_age(payload))}
    resp = jsonify(body)
    # The browser must not hold its own copy: this endpoint's whole job is to
    # decide freshness, and a cached response would hide a refresh that landed.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/now", methods=["GET", "OPTIONS"])
def api_now():
    """The one-line version of /api/forecast, for the home-page strip.

    /api/forecast returns all nine sources with their full hourly arrays --
    hundreds of KB, and a cold location blocks for ~10s while nine upstreams
    are fetched. Neither is acceptable on a page the user did not open for the
    weather, so this projects the same cached payload down to what a strip can
    show and, crucially, NEVER blocks: an uncached location starts the
    collection in the background and answers 202 straight away, leaving the
    caller to retry. It shares the cache, the lock and the collector with
    /api/forecast, so it adds no upstream traffic of its own.
    """
    if request.method == "OPTIONS":
        return app.make_response("")
    try:
        lat, lon = _float("lat", -90, 90), _float("lon", -180, 180)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    labels = _labels(lat, lon)
    client_tz = _client_tz()
    key = _key(lat, lon)

    cached = _read(key)
    if cached is None or _age(cached) >= STALE_MAX:
        # Nothing usable. Start the work (if this IP has budget and a slot is
        # free), tell the caller to come back either way.
        if _cold_budget_ok():
            _refresh_async(lat, lon, key, client_tz, cached)
        resp = jsonify({"pending": True, "name": labels["name"]})
        resp.headers["Cache-Control"] = "no-store"
        return resp, 202

    state = "fresh"
    if _age(cached) >= TTL or not _trusted(cached):
        state = "stale"
        _refresh_async(lat, lon, key, client_tz, cached)

    body = _now_from(cached, labels["name"])
    body["cache"] = {"state": state, "age_s": int(_age(cached))}
    resp = jsonify(body)
    # Same reasoning as /api/forecast: freshness is decided here, and a browser
    # copy would hide a refresh that has already landed.
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _now_from(payload, name):
    """Project a full cached forecast down to "what is it doing right now".

    Temperature comes from the current hour of the consensus hourly array, not
    from the daily high -- at 6am the day's high is not what it feels like
    outside. The condition phrase is the day's headline, which is the finest
    granularity any source gives us: only temperature and precipitation
    probability are hourly, so there is no such thing as an hourly condition
    here, and claiming one would be inventing data.
    """
    summary = payload.get("summary") or {}
    # The entry's own (server-derived) tz decides which local day and hour it
    # is; a caller's tz is never consulted here.
    now = c.datetime.now(c.ZoneInfo(_payload_tz(payload)))
    today = now.strftime("%Y-%m-%d")
    day = summary.get(today)
    if day is None:
        # Past midnight in a timezone whose forecast was collected yesterday.
        future = sorted(k for k in summary if k >= today)
        today = future[0] if future else (sorted(summary)[-1] if summary else None)
        day = summary.get(today) if today else None
    if day is None:
        return {"pending": True, "name": name}

    temp = None
    hourly = day.get("hourly") or []
    for entry in hourly:
        if entry.get("h") == now.hour:
            temp = entry.get("temp")
            break
    if temp is None:                       # no hourly consensus for this hour
        temp = day.get("hi")

    is_day = _is_daylight(now, day.get("sun") or {})
    cond = day.get("cond")
    return {
        "name": name,
        "temp": temp,
        "cond": cond,
        "icon": c.cond_icon(cond, is_day),
        "is_day": is_day,
        "hi": day.get("hi"),
        "lo": day.get("lo"),
        "pop": day.get("pop"),
        "date": today,
        "as_of": now.strftime("%Y-%m-%dT%H:%M"),
        "generated_at": payload.get("generated_at"),
    }


def _tz_ok(tz):
    try:
        c.ZoneInfo(tz)
        return True
    except Exception:  # noqa: BLE001 — an unknown zone just means fall back to UTC
        return False


def _is_daylight(now, sun):
    """Between sunrise and sunset in the location's own local time.

    Both are "HH:MM" strings already converted to that timezone by
    collect.sun_times(). A polar day or night leaves one of them None, in which
    case fall back to a plain hour test rather than guessing.
    """
    rise, set_ = sun.get("sunrise"), sun.get("sunset")
    if not rise or not set_:
        return 6 <= now.hour < 20
    return rise <= now.strftime("%H:%M") < set_


@app.get("/api/health")
def api_health():
    n = len([x for x in os.listdir(CACHE_DIR) if x.endswith(".json")])
    return jsonify({"ok": True, "cached_locations": n})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8028, debug=True)
