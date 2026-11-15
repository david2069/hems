# hems

An engine for [OUTUS](https://github.com/david2069/outus), the Open Utility Tariff & Usage
Specification. It reads OUTUS plans and price series, keeps the usage ledger, and writes
OUTUS bills with every line explained.

- **Implements:** OUTUS v1 (electricity). The spec is published separately; hems is one
  implementation of it.
- **Python ≥ 3.11, no dependencies, no I/O.** Callers supply readings, prices and the plan,
  and store the results.
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

```
pip install -e ".[test]"
pytest
```

The tests check every bill hems writes against the OUTUS v1 schemas, copied in
`tests/outus-v1/`.

## Licence
MIT
