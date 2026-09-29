# Upstream isolation audit

## Baseline

- Source: `polyackiy/camoufox-profile-manager` at `f0bf304` (`v0.5.0`).
- License: MIT; the upstream `LICENSE` and copyright notice remain untouched.
- Upstream architecture: `src/camoufox_pm/` owns FastAPI, SQLite, browser
  sessions, profiles, scheduler, API routes and web UI. Its test suite is in
  `tests/`.

## Chosen boundary

`social_pod_engine/` is a sibling package. It has no imports from
`camoufox_pm`, no database migrations, no route registration and no changes to
the upstream frontend. A caller supplies a Playwright-compatible page owned by
the existing CPM lifecycle. The adapter may inspect or navigate that page, but
cannot create, close or persist a CPM profile.

All changes in this branch are new files. Compare with:

```powershell
git diff --name-only --diff-filter=M upstream/main
```

The command must produce no output.

## Test separation

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m pytest tests -m "not browser"  # upstream fast suite
python -m pytest social_pod_engine_tests  # extension suite
```

On this Windows environment the fast upstream suite passed with `444 passed,
2 skipped, 23 deselected`. The extension suite passed with `9 passed`.

The browser-marked upstream suite was also exercised. Its 13 failures occur
before the extension is imported: the current Camoufox browser cache returns a
truncated fingerprint payload, so CPM's existing fingerprint resolver leaves
the pin empty. This reproduced both with the unconstrained package install and
with Camoufox `0.5.4`, the version in `uv.lock`. Ten browser tests passed.

## Deferred integration

Do not wire this package into CPM's app, database or UI until an intentional
integration design has a migration, API compatibility review and tests. Do not
deduplicate or refactor upstream code as part of that work.
