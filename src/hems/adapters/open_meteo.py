"""Open-Meteo (free, no key): hourly air temperature and plane-of-array irradiance, forecast
and archive, as ``{start, gti, temp_c}`` rows for :func:`hems.forecast.solar` and the load
model. Times are requested in UTC (``timezone=GMT``), so ``start`` is plain epoch seconds.

``azimuth`` is Open-Meteo's: 0 = south, −90 = east, 90 = west, ±180 = north.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse
import urllib.request

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = "temperature_2m,global_tilted_irradiance"
TIMEOUT_S = 15


def forecast_params(lat: float, lon: float, *, tilt: float, azimuth: float,
                    days: int = 2) -> dict:
    return {"latitude": lat, "longitude": lon, "timezone": "GMT", "forecast_days": days,
            "hourly": HOURLY, "tilt": tilt, "azimuth": azimuth}


def archive_params(lat: float, lon: float, *, start: str, end: str, tilt: float,
                   azimuth: float) -> dict:
    return {"latitude": lat, "longitude": lon, "timezone": "GMT", "start_date": start,
            "end_date": end, "hourly": HOURLY, "tilt": tilt, "azimuth": azimuth}


def parse_hourly(raw: dict) -> list[dict]:
    """Rows ``{start, gti, temp_c}``; a missing value is None."""
    h = raw.get("hourly") or {}
    off = int(raw.get("utc_offset_seconds") or 0)
    times = h.get("time") or []
    gti = h.get("global_tilted_irradiance") or []
    temp = h.get("temperature_2m") or []
    out = []
    for i, t in enumerate(times):
        local = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone(dt.timedelta(seconds=off)))
        out.append({"start": int(local.timestamp()),
                    "gti": gti[i] if i < len(gti) else None,
                    "temp_c": temp[i] if i < len(temp) else None})
    return out


def fetch(url: str, params: dict, *, timeout: float = TIMEOUT_S,
          user_agent: str = "hems") -> dict:
    """GET ``url`` with ``params``; the parsed JSON. Raises on a network or HTTP error."""
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())
