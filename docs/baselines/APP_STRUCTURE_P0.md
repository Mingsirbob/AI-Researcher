# App Structure Refactor P0 Baseline

Captured on 2026-07-30 from commit `abe5ee5` before the application-structure
refactor.

## Verification baseline

- Backend: 160 tests passed with Python 3.11.15 from the `quant` Conda environment.
- Frontend: `npm run build` passed.
- Frontend tests: the supported command is `npm test`; no `test:unit` script exists.
- OpenAPI: 87 paths and 95 public operations.
- Canonical OpenAPI SHA-256:
  `ec8248a471815d4c5cc8ba56c5ef7664e4d57a816787048cb8289223b69070fc`.

## Database baseline

All databases returned `ok` from `PRAGMA quick_check`.

| Database | Business tables | Responsibility |
| --- | ---: | --- |
| `research_state.db` | 33 | research and evidence |
| `quant_research.db` | 32 | factors, models, signals, and backtests |
| `paper_trading.db` | 11 | paper accounts, orders, positions, and NAV |

Runtime database files are intentionally ignored by Git. This document records
their structural boundary, not their mutable data contents.
