"""Forecasts (#612 §4): solar, the site's load and the battery's temperature limit, with a
walk-forward backtest and HAEO-format publishing.

- **Solar:** plane-of-array irradiance × kWp × derate, with a cell-temperature loss (the panel
  coefficient is a setup field), self-calibrated against measured solar; P10/P50/P90 when
  ensemble members are given.
- **Load:** per site from compact hourly history. Time of day × day type × season, plus
  heating and cooling terms around a **learned comfort band**. Each future hour is predicted
  **directly** from its calendar and its forecast temperature, never from earlier
  predictions. With little history it degrades and says so: a thin season falls back to the
  day-type profile, and a short history to the cold start (the weekday-hour median of the
  last 11 weeks). Declared loads are taken out of the base; they are modelled on their own.
- **Battery:** charge and discharge power limited by cabinet temperature, from a setup table
  until a reading proves it.

Pure Python, no I/O; the model is a JSON-safe dict the caller stores. Open-Meteo fetching is
in :mod:`hems.adapters.open_meteo`.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1

# ── Solar ──────────────────────────────────────────────────────────────────────────────

#: Inverter, wiring and soiling derate (irradiance → AC).
DERATE = 0.85
#: Panel power temperature coefficient (%/°C). A setup field; this pre-fill is typical of
#: crystalline panels, please confirm from the panel datasheet.
TEMP_COEFF_PCT = -0.35
#: Nominal operating cell temperature (°C), for the cell temperature from air temperature.
NOCT_C = 45.0
#: Self-calibration: days kept, the fewest that count, a day's minimum forecast, the bounds.
CAL_DAYS = 30
CAL_MIN_DAYS = 5
CAL_MIN_KWH = 1.0
CAL_MIN, CAL_MAX = 0.5, 1.5


def _pv_kw(gti: float | None, temp_c: float | None, kwp: float, derate: float,
           temp_coeff_pct: float, noct_c: float) -> float:
    g = max(0.0, float(gti or 0.0))
    if g <= 0:
        return 0.0
    loss = 1.0
    if temp_c is not None:
        cell = float(temp_c) + (noct_c - 20.0) / 800.0 * g
        loss = 1.0 + temp_coeff_pct / 100.0 * (cell - 25.0)
    return max(0.0, g / 1000.0 * kwp * derate * loss)


def _pct(vals: list[float], q: float) -> float:
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def solar(hours: list[dict], kwp: float, *, derate: float = DERATE,
          temp_coeff_pct: float = TEMP_COEFF_PCT, noct_c: float = NOCT_C,
          calibration: float = 1.0) -> list[dict]:
    """Hourly AC kW from ``{start, gti, temp_c}`` (W/m², °C; ``gti_members`` for an
    ensemble). Each row: ``{start, kw}``, plus ``p10``/``p50``/``p90`` with members."""
    out = []
    for h in hours:
        kw = _pv_kw(h.get("gti"), h.get("temp_c"), kwp, derate, temp_coeff_pct, noct_c)
        row = {"start": h["start"], "kw": round(kw * calibration, 3)}
        members = [m for m in (h.get("gti_members") or []) if m is not None]
        if members:
            kws = [_pv_kw(m, h.get("temp_c"), kwp, derate, temp_coeff_pct, noct_c) * calibration
                   for m in members]
            for name, q in (("p10", 0.1), ("p50", 0.5), ("p90", 0.9)):
                row[name] = round(_pct(kws, q), 3)
        out.append(row)
    return out


def new_calibration() -> dict:
    return {"schema_version": SCHEMA_VERSION, "days": []}


def add_solar_day(state: dict, *, day: str, predicted_kwh: float | None,
                  measured_kwh: float | None) -> None:
    """Record one day's forecast and measured solar. Days with too little forecast sun, or
    no measurement, are ignored; the last ``CAL_DAYS`` are kept."""
    if measured_kwh is None or predicted_kwh is None or predicted_kwh < CAL_MIN_KWH:
        return
    days = [d for d in state["days"] if d[0] != day]
    days.append([day, float(predicted_kwh), float(measured_kwh)])
    state["days"] = sorted(days)[-CAL_DAYS:]


def calibration_factor(state: dict) -> float:
    """Measured ÷ forecast over the kept days, bounded; 1.0 until there are enough days."""
    days = state.get("days") or []
    if len(days) < CAL_MIN_DAYS:
        return 1.0
    pred = sum(d[1] for d in days)
    meas = sum(d[2] for d in days)
    return round(min(CAL_MAX, max(CAL_MIN, meas / pred)), 4) if pred > 0 else 1.0


# ── Load ───────────────────────────────────────────────────────────────────────────────

#: Fewer days of history than this: cold start only.
MIN_HISTORY_DAYS = 28
#: A profile cell needs this many days before it is used.
MIN_DAYS_PER_CELL = 8
#: The cold start: the weekday-hour median over this many days.
COLD_START_DAYS = 77
#: The model learns from at most this many days.
TRAIN_DAYS = 365
#: Comfort band search (°C): heating below ``low``, cooling above ``high``.
COMFORT_LOW = range(12, 20)
COMFORT_HIGH = range(20, 29)
#: Below this (kWh per hour per °C) a heating or cooling term is no effect at all.
TEMP_EFFECT_MIN = 0.001

_SEASON = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
           6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}


def _day_type(d: dt.date, is_holiday: Callable[[dt.date], bool] | None) -> str:
    if is_holiday and is_holiday(d):
        return "holiday"
    return "weekend" if d.weekday() >= 5 else "weekday"


def _rows(history: list[dict], tz: ZoneInfo, declared, is_holiday) -> list[dict]:
    take = {}
    for series in declared or []:
        for k, v in series.items():
            take[int(k)] = take.get(int(k), 0.0) + float(v or 0.0)
    out = []
    for h in history:
        if h.get("kwh") is None:
            continue
        local = dt.datetime.fromtimestamp(h["start"], tz)
        out.append({"start": h["start"], "date": local.date(), "hour": local.hour,
                    "wd": local.weekday(), "season": _SEASON[local.month],
                    "dtype": _day_type(local.date(), is_holiday),
                    "kwh": max(0.0, float(h["kwh"]) - take.get(int(h["start"]), 0.0)),
                    "temp": h.get("temp_c")})
    return out


def _hd(t: float, lo: float) -> float:
    return max(0.0, lo - t)


def _cd(t: float, hi: float) -> float:
    return max(0.0, t - hi)


def _fit_terms(rows: list[dict], lo: float, hi: float) -> tuple[float, float, float]:
    """Within-cell least squares of load on heating and cooling degrees: (a, b, SSE)."""
    cells: dict[str, list[dict]] = {}
    for r in rows:
        cells.setdefault(f"{r['season']}:{r['dtype']}:{r['hour']}", []).append(r)
    sxx = sxy = syy = sx1y = sx2y = sx1x1 = sx2x2 = sx1x2 = 0.0
    for rs in cells.values():
        n = len(rs)
        h = [_hd(r["temp"], lo) for r in rs]
        c = [_cd(r["temp"], hi) for r in rs]
        y = [r["kwh"] for r in rs]
        mh, mc, my = sum(h) / n, sum(c) / n, sum(y) / n
        for i in range(n):
            x1, x2, yy = h[i] - mh, c[i] - mc, y[i] - my
            sx1x1 += x1 * x1
            sx2x2 += x2 * x2
            sx1x2 += x1 * x2
            sx1y += x1 * yy
            sx2y += x2 * yy
            syy += yy * yy
    det = sx1x1 * sx2x2 - sx1x2 * sx1x2
    if det > 1e-9:
        a = (sx1y * sx2x2 - sx2y * sx1x2) / det
        b = (sx2y * sx1x1 - sx1y * sx1x2) / det
    else:
        a = sx1y / sx1x1 if sx1x1 > 1e-9 else 0.0
        b = sx2y / sx2x2 if sx2x2 > 1e-9 else 0.0
    # a load doesn't fall as it gets colder (or hotter): refit without a negative term
    if a < 0:
        a, b = 0.0, (sx2y / sx2x2 if sx2x2 > 1e-9 else 0.0)
    if b < 0:
        b, a = 0.0, max(0.0, sx1y / sx1x1 if sx1x1 > 1e-9 else 0.0)
    sse = (syy - 2 * a * sx1y - 2 * b * sx2y + a * a * sx1x1 + b * b * sx2x2
           + 2 * a * b * sx1x2)
    return a, b, sse


def _cold(rows: list[dict]) -> dict[str, float]:
    if not rows:
        return {}
    last = max(r["date"] for r in rows)
    since = last - dt.timedelta(days=COLD_START_DAYS - 1)
    by: dict[str, list[float]] = {}
    for r in rows:
        if r["date"] >= since:
            by.setdefault(f"{r['wd']}:{r['hour']}", []).append(r["kwh"])
    return {k: round(statistics.median(v), 4) for k, v in by.items()}


def fit_load(history: list[dict], *, tz: ZoneInfo, declared: list[dict] | None = None,
             is_holiday: Callable[[dt.date], bool] | None = None,
             min_days_per_cell: int = MIN_DAYS_PER_CELL) -> dict:
    """The load model from hourly ``{start, kwh, temp_c}`` (start in epoch seconds).
    ``declared``: each declared load's hourly kWh ``{start: kwh}``, taken out of the base.
    ``is_holiday`` makes holidays their own day type."""
    rows = _rows(history, tz, declared, is_holiday)
    dates = {r["date"] for r in rows}
    model: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "tz": str(tz.key),
                             "history_days": len(dates), "cold": _cold(rows),
                             "cells": {}, "pooled": {}, "comfort": None,
                             "heating_kwh_per_degree": 0.0, "cooling_kwh_per_degree": 0.0}
    model["degraded"] = len(dates) < MIN_HISTORY_DAYS
    if model["degraded"]:
        return model
    if dates:
        first = max(dates) - dt.timedelta(days=TRAIN_DAYS - 1)
        rows = [r for r in rows if r["date"] >= first]
    warm = [r for r in rows if r["temp"] is not None]
    best = (0.0, 0.0, math.inf, None, None)
    if warm:
        for lo in COMFORT_LOW:
            for hi in COMFORT_HIGH:
                a, b, sse = _fit_terms(warm, lo, hi)
                if sse < best[2] - 1e-9:
                    best = (a, b, sse, lo, hi)
    a, b, _, lo, hi = best
    if a < TEMP_EFFECT_MIN and b < TEMP_EFFECT_MIN:
        a = b = 0.0
        lo = hi = None                       # the load doesn't follow the weather
    if lo is not None:
        model["comfort"] = {"low_c": float(lo), "high_c": float(hi)}
        model["heating_kwh_per_degree"] = round(a, 5)
        model["cooling_kwh_per_degree"] = round(b, 5)

    def temp_kwh(t):
        return 0.0 if t is None or lo is None else a * _hd(t, lo) + b * _cd(t, hi)

    for key_of, target in ((lambda r: f"{r['season']}:{r['dtype']}:{r['hour']}", "cells"),
                           (lambda r: f"{r['dtype']}:{r['hour']}", "pooled")):
        acc: dict[str, list[tuple]] = {}
        for r in rows:
            acc.setdefault(key_of(r), []).append((r["date"], r["kwh"] - temp_kwh(r["temp"]),
                                                  temp_kwh(r["temp"])))
        for k, v in acc.items():
            if len({x[0] for x in v}) >= min_days_per_cell:
                model[target][k] = {"p": round(statistics.fmean(x[1] for x in v), 5),
                                    "t": round(statistics.fmean(x[2] for x in v), 5)}
    model["degraded"] = not model["cells"]
    return model


def predict_load(model: dict, hours: list[dict],
                 is_holiday: Callable[[dt.date], bool] | None = None) -> list[dict]:
    """kWh for each future hour ``{start, temp_c}``, directly from its calendar and forecast
    temperature. ``method``: ``model``, or ``cold_start`` where the model has no cell."""
    tz = ZoneInfo(model["tz"])
    c = model.get("comfort") or {}
    a, b = model["heating_kwh_per_degree"], model["cooling_kwh_per_degree"]
    out = []
    for h in hours:
        local = dt.datetime.fromtimestamp(h["start"], tz)
        dtype = _day_type(local.date(), is_holiday)
        cell = (model["cells"].get(f"{_SEASON[local.month]}:{dtype}:{local.hour}")
                or model["pooled"].get(f"{dtype}:{local.hour}"))
        t = h.get("temp_c")
        if cell is not None:
            if t is None or not c:
                kwh = cell["p"] + cell["t"]
            else:
                kwh = cell["p"] + a * _hd(t, c["low_c"]) + b * _cd(t, c["high_c"])
            out.append({"start": h["start"], "kwh": round(max(0.0, kwh), 4), "method": "model"})
            continue
        v = model["cold"].get(f"{local.weekday()}:{local.hour}")
        out.append({"start": h["start"], "kwh": None if v is None else round(v, 4),
                    "method": "cold_start"})
    return out


# ── Battery ────────────────────────────────────────────────────────────────────────────

def battery_power_limit(rated_kw: float, temp_c: float | None,
                        table: list[tuple[float, float]] | None) -> float:
    """Charge or discharge kW allowed at this cabinet temperature: rated × the setup table's
    fraction (linear between points, flat beyond them). No table or no reading: rated."""
    if not table or temp_c is None:
        return float(rated_kw)
    pts = sorted(table)
    if temp_c <= pts[0][0]:
        f = pts[0][1]
    elif temp_c >= pts[-1][0]:
        f = pts[-1][1]
    else:
        f = pts[-1][1]
        for (t0, f0), (t1, f1) in zip(pts, pts[1:]):
            if t0 <= temp_c <= t1:
                f = f0 + (f1 - f0) * (temp_c - t0) / (t1 - t0)
                break
    return round(float(rated_kw) * f, 4)


# ── Backtest and checks ──────────────────────────────────────────────────────────────────

#: A declared load matches when its week's actual is within this fraction of the forecast.
DECLARED_TOLERANCE = 0.25


def _metrics(pairs: list[tuple[float, float, dt.date]]) -> dict:
    if not pairs:
        return {"mae_kwh": None, "rmse_kwh": None, "bias_kwh": None, "daily_mape_pct": None,
                "hours": 0}
    err = [p - a for p, a, _ in pairs]
    by_day: dict[dt.date, list[float]] = {}
    for p, a, d in pairs:
        s = by_day.setdefault(d, [0.0, 0.0])
        s[0] += p
        s[1] += a
    mape = [abs(p - a) / a * 100 for p, a in by_day.values() if a > 0]
    return {"mae_kwh": round(statistics.fmean(abs(e) for e in err), 4),
            "rmse_kwh": round(math.sqrt(statistics.fmean(e * e for e in err)), 4),
            "bias_kwh": round(statistics.fmean(err), 4),
            "daily_mape_pct": round(statistics.fmean(mape), 2) if mape else None,
            "hours": len(pairs)}


def backtest_load(history: list[dict], *, tz: ZoneInfo, min_train_days: int = MIN_HISTORY_DAYS,
                  refit_days: int = 7, declared: list[dict] | None = None,
                  is_holiday: Callable[[dt.date], bool] | None = None) -> dict:
    """Walk-forward accuracy: for each day after the first ``min_train_days``, a model fitted
    only on earlier days (refitted every ``refit_days``, so horizons run 1 to ``refit_days``
    days ahead) predicts its hours. Compared with the cold start and persistence (the same
    hour the day before). Temperatures are the recorded ones, so the model's figure is what
    it would score with a perfect temperature forecast."""
    rows = sorted(_rows(history, tz, declared, is_holiday), key=lambda r: r["start"])
    days = sorted({r["date"] for r in rows})
    by_day: dict[dt.date, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)
    base = [{"start": r["start"], "kwh": r["kwh"], "temp_c": r["temp"]} for r in rows]
    res: dict[str, list] = {"model": [], "cold_start": [], "persistence": []}
    model = cold = None
    for i, d in enumerate(days[min_train_days:], start=min_train_days):
        if model is None or (i - min_train_days) % refit_days == 0:
            cut = int(dt.datetime.combine(d, dt.time(), tz).timestamp())
            train = [h for h in base if h["start"] < cut]
            model = fit_load(train, tz=tz, is_holiday=is_holiday)
            cold = fit_load(train, tz=tz, is_holiday=is_holiday, min_days_per_cell=10 ** 9)
        hrs = [{"start": r["start"], "temp_c": r["temp"]} for r in by_day[d]]
        actual = {r["start"]: r["kwh"] for r in by_day[d]}
        for name, m in (("model", model), ("cold_start", cold)):
            for p in predict_load(m, hrs, is_holiday):
                if p["kwh"] is not None:
                    res[name].append((p["kwh"], actual[p["start"]], d))
        prev = {r["hour"]: r["kwh"] for r in by_day.get(d - dt.timedelta(days=1), [])}
        for r in by_day[d]:
            if r["hour"] in prev:
                res["persistence"].append((prev[r["hour"]], r["kwh"], d))
    return {"days": len(days) - min_train_days if len(days) > min_train_days else 0,
            "refit_days": refit_days, **{k: _metrics(v) for k, v in res.items()}}


def declared_check(predicted: list[float | None], actual: list[float | None],
                   tolerance: float = DECLARED_TOLERANCE) -> dict:
    """The 7-day check after setup: does a declared load use what its answers said? Hours
    with no measurement are left out; ``matches`` is None with no measurement at all."""
    pairs = [(p, a) for p, a in zip(predicted, actual) if p is not None and a is not None]
    if not pairs:
        return {"matches": None, "predicted_kwh": None, "actual_kwh": None}
    p = sum(x[0] for x in pairs)
    a = sum(x[1] for x in pairs)
    ok = abs(a - p) <= tolerance * p if p > 0 else a <= 0
    return {"matches": ok, "predicted_kwh": round(p, 3), "actual_kwh": round(a, 3)}


# ── Publishing ─────────────────────────────────────────────────────────────────────────

def haeo(points: list[dict], *, tz: ZoneInfo, key: str = "kw", step_s: int = 1800) -> list[dict]:
    """A HAEO-format ``forecast`` attribute: ``[{time, value}]`` every 30 minutes, each hour's
    power repeated over its half hours. (HA should not record the attribute.)"""
    out = []
    for p in points:
        if p.get(key) is None:
            continue
        for k in range(3600 // step_s):
            t = dt.datetime.fromtimestamp(p["start"] + k * step_s, tz)
            out.append({"time": t.isoformat(), "value": p[key]})
    return out
