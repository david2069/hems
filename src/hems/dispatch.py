"""Smart Dispatch (#612 §7): the user's optimisation target, prices per step from an OUTUS
plan, a 24-hour battery plan and the action now.

- **The target is the user's.** Once a utility plan is entered, ``questions`` asks the target
  (Emergency Backup only, TOU, lowest cost, self-consumption, most export earnings), the SoC
  band for it and the utility's limits (grid charging and export: allowed, limited to a size,
  or prohibited). Pre-filled answers are marked "please confirm".
- **The planner** is dynamic programming over SoC (1 % steps, 30-minute steps by default):
  exact for the discretised problem, deterministic, pure Python. Each step's cost is what is
  minimised for the target: import − export at the step's prices, wear per kWh discharged,
  the demand charge on the window's peak (when shaving), less the value of the energy left
  at the end. The plan is never worse than self-consumption: if it would be, self-consumption
  is the plan.
- **Actions** are relative to self-consumption, which the aGate does on its own: ``self``
  (nothing to do), ``charge`` (from the grid), ``discharge`` (export from the battery) and
  ``hold`` (keep the battery's energy, or let solar export now).

No I/O: the caller supplies the steps (prices, solar and load forecasts) and the battery.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from typing import Any
from zoneinfo import ZoneInfo

from .billing import _direction, _interval_rate, _sign, _tariffs
from .prices import PriceBook
from .windows import HolidayCheck, in_window

#: The user's optimisation targets, value → label.
OBJECTIVES = {
    "backup": "Emergency Backup only: keep the battery charged for outages",
    "tou": "Follow my time-of-use rates",
    "lowest_cost": "Lowest bill, including demand charges",
    "self_consumption": "Self-consumption: use my own solar and battery first",
    "export_earnings": "Most export earnings, with low grid charges",
}

LIMITS = {"allowed": "Allowed", "limited": "Limited to a size (kW)", "prohibited": "Prohibited"}

#: Each target's pre-filled answers (please confirm). Grid charging is off until the user
#: turns it on; only "most export earnings" exports from the battery.
DEFAULTS = {
    "backup": {"min_soc_pct": 20.0, "max_soc_pct": 100.0, "grid_charge": "prohibited",
               "grid_charge_kw": None, "export": "allowed", "export_kw": None,
               "battery_export": False, "demand_aware": False},
    "tou": {"min_soc_pct": 20.0, "max_soc_pct": 100.0, "grid_charge": "prohibited",
            "grid_charge_kw": None, "export": "allowed", "export_kw": None,
            "battery_export": False, "demand_aware": False},
    "lowest_cost": {"min_soc_pct": 20.0, "max_soc_pct": 100.0, "grid_charge": "prohibited",
                    "grid_charge_kw": None, "export": "allowed", "export_kw": None,
                    "battery_export": False, "demand_aware": True},
    "self_consumption": {"min_soc_pct": 10.0, "max_soc_pct": 100.0, "grid_charge": "prohibited",
                         "grid_charge_kw": None, "export": "allowed", "export_kw": None,
                         "battery_export": False, "demand_aware": False},
    "export_earnings": {"min_soc_pct": 20.0, "max_soc_pct": 100.0, "grid_charge": "prohibited",
                        "grid_charge_kw": None, "export": "allowed", "export_kw": None,
                        "battery_export": True, "demand_aware": True},
}

#: Below this (kW) a step's difference from self-consumption is noise: the action is ``self``.
DEADBAND_KW = 0.2
#: Demand caps are searched in steps of this (kW).
DEMAND_STEP_KW = 0.25

_EPS = 1e-9
_INF = float("inf")


# ── The plan's facts and the questions ───────────────────────────────────────────────────

def _all_charges(plan: dict) -> list[tuple[dict, dict]]:
    out = [({}, c) for c in plan.get("charges") or []]
    for t in _tariffs(plan):
        out += [(t, c) for c in t.get("charges") or []]
    return out


def plan_facts(plan: dict | None) -> dict[str, bool]:
    """What the utility plan says about dispatch: two-way (it pays or charges for export),
    an export credit, an export penalty (an export charge, or prices that can go negative),
    demand charges, time-of-use bands, dynamic (series) prices."""
    f = dict.fromkeys(("two_way", "export_credit", "export_penalty", "demand_charges",
                       "tou_bands", "dynamic"), False)
    for t, c in _all_charges(plan or {}):
        export = _direction(t, c) == "export"
        rates = [r.get("rate") for r in c.get("rates") or []]
        if export:
            f["two_way"] = True
            credit = _sign(c) < 0
            f["export_credit"] |= credit
            f["export_penalty"] |= not credit or any((r or 0) < 0 for r in rates)
        if c.get("basis") == "demand":
            f["demand_charges"] = True
        if c.get("class") == "interval" and (c.get("basis") == "value_table"
                                             or len({r for r in rates if r is not None}) > 1):
            f["tou_bands"] = True
        if (t.get("price_source") or {}).get("kind") == "series":
            f["dynamic"] = True
            if export or t.get("applies_to") == "export":
                f["export_penalty"] = True
    return f


def _default_objective(facts: dict) -> str:
    if facts["demand_charges"]:
        return "lowest_cost"
    if facts["tou_bands"] or facts["dynamic"]:
        return "tou"
    return "self_consumption"


def questions(plan: dict | None = None, *, solar_kwp: float | None = None,
              values: dict | None = None) -> list[dict]:
    """The questions to ask once a utility plan is entered, in order, each with its value
    (the user's, else pre-filled and ``please_confirm``) and why it is asked. Questions that
    don't apply to this plan or site are left out."""
    facts = plan_facts(plan)
    given = dict(values or {})
    objective = given.get("objective") or _default_objective(facts)
    pre = {"objective": objective, "solar_kwp": solar_kwp,
           **DEFAULTS.get(objective, DEFAULTS["self_consumption"])}
    if not (facts["two_way"] or solar_kwp):
        pre["export"] = "prohibited"

    def q(key: str, label: str, kind: str, why: str, **kw: Any) -> dict:
        out = {"key": key, "label": label, "kind": kind, "why": why}
        if kind != "info":
            out["value"] = given.get(key, pre.get(key))
            out["please_confirm"] = key not in given
        return {**out, **kw}

    limits = [{"value": k, "label": v} for k, v in LIMITS.items()]
    out = [q("objective", "What should the battery optimise for?", "choice",
             "The planner's goal; every step's cost follows it.",
             choices=[{"value": k, "label": v} for k, v in OBJECTIVES.items()]),
           q("min_soc_pct", "Lowest charge to keep (reserve)", "number",
             "Never planned below this, so backup is always there.", unit="%"),
           q("max_soc_pct", "Highest charge to plan to", "number",
             "Never charged above this by the planner.", unit="%"),
           q("solar_kwp", "Solar size", "number",
             "Solar sets how much the battery can store for free; leave blank for no solar.",
             unit="kWp"),
           q("grid_charge", "Charging the battery from the grid", "choice",
             "Some utilities and incentives forbid or cap it.", choices=limits)]
    grid = out[-1]["value"]
    if grid == "limited":
        out.append(q("grid_charge_kw", "Most grid power into the battery", "number",
                     "The cap your utility or installer set.", unit="kW"))
    if facts["two_way"] or solar_kwp:
        out.append(q("export", "Exporting to the grid", "choice",
                     "Your connection may forbid or cap export.", choices=limits))
        exp = out[-1]["value"]
        if exp == "limited":
            out.append(q("export_kw", "Most export power", "number",
                         "Your connection's export limit.", unit="kW"))
        if exp != "prohibited":
            out.append(q("battery_export", "Export from the battery", "bool",
                         "Lets the battery sell to the grid when export pays more than "
                         "keeping the energy."))
    if facts["demand_charges"]:
        out.append(q("demand_aware", "Shave peaks in demand-charge windows", "bool",
                     "Your plan charges for the highest demand in its windows; the battery "
                     "can cover those peaks."))
    if facts["export_penalty"]:
        out.append(q("export_penalty", "Export penalties", "info",
                     "Your plan charges for some export, or its export price can go negative; "
                     "the planner stores solar instead of exporting into it."))
    return out


def check(values: dict) -> list[str]:
    """Problems with the answers. Empty = fine."""
    out = []
    if values.get("objective") not in OBJECTIVES:
        out.append(f"objective: one of {', '.join(OBJECTIVES)}")
    lo, hi = values.get("min_soc_pct"), values.get("max_soc_pct")
    for k, v in (("min_soc_pct", lo), ("max_soc_pct", hi)):
        if not isinstance(v, (int, float)) or not 0 <= v <= 100:
            out.append(f"{k}: 0 to 100")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo > hi:
        out.append("min SoC is above max SoC")
    for k in ("grid_charge", "export"):
        v = values.get(k, "allowed")
        if v not in LIMITS:
            out.append(f"{k}: one of {', '.join(LIMITS)}")
        elif v == "limited" and not (isinstance(values.get(f"{k}_kw"), (int, float))
                                     and values[f"{k}_kw"] > 0):
            out.append(f"{k}_kw: needed when {k.replace('_', ' ')} is limited")
    return out


# ── Prices per step ──────────────────────────────────────────────────────────────────────

def step_prices(plan: dict, start_ts: float, n: int, step_s: int = 1800,
                prices: PriceBook | None = None, is_holiday: HolidayCheck | None = None
                ) -> list[dict]:
    """Per step: the import price (all import interval charges), the export value (credits
    less export charges, including an export charge's window; its free band is ignored, so
    the penalty is conservative), the band, and the demand rate ($/kW) when a demand charge's
    window covers the step."""
    tz = ZoneInfo(plan.get("timezone") or "UTC")
    out = []
    for i in range(n):
        ts = start_ts + i * step_s
        local = dt.datetime.fromtimestamp(ts, tz)
        imp = exp = demand = 0.0
        band = None
        for t, c in _all_charges(plan):
            if t.get("applies_to") in ("controlled_load", "ev"):
                continue
            if not in_window(c.get("window"), local, is_holiday):
                continue
            export = _direction(t, c) == "export"
            if c.get("class") == "interval":
                rate, b, _kind = _interval_rate(t, c, local, ts, prices, is_holiday)
                if rate is None:
                    continue
                if export:
                    exp -= _sign(c) * rate
                else:
                    imp += _sign(c) * rate
                    band = band or b
            elif c.get("basis") == "demand":
                demand += float(c.get("rate") or 0.0)
            elif export and c.get("basis") == "excess_over_free_band":
                exp -= float(c.get("rate") or 0.0)
        out.append({"start": int(ts), "duration_s": step_s, "import": round(imp, 6),
                    "export": round(exp, 6), "band": band, "demand_rate": demand})
    return out


# ── The planner ──────────────────────────────────────────────────────────────────────────

class _Model:
    """Everything one plan needs, worked out once."""

    def __init__(self, steps: list[dict], battery: dict, values: dict, state: dict,
                 soc_step_pct: float) -> None:
        self.steps = steps
        self.obj = values.get("objective") or "self_consumption"
        d = DEFAULTS.get(self.obj, DEFAULTS["self_consumption"])
        v = {**d, **{k: x for k, x in values.items() if x is not None}}
        self.cap = float(battery["capacity_kwh"])
        self.eta = math.sqrt(float(battery.get("round_trip") or 0.9))
        self.pc = float(battery.get("max_charge_kw") or 5.0)
        self.pd = float(battery.get("max_discharge_kw") or 5.0)
        self.wear = 0.0 if self.obj == "self_consumption" else float(
            battery.get("wear_per_kwh") or 0.0)
        self.soc0 = float(battery.get("soc_pct") or 0.0)
        self.min_soc, self.max_soc = float(v["min_soc_pct"]), float(v["max_soc_pct"])
        self.grid_charge, self.grid_charge_kw = v["grid_charge"], v.get("grid_charge_kw")
        self.export, self.export_kw = v["export"], v.get("export_kw")
        self.battery_export = bool(v["battery_export"]) and self.export != "prohibited"
        self.no_discharge = self.obj == "backup"
        self.demand_aware = bool(v["demand_aware"]) and self.obj in ("lowest_cost",
                                                                     "export_earnings")
        self.peak0 = float(state.get("demand_peak_kw") or 0.0)
        self.step_pct = float(soc_step_pct)
        self.step_kwh = self.cap * self.step_pct / 100.0
        # What is minimised: money, except self-consumption (kWh imported).
        self.pi = [1.0 if self.obj == "self_consumption" else float(s["import"]) for s in steps]
        self.pe = [0.0 if self.obj == "self_consumption" else float(s["export"]) for s in steps]
        self.rate = [float(s.get("demand_rate") or 0.0) if self.demand_aware else 0.0
                     for s in steps]
        typical = statistics.median(self.pi) if self.pi else 0.0
        self.v_end = (max(self.pi, default=1.0) * 10 if self.no_discharge else typical) * self.eta
        # Backup only: energy stored sooner is worth more than any price, so the battery
        # fills as early as it can (an outage can come at any time).
        self.hold = (max(map(abs, self.pi + self.pe), default=1.0) + 1.0
                     if self.no_discharge else 0.0)

    def h(self, t: int) -> float:
        return float(self.steps[t].get("duration_s") or 1800) / 3600.0

    def flows(self, t: int, de: float) -> dict | None:
        """A step's flows for a battery energy change ``de`` (kWh); None if not allowed."""
        s, h = self.steps[t], self.h(t)
        b = (de / self.eta if de > 0 else de * self.eta) / h
        surplus = float(s.get("solar_kw") or 0.0) - float(s.get("load_kw") or 0.0)
        if b < 0:
            # A discharge can overshoot by under one SoC step: deliver only what is allowed
            # and let the rest go (SoC steps are coarser than the home's load).
            floor = -self.pd
            if not (self.battery_export and self.pe[t] > 0):
                floor = max(floor, -max(0.0, -surplus))
            if b < floor - _EPS:
                if b < floor - self.step_kwh * self.eta / h - _EPS:
                    return None
                b = floor
        if b > self.pc + _EPS or (self.no_discharge and b < -_EPS):
            return None
        grid_charge = max(0.0, b - max(0.0, surplus))
        if self.grid_charge == "prohibited" and grid_charge > _EPS:
            return None
        if self.grid_charge == "limited" and grid_charge > float(self.grid_charge_kw) + _EPS:
            return None
        batt_export = max(0.0, -b - max(0.0, -surplus))
        if batt_export > _EPS and not self.battery_export:
            return None
        net = -surplus + b
        imp, exp = max(0.0, net), max(0.0, -net)
        if self.export == "prohibited":
            sent = 0.0
        elif self.export == "limited":
            if batt_export > _EPS and exp > float(self.export_kw) + _EPS:
                return None
            sent = min(exp, float(self.export_kw))
        else:
            sent = exp
        return {"b": b, "imp": imp, "sent": sent, "curtailed": exp - sent,
                "grid_charge": grid_charge, "batt_export": batt_export, "surplus": surplus}

    def cost(self, t: int, de: float, f: dict, cap_kw: float | None) -> float:
        h = self.h(t)
        c = f["imp"] * h * self.pi[t] - f["sent"] * h * self.pe[t] + self.wear * max(0.0, -de)
        if cap_kw is not None and self.rate[t] > 0 and f["imp"] > cap_kw + _EPS:
            return _INF
        return c

    def demand(self, peak: float) -> float:
        return max(self.rate, default=0.0) * max(0.0, peak - self.peak0)

    def solve(self, cap_kw: float | None) -> tuple[float, list[int], list[float]]:
        """DP over SoC states; returns (score, path of state indices, V at t=0)."""
        n = int(round(100.0 / self.step_pct))
        lo = math.ceil(self.min_soc / self.step_pct - _EPS)
        hi = math.floor(self.max_soc / self.step_pct + _EPS)
        i0 = max(0, min(n, int(round(self.soc0 / self.step_pct))))
        smin, smax = min(lo, i0), max(hi, i0)
        dmax = int(math.ceil(max(self.pc * self.eta, self.pd / self.eta)
                             * max((self.h(t) for t in range(len(self.steps))), default=0.5)
                             / self.step_kwh)) + 1
        T = len(self.steps)
        V = [-self.v_end * i * self.step_kwh if smin <= i <= smax else _INF
             for i in range(n + 1)]
        choice: list[list[int]] = []
        for t in range(T - 1, -1, -1):
            costs = {}
            for d in range(-dmax, dmax + 1):
                f = self.flows(t, d * self.step_kwh)
                if f is not None:
                    c = self.cost(t, d * self.step_kwh, f, cap_kw)
                    if c < _INF:
                        costs[d] = c
            Vt = [_INF] * (n + 1)
            ct = [0] * (n + 1)
            for s1 in range(smin, smax + 1):
                best, bd = _INF, 0
                for d, c in costs.items():
                    s2 = s1 + d
                    if s2 < smin or s2 > smax:
                        continue
                    if (s2 < lo and d < 0) or (s2 > hi and d > 0):
                        continue
                    x = c + V[s2] - self.hold * s2 * self.step_kwh
                    if x < best - 1e-12:
                        best, bd = x, d
                Vt[s1], ct[s1] = best, bd
            V = Vt
            choice.append(ct)
        choice.reverse()
        path, s = [i0], i0
        if V[i0] < _INF:
            for t in range(T):
                s += choice[t][s]
                path.append(s)
        return V[i0], path, V

    def native(self, e: float, t: int) -> float:
        """Self-consumption's energy change (kWh) from ``e``: store surplus solar, cover the
        deficit, within the SoC band and power limits."""
        s, h = self.steps[t], self.h(t)
        surplus = float(s.get("solar_kw") or 0.0) - float(s.get("load_kw") or 0.0)
        emin, emax = self.cap * self.min_soc / 100, self.cap * self.max_soc / 100
        if surplus > 0:
            return max(0.0, min(surplus * h, self.pc * h, max(0.0, emax - e) / self.eta)
                       * self.eta)
        if self.no_discharge:
            return 0.0
        return -max(0.0, min(-surplus * h, self.pd * h, max(0.0, e - emin) * self.eta)
                    / self.eta)


def _evaluate(m: _Model, es: list[float], native: bool) -> dict:
    """Score and steps for a path of battery energies (kWh) ``es`` (len T + 1)."""
    rows, score, money, peak = [], 0.0, 0.0, 0.0
    for t, s in enumerate(m.steps):
        de = es[t + 1] - es[t]
        f = m.flows(t, de) if not native else None
        if f is None:            # self-consumption: its own flows, never refused
            h = m.h(t)
            b = (de / m.eta if de > 0 else de * m.eta) / h
            surplus = float(s.get("solar_kw") or 0.0) - float(s.get("load_kw") or 0.0)
            net = -surplus + b
            exp = max(0.0, -net)
            sent = 0.0 if m.export == "prohibited" else (
                min(exp, float(m.export_kw)) if m.export == "limited" else exp)
            f = {"b": b, "imp": max(0.0, net), "sent": sent, "curtailed": exp - sent,
                 "grid_charge": 0.0, "batt_export": 0.0, "surplus": surplus}
        h = m.h(t)
        score += (f["imp"] * h * m.pi[t] - f["sent"] * h * m.pe[t] + m.wear * max(0.0, -de)
                  - m.hold * es[t + 1])
        cost = f["imp"] * h * float(s["import"]) - f["sent"] * h * float(s["export"])
        money += cost
        if m.rate[t] > 0:
            peak = max(peak, f["imp"])
        rows.append({"start": s["start"], "duration_s": s.get("duration_s") or 1800,
                     "e0": es[t], "de": de, **f, "cost": cost,
                     "soc_pct": round(es[t + 1] / m.cap * 100, 4)})
    left = (es[-1] - es[0]) * m.v_end
    score += m.demand(peak) - left
    return {"rows": rows, "score": score, "money": money, "peak": peak}


def _label(m: _Model, t: int, r: dict, future_max: float) -> tuple[str, str]:
    s = m.steps[t]
    pi, pe = float(s["import"]), float(s["export"])
    nat = m.native(r["e0"], t)
    b_nat = (nat / m.eta if nat > 0 else nat * m.eta) / m.h(t)
    diff = r["b"] - b_nat
    # One SoC step of charge is coarser than DEADBAND_KW on a large battery: not a decision.
    band = max(DEADBAND_KW, m.step_kwh / m.eta / m.h(t) + _EPS)
    if m.obj == "backup" and abs(diff) <= band and r["b"] >= 0:
        return "self", "Emergency Backup only: keep the battery charged"
    if abs(diff) <= band:
        return "self", "Self-consumption: the battery follows the home and solar"
    if diff > 0 and r["grid_charge"] > DEADBAND_KW:
        return "charge", (f"Charge from the grid at {pi:.2f}/kWh; later it costs up to "
                          f"{future_max:.2f}/kWh")
    if diff < 0 and r["batt_export"] > DEADBAND_KW:
        return "discharge", f"Export from the battery at {pe:.2f}/kWh"
    if m.obj == "backup":
        return "hold", "Emergency Backup only: keep the battery charged"
    if r["surplus"] > 0 and diff < 0:
        return "hold", f"Let solar export now at {pe:.2f}/kWh; store it later"
    why = (f"keep the demand peak at {r['imp']:.1f} kW or less"
           if m.rate[t] > 0 and m.demand_aware else
           f"its energy is worth up to {future_max:.2f}/kWh later")
    return "hold", f"Hold the battery: {why}"


def plan_24h(steps: list[dict], battery: dict, values: dict, *, state: dict | None = None,
             soc_step_pct: float = 1.0) -> dict:
    """The plan for ``steps`` (each: start, duration_s, import and export prices, solar_kw,
    load_kw, optional demand_rate) for ``battery`` (capacity_kwh, soc_pct, max_charge_kw,
    max_discharge_kw, round_trip, wear_per_kwh) and the user's answers ``values``.
    ``state``: off_grid, demand_peak_kw (the period's peak so far)."""
    state = state or {}
    m = _Model(steps, battery, values, state, soc_step_pct)
    T = len(steps)
    e = m.cap * m.soc0 / 100
    base_es = [e]
    for t in range(T):
        base_es.append(base_es[-1] + m.native(base_es[-1], t))
    base = _evaluate(m, base_es, native=True)

    assumptions = [
        f"Battery {m.cap:g} kWh, {m.pc:g} kW charge / {m.pd:g} kW discharge, "
        f"round trip {m.eta ** 2:.0%}",
        f"SoC kept between {m.min_soc:g} % and {m.max_soc:g} %",
        f"Energy left at the end is valued at {m.v_end:.3f} per kWh",
    ]
    if m.obj == "self_consumption":
        assumptions.append("Self-consumption minimises kWh imported, not cost")
    if m.demand_aware and any(m.rate):
        assumptions.append(f"Demand: the period's peak so far is {m.peak0:g} kW; only a "
                           f"higher peak costs more")

    plan, V0, i0 = base, None, None
    if not state.get("off_grid"):
        best = None
        if m.demand_aware and any(m.rate):
            top = max(m.peak0, base["peak"])
            caps = [m.peak0 + k * DEMAND_STEP_KW
                    for k in range(int(math.ceil((top - m.peak0) / DEMAND_STEP_KW)) + 1)]

            def run(i: int):
                sc, path, V = m.solve(caps[i])
                return (sc + m.demand(caps[i]) if sc < _INF else _INF), path, V

            lo_i, hi_i = 0, len(caps) - 1          # the lowest cap that is feasible
            while lo_i < hi_i:
                mid = (lo_i + hi_i) // 2
                if m.solve(caps[mid])[0] < _INF:
                    hi_i = mid
                else:
                    lo_i = mid + 1
            a, b = lo_i, len(caps) - 1             # ternary search on the total
            memo: dict[int, Any] = {}
            get = lambda i: memo.setdefault(i, run(i))  # noqa: E731
            while b - a > 2:
                m1, m2 = a + (b - a) // 3, b - (b - a) // 3
                if get(m1)[0] <= get(m2)[0]:
                    b = m2
                else:
                    a = m1
            best = min((get(i) for i in range(a, b + 1)), key=lambda x: x[0])
        else:
            best = m.solve(None)
        score, path, V0 = best
        i0 = path[0]
        if score < _INF:
            dp = _evaluate(m, [i * m.step_kwh for i in path], native=False)
            if dp["score"] <= base["score"] + 1e-9:
                plan = dp

    out_steps = []
    for t, r in enumerate(plan["rows"]):
        future = max(m.pi[t + 1:], default=m.pi[t])
        if state.get("off_grid"):
            action, reason = "self", "Off grid: the aGate runs the home; no grid actions"
        elif plan is base:
            action, reason = "self", "Self-consumption is best for this step"
        else:
            action, reason = _label(m, t, r, future)
        h = m.h(t)
        out_steps.append({
            "start": r["start"], "duration_s": r["duration_s"], "action": action,
            "battery_kw": round(r["b"], 4), "soc_pct": r["soc_pct"],
            "grid_import_kwh": round(r["imp"] * h, 4),
            "grid_export_kwh": round(r["sent"] * h, 4),
            "curtailed_kwh": round(r["curtailed"] * h, 4),
            "grid_charge_kw": round(r["grid_charge"], 4),
            "battery_export_kw": round(r["batt_export"], 4),
            "import_price": steps[t]["import"], "export_price": steps[t]["export"],
            "cost": round(r["cost"], 4), "reason": reason})

    value = 0.0
    if V0 is not None and i0 is not None:
        up = V0[i0 + 1] if i0 + 1 < len(V0) else _INF
        down = V0[i0 - 1] if i0 > 0 else _INF
        if up < _INF:
            value = (V0[i0] - up) / m.step_kwh
        elif down < _INF:
            value = (down - V0[i0]) / m.step_kwh
    value = max(0.0, value)
    return {
        "objective": m.obj, "steps": out_steps,
        "cost": round(plan["money"], 4), "net_cost": round(plan["score"], 4),
        "baseline": {"cost": round(base["money"], 4), "net_cost": round(base["score"], 4),
                     "end_soc_pct": round(base_es[-1] / m.cap * 100, 4)},
        "saving": round(base["score"] - plan["score"], 4),
        "demand_peak_kw": round(plan["peak"], 4),
        "stored_value_per_kwh": round(value, 4),
        "cost_per_10pct": round(value * m.cap * 0.1, 4),
        "assumptions": assumptions,
    }


def decide(result: dict, now_ts: float) -> dict:
    """The action now: the step covering ``now_ts``, how long the same action runs, the SoC
    it aims for and why. Outside the plan: ``self``."""
    rows = result.get("steps") or []
    for i, s in enumerate(rows):
        if s["start"] <= now_ts < s["start"] + s["duration_s"]:
            j = i
            while j + 1 < len(rows) and rows[j + 1]["action"] == s["action"]:
                j += 1
            return {"action": s["action"], "start": s["start"],
                    "until": rows[j]["start"] + rows[j]["duration_s"],
                    "battery_kw": s["battery_kw"], "target_soc_pct": rows[j]["soc_pct"],
                    "reason": s["reason"]}
    return {"action": "self", "start": int(now_ts), "until": int(now_ts),
            "battery_kw": 0.0, "target_soc_pct": None, "reason": "No plan covers now"}
