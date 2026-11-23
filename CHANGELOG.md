# Changelog

## [Unreleased]

## [0.3.0] — 2026-11-24

### Added
- `hems.dispatch` (#612 §7, Smart Dispatch):
  - `questions(plan)`: once a utility plan is entered, the user's target (`OBJECTIVES`:
    Emergency Backup only, TOU, lowest cost including demand charges, self-consumption, most
    export earnings), the SoC band for it, solar size, grid charging and export (allowed,
    limited to a kW size, or prohibited), export from the battery, demand-peak shaving and an
    export-penalty note. Only the questions the plan and site need; pre-filled answers are
    "please confirm". `plan_facts`, `DEFAULTS` per target, `check`.
  - `step_prices(plan, …)`: import price, export value (credits less export charges) and
    demand rate per step, from the plan and a price series.
  - `plan_24h(steps, battery, values)`: dynamic programming over SoC (1 % × 30 min) with
    power limits, round trip, wear per kWh, the SoC band, grid-charge and export limits,
    negative feed-in, the demand charge on the window's peak (lowest feasible cap searched),
    Emergency Backup holding and filling first, off grid = no grid actions. Never worse than
    self-consumption. Each step: action (`self`, `charge`, `discharge`, `hold`), kW, SoC,
    grid flows, cost and reason; the result: cost, baseline, saving, value of stored energy,
    cost per 10 %, assumptions.
  - `decide(result, now)`: the action now, until when, its target SoC and why.

## [0.2.1] — 2026-11-24

### Changed
- `hems.loads.DEVICE_TYPES`: the device types as a fixed list, value → label (EV charger,
  hot water, heat pump, air conditioning, pool pump, spa, well pump, dryer, washing machine,
  dishwasher, oven, other). `check()` refuses any other value, so a typed "EV charger" no
  longer silently loses the EV fields.

## [0.2.0] — 2026-11-17

### Added
- `hems.wear` (#612 §6): warranty inputs as setup fields the cloud only pre-fills (please
  confirm); wear cost per kWh = (replacement − end-of-warranty value) ÷ warranted throughput,
  with its formula; the throughput pace budget and its rising (never blocking) penalty; pace
  ratio; cycles and the optional daily cap; usable capacity from SoH; dispatch events from
  samples (returned, never stored); round-trip efficiency from SoC-matched cycles as a rolling
  30-day median, banded by power and temperature once a band has 5 cycles; daily records.
- `hems.forecast` (#612 §4): solar from plane-of-array irradiance × kWp × derate with a
  cell-temperature loss and self-calibration against measured days (P10/P50/P90 from ensemble
  members); the load model (time of day × day type × season, heating and cooling around a
  learned comfort band, direct per-hour prediction, cold start from the weekday-hour median of
  the last 11 weeks, declared loads taken out); the battery temperature limit from a setup
  table; a walk-forward backtest against the cold start and persistence; the 7-day declared
  load check; HAEO-format 30-minute publishing.
- `hems.loads` (#612 §5): the opt-in notice; always-on load and usage periods pre-filled to
  confirm; heavy loads (measurement best first, units summed and converted, run times or
  "follows the weather", circuit), deferrable, sheddable and soak control, the EV charger, all
  checked against the bridge's capabilities; a load's expected hours; the soak trigger.
- `hems.adapters.open_meteo`: forecast and archive requests and parsing (UTC rows).

### Changed
- `tzdata` is a dependency (#612 design, point 7), so time zones work on images without one.

## [0.1.2] — 2026-11-15

### Fixed
- Rates in bill formulas read `$0.30`, `$1.10`, not `$0.3`, `$1.1` (two to four decimals).

## [0.1.1] — 2026-11-15

### Added
- `ledger.open_interval(state)`: the interval still open, so far, so a provisional bill includes the current five minutes.

### Fixed
- A v1 fixed charge with no description was labelled with its raw type (`supply`); it is now `Supply`, `Metering fee`.

## [0.1.0] — 2026-11-15

### Added
- `hems` 0.1.0, an OUTUS v1 engine: ledger, prices, billing (interval and period charges, explainable line items, provisional bills with projection), billing periods, plan checks, setup steps, and v1 tariff-profile import. Moved out of the OUTUS repo so the spec is published on its own.
