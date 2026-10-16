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


def _collect_into_cache(loc, key):
    t0 = time.time()
    payload = c.collect(loc)
    srcs = payload.get("sources") or []
    bad = [x["id"] for x in srcs if not x.get("ok")]
    log.info("collected %s (%s) in %.1fs: %d/%d sources ok%s", key, loc.name,
             time.time() - t0, len(srcs) - len(bad), len(srcs),
             f", failed: {','.join(bad)}" if bad else "")
    payload["cached_at"] = time.time()
    _write(key, payload)
    return payload


def _refresh_async(loc, key):
    """Warm the entry behind an already-sent stale response."""
    with _locks_guard:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def run():
        try:
            with _lock_for(key):
                _collect_into_cache(loc, key)
        except Exception:  # noqa: BLE001 — the stale copy already went out
            log.exception("background refresh failed for %s", key)
        finally:
            with _locks_guard:
                _refreshing.discard(key)

    threading.Thread(target=run, daemon=True).start()


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or request.remote_addr or "?"


def _cold_budget_ok():
    now = time.time()
    ip = _client_ip()
    with _ip_guard:
        hits = [t for t in _ip_hits.get(ip, []) if now - t < COLD_WINDOW]
        if len(hits) >= COLD_PER_IP:
            _ip_hits[ip] = hits
            return False
        hits.append(now)
        _ip_hits[ip] = hits
        _ip_hits.move_to_end(ip)
        while len(_ip_hits) > 2048:
            _ip_hits.popitem(last=False)
    return True


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
    try:
        hits = c.geocode(q, count=8)
    except Exception as e:  # noqa: BLE001
        log.warning("geocode failed for %r: %s: %s", q, type(e).__name__, e)
        return jsonify({"error": f"lookup failed: {type(e).__name__}"}), 502
    return jsonify({"results": [h.as_dict() for h in hits]})


@app.get("/api/reverse")
def api_reverse():
    try:
        lat, lon = _float("lat", -90, 90), _float("lon", -180, 180)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        return jsonify(c.reverse_geocode(lat, lon).as_dict())
    except Exception as e:  # noqa: BLE001
        log.warning("reverse geocode failed for %.2f,%.2f: %s: %s", lat, lon, type(e).__name__, e)
        return jsonify({"error": f"lookup failed: {type(e).__name__}"}), 502


@app.get("/api/forecast")
def api_forecast():
    try:
        lat, lon = _float("lat", -90, 90), _float("lon", -180, 180)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    tz = (request.args.get("tz") or "UTC").strip()
    if not _TZ_OK.match(tz):
        tz = "UTC"
    loc = c.Loc(name=(request.args.get("name") or f"{lat:.3f}, {lon:.3f}").strip()[:80],
                lat=lat, lon=lon, tz=tz,
                country=(request.args.get("country") or None),
                admin1=(request.args.get("admin1") or None))
    key = _key(lat, lon)

    cached = _read(key)
    if cached:
        age = _age(cached)
        if age < TTL:
            return _served(cached, "fresh")
        if age < STALE_MAX:
            _refresh_async(loc, key)
            return _served(cached, "stale")

    if not _cold_budget_ok():
        if cached:
            return _served(cached, "stale")
        return jsonify({"error": "Too many new locations from this address just now. "
                                 "Give it a few minutes."}), 429

    lock = _lock_for(key)
    with lock:
        # Someone else may have collected this exact place while we waited.
        again = _read(key)
        if again and _age(again) < TTL:
            return _served(again, "fresh")
        try:
            return _served(_collect_into_cache(loc, key), "cold")
        except Exception as e:  # noqa: BLE001
            log.exception("cold collection failed for %s", key)
            if cached:
                return _served(cached, "stale")
            return jsonify({"error": f"collection failed: {type(e).__name__}"}), 502


def _served(payload, state):
    body = dict(payload)
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

    tz = (request.args.get("tz") or "UTC").strip()
    if not _TZ_OK.match(tz):
        tz = "UTC"
    loc = c.Loc(name=(request.args.get("name") or f"{lat:.3f}, {lon:.3f}").strip()[:80],
                lat=lat, lon=lon, tz=tz,
                country=(request.args.get("country") or None),
                admin1=(request.args.get("admin1") or None))
    key = _key(lat, lon)

    cached = _read(key)
    if cached is None or _age(cached) >= STALE_MAX:
        # Nothing usable. Start the work, tell the caller to come back.
        if _cold_budget_ok():
            _refresh_async(loc, key)
        resp = jsonify({"pending": True, "name": loc.name})
        resp.headers["Cache-Control"] = "no-store"
        return resp, 202

    state = "fresh"
    if _age(cached) >= TTL:
        state = "stale"
        _refresh_async(loc, key)

    body = _now_from(cached, loc)
    body["cache"] = {"state": state, "age_s": int(_age(cached))}
    resp = jsonify(body)
    # Same reasoning as /api/forecast: freshness is decided here, and a browser
    # copy would hide a refresh that has already landed.
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _now_from(payload, loc):
    """Project a full cached forecast down to "what is it doing right now".

    Temperature comes from the current hour of the consensus hourly array, not
    from the daily high -- at 6am the day's high is not what it feels like
    outside. The condition phrase is the day's headline, which is the finest
    granularity any source gives us: only temperature and precipitation
    probability are hourly, so there is no such thing as an hourly condition
    here, and claiming one would be inventing data.
    """
    summary = payload.get("summary") or {}
    now = c.datetime.now(c.ZoneInfo(loc.tz)) if _tz_ok(loc.tz) else c.datetime.now(c.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    day = summary.get(today)
    if day is None:
        # Past midnight in a timezone whose forecast was collected yesterday.
        future = sorted(k for k in summary if k >= today)
        today = future[0] if future else (sorted(summary)[-1] if summary else None)
        day = summary.get(today) if today else None
    if day is None:
        return {"pending": True, "name": loc.name}

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
        "name": (payload.get("location") or {}).get("name") or loc.name,
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
