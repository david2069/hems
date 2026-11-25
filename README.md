# hems

An engine for [OUTUS](https://github.com/david2069/outus), the Open Utility Tariff & Usage
Specification. It reads OUTUS plans and price series, keeps the usage ledger, and writes
OUTUS bills with every line explained.

- **Implements:** OUTUS v1 (electricity). The spec is published separately; hems is one
  implementation of it.
- **Python ≥ 3.11, no I/O.** One dependency, `tzdata`, so time zones work on any image.
  Callers supply readings, prices and the plan, and store the results. I/O adapters (such as
  Open-Meteo) are separate modules under `hems.adapters`.
- **Used by:** the FranklinWH Direct Connect and Modbus bridges.

| Module | Job |
|---|---|
| `hems.ledger` | grid readings from the devices behind a meter → 5-minute usage intervals |
| `hems.prices` | a price series: price at an interval's start, later kinds replace earlier ones |
| `hems.billing` | an OUTUS bill from a plan and the ledger, with formulas; provisional bills project to the period's end; `now_rates` for the live band and rate |
| `hems.periods` | billing period bounds (monthly, bimonthly, quarterly) |
| `hems.plans` | checks a schema can't express (overlapping plans, wrong charge class, unpriced gaps) |
| `hems.setup` | setup steps in order: site → meter → plan → tariffs |
| `hems.profile_v1` | imports a `franklinwh-bridge/tariff-profile` v1 file as an OUTUS plan |
| `hems.wear` | battery wear: warranty setup fields, wear cost per kWh, throughput pace budget, dispatch events, round-trip efficiency from SoC-matched cycles |
| `hems.forecast` | solar (irradiance × kWp, cell temperature, self-calibration), the load model (calendar × season, comfort band, cold start, hour-of-day and baseline fallbacks), battery temperature limit, walk-forward backtest, HAEO publishing |
| `hems.loads` | HEMS setup: device types (value → label), always-on load and usage periods to confirm, heavy loads and EV charger checked against capabilities, expected hours, soak trigger |
| `hems.dispatch` | Smart Dispatch: the questions to ask once a plan is entered (target: Emergency Backup only, TOU, lowest cost with demand charges, self-consumption, most export earnings; SoC band; grid charging and export allowed, limited or prohibited), prices per step from a plan, a 24-hour SoC plan by dynamic programming (never worse than self-consumption), the action now |
| `hems.adapters.open_meteo` | Open-Meteo forecast and archive: irradiance and temperature rows (does network I/O) |

```
pip install -e ".[test]"
pytest
```

The tests check every bill hems writes against the OUTUS v1 schemas, copied in
`tests/outus-v1/`.

## Licence
MIT
