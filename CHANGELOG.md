# Changelog

## [Unreleased]

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
