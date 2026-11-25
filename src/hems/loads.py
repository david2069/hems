"""HEMS setup and loads (#612 §5).

Setup is opt-in, with the notice below. Anything measurable is pre-filled from history and
marked "please confirm": the always-on load and the high, medium and low usage periods.
Each heavy load says what it is, how it is measured (best first: an HA power entity, an HA
energy entity, then nameplate kW), its power and/or duration, when it runs (or that it
follows the weather; each run window may be limited to days of the week), its circuit, and how it may be controlled:

- **deferrable:** energy by a deadline, minimum run, maximum starts, splittable or not;
- **sheddable:** maximum off time, minimum on time, priority;
- **soak:** soaks up energy when the export price is at or below a threshold (negative
  included), the export limit binds, or the battery is full while exporting; limited by a
  kWh quota, with a price penalty for the planner.

Loads are checked against the controls the bridge declares (its capabilities): no switching
by smart circuit without ``smart_circuits``. Pure Python, no I/O.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from typing import Any
from zoneinfo import ZoneInfo

NOTICE = "Planning is only as accurate as your answers."

#: How a load is measured, best first.
MEASUREMENTS = ("ha_power", "ha_energy", "nameplate")
CIRCUITS = ("smart_circuit", "controlled_load", "general")
CONTROL_TYPES = ("deferrable", "sheddable", "soak")
SWITCHES = ("smart_circuit", "ha_switch", "ha_climate", "ha_number")
EV_CHARGER = "ev_charger"
#: Device types: the stored value and the label a form shows. Only these are accepted;
#: "other" covers anything not listed.
DEVICE_TYPES = {
    EV_CHARGER: "EV charger",
    "hot_water": "Hot water",
    "heat_pump": "Heat pump",
    "air_conditioner": "Air conditioning",
    "pool_pump": "Pool pump",
    "spa": "Spa or hot tub",
    "well_pump": "Well pump",
    "dryer": "Clothes dryer",
    "washing_machine": "Washing machine",
    "dishwasher": "Dishwasher",
    "oven": "Oven or cooktop",
    "other": "Other",
}
EV_FIELDS = ("kw", "phases", "control_path", "arrival", "departure", "energy_needed_kwh")
SOAK_TRIGGERS = ("export_price_at_or_below", "export_limited", "battery_full_exporting")
_REQUIRED = {"deferrable": ("energy_kwh", "deadline"),
             "sheddable": ("max_off_min", "min_on_min", "priority"),
             "soak": ("quota_kwh", "price_penalty")}

#: A run window's ``days``: weekday numbers, Monday = 0 (as ``date.weekday()``); these are
#: their labels. No ``days``: every day.
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

#: The always-on load: the low end of the overnight hours.
OVERNIGHT_HOURS = range(0, 6)
OVERNIGHT_QUANTILE = 0.1

_POWER = {"W": 0.001, "kW": 1.0, "MW": 1000.0}
_ENERGY = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}


def _q(vals: list[float], q: float) -> float:
    s = sorted(vals)
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def always_on(history: list[dict], *, tz: ZoneInfo) -> dict | None:
    """The overnight minimum from hourly ``{start, kwh}``, to confirm. None without history."""
    vals = [float(h["kwh"]) for h in history if h.get("kwh") is not None
            and dt.datetime.fromtimestamp(h["start"], tz).hour in OVERNIGHT_HOURS]
    if not vals:
        return None
    return {"value_kw": round(_q(vals, OVERNIGHT_QUANTILE), 3), "confirm": True,
            "basis": "overnight minimum, 00:00–06:00"}


def _bands(profile: list[float | None]) -> list[str | None]:
    vals = [v for v in profile if v is not None]
    if not vals:
        return [None] * 24
    lo, hi = _q(vals, 1 / 3), _q(vals, 2 / 3)
    return [None if v is None else "high" if v > hi else "low" if v < lo else "medium"
            for v in profile]


def usage_periods(history: list[dict], *, tz: ZoneInfo) -> dict:
    """The usage heatmap (weekday Monday=0 × hour, mean kWh) and the high, medium and low
    periods of a weekday and a weekend day, to confirm."""
    cells: dict[tuple[int, int], list[float]] = {}
    for h in history:
        if h.get("kwh") is None:
            continue
        t = dt.datetime.fromtimestamp(h["start"], tz)
        cells.setdefault((t.weekday(), t.hour), []).append(float(h["kwh"]))
    heat = [[round(statistics.fmean(cells[(d, hr)]), 4) if (d, hr) in cells else None
             for hr in range(24)] for d in range(7)]

    def profile(days):
        out = []
        for hr in range(24):
            v = [heat[d][hr] for d in days if heat[d][hr] is not None]
            out.append(statistics.fmean(v) if v else None)
        return out

    return {"confirm": True, "heatmap": heat, "weekday": _bands(profile(range(5))),
            "weekend": _bands(profile((5, 6)))}


def _sum(readings: list[tuple[Any, str]], units: dict[str, float]) -> float | None:
    total = 0.0
    for value, unit in readings:
        if unit not in units:
            raise ValueError(f"unit {unit!r} is not one of {', '.join(units)}")
        try:
            total += float(value) * units[unit]
        except (TypeError, ValueError):
            return None                      # one unreadable entity: the sum is unknown
    return total


def power_kw(readings: list[tuple[Any, str]]) -> float | None:
    """Sum of power entities ``[(value, unit)]`` in kW (W, kW, MW); None if any is unreadable."""
    return _sum(readings, _POWER)


def energy_kwh(readings: list[tuple[Any, str]]) -> float | None:
    """Sum of energy entities ``[(value, unit)]`` in kWh (Wh, kWh, MWh)."""
    return _sum(readings, _ENERGY)


def check(load: dict, capabilities: set[str] | frozenset[str]) -> list[str]:
    """What is missing or can't work for this load, given the bridge's capabilities."""
    out = []
    if not load.get("device_type"):
        out.append("device type: missing")
    elif load["device_type"] not in DEVICE_TYPES:
        out.append(f"device type: one of {', '.join(DEVICE_TYPES)}")
    m = load.get("measurement") or {}
    if m.get("kind") not in MEASUREMENTS:
        out.append(f"measurement: one of {', '.join(MEASUREMENTS)}")
    elif m["kind"] != "nameplate" and not m.get("entities"):
        out.append("measurement: name the entities")
    elif m["kind"] == "nameplate" and not load.get("power_kw"):
        out.append("measurement: nameplate needs the power in kW")
    if not load.get("power_kw") and not load.get("duration_h"):
        out.append("power or duration: give one or both")
    ev = load.get("device_type") == EV_CHARGER
    if not ev and not load.get("runs"):
        out.append("when it runs: give its times, or that it follows the weather")
    for w in load.get("runs") if isinstance(load.get("runs"), list) else []:
        d = w.get("days") if isinstance(w, dict) else None
        if d is not None and (not isinstance(d, list) or not d or not all(
                isinstance(x, int) and 0 <= x <= 6 for x in d)):
            out.append("days: weekday numbers 0 (Monday) to 6, at least one")
            break
    if load.get("circuit") not in CIRCUITS:
        out.append(f"circuit: one of {', '.join(CIRCUITS)}")
    if ev:
        e = load.get("ev") or {}
        missing = [f for f in EV_FIELDS if e.get(f) in (None, "")]
        if missing:
            out.append("EV charger: missing " + ", ".join(missing))
    c = load.get("control")
    if c:
        if c.get("type") not in CONTROL_TYPES:
            out.append(f"control type: one of {', '.join(CONTROL_TYPES)}")
        else:
            missing = [f for f in _REQUIRED[c["type"]] if c.get(f) in (None, "")]
            if missing:
                out.append(f"{c['type']}: missing " + ", ".join(missing))
            if c["type"] == "soak" and not any(c.get(t) not in (None, False)
                                               for t in SOAK_TRIGGERS):
                out.append("soak: needs a trigger (" + ", ".join(SOAK_TRIGGERS) + ")")
        sw = load.get("switch") or {}
        if sw.get("kind") not in SWITCHES:
            out.append(f"switch: one of {', '.join(SWITCHES)}")
    sw = load.get("switch") or {}
    if (sw.get("kind") == "smart_circuit" or (c and load.get("circuit") == "smart_circuit"
                                              and not sw)) \
            and "smart_circuits" not in capabilities:
        out.append("switching by smart circuit needs the smart_circuits capability")
    return out


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def expected_hourly(load: dict, day: dt.date, *, tz: ZoneInfo) -> dict[int, float] | None:
    """A scheduled load's kWh per hour on ``day`` (``{start: kwh}``), from its run windows and
    power; ``{}`` on a day none of its windows runs. A window with ``days`` runs on those
    weekdays; its hours after midnight belong to the next day. None for a load that follows
    the weather or has no times."""
    runs, kw = load.get("runs"), load.get("power_kw")
    if not isinstance(runs, list) or not runs or not kw:
        return None
    today, yesterday = day.weekday(), (day.weekday() - 1) % 7
    minutes = [0.0] * 24
    for w in runs:
        a, b = _hm(w["start"]), _hm(w["end"])
        days = w.get("days")

        def on(wd, days=days):
            return days is None or wd in days
        if b > a:
            spans = [(a, b)] if on(today) else []
        else:
            spans = ([(a, 1440)] if on(today) else []) + ([(0, b)] if on(yesterday) else [])
        for s, e in spans:
            for hr in range(24):
                lo, hi = max(s, hr * 60), min(e, hr * 60 + 60)
                if hi > lo:
                    minutes[hr] += hi - lo
    out = {}
    for hr, mins in enumerate(minutes):
        if mins > 0:
            start = int(dt.datetime.combine(day, dt.time(hr), tz).timestamp())
            out[start] = round(float(kw) * min(mins, 60) / 60.0, 4)
    return out


def soak(control: dict, *, export_price: float | None = None, export_limited: bool = False,
         battery_full_exporting: bool = False, used_kwh: float = 0.0) -> dict:
    """Should a soak load run now? ``{on, why}``. Stops once its kWh quota is used."""
    quota = control.get("quota_kwh")
    if quota is not None and used_kwh >= float(quota):
        return {"on": False, "why": f"quota used ({used_kwh:g} of {float(quota):g} kWh)"}
    limit = control.get("export_price_at_or_below")
    if limit is not None and export_price is not None and export_price <= float(limit):
        return {"on": True, "why": f"export price {export_price:g} at or below {float(limit):g}"}
    if export_limited:
        return {"on": True, "why": "export limit reached"}
    if battery_full_exporting:
        return {"on": True, "why": "battery full and exporting"}
    return {"on": False, "why": "no trigger"}
