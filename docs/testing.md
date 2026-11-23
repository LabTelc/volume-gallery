# Testing

## Backend (pytest)

```bash
cd backend
pip install -r requirements-dev.txt
pytest                  # whole suite
pytest tests/test_shares.py -k cap   # one file / keyword
pytest --cov=. --cov-report=term-missing
```

`tests/conftest.py` points every `VG_*` path at a temporary directory and sets the
environment **before** `app` is imported, so the suite never touches real data. The
database is dropped and recreated for every test. The `TestClient` is used without a
context manager, so the lifespan (background workers, Redis) does not run; the
startup tasks are tested directly in `test_audit_and_startup.py`.

Do not add `backend/tests/__init__.py` (`backend/__init__.py` exists and would change
how the modules are imported).

## Frontend (node:test)

```bash
cd frontend
npm ci
npm test
```

These cover the pure ES-module logic (animation links, scale bar, histogram,
slice geometry, transfer functions). The webpack build is not run in CI because it needs
`vendor/kiln-render`, which is produced by the Docker build.

## Known gaps recorded as tests

Some tests document real defects instead of hiding them. They are marked `xfail`
(backend) or `todo` (frontend) and will start reporting once the code is fixed,
at which point the marker should be removed.

- `min_level` check in `routers/volumes.py` compares against `num_lod_levels`, which stores
  the *max* LOD, so it is off by one.
- A revoked file token keeps working until the 60 s token cache expires.
- Revoking a volume grant does not stop already-issued file tokens.
- `PRESETS[name]` (transfer function) and `FORMATS[dtype]` (slice geometry) do plain
  object lookups, so names like `constructor` resolve to inherited properties.

## CI

- `ci.yml`: backend pytest + coverage, frontend tests.
- `docker.yml`: compose validation, `nginx -t`; the full image build is monthly / manual.
- `security.yml`: pip-audit, npm audit, CodeQL (weekly and on PRs).
- `dependabot.yml`: weekly dependency PRs.
