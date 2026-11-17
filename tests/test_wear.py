"""hems.wear (#612 §6): warranty inputs as setup fields, the wear cost per kWh, the throughput
pace budget, dispatch events and round-trip efficiency from SoC-matched cycles."""
import datetime as dt

import pytest

from hems import wear

WARRANTY = {"warranty_end": "2036-01-01", "warranted_throughput_mwh": 43.0,
            "end_soh_pct": 70.0, "install_date": "2026-01-01", "replacement_value": 15000.0}


def test_every_warranty_input_is_a_setup_field_the_cloud_only_prefills():
    cloud = {"expirationTime": "2036-01-01", "throughput": 43, "remainThroughput": 40000}
    f = wear.setup_fields({}, cloud=cloud)
    assert f["warranty_end"] == {"value": "2036-01-01", "source": "cloud", "confirm": True}
    assert f["warranted_throughput_mwh"]["value"] == 43.0 and f["warranted_throughput_mwh"]["confirm"]
    # in no API: the warranty document's guaranteed SoH, the install date, the value
    assert f["end_soh_pct"] == {"value": None, "source": None, "confirm": True}
    # the user's answer wins and needs no confirming
    f = wear.setup_fields({"warranty_end": "2035-06-30"}, cloud=cloud)
    assert f["warranty_end"] == {"value": "2035-06-30", "source": "user", "confirm": False}
    # epoch milliseconds from the cloud read as a date
    ms = int(dt.datetime(2036, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert wear.setup_fields({}, cloud={"expirationTime": ms})["warranty_end"]["value"] == "2036-01-01"
    # works with no cloud at all
    assert wear.setup_fields(WARRANTY)["warranty_end"]["source"] == "user"


def test_wear_cost_per_kwh_explains_itself():
    c = wear.cost_per_kwh(WARRANTY)
    # (15000 − 15000 × 70 %) ÷ 43 000 kWh
    assert c["per_kwh"] == pytest.approx(4500 / 43000, abs=1e-6)
    assert "43,000 kWh" in c["formula"] and c["end_value"]["derived"]
    # an end-of-warranty value the user gave is used as is
    c = wear.cost_per_kwh({**WARRANTY, "end_value": 6000.0})
    assert c["per_kwh"] == pytest.approx(9000 / 43000, abs=1e-6) and not c["end_value"]["derived"]
    assert wear.cost_per_kwh({**WARRANTY, "replacement_value": None}) is None


def test_pace_budget_is_remaining_over_days_left_and_a_soft_penalty():
    p = wear.pace(remaining_kwh=36500.0, today=dt.date(2026, 1, 1), warranty_end="2036-01-01")
    assert p["days_left"] == 3652 and p["daily_kwh"] == pytest.approx(36500 / 3652, abs=1e-3)
    assert wear.pace(remaining_kwh=10.0, today=dt.date(2037, 1, 1), warranty_end="2036-01-01") is None
    # remaining without the cloud: warranted − discharged since install
    assert wear.remaining_kwh(WARRANTY, used_kwh=3000.0) == 40000.0
    assert wear.remaining_kwh(WARRANTY, used_kwh=3000.0, cloud_remaining_kwh=39000.0) == 39000.0
    # no penalty up to the pace, rising (not a stop) above it
    assert wear.pace_penalty(10.0, 10.0, 0.1) == 0
    a, b = wear.pace_penalty(12.0, 10.0, 0.1), wear.pace_penalty(14.0, 10.0, 0.1)
    assert 0 < a < b and b - a > a                                   # marginal cost rises
    assert wear.pace_ratio(used_kwh=390.0, days=30, daily_kwh=10.0) == pytest.approx(1.3)


def test_cycles_and_optional_daily_cap_and_usable_capacity_from_soh():
    assert wear.cycles(discharged_kwh=27.2, usable_kwh=13.6) == pytest.approx(2.0)
    assert wear.over_cap(2.0, cap=1.5) is True and wear.over_cap(2.0, cap=None) is None
    assert wear.usable_kwh(13.6, 95.0) == pytest.approx(12.92)
    assert wear.usable_kwh(13.6, None) == 13.6


def _run(tracker, seq, t0=0, step=60, **kw):
    out = []
    for i, (w, soc) in enumerate(seq):
        out += wear.add_sample(tracker, t0 + i * step, battery_w=w, soc=soc, **kw)
    return out


def test_dispatch_events_are_returned_never_stored():
    t = wear.new_tracker()
    # 30 min charging at 2 kW, then 30 min discharging at 3 kW, then idle
    seq = [(-2000, 50 + i * 0.25) for i in range(31)] + [(3000, 57.5 - i * 0.4) for i in range(31)]
    seq += [(0, 45.0)] * 20
    ev = _run(t, seq, source="schedule", battery_temp_c=25.0)
    assert [e["direction"] for e in ev] == ["charge", "discharge"]
    ch, dis = ev
    assert ch["energy_in_kwh"] == pytest.approx(1.0, rel=0.05) and ch["energy_out_kwh"] == 0
    assert dis["energy_out_kwh"] == pytest.approx(1.5, rel=0.05)
    assert ch["soc_start"] == 50 and dis["soc_end"] < dis["soc_start"]
    assert dis["source"] == "schedule" and dis["battery_temp_c"] == 25.0
    assert dis["mean_power_w"] == pytest.approx(3000, rel=0.01)
    with pytest.raises(ValueError):
        wear.add_sample(t, 10_000, battery_w=0, soc=40, source="nobody")


def _cycle_samples(eff, n_cycles, start=0, temp=20.0, power=2000.0, kwh=5.0):
    """Charge kwh then discharge kwh × eff, back to the same SoC, n times (hourly samples)."""
    out, t, chg, dis = [], start, 0.0, 0.0
    for _ in range(n_cycles):
        out.append({"ts": t, "soc": 40.0, "charged_kwh": chg, "discharged_kwh": dis,
                    "power_w": power, "temp_c": temp})
        chg += kwh
        t += 3 * 3600
        out.append({"ts": t, "soc": 80.0, "charged_kwh": chg, "discharged_kwh": dis,
                    "power_w": power, "temp_c": temp})
        dis += kwh * eff
        t += 3 * 3600
    out.append({"ts": t, "soc": 40.0, "charged_kwh": chg, "discharged_kwh": dis,
                "power_w": power, "temp_c": temp})
    return out


def test_round_trip_efficiency_from_soc_matched_cycles_and_rolling_median():
    cyc = wear.soc_matched_cycles(_cycle_samples(0.9, 6))
    assert len(cyc) == 6 and all(c["efficiency"] == pytest.approx(0.9) for c in cyc)
    # a short SoC wobble with little energy is not a cycle
    tiny = [{"ts": 0, "soc": 50, "charged_kwh": 0, "discharged_kwh": 0},
            {"ts": 600, "soc": 51, "charged_kwh": 0.1, "discharged_kwh": 0},
            {"ts": 1200, "soc": 50, "charged_kwh": 0.1, "discharged_kwh": 0.09}]
    assert wear.soc_matched_cycles(tiny) == []
    # rolling 30-day median: old cycles drop out; one outlier doesn't swing it
    old = wear.soc_matched_cycles(_cycle_samples(0.5, 5, start=0))
    new = wear.soc_matched_cycles(_cycle_samples(0.9, 6, start=40 * 86400))
    odd = wear.soc_matched_cycles(_cycle_samples(0.6, 1, start=45 * 86400))
    e = wear.efficiency(old + new + odd, now_ts=46 * 86400)
    assert e["median"] == pytest.approx(0.9) and e["n"] == 7


def test_efficiency_is_banded_by_power_and_temperature_once_there_is_enough_data():
    hot = wear.soc_matched_cycles(_cycle_samples(0.85, 6, temp=38.0, power=4500.0))
    mild = wear.soc_matched_cycles(_cycle_samples(0.92, 6, start=6 * 6 * 3600 * 2,
                                                  temp=20.0, power=1500.0))
    e = wear.efficiency(hot + mild, now_ts=10 * 86400)
    assert wear.efficiency_for(e, power_w=4400, temp_c=37) == pytest.approx(0.85)
    assert wear.efficiency_for(e, power_w=1400, temp_c=21) == pytest.approx(0.92)
    # a band with too few cycles falls back to the overall median
    few = wear.efficiency(hot[:2] + mild, now_ts=10 * 86400)
    assert wear.efficiency_for(few, power_w=4400, temp_c=37) == few["median"]
    assert wear.efficiency_for(wear.efficiency([], now_ts=0), power_w=1, temp_c=1) is None


def test_daily_record_carries_schema_version():
    r = wear.daily_record(dt.date(2026, 11, 17), soh_pct=95.6, used_kwh=3000.0,
                          remaining_kwh=40000.0)
    assert r == {"schema_version": wear.SCHEMA_VERSION, "date": "2026-11-17", "soh_pct": 95.6,
                 "used_kwh": 3000.0, "remaining_kwh": 40000.0}
