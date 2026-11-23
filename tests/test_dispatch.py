"""hems.dispatch: the Smart Dispatch planner and the user's optimisation target (owner,
2026-11-24). A plan like FWHAI Smart Dispatch, driven by battery SoC and dynamic inputs
(price, solar, load), with the target and the utility's limits asked once a plan is entered."""
import datetime as dt
import json
import pathlib
import time
from zoneinfo import ZoneInfo

import pytest

from hems import dispatch

EX = pathlib.Path(__file__).parent / "outus-v1" / "examples" / "electricity"
AU = json.loads((EX / "au-tou-plan.json").read_text())
US = json.loads((EX / "us-tou-net-billing-plan.json").read_text())

BATTERY = {"capacity_kwh": 13.6, "soc_pct": 50.0, "max_charge_kw": 5.0,
           "max_discharge_kw": 5.0, "round_trip": 0.9}


def answers(objective, **kw):
    v = {"objective": objective, **dispatch.DEFAULTS[objective]}
    v.update(kw)
    return v


def steps(n=48, *, imp=0.30, exp=0.05, solar=0.0, load=1.0, start=1_800_000_000):
    """``imp``, ``exp``, ``solar``, ``load``: a value or a function of the step index."""
    f = lambda x, i: x(i) if callable(x) else x  # noqa: E731
    return [{"start": start + i * 1800, "duration_s": 1800, "import": f(imp, i),
             "export": f(exp, i), "solar_kw": f(solar, i), "load_kw": f(load, i)}
            for i in range(n)]


def tou(i):                       # cheap overnight, expensive 16:00–21:00 (steps 32–41)
    return 0.50 if 32 <= i < 42 else (0.10 if i < 12 else 0.25)


def sun(i):                       # 07:00–17:00 bell, 6 kW peak
    return max(0.0, 6.0 * (1 - abs(i - 24) / 10)) if 14 <= i <= 34 else 0.0


# ── The target and the utility's limits ─────────────────────────────────────────────────

def test_objectives_have_labels():
    assert set(dispatch.OBJECTIVES) == {"backup", "tou", "lowest_cost", "self_consumption",
                                        "export_earnings"}
    assert all(dispatch.OBJECTIVES.values())


def test_plan_facts_from_the_au_plan():
    f = dispatch.plan_facts(AU)
    assert f["two_way"] and f["demand_charges"] and f["tou_bands"] and f["export_penalty"]
    assert not f["dynamic"]


def test_plan_facts_from_the_us_plan():
    f = dispatch.plan_facts(US)
    assert f["two_way"] and f["tou_bands"]
    assert not f["demand_charges"] and not f["export_penalty"]


def test_a_utility_plan_asks_the_target_and_limits():
    qs = {q["key"]: q for q in dispatch.questions(AU, solar_kwp=6.6)}
    for k in ("objective", "min_soc_pct", "max_soc_pct", "grid_charge", "export",
              "battery_export", "demand_aware"):
        assert k in qs, k
    assert {c["value"] for c in qs["grid_charge"]["choices"]} == {"allowed", "limited",
                                                                   "prohibited"}
    assert {c["value"] for c in qs["export"]["choices"]} == {"allowed", "limited", "prohibited"}
    assert qs["objective"]["please_confirm"]
    assert "export_penalty" in qs                     # an informational line with its why


def test_no_demand_question_without_demand_charges():
    keys = {q["key"] for q in dispatch.questions(US, solar_kwp=None)}
    assert "demand_aware" not in keys


def test_limited_asks_its_kw():
    qs = {q["key"] for q in dispatch.questions(AU, values={"grid_charge": "limited",
                                                           "export": "limited"})}
    assert {"grid_charge_kw", "export_kw"} <= qs


def test_no_export_questions_without_solar_or_export_tariff():
    plan = json.loads(json.dumps(US))
    plan["tariffs"] = [t for t in plan["tariffs"] if t["applies_to"] != "export"]
    keys = {q["key"] for q in dispatch.questions(plan, solar_kwp=None)}
    assert "export" not in keys and "battery_export" not in keys


def test_check_answers():
    assert dispatch.check(answers("lowest_cost")) == []
    bad = dict(answers("lowest_cost", min_soc_pct=90, max_soc_pct=40, grid_charge="limited",
                       grid_charge_kw=None), objective="cheapest")
    issues = " ".join(dispatch.check(bad))
    assert "objective" in issues and "min" in issues and "grid_charge_kw" in issues


# ── Prices per step from a plan ──────────────────────────────────────────────────────────

def test_step_prices_follow_the_au_bands_and_credits():
    tz = ZoneInfo(AU["timezone"])
    start = dt.datetime(2026, 7, 6, 0, 0, tzinfo=tz).timestamp()   # a Monday in July
    sp = dispatch.step_prices(AU, start, 48)
    at = {dt.datetime.fromtimestamp(s["start"], tz).strftime("%H:%M"): s for s in sp}
    assert at["17:00"]["import"] == pytest.approx(0.52)
    assert at["11:00"]["import"] == pytest.approx(0.12)
    assert at["03:00"]["import"] == pytest.approx(0.22)
    assert at["03:00"]["export"] == pytest.approx(0.05)
    assert at["17:30"]["export"] == pytest.approx(0.15)              # feed-in + bonus
    assert at["11:00"]["export"] == pytest.approx(0.05 - 0.012)      # export charge window
    assert at["17:00"]["demand_rate"] == pytest.approx(0.45)
    assert at["11:00"]["demand_rate"] == 0.0


# ── The planner ──────────────────────────────────────────────────────────────────────────

def test_soc_never_leaves_the_users_band():
    r = dispatch.plan_24h(steps(imp=tou, solar=sun), BATTERY,
                          answers("lowest_cost", min_soc_pct=30, max_soc_pct=90,
                                  grid_charge="allowed"))
    socs = [s["soc_pct"] for s in r["steps"]]
    assert min(socs) >= 30 - 1e-9 and max(socs) <= 90 + 1e-9


def test_tou_spread_charges_cheap_and_covers_the_peak():
    r = dispatch.plan_24h(steps(imp=tou), BATTERY, answers("lowest_cost", grid_charge="allowed"))
    acts = [s["action"] for s in r["steps"]]
    assert "charge" in acts[:12]
    peak = r["steps"][32:42]
    assert sum(s["grid_import_kwh"] for s in peak) < 0.5
    assert r["saving"] > 0


def test_grid_charge_prohibited_never_charges_from_grid():
    r = dispatch.plan_24h(steps(imp=tou, solar=sun), BATTERY, answers("lowest_cost"))
    assert all(s["grid_charge_kw"] <= 1e-6 for s in r["steps"])
    assert "charge" not in [s["action"] for s in r["steps"]]


def test_grid_charge_limited_by_size():
    r = dispatch.plan_24h(steps(imp=tou), BATTERY,
                          answers("lowest_cost", grid_charge="limited", grid_charge_kw=2.0))
    assert max(s["grid_charge_kw"] for s in r["steps"]) <= 2.0 + 1e-6


def test_battery_export_off_never_exports_from_the_battery():
    r = dispatch.plan_24h(steps(exp=lambda i: 1.0 if i == 36 else 0.05), BATTERY,
                          answers("lowest_cost"))
    assert all(s["battery_export_kw"] <= 1e-6 for s in r["steps"])


def test_export_earnings_exports_into_a_price_spike():
    r = dispatch.plan_24h(steps(exp=lambda i: 1.0 if i == 36 else 0.05), BATTERY,
                          answers("export_earnings"))
    assert r["steps"][36]["action"] == "discharge"
    assert r["steps"][36]["battery_export_kw"] > 1.0


def test_export_prohibited_exports_nothing():
    r = dispatch.plan_24h(steps(exp=lambda i: 1.0 if i == 36 else 0.05, solar=sun), BATTERY,
                          answers("export_earnings", export="prohibited"))
    assert all(s["grid_export_kwh"] <= 1e-6 for s in r["steps"])


def test_export_limit_caps_export():
    r = dispatch.plan_24h(steps(solar=sun, load=0.5), BATTERY,
                          answers("lowest_cost", export="limited", export_kw=1.5))
    assert max(s["grid_export_kwh"] for s in r["steps"]) <= 1.5 * 0.5 + 1e-6


def test_negative_feed_in_stores_solar_and_never_exports_the_battery_into_it():
    exp = lambda i: -0.10 if 14 <= i <= 34 else 0.20          # noqa: E731
    r = dispatch.plan_24h(steps(solar=sun, load=0.5, exp=exp), dict(BATTERY, soc_pct=20.0),
                          answers("export_earnings"))
    assert all(s["battery_export_kw"] <= 1e-6 for s in r["steps"][14:35])
    assert max(s["soc_pct"] for s in r["steps"]) >= 95


def test_demand_charge_shaves_the_peak():
    st = steps(load=lambda i: 6.0 if 34 <= i < 38 else 1.0)
    for s in st[32:42]:
        s["demand_rate"] = 5.0
    full = dict(BATTERY, soc_pct=100.0)
    flat = dispatch.plan_24h(st, full, answers("tou", min_soc_pct=10))
    shaved = dispatch.plan_24h(st, full, answers("lowest_cost", min_soc_pct=10))
    peak = lambda r: max(s["grid_import_kwh"] for s in r["steps"][32:42]) * 2  # noqa: E731
    assert peak(shaved) <= 1.0 + 1e-6
    assert peak(shaved) <= peak(flat)


def test_backup_only_keeps_the_battery_full():
    r = dispatch.plan_24h(steps(imp=tou, solar=sun), dict(BATTERY, soc_pct=80.0),
                          answers("backup"))
    assert all(s["battery_kw"] >= -1e-9 for s in r["steps"])
    assert r["steps"][-1]["soc_pct"] >= 80.0
    assert dispatch.decide(r, r["steps"][0]["start"])["action"] in ("hold", "self", "charge")


def test_off_grid_plans_no_grid_actions():
    r = dispatch.plan_24h(steps(imp=tou), BATTERY, answers("lowest_cost", grid_charge="allowed"),
                          state={"off_grid": True})
    assert {s["action"] for s in r["steps"]} == {"self"}
    assert "off grid" in r["steps"][0]["reason"].lower()


def test_never_worse_than_self_consumption():
    for obj in ("tou", "lowest_cost", "export_earnings"):
        r = dispatch.plan_24h(steps(imp=tou, solar=sun, exp=0.08), BATTERY,
                              answers(obj, grid_charge="allowed"))
        assert r["net_cost"] <= r["baseline"]["net_cost"] + 1e-6, obj


def test_wear_blocks_a_thin_spread():
    thin = lambda i: 0.30 if 32 <= i < 42 else 0.27          # noqa: E731
    b = dict(BATTERY, wear_per_kwh=0.10)
    r = dispatch.plan_24h(steps(imp=thin), b, answers("lowest_cost", grid_charge="allowed"))
    assert "charge" not in [s["action"] for s in r["steps"]]


def test_self_consumption_ignores_prices():
    r = dispatch.plan_24h(steps(imp=tou, solar=sun), BATTERY, answers("self_consumption"))
    assert "charge" not in [s["action"] for s in r["steps"]]
    assert all(s["battery_export_kw"] <= 1e-6 for s in r["steps"])


def test_deterministic_and_fast():
    args = (steps(imp=tou, solar=sun), BATTERY, answers("lowest_cost", grid_charge="allowed"))
    t = time.perf_counter()
    a = dispatch.plan_24h(*args)
    assert time.perf_counter() - t < 0.5
    assert a == dispatch.plan_24h(*args)


def test_stored_value_and_cost_per_10pct():
    r = dispatch.plan_24h(steps(imp=tou), BATTERY, answers("lowest_cost", grid_charge="allowed"))
    assert r["stored_value_per_kwh"] > 0
    assert r["cost_per_10pct"] == pytest.approx(r["stored_value_per_kwh"] * 1.36, rel=1e-3)


def test_each_step_has_a_reason_and_the_result_its_assumptions():
    r = dispatch.plan_24h(steps(imp=tou), BATTERY, answers("lowest_cost", grid_charge="allowed"))
    assert all(s["reason"] for s in r["steps"])
    assert r["assumptions"]


# ── Decide now ───────────────────────────────────────────────────────────────────────────

def test_decide_returns_the_step_now_and_how_long_it_runs():
    r = dispatch.plan_24h(steps(imp=tou), BATTERY, answers("lowest_cost", grid_charge="allowed"))
    d = dispatch.decide(r, r["steps"][0]["start"] + 60)
    assert d["action"] == r["steps"][0]["action"]
    assert d["until"] > d["start"]
    assert 0 <= d["target_soc_pct"] <= 100 and d["reason"]


def test_decide_outside_the_plan_is_self():
    r = dispatch.plan_24h(steps(), BATTERY, answers("lowest_cost"))
    assert dispatch.decide(r, 0)["action"] == "self"


def test_backup_only_stores_solar_as_soon_as_it_can():
    r = dispatch.plan_24h(steps(imp=tou, solar=sun, exp=0.20), dict(BATTERY, soc_pct=40.0),
                          answers("backup"))
    first_sun = next(i for i in range(48) if sun(i) > 1.0)
    assert r["steps"][first_sun]["battery_kw"] > 0


def test_a_large_battery_is_not_noise():
    big = dict(BATTERY, capacity_kwh=27.2, max_charge_kw=10.0, max_discharge_kw=10.0)
    r = dispatch.plan_24h(steps(solar=sun), big, answers("self_consumption"))
    assert {s["action"] for s in r["steps"]} == {"self"}
