"""Battery wear: warranty inputs, the wear cost per kWh, the throughput pace budget, dispatch
events and round-trip efficiency (#612 §6).

- **Every warranty input is a setup field.** The cloud only pre-fills them, marked "please
  confirm"; the guaranteed end-of-warranty SoH is in no API (it is in the warranty document).
  So wear works without the cloud: setup fields plus SoH read locally.
- **Wear cost per kWh** = (replacement value − end-of-warranty value) ÷ warranted throughput.
- **Pace budget:** remaining throughput ÷ days left. Going above it costs a rising penalty;
  it is never a hard stop, so outages and VPP events are still served.
- **Round-trip efficiency** only from SoC-matched cycles (a single dispatch can't give it:
  SoC error dominates short events), as a rolling 30-day median, banded by power and
  temperature once a band has enough cycles.

Throughput is discharged energy. No I/O: dispatch events, daily records and tracker state are
returned for the caller to store.
"""
from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

SCHEMA_VERSION = 1

#: The setup fields, in the order a setup screen asks for them.
FIELDS = ("warranty_end", "warranted_throughput_mwh", "end_soh_pct", "install_date",
          "replacement_value", "end_value")

#: The rise of the pace penalty: wear cost × this × excess² ÷ pace (per day).
PACE_STEEPNESS = 1.0

#: Event sources (§6 recording).
SOURCES = ("planner", "schedule", "vpp", "user")
#: Below this battery power (W) the battery is idle.
FLOW_W = 50.0
#: Idle this long (s) ends a charge or discharge event.
IDLE_S = 300.0
#: A sample gap longer than this (s) is not integrated.
MAX_GAP_S = 3600.0

#: SoC-matched cycles: back within this many SoC points…
SOC_MATCH = 1.0
#: …with at least this much charged and discharged in between (kWh)… On recorded 5-minute
#: history, 1 kWh cycles spread 0.79–1.16 (p10–p90); 3 kWh cycles 0.95–1.05.
MIN_CYCLE_KWH = 3.0
#: …and no longer than this (s).
MAX_CYCLE_S = 7 * 86400
#: The efficiency the planner uses: the median of this many days of cycles.
EFFICIENCY_DAYS = 30
#: A power or temperature band needs this many cycles before it has its own median.
MIN_PER_BAND = 5
#: Band edges: battery power (W) and temperature (°C).
POWER_BANDS_W = (2000.0, 4000.0)
TEMP_BANDS_C = (10.0, 30.0)


def _date(v: Any) -> str | None:
    """A date as YYYY-MM-DD from an ISO string, a date, or epoch seconds or milliseconds."""
    if v in (None, ""):
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:10]
    if isinstance(v, (int, float)):
        s = v / 1000.0 if v > 1e11 else float(v)
        return dt.datetime.fromtimestamp(s, dt.timezone.utc).date().isoformat()
    return str(v)[:10]


def _cloud_values(cloud: dict | None) -> dict[str, Any]:
    """What the FranklinWH cloud's ``warrantyInfo`` can pre-fill."""
    c = cloud or {}
    out: dict[str, Any] = {}
    if c.get("expirationTime") not in (None, ""):
        out["warranty_end"] = _date(c["expirationTime"])
    if c.get("throughput") not in (None, ""):
        out["warranted_throughput_mwh"] = float(c["throughput"])
    return out


def setup_fields(values: dict | None, *, cloud: dict | None = None) -> dict[str, dict]:
    """Each setup field as ``{value, source, confirm}``. The user's value wins and needs no
    confirming; a cloud value is a pre-fill to confirm; a missing one waits for the user."""
    values = values or {}
    pre = _cloud_values(cloud)
    out = {}
    for f in FIELDS:
        if values.get(f) not in (None, ""):
            out[f] = {"value": values[f], "source": "user", "confirm": False}
        elif f in pre:
            out[f] = {"value": pre[f], "source": "cloud", "confirm": True}
        else:
            out[f] = {"value": None, "source": None, "confirm": True}
    return out


def _v(values: dict, key: str) -> Any:
    """A field's value from plain values or from :func:`setup_fields` output."""
    v = values.get(key)
    return v.get("value") if isinstance(v, dict) else v


def cost_per_kwh(values: dict) -> dict | None:
    """The wear cost per discharged kWh, with its formula. The end-of-warranty value is the
    user's, else replacement × guaranteed end SoH (marked derived, please confirm). None
    while an input is missing."""
    rep, mwh = _v(values, "replacement_value"), _v(values, "warranted_throughput_mwh")
    end, soh = _v(values, "end_value"), _v(values, "end_soh_pct")
    if rep in (None, "") or not mwh:
        return None
    derived = end in (None, "")
    if derived:
        if soh in (None, ""):
            return None
        end = float(rep) * float(soh) / 100.0
    kwh = float(mwh) * 1000.0
    per = (float(rep) - float(end)) / kwh
    end_value = {"value": round(float(end), 2), "derived": derived}
    if derived:
        end_value["note"] = "replacement value × end-of-warranty SoH; please confirm"
    return {"per_kwh": round(per, 6),
            "formula": f"(${float(rep):,.2f} − ${float(end):,.2f}) ÷ {kwh:,.0f} kWh",
            "end_value": end_value}


def remaining_kwh(values: dict, *, used_kwh: float | None = None,
                  cloud_remaining_kwh: float | None = None) -> float | None:
    """Warranted throughput left (kWh): the cloud's figure when given, else warranted −
    discharged since install (the bridge's own counters)."""
    if cloud_remaining_kwh is not None:
        return float(cloud_remaining_kwh)
    mwh = _v(values, "warranted_throughput_mwh")
    if not mwh or used_kwh is None:
        return None
    return max(0.0, float(mwh) * 1000.0 - float(used_kwh))


def pace(*, remaining_kwh: float | None, today: dt.date, warranty_end: Any) -> dict | None:
    """The daily throughput pace: remaining ÷ days left. None after the warranty ends or
    without the inputs."""
    end = _date(warranty_end)
    if remaining_kwh is None or not end:
        return None
    days = (dt.date.fromisoformat(end) - today).days
    if days <= 0:
        return None
    daily = float(remaining_kwh) / days
    return {"daily_kwh": round(daily, 4), "days_left": days,
            "formula": f"{float(remaining_kwh):,.0f} kWh ÷ {days:,} days"}


def pace_penalty(planned_kwh: float, daily_kwh: float, wear_per_kwh: float,
                 steepness: float = PACE_STEEPNESS) -> float:
    """The planner's extra cost for a day's discharge above the pace: nothing up to it, then
    wear × steepness × excess² ÷ pace, so each further kWh costs more. Never a stop."""
    if not daily_kwh or daily_kwh <= 0:
        return 0.0
    excess = max(0.0, float(planned_kwh) - float(daily_kwh))
    return float(wear_per_kwh) * steepness * excess * excess / float(daily_kwh)


def pace_ratio(*, used_kwh: float, days: float, daily_kwh: float) -> float | None:
    """How fast throughput is being used against the pace (1.3 = 1.3× the warranty pace)."""
    if not days or not daily_kwh:
        return None
    return round(float(used_kwh) / float(days) / float(daily_kwh), 4)


def cycles(*, discharged_kwh: float, usable_kwh: float) -> float | None:
    """Full cycles: discharged ÷ usable capacity."""
    return float(discharged_kwh) / float(usable_kwh) if usable_kwh else None


def over_cap(cycles_: float | None, cap: float | None) -> bool | None:
    """Above the optional cycles-per-day cap? None when there is no cap."""
    if cap is None or cycles_ is None:
        return None
    return cycles_ > cap


def usable_kwh(rated_kwh: float, soh_pct: float | None) -> float:
    """Usable capacity after wear: a falling SoH lowers it automatically."""
    return float(rated_kwh) if soh_pct is None else float(rated_kwh) * float(soh_pct) / 100.0


def daily_record(day: dt.date, *, soh_pct: float | None, used_kwh: float | None,
                 remaining_kwh: float | None) -> dict:
    """One day's SoH and throughput, kept for the whole warranty term."""
    return {"schema_version": SCHEMA_VERSION, "date": day.isoformat(), "soh_pct": soh_pct,
            "used_kwh": used_kwh, "remaining_kwh": remaining_kwh}


# ── Dispatch events ──────────────────────────────────────────────────────────────────────

def new_tracker() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "open": None, "last": None, "idle_since": None}


def _direction(w: float | None) -> str | None:
    if w is None or abs(w) < FLOW_W:
        return None
    return "discharge" if w > 0 else "charge"


def _close(ev: dict, end: float) -> dict:
    dur = max(1.0, end - ev["start"])
    kwh = ev["energy_out_kwh"] if ev["direction"] == "discharge" else ev["energy_in_kwh"]
    out = {k: v for k, v in ev.items() if not k.startswith("_")}
    out["end"] = end
    out["energy_in_kwh"] = round(ev["energy_in_kwh"], 6)
    out["energy_out_kwh"] = round(ev["energy_out_kwh"], 6)
    out["mean_power_w"] = round(kwh * 3_600_000.0 / dur, 1)
    for k in ("battery_temp_c", "ambient_temp_c"):
        vals = ev["_" + k]
        out[k] = round(statistics.fmean(vals), 2) if vals else None
    return out


def add_sample(tracker: dict, ts: float, *, battery_w: float | None, soc: float | None,
               source: str | None = None, battery_temp_c: float | None = None,
               ambient_temp_c: float | None = None) -> list[dict]:
    """Record one sample (``battery_w``: + discharging, − charging). Returns the dispatch
    events that closed (each charge or discharge block): energy in and out, SoC start and
    end, mean power, temperatures and source."""
    if source is not None and source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    closed: list[dict] = []
    last = tracker["last"]
    ev = tracker["open"]
    # integrate the previous sample's power over the gap into the open event
    if ev is not None and last is not None and 0 < ts - last[0] <= MAX_GAP_S and last[1]:
        kwh = abs(last[1]) * (ts - last[0]) / 3_600_000.0
        if _direction(last[1]) == ev["direction"]:
            ev["energy_out_kwh" if last[1] > 0 else "energy_in_kwh"] += kwh
            ev["_last_flow"] = ts
    d = _direction(battery_w)
    if ev is not None:
        if d == ev["direction"]:
            tracker["idle_since"] = None
            if soc is not None:
                ev["soc_end"] = soc
        elif d is None:
            tracker["idle_since"] = tracker["idle_since"] or ts
            if ts - tracker["idle_since"] >= IDLE_S:
                closed.append(_close(ev, ev["_last_flow"]))
                tracker["open"] = ev = None
        else:
            closed.append(_close(ev, ts))
            tracker["open"] = ev = None
    if ev is None and d is not None:
        ev = tracker["open"] = {
            "schema_version": SCHEMA_VERSION, "direction": d, "start": ts,
            "energy_in_kwh": 0.0, "energy_out_kwh": 0.0, "soc_start": soc, "soc_end": soc,
            "source": source, "_last_flow": ts, "_battery_temp_c": [], "_ambient_temp_c": []}
        tracker["idle_since"] = None
    if ev is not None and d == ev["direction"]:
        if battery_temp_c is not None:
            ev["_battery_temp_c"].append(float(battery_temp_c))
        if ambient_temp_c is not None:
            ev["_ambient_temp_c"].append(float(ambient_temp_c))
    tracker["last"] = (float(ts), battery_w)
    return closed


# ── Round-trip efficiency ────────────────────────────────────────────────────────────────

def soc_matched_cycles(samples: list[dict]) -> list[dict]:
    """Cycles from SoC x back to SoC x with real energy both ways, and their efficiency
    (discharged ÷ charged). ``samples``: ``{ts, soc, charged_kwh, discharged_kwh}`` with
    cumulative energy counters, oldest first; ``power_w`` and ``temp_c`` when known."""
    s = [x for x in samples if x.get("soc") is not None
         and x.get("charged_kwh") is not None and x.get("discharged_kwh") is not None]
    out, i = [], 0
    while i < len(s) - 1:
        a, found = s[i], None
        for j in range(i + 1, len(s)):
            b = s[j]
            if b["ts"] - a["ts"] > MAX_CYCLE_S:
                break
            chg = b["charged_kwh"] - a["charged_kwh"]
            dis = b["discharged_kwh"] - a["discharged_kwh"]
            if abs(b["soc"] - a["soc"]) <= SOC_MATCH and chg >= MIN_CYCLE_KWH \
                    and dis >= MIN_CYCLE_KWH:
                found = j
                break
        if found is None:
            i += 1
            continue
        span = s[i:found + 1]
        b = s[found]
        chg = b["charged_kwh"] - a["charged_kwh"]
        dis = b["discharged_kwh"] - a["discharged_kwh"]
        pw = [abs(x["power_w"]) for x in span if x.get("power_w") is not None]
        tc = [x["temp_c"] for x in span if x.get("temp_c") is not None]
        out.append({"start": a["ts"], "end": b["ts"], "energy_in_kwh": round(chg, 4),
                    "energy_out_kwh": round(dis, 4), "efficiency": round(dis / chg, 4),
                    "mean_power_w": round(statistics.fmean(pw), 1) if pw else None,
                    "mean_temp_c": round(statistics.fmean(tc), 2) if tc else None})
        i = found
    return out


def _band(v: float | None, edges: tuple[float, ...]) -> int | None:
    if v is None:
        return None
    return sum(1 for e in edges if v >= e)


def efficiency(cycles_: list[dict], *, now_ts: float, days: int = EFFICIENCY_DAYS) -> dict:
    """The rolling median efficiency over ``days``, and per (power band, temperature band)
    where a band has at least ``MIN_PER_BAND`` cycles. The raw cycles stay with the caller."""
    recent = [c for c in cycles_ if now_ts - c["end"] <= days * 86400]
    vals = [c["efficiency"] for c in recent]
    bands: dict[str, list[float]] = {}
    for c in recent:
        pb, tb = _band(c.get("mean_power_w"), POWER_BANDS_W), _band(c.get("mean_temp_c"),
                                                                    TEMP_BANDS_C)
        if pb is not None and tb is not None:
            bands.setdefault(f"{pb}:{tb}", []).append(c["efficiency"])
    return {"median": round(statistics.median(vals), 4) if vals else None, "n": len(vals),
            "days": days,
            "bands": {k: {"median": round(statistics.median(v), 4), "n": len(v)}
                      for k, v in sorted(bands.items()) if len(v) >= MIN_PER_BAND}}


def efficiency_for(eff: dict, *, power_w: float | None, temp_c: float | None) -> float | None:
    """The efficiency to plan with at this power and temperature: the band's median when it
    has enough cycles, else the overall median (None with no cycles)."""
    key = f"{_band(power_w, POWER_BANDS_W)}:{_band(temp_c, TEMP_BANDS_C)}"
    band = (eff.get("bands") or {}).get(key)
    return band["median"] if band else eff.get("median")
