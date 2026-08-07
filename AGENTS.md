# Repository Guidelines

## Project Structure

The backend lives in `app/`, organized by domain: `api/` (FastAPI routers), `core/` (configuration, SQLite, migrations, runtime events), `market/` (行情 and stock pools), `research/` (evidence, documents, company research), `quant/` (factors, models, backtests), `strategy/` (strategy registry/compiler/runtime), `paper/` (paper accounts and execution), and `workflows/` (cross-domain batches). Vue 3 source is in `frontend/src/`; component tests are in `frontend/tests/`. Python tests are in `tests/`, operational scripts in `scripts/`, and design/operations documentation in `docs/`. Runtime databases and downloaded documents belong under `data/` and are ignored by Git.

## Build, Test, and Development

Use the locked Conda environment:

```powershell
conda activate quant
python scripts/check_environment.py
python run.py                         # http://127.0.0.1:8000
pytest -q                             # backend suite
cd frontend; npm ci; npm run build    # typecheck + Vite production build
npm run test                           # Vue/Vitest tests
npm run test:e2e                       # Playwright tests (server required)
```

Use `--dry-run` for data-update scripts before writing SQLite. Keep iFinD credentials in `.env`, never in source or logs.

## Coding Style and Naming

Python uses 4-space indentation, type hints, `snake_case` functions/variables, and `PascalCase` classes. Keep domain logic in its owning package and assemble dependencies in `app/container.py`; do not import legacy root-level modules. Vue components use `<script setup lang="ts">`, `PascalCase.vue` filenames, and composables/stores for shared state. Prefer existing API client types, Lucide icons, and the project’s formatting/style utilities.

## Testing Guidelines

Name Python tests `tests/test_<area>.py` and test functions `test_<behavior>`. Add focused regression coverage for API, migration, data-contract, and strategy changes. Frontend unit tests use `.spec.ts`; build/typecheck must pass for Vue changes. Tests should use temporary databases and fixtures rather than runtime `data/*.db` files.

## Commits and Pull Requests

Follow the existing Conventional Commit style: `feat:`, `fix:`, `refactor:`, `docs:`, or `test:` followed by a concise imperative summary. Pull requests should explain the behavior change, list verification commands, identify migrations or data-format changes, and include screenshots for UI changes. Do not commit `.env`, credentials, generated frontend output, runtime databases, or downloaded documents.

## Architecture Notes

Respect database boundaries: `research_state.db` stores research evidence, `quant_research.db` stores factors/models/backtests/strategies, and `paper_trading.db` stores paper accounts and execution state. Use immutable, date-aware snapshots for research and keep the raw market database read-only. Changes affecting migrations or iFinD contracts must document rollback/operational impact in `docs/`.
