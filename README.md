# Weather Outlook

Consensus weather forecast for **anywhere on Earth**, live at
<https://weather.slippylabs.com>.

Nine forecast sources are queried for the same point, normalised to one schema
and averaged into a single daily chance of rain with a skill-based weight. The
page shows the average *and* the spread between the sources, because the
spread is the part that tells you whether the number means anything: a short
band means they agree, a wide one means nobody knows yet.

Optionally, pick a day and a window of hours — "the ceremony", "kickoff" — and
that day gets read against the window instead of the whole day, with a verdict
and a per-factor breakdown of rain, wind, humidity and temperature.

## Sources

| id | Source | How | Reach |
|---|---|---|---|
| twc | The Weather Channel (weather.com; same engine as Wunderground) | `api.weather.com` v3 with the public key embedded in wunderground.com | 15 days daily + hourly, global |
| accuweather | AccuWeather | coordinate resolved through `web-api/three-day-redirect`, then the server-rendered daily and hourly pages, fetched with `curl_cffi` (Chrome TLS fingerprint — plain curl gets an Akamai 403) | 15 days daily, 4 days hourly, global |
| ecmwf_ens / gefs | ECMWF ensemble (51 runs), GEFS ensemble (31 runs) | Open-Meteo ensemble API; rain % = share of members with ≥0.254 mm over the daytime window (hourly: ≥0.05 mm/h, because 6-hourly blocks are spread evenly across hours past ~day 6); temps = member mean | 16 days |
| nws | National Weather Service | `api.weather.gov` gridpoints | 7 days daily + hourly, **United States only** |
| ecmwf / gfs / gem | ECMWF, GFS, GEM (no best_match blend — it resolves to GFS in much of the world and double-counted it; daily rain % is the daytime-window max, not the 24 h max) | Open-Meteo `models=` | 16 days |
| metno | MET Norway | `api.met.no` locationforecast | ~9 days, temperatures only |

A source that fails is recorded on its own entry (`ok: false`) and never blocks
the run. That is also how a source with a limited footprint behaves outside it:
NWS 404s abroad, so it simply drops out of the average and the page says
"not available for this location".

Each source also yields humidity, wind speed (mph) and direction: the source's
own daily figure where it publishes one, otherwise the daytime average of its
hourly data (`fill_daily_from_hourly`).

## Design notes

**There is no module-level location.** Everything is a function of a `Loc`
(name, lat, lon, IANA timezone). The timezone matters more than it looks:
which calendar day an hour belongs to is a property of the place being
forecast, so the parsers take an explicit `tz` and the Open-Meteo requests
carry `timezone=<loc.tz>`. Reading it from the server would bucket a Tokyo
forecast onto New York's days.

**Sources are collected in parallel.** Nine sequential HTTP conversations took
over twenty seconds, nearly all of it spent waiting. They are read back in
source order, because the summary takes the first human-written condition
phrase it finds and that ordering is what makes it weather.com's rather than a
model's.

**The API is a cache with a collector behind it**, not a collector with a cache
in front. Fresh (<30 min) is served from disk; stale is served immediately
*and* refreshed in the background; only a genuinely cold location waits. A
per-location lock means two visitors opening the same city start one
collection, not two.

## Layout

    collect.py   pure parsers + fetchers + geocoding; no Flask, no I/O beyond HTTP
    app.py       Flask API: /api/geocode, /api/reverse, /api/forecast, /api/health
    www/         static front end (Slippy Labs HUD theme)
    deploy/      systemd unit + nginx vhost
    tests/       parser tests against recorded fixtures

## Run / test

    .venv/bin/python -m pytest -q tests        # parsers against tests/fixtures
    .venv/bin/python collect.py "Boulder, CO"  # one-shot collection to stdout
    .venv/bin/python app.py                    # dev server on 127.0.0.1:8028

## Deploy

    sudo cp deploy/weather.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now weather.service
    sudo install -D -m 0644 deploy/10-memory.conf /etc/systemd/system/weather.service.d/10-memory.conf
    sudo cp deploy/security-headers-geo.conf /etc/nginx/snippets/
    sudo cp deploy/weather.slippylabs.com.conf /etc/nginx/sites-available/
    sudo ln -sf ../sites-available/weather.slippylabs.com.conf /etc/nginx/sites-enabled/
    sudo nginx -t && sudo systemctl reload nginx

`ProtectSystem=strict` makes all of `/` read-only, so the unit grants exactly
two writable paths: the forecast cache and gunicorn's control socket
directory. The application code stays read-only to the service.

The vhost includes `snippets/security-headers-geo.conf` instead of the
estate-wide `security-headers.conf`: the shared one sends
`Permissions-Policy: geolocation=()`, which makes the browser deny "Use my
location" without ever prompting. The copy differs only in `geolocation=(self)`.

Abuse limits (2026-10-16): the per-IP budgets key on nginx's `X-Real-IP`
(Cloudflare-resolved), never on client-supplied `X-Forwarded-For`; at most
`COLD_MAX` (3) collections run at once, extra callers get the stale copy, a
202 or a 503 with `Retry-After`; `/api/geocode` and `/api/reverse` answers are
LRU-cached and only cache misses spend a per-IP budget. The shared forecast
cache stores no caller-supplied label: name/country/admin1 come from each
request (cleaned, length-capped), and the tz is derived server-side from
Open-Meteo (`collect.lookup_tz`), with a valid caller tz only as a fallback.
`deploy/10-memory.conf` caps memory (MemoryHigh 384M, MemoryMax 768M).

Collector failures (a source erroring, a failed cold or background collection,
a geocoder error) are logged to the journal: `journalctl -u weather`.

## Credits

Forked from [mattpetosa/wedding-weather-tracker](https://github.com/mattpetosa/wedding-weather-tracker),
which did this for one weekend in Red Bank, NJ. The multi-source averaging,
the skill weights and the parsers are its work; this fork generalised it to any
location, put a cached API in front of it and re-themed the front end. MIT,
same as upstream.
