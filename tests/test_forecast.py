"""hems.forecast (#612 §4): solar from irradiance with cell-temperature loss and
self-calibration, the load model (calendar × season with heating and cooling around a learned
comfort band, cold start, declared loads taken out), the battery temperature limit, the
walk-forward backtest, and HAEO-format publishing. Plus the Open-Meteo adapter's parsing."""
import datetime as dt
import math
from zoneinfo import ZoneInfo

import pytest

from hems import forecast
from hems.adapters import open_meteo

SYD = ZoneInfo("Australia/Sydney")


def ts(*a):
    return int(dt.datetime(*a, tzinfo=SYD).timestamp())


# ── Solar ──────────────────────────────────────────────────────────────────────────────

def test_solar_is_irradiance_times_kwp_derated_with_cell_temperature_loss():
    cool = forecast.solar([{"start": 0, "gti": 1000.0, "temp_c": 0.0}], kwp=10.0,
                          derate=0.85, temp_coeff_pct=-0.4, noct_c=45.0)
    hot = forecast.solar([{"start": 0, "gti": 1000.0, "temp_c": 35.0}], kwp=10.0,
                         derate=0.85, temp_coeff_pct=-0.4, noct_c=45.0)
    # cell = air + (NOCT − 20) / 800 × G = air + 31.25
    assert cool[0]["kw"] == pytest.approx(10 * 0.85 * (1 - 0.004 * (31.25 - 25)), abs=1e-3)
    assert hot[0]["kw"] < cool[0]["kw"]
    night = forecast.solar([{"start": 0, "gti": 0.0, "temp_c": 10.0}], kwp=10.0)
    assert night[0]["kw"] == 0
    # P10/P50/P90 where ensemble members are given
    ens = forecast.solar([{"start": 0, "gti": 500.0, "temp_c": 20.0,
                           "gti_members": [100.0, 300.0, 500.0, 700.0, 900.0]}], kwp=10.0)
    assert ens[0]["p10"] < ens[0]["p50"] < ens[0]["p90"]


def test_solar_self_calibrates_against_measured_days():
    st = forecast.new_calibration()
    assert forecast.calibration_factor(st) == 1.0
    for d in range(10):
        forecast.add_solar_day(st, day=f"2026-11-{d + 1:02d}", predicted_kwh=40.0,
                               measured_kwh=32.0)
    assert forecast.calibration_factor(st) == pytest.approx(0.8)
    # days without enough sun, or with no measurement, don't count; it stays bounded
    forecast.add_solar_day(st, day="2026-11-11", predicted_kwh=0.2, measured_kwh=5.0)
    forecast.add_solar_day(st, day="2026-11-12", predicted_kwh=40.0, measured_kwh=None)
    assert forecast.calibration_factor(st) == pytest.approx(0.8)
    wild = forecast.new_calibration()
    for d in range(10):
        forecast.add_solar_day(wild, day=f"2026-11-{d + 1:02d}", predicted_kwh=10.0,
                               measured_kwh=50.0)
    assert forecast.calibration_factor(wild) == forecast.CAL_MAX
    assert len(st["days"]) <= forecast.CAL_DAYS


# ── Load ───────────────────────────────────────────────────────────────────────────────

def _history(weeks, *, start=(2026, 1, 5), base=0.5, evening=1.5, heat=0.2, cool=0.3,
             temp=lambda h, d: 22.0):
    """Hourly history: a base load, an evening bump, heating below 16 °C, cooling above 24 °C."""
    out = []
    t0 = dt.datetime(*start, tzinfo=SYD)
    for d in range(weeks * 7):
        for h in range(24):
            t = t0 + dt.timedelta(days=d, hours=h)
            c = temp(h, d)
            kwh = base + (evening if 17 <= h <= 21 else 0.0)
            kwh += heat * max(0.0, 16 - c) + cool * max(0.0, c - 24)
            out.append({"start": int(t.timestamp()), "kwh": round(kwh, 4), "temp_c": c})
    return out


def _wave(h, d):
    return 20 + 10 * math.sin(2 * math.pi * d / 14) + 3 * math.sin(2 * math.pi * (h - 9) / 24)


def test_load_model_learns_the_profile_and_comfort_band():
    hist = _history(12, temp=_wave)
    m = forecast.fit_load(hist, tz=SYD)
    assert 14 <= m["comfort"]["low_c"] <= 18 and 22 <= m["comfort"]["high_c"] <= 26
    assert m["heating_kwh_per_degree"] == pytest.approx(0.2, abs=0.08)
    assert m["cooling_kwh_per_degree"] == pytest.approx(0.3, abs=0.08)
    # direct: each future hour from its calendar and its forecast temperature only
    nxt = [{"start": ts(2026, 3, 30, 19), "temp_c": 30.0},
           {"start": ts(2026, 3, 30, 3), "temp_c": 10.0}]
    p = forecast.predict_load(m, nxt)
    assert p[0]["kwh"] == pytest.approx(0.5 + 1.5 + 0.3 * 6, abs=0.35)
    assert p[1]["kwh"] == pytest.approx(0.5 + 0.2 * 6, abs=0.35)
    assert {x["method"] for x in p} == {"model"}


def test_cold_start_is_the_weekday_hour_median_of_the_last_11_weeks():
    hist = _history(2)                                    # too little for the model
    m = forecast.fit_load(hist, tz=SYD)
    p = forecast.predict_load(m, [{"start": ts(2026, 1, 19, 19), "temp_c": None}])
    assert p[0]["method"] == "cold_start" and p[0]["kwh"] == pytest.approx(2.0)
    assert m["degraded"] is True and m["history_days"] == 14
    # only the last 11 weeks count
    old = _history(4, start=(2025, 9, 1), base=5.0)
    m2 = forecast.fit_load(old + _history(11, start=(2026, 1, 5)), tz=SYD, min_days_per_cell=999)
    p2 = forecast.predict_load(m2, [{"start": ts(2026, 3, 23, 3), "temp_c": None}])
    assert p2[0]["kwh"] == pytest.approx(0.5)


def test_a_weekday_hour_with_no_history_falls_back_to_the_hour_of_day_median():
    hist = [h for h in _history(1) if dt.datetime.fromtimestamp(h["start"], SYD).weekday() == 0]
    m = forecast.fit_load(hist, tz=SYD)                   # one Monday only
    p = forecast.predict_load(m, [{"start": ts(2026, 1, 15, 19), "temp_c": None}])  # Thursday
    assert p[0]["method"] == "hour_of_day" and p[0]["kwh"] == pytest.approx(2.0)


def test_with_no_history_at_all_the_baseline_is_used_and_said():
    m = forecast.fit_load([], tz=SYD)
    hrs = [{"start": ts(2026, 1, 15, 19), "temp_c": None}]
    assert forecast.predict_load(m, hrs)[0] == {"start": hrs[0]["start"], "kwh": None,
                                               "method": "cold_start"}
    p = forecast.predict_load(m, hrs, baseline_kwh=0.6)
    assert p[0]["method"] == "baseline" and p[0]["kwh"] == pytest.approx(0.6)


def test_declared_loads_are_taken_out_of_the_base():
    hist = _history(12)
    pool = {h["start"]: 1.0 for h in hist
            if dt.datetime.fromtimestamp(h["start"], SYD).hour in (10, 11, 12)}
    with_pool = [{**h, "kwh": h["kwh"] + pool.get(h["start"], 0.0)} for h in hist]
    m = forecast.fit_load(with_pool, tz=SYD, declared=[pool])
    p = forecast.predict_load(m, [{"start": ts(2026, 3, 30, 11), "temp_c": 22.0}])
    assert p[0]["kwh"] == pytest.approx(0.5, abs=0.05)


def test_battery_power_limit_by_temperature_is_a_setup_table():
    table = [(-10.0, 0.3), (0.0, 0.6), (10.0, 1.0), (45.0, 1.0), (55.0, 0.5)]
    assert forecast.battery_power_limit(5.0, 25.0, table) == 5.0
    assert forecast.battery_power_limit(5.0, 5.0, table) == pytest.approx(4.0)
    assert forecast.battery_power_limit(5.0, -20.0, table) == pytest.approx(1.5)
    assert forecast.battery_power_limit(5.0, 25.0, None) == 5.0


# ── Backtest and checks ──────────────────────────────────────────────────────────────────

def test_walk_forward_backtest_reports_accuracy_against_baselines():
    hist = _history(10, temp=_wave)
    r = forecast.backtest_load(hist, tz=SYD, min_train_days=28, refit_days=7)
    assert r["days"] == 10 * 7 - 28
    for k in ("model", "cold_start", "persistence"):
        assert set(r[k]) >= {"mae_kwh", "rmse_kwh", "bias_kwh", "daily_mape_pct"}
    assert r["model"]["mae_kwh"] < r["cold_start"]["mae_kwh"]
    assert r["model"]["mae_kwh"] < 0.1


def test_declared_load_check_flags_a_load_that_does_not_match():
    pred = [1.0] * 24 * 7
    ok = forecast.declared_check(pred, [1.1] * 24 * 7)
    bad = forecast.declared_check(pred, [0.2] * 24 * 7)
    assert ok["matches"] is True and bad["matches"] is False
    assert bad["actual_kwh"] == pytest.approx(33.6) and bad["predicted_kwh"] == pytest.approx(168)
    assert forecast.declared_check(pred, [None] * 168)["matches"] is None


def test_publishing_is_haeo_format_in_30_minute_steps():
    pts = [{"start": ts(2026, 11, 17, 10), "kw": 2.0}, {"start": ts(2026, 11, 17, 11), "kw": 3.0}]
    out = forecast.haeo(pts, tz=SYD)
    assert [p["value"] for p in out] == [2.0, 2.0, 3.0, 3.0]
    assert out[1]["time"] == "2026-11-17T10:30:00+11:00"


# ── Open-Meteo adapter (parsing only: no network in tests) ─────────────────────────────────

def test_open_meteo_parsing_and_params():
    raw = {"utc_offset_seconds": 0, "hourly": {
        "time": ["2026-11-17T00:00", "2026-11-17T01:00"],
        "global_tilted_irradiance": [0, 120.5], "temperature_2m": [15.2, None]}}
    rows = open_meteo.parse_hourly(raw)
    assert rows[1] == {"start": int(dt.datetime(2026, 11, 17, 1, tzinfo=dt.timezone.utc)
                                     .timestamp()), "gti": 120.5, "temp_c": None}
    p = open_meteo.forecast_params(-33.9, 151.2, tilt=20, azimuth=0, days=2)
    assert p["timezone"] == "GMT" and "global_tilted_irradiance" in p["hourly"]
    a = open_meteo.archive_params(-33.9, 151.2, start="2026-01-01", end="2026-01-31",
                                  tilt=20, azimuth=0)
    assert a["start_date"] == "2026-01-01" and a["end_date"] == "2026-01-31"


def test_a_load_that_ignores_the_weather_learns_no_comfort_band():
    m = forecast.fit_load(_history(12, heat=0.0, cool=0.0, temp=_wave), tz=SYD)
    assert m["comfort"] is None
    assert m["heating_kwh_per_degree"] == 0 and m["cooling_kwh_per_degree"] == 0
