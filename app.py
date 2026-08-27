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
    payload = c.collect(loc)
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
            pass
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


@app.get("/api/health")
def api_health():
    n = len([x for x in os.listdir(CACHE_DIR) if x.endswith(".json")])
    return jsonify({"ok": True, "cached_locations": n})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8028, debug=True)
