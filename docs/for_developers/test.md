## Test

We have unit tests for both frontend and backend. They are automatically run by GitHub workflow on submitting Pull Requests. You can also run them locally.

### Run everything (Docker)

From the repo root:

```
make test_run_all
```

This builds and runs `test_studio_backend` (pytest, excluding `heavier_processing`) and `test_studio_frontend` (yarn test:ci) via `docker-compose.test.yml`, then the lambda/infrastructure tests.

Other Makefile targets:

| Target | What it runs |
|---|---|
| `make test_backend` | Backend tests only -- `pytest studio/tests/app/ -m "not heavier_processing"` in Docker |
| `make test_backend_full` | Backend tests including `heavier_processing` |
| `make test_frontend` | Frontend tests only in Docker |

### Frontend only (local, from `frontend/`)

| Command | Use case |
|---|---|
| `yarn test` | Interactive watch mode -- best for dev |
| `yarn test:ci` | Single CI run (`CI=true`, no watch) |
| `yarn test-coverage` | With coverage report |
| `yarn test -- --testPathPattern="<pattern>"` | Run a specific file/pattern |

Stack: Jest + React Testing Library via `react-app-rewired` (CRA). Test files live in `__tests__/` directories or alongside source as `*.test.{ts,tsx}`.

### Backend only (local, from repo root)

```
cd studio && poetry run pytest tests/app/ -m "not heavier_processing"
```

Tests live in `studio/tests/` and `studio/app/optinist/microscopes/tests/`. Conftest at `studio/tests/app/conftest.py`. Backend tests may require env vars -- see `docker-compose.test.yml` for `PYTHONPATH`, `STRIPE_*`, etc.

#### The suite writes into `studio/test_data`, and deletes part of it

`pyproject.toml` points `OPTINIST_DIR` at `studio/test_data` for pytest, so the
run uses that tree as its data directory rather than `/tmp/studio`. Two
consequences worth knowing before you chase a phantom failure:

- **`studio/test_data/output` is deleted at session teardown.** The session
  fixture in `conftest.py` ends with `shutil.rmtree`. That directory is
  generated, not a fixture, so this is intended -- but it means a tree you have
  run several partial suites against can leave `test_experiment.py`,
  `test_outputs.py`, `test_workflow.py`, `test_filepath_creater.py` and
  `test_workflow_reader.py` failing with `FileNotFoundError` on the *next* run.
  Those failures are local state, not your change. To confirm, re-run in a clean
  checkout: `git worktree add /tmp/baseline HEAD` (copy `studio/config` across
  for the gitignored Firebase creds and `.env`), then run the suite there.
- **Lock files are left behind and are gitignored.** `InputFileLock` creates
  `input/<workspace>/.locks/<file>.lock` and the logger's
  `ConcurrentRotatingFileHandler` creates `logs/.__studio.lock`. Both are covered
  by the `.gitignore` in those directories; if you add a new runtime artefact
  under `studio/test_data`, ignore it there rather than at the repo root, because
  118 files in that tree are tracked fixtures.

### E2E release tests (Playwright, from `frontend/`)

Browser tests automating release verification, with stable per-feature test
IDs (`AUTH-01`, `WF-04`, ...). They need a running environment and a test
account:

```
yarn test:e2e
```

Setup, credentials, running, and troubleshooting: `frontend/e2e/README.md`.

### Test-sheet coverage maps

Which manual test-sheet rows are already automated is tracked in two documents,
one per sheet family:

| Document | Sheet family |
|---|---|
| `infrastructure/documentation/RELEASE_TEST_COVERAGE.md` | `Araya-OptiNiSt Release Test Cases Template` (`BT-1xx` .. `BT-11xx`), almost all Playwright |
| `infrastructure/documentation/SYSTEM_TEST_COVERAGE.md` | `Araya-Optinist System Test Cases Template`, a larger scheme covered mostly by the jest and pytest suites above |
