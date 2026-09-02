"""/api/now — the projection the home-page strip reads.

Built on a synthetic cache entry rather than a live one so the assertions are
deterministic: the point is the projection logic (which hour, which day, day vs
night, what happens when data is missing), not the forecast itself.
"""
import datetime as dt
import importlib, json, os, sys, time
import pytest


def _utc_now():
    """The query below asks for tz=UTC, so every expectation in this file has
    to be built in UTC too -- the endpoint reads the hour in the LOCATION's
    timezone, not the server's."""
    return dt.datetime.now(dt.timezone.utc)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _payload(cond="Sunny", sunrise="06:30", sunset="19:45", hours=None, name="Testville"):
    hourly = [{"h": h, "pop": 10, "n": 5, "temp": (hours or {}).get(h, 60 + h),
               "hum": 50, "wind": 5} for h in range(24)]
    today = _utc_now().strftime("%Y-%m-%d")
    return {
        "generated_at": "2026-09-01T12:00:00+00:00",
        "location": {"name": name, "lat": 40.35, "lon": -74.06, "tz": "UTC"},
        "days": [today],
        "sources": [],
        "summary": {today: {"pop": 42, "hi": 85, "lo": 61, "cond": cond,
                            "hourly": hourly, "hourly_available": True,
                            "sun": {"sunrise": sunrise, "sunset": sunset}}},
        "cached_at": time.time(),
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WEATHER_CACHE_DIR", str(tmp_path))
    for m in ("app",):
        sys.modules.pop(m, None)
    app = importlib.import_module("app")
    importlib.reload(app)
    app.app.config["TESTING"] = True
    c = app.app.test_client()
    c._app = app
    c._dir = tmp_path
    return c


def _seed(client, payload, lat=40.35, lon=-74.06):
    key = client._app._key(lat, lon)
    (client._dir / f"{key}.json").write_text(json.dumps(payload))


Q = "/api/now?lat=40.35&lon=-74.06&tz=UTC&name=Testville"


def test_uncached_returns_202_without_blocking(client, monkeypatch):
    """A home page must never wait ~10s for nine upstream forecast services."""
    monkeypatch.setattr(client._app, "_refresh_async", lambda *a, **k: None)
    t0 = time.time()
    r = client.get(Q)
    assert r.status_code == 202
    assert r.get_json()["pending"] is True
    assert time.time() - t0 < 1.0


def test_cached_projection(client):
    hour = _utc_now().hour
    _seed(client, _payload(hours={hour: 71}))
    d = client.get(Q).get_json()
    assert d["temp"] == 71, "temperature must come from the CURRENT hour, not the daily high"
    assert d["hi"] == 85 and d["lo"] == 61 and d["pop"] == 42
    assert d["cache"]["state"] == "fresh"
    assert d["name"] == "Testville"


def test_temperature_is_hourly_not_the_daily_high(client):
    hour = _utc_now().hour
    _seed(client, _payload(hours={hour: 48}))
    assert client.get(Q).get_json()["temp"] == 48


def test_falls_back_to_the_high_when_the_hour_has_no_consensus(client):
    p = _payload()
    for e in p["summary"][_utc_now().strftime("%Y-%m-%d")]["hourly"]:
        e["temp"] = None
    _seed(client, p)
    assert client.get(Q).get_json()["temp"] == 85


def test_day_and_night_icons(client):
    """Sunrise/sunset come from the payload, so they can be moved for the test."""
    _seed(client, _payload(cond="Sunny", sunrise="00:00", sunset="23:59"))
    d = client.get(Q).get_json()
    assert d["is_day"] is True and d["icon"] == "☀️"

    _seed(client, _payload(cond="Sunny", sunrise="23:58", sunset="23:59"))
    d = client.get(Q).get_json()
    assert d["is_day"] is False and d["icon"] == "🌙"


def test_polar_day_without_sun_times_still_answers(client):
    _seed(client, _payload(sunrise=None, sunset=None))
    d = client.get(Q).get_json()
    assert isinstance(d["is_day"], bool)
    assert d["icon"] != "🌡️"


def test_stale_entry_is_served_and_refreshed_behind_the_response(client, monkeypatch):
    calls = []
    monkeypatch.setattr(client._app, "_refresh_async", lambda *a, **k: calls.append(1))
    p = _payload()
    p["cached_at"] = time.time() - (client._app.TTL + 60)
    _seed(client, p)
    d = client.get(Q).get_json()
    assert d["cache"]["state"] == "stale"
    assert d["temp"] is not None, "a stale answer is still an answer"
    assert calls, "a stale hit must kick off a refresh"


def test_expired_entry_is_not_served(client, monkeypatch):
    monkeypatch.setattr(client._app, "_refresh_async", lambda *a, **k: None)
    p = _payload()
    p["cached_at"] = time.time() - (client._app.STALE_MAX + 60)
    _seed(client, p)
    r = client.get(Q)
    assert r.status_code == 202 and r.get_json()["pending"] is True


def test_summary_missing_today_uses_the_next_day_it_has(client):
    """Past midnight in a zone whose forecast was collected yesterday."""
    p = _payload()
    day = list(p["summary"])[0]
    p["summary"] = {"2099-01-01": p["summary"][day]}
    _seed(client, p)
    d = client.get(Q).get_json()
    assert d.get("pending") is None and d["date"] == "2099-01-01"


def test_bad_coordinates_are_rejected(client):
    assert client.get("/api/now?lat=999&lon=0").status_code == 400
    assert client.get("/api/now?lon=0").status_code == 400


def test_unknown_timezone_falls_back_instead_of_erroring(client):
    _seed(client, _payload())
    r = client.get("/api/now?lat=40.35&lon=-74.06&tz=Not/AZone")
    assert r.status_code == 200 and r.get_json()["temp"] is not None


@pytest.mark.parametrize("origin,allowed", [
    ("https://slippylabs.com", True),
    ("https://calendar.slippylabs.com", True),
    ("https://weather.slippylabs.com", True),
    ("https://example.com", False),
    ("https://slippylabs.com.evil.com", False),
    ("https://evilslippylabs.com", False),
    ("http://slippylabs.com", False),
    ("https://slippylabs.com:8443", False),
])
def test_cors_origins(client, origin, allowed):
    _seed(client, _payload())
    r = client.get(Q, headers={"Origin": origin})
    got = r.headers.get("Access-Control-Allow-Origin")
    assert (got == origin) is allowed, f"{origin} -> {got!r}"
    # Vary must be set whatever the verdict, or a cache can replay one
    # origin's answer to another.
    assert r.headers.get("Vary") == "Origin"
    # This endpoint serves public data and must never accept cookies.
    assert r.headers.get("Access-Control-Allow-Credentials") is None


def test_cors_preflight(client):
    r = client.open(Q, method="OPTIONS", headers={"Origin": "https://slippylabs.com"})
    assert r.status_code == 200
    assert r.headers["Access-Control-Allow-Origin"] == "https://slippylabs.com"
    assert "GET" in r.headers["Access-Control-Allow-Methods"]


def test_forecast_endpoint_still_works(client):
    """The new after_request hook runs on every route, including the old one."""
    _seed(client, _payload())
    r = client.get("/api/forecast?lat=40.35&lon=-74.06&tz=UTC&name=Testville")
    assert r.status_code == 200
    assert "summary" in r.get_json()
