# Wedding Weather Tracker

Multi-source rain outlook for one weekend, built for phones, live at https://weather.mhpwebserver.com. Pulls weather.com, AccuWeather, NWS, ECMWF/GFS/GEM and the ECMWF+GEFS ensembles hourly, averages them with skill-based weights, and shows a ceremony-window verdict for the key day. Fork it: change `LOCATION`, `EVENT_DAYS`, `KEY_DAY` and `KEY_WINDOW` for your own event.

Consensus rain outlook for one weekend, built for phones. Every hour `collect.py`
pulls the Red Bank forecast from ten sources, averages the daily chance of rain
(and the hourly chance where a source publishes it), and writes `data/latest.json`.
`www/` is a static page that reads that file. There is no backend.

## Sources
| id | Source | How | Reach |
|---|---|---|---|
| twc | The Weather Channel (weather.com; same engine as Wunderground) | `api.weather.com` v3 with the public key embedded in wunderground.com | 15 days daily + hourly |
| accuweather | AccuWeather | server-rendered daily page + hourly pages, fetched with `curl_cffi` (Chrome TLS fingerprint — plain curl gets an Akamai 403) | 15 days daily, 4 days hourly |
| ecmwf_ens / gefs | ECMWF ensemble (51 runs), GEFS ensemble (31 runs) | Open-Meteo ensemble API; rain % = share of members with ≥0.254 mm over 6am–8pm (hourly: ≥0.05 mm/h, because 6-hourly blocks are spread evenly across hours past ~day 6); temps = member mean | 16 days (GEFS needs `gfs_seamless`; `gfs025` is None past 10 days) |
| nws | National Weather Service | `api.weather.gov` gridpoints PHI | 7 days daily + hourly |
| ecmwf / gfs / gem | ECMWF, GFS, GEM (no best_match blend — it resolves to GFS here and double-counted it; daily rain % is the daytime-window max, not the 24 h max) | Open-Meteo `models=` | 16 days (GEM temps 10 days; ECMWF queried at lon -74.15 because its cell at -74.06 is ocean) |
| metno | MET Norway | `api.met.no` locationforecast | ~9 days, temperatures only |

Each source also yields humidity, wind speed (mph) and direction: the source's own daily figure where it publishes one (TWC dayparts, NWS/AccuWeather wind text), otherwise the 6am–8pm average of its hourly data (`fill_daily_from_hourly`). Tiles show the cross-source average; direction is the most common compass point.

A source failing is recorded on its own entry (`ok: false`) and never blocks the run.

## Run / test
    .venv/bin/python -m pytest -q tests     # parsers against fixtures in tests/fixtures
    .venv/bin/python collect.py             # live collection → data/latest.json (+ data/history/)
    cd www && python3 -m http.server 8799   # local preview (www/data → ../data symlink)

## Deploy (this box)
    sudo cp deploy/weather.mhpwebserver.com.conf /etc/nginx/sites-available/ && sudo ln -s ../sites-available/weather.mhpwebserver.com.conf /etc/nginx/sites-enabled/
    sudo cp deploy/weather-collect.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now weather-collect.timer
    sudo nginx -t && sudo systemctl reload nginx

Hash links open a day expanded, e.g. `https://weather.mhpwebserver.com/#2026-09-06`.

## Headline weighting

The tile headline is a **skill-weighted** mean, not a plain average (`source_weight()` in `collect.py`; the plain average is kept as `pop_plain`). Weights by lead time: ECMWF ensemble ×2, GEFS ×1.5, NWS ×1.5 (×2 inside 3 days), weather.com ×1.5, ECMWF/GFS ×1, GEM ×0.75, AccuWeather ×1 inside a week and ×0.5 beyond. The same weights apply to the hourly bars and the ceremony verdict. The min–max band is still the raw per-source range.
