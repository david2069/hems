"""hems.loads (#612 §5): HEMS setup pre-fills (always-on load, usage periods), heavy loads
with their measurement, control types and EV charger, checked against the bridge's declared
capabilities; a load's expected hours; and the soak trigger."""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from hems import loads

SYD = ZoneInfo("Australia/Sydney")


def _hist(days=14):
    out = []
    t0 = dt.datetime(2026, 11, 2, tzinfo=SYD)                      # a Monday
    for d in range(days):
        for h in range(24):
            t = t0 + dt.timedelta(days=d, hours=h)
            kwh = 0.3 if h < 6 else 2.0 if 17 <= h <= 20 else 0.8
            out.append({"start": int(t.timestamp()), "kwh": kwh})
    return out


def test_setup_is_opt_in_with_the_notice_and_prefills_to_confirm():
    assert loads.NOTICE == "Planning is only as accurate as your answers."
    a = loads.always_on(_hist(), tz=SYD)
    assert a == {"value_kw": pytest.approx(0.3), "confirm": True,
                 "basis": "overnight minimum, 00:00–06:00"}
    p = loads.usage_periods(_hist(), tz=SYD)
    assert p["confirm"] is True
    assert p["weekday"][18] == "high" and p["weekday"][3] == "low" and p["weekday"][12] == "medium"
    assert len(p["heatmap"]) == 7 and len(p["heatmap"][0]) == 24
    assert loads.always_on([], tz=SYD) is None


def test_measurement_best_first_sums_entities_and_converts_units():
    assert loads.MEASUREMENTS == ("ha_power", "ha_energy", "nameplate")
    assert loads.power_kw([("1500", "W"), (0.5, "kW")]) == pytest.approx(2.0)
    assert loads.energy_kwh([(1200, "Wh"), (0.3, "kWh"), (0.001, "MWh")]) == pytest.approx(2.5)
    assert loads.power_kw([("unavailable", "W")]) is None
    with pytest.raises(ValueError):
        loads.power_kw([(1, "furlongs")])


POOL = {"id": "pool", "device_type": "pool_pump",
        "measurement": {"kind": "ha_power", "entities": ["sensor.pool_power"]},
        "power_kw": 1.1, "duration_h": 4.0,
        "runs": [{"start": "10:00", "end": "14:00"}],
        "circuit": "smart_circuit", "control": {"type": "deferrable", "energy_kwh": 4.4,
                                                 "deadline": "18:00", "min_run_min": 60,
                                                 "max_starts": 2, "splittable": True},
        "switch": {"kind": "smart_circuit", "circuit": 1}}


def test_loads_are_checked_against_declared_capabilities():
    assert loads.check(POOL, {"smart_circuits", "battery_power_w"}) == []
    probs = loads.check(POOL, {"battery_power_w"})
    assert any("smart_circuits" in p for p in probs)
    shed = {**POOL, "control": {"type": "sheddable", "max_off_min": 30, "min_on_min": 10,
                                 "priority": 2}}
    assert loads.check(shed, {"smart_circuits"}) == []
    assert any("priority" in p for p in loads.check(
        {**shed, "control": {"type": "sheddable"}}, {"smart_circuits"}))
    soak = {**POOL, "control": {"type": "soak", "export_price_at_or_below": -1.0,
                                 "quota_kwh": 5.0, "price_penalty": 2.0}}
    assert loads.check(soak, {"smart_circuits"}) == []
    assert any("measurement" in p for p in loads.check({**POOL, "measurement": None},
                                                      {"smart_circuits"}))
    assert any("control type" in p for p in loads.check(
        {**POOL, "control": {"type": "magic"}}, {"smart_circuits"}))


def test_ev_charger_fields():
    ev = {"id": "ev", "device_type": loads.EV_CHARGER,
          "measurement": {"kind": "nameplate"}, "power_kw": 7.0, "circuit": "general",
          "ev": {"kw": 7.0, "phases": 1, "control_path": "ha_switch",
                 "soc_entity": "sensor.car_soc", "arrival": "18:00", "departure": "07:30",
                 "energy_needed_kwh": 20.0, "v2l": False, "v2h": False},
          "control": {"type": "deferrable", "energy_kwh": 20.0, "deadline": "07:30"},
          "switch": {"kind": "ha_switch", "entity": "switch.charger"}}
    assert loads.check(ev, {"battery_power_w"}) == []
    bad = {**ev, "ev": {"kw": 7.0}}
    assert any("departure" in p for p in loads.check(bad, set()))


def test_expected_hours_for_a_scheduled_load():
    day = dt.date(2026, 11, 17)
    exp = loads.expected_hourly(POOL, day, tz=SYD)
    starts = sorted(exp)
    assert len(starts) == 4 and all(v == pytest.approx(1.1) for v in exp.values())
    assert dt.datetime.fromtimestamp(starts[0], SYD).hour == 10
    weather = {**POOL, "runs": "follows_weather"}
    assert loads.expected_hourly(weather, day, tz=SYD) is None


def test_soak_triggers_and_quota():
    soak = {"type": "soak", "export_price_at_or_below": 0.0, "quota_kwh": 5.0,
            "price_penalty": 2.0}
    assert loads.soak(soak, export_price=-3.0)["on"] is True
    assert loads.soak(soak, export_price=5.0)["on"] is False
    assert loads.soak(soak, export_limited=True)["on"] is True
    assert loads.soak(soak, battery_full_exporting=True)["on"] is True
    used = loads.soak(soak, export_price=-3.0, used_kwh=5.0)
    assert used["on"] is False and "quota" in used["why"]
