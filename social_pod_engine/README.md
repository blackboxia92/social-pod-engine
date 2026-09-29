# Social Pod Engine extension

This is an intentionally isolated extension layer on top of Camoufox Profile
Manager (CPM). It does not import CPM, change its FastAPI application, add
tables to its SQLite database, modify its frontend, or own browser sessions.

## Boundary

```
CPM upstream (unchanged)                    Social Pod Engine (new)
profiles / storage / sessions / API   <---   adapters receive a supplied page
```

The caller obtains and owns a browser page using CPM's existing lifecycle, then
passes that page to an adapter. No adapter opens, closes, or persists a CPM
profile. This keeps upgrades and upstream test results independent.

## Domain and storage (Phase 1)

The control plane owns `Persona`, `SocialAccount`, and a minimal
`AccountGroup` anchor. A social account stores CPM's profile only as the opaque
`upstream_profile_id`; there are no cross-database joins or foreign keys.

`SocialPodDatabase` uses its own SQLite file at
`social_pod_engine/data/social_pod.sqlite3` by default. Its SQLAlchemy schema
uses portable JSON columns for `metadata` and normalized `tags`, so a future
PostgreSQL migration is contained in this package.

The four independent account dimensions are `health_status`, `session_status`,
`lifecycle_status`, and `quota_status`. They are intentionally not collapsed
into one global state.

## Included adapters

- `x`: implemented read-only session health and profile-handle extraction.
- `instagram`: explicit placeholder; every operational call raises
  `PlatformAdapterNotImplemented`.
- `facebook`: explicit placeholder; every operational call raises
  `PlatformAdapterNotImplemented`.

X's engagement capabilities are declared as **planned**, never implemented or
invoked. Adding an operation must be a separate, reviewed change with explicit
authorization, platform-policy review, rate limits, and auditability.

## Test commands

Run independently from the unchanged upstream suite:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m pytest tests -m "not browser"              # upstream
python -m pytest social_pod_engine/tests               # Phase 1 extension
python -m pytest social_pod_engine_tests               # legacy extension checks
```
