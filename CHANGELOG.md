# Changelog

## [Unreleased]

## [0.1.1] — 2026-11-15

### Added
- `ledger.open_interval(state)`: the interval still open, so far, so a provisional bill includes the current five minutes.

### Fixed
- A v1 fixed charge with no description was labelled with its raw type (`supply`); it is now `Supply`, `Metering fee`.

## [0.1.0] — 2026-11-15

### Added
- `hems` 0.1.0, an OUTUS v1 engine: ledger, prices, billing (interval and period charges, explainable line items, provisional bills with projection), billing periods, plan checks, setup steps, and v1 tariff-profile import. Moved out of the OUTUS repo so the spec is published on its own.
