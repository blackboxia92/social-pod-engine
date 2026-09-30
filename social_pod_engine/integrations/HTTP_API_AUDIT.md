# Camoufox Profile Manager HTTP boundary audit

Audited against the public routes registered by the current upstream checkout.
All versioned routes below are under `/api/v1`; the upstream also retains an
unversioned `/api` compatibility prefix. Profile and system routes require an
upstream login session or `X-API-Key` when that authentication is enabled.

| Operation | Public HTTP route | Available |
| --- | --- | --- |
| Service health | `GET /health` | Yes |
| Effective service configuration | `GET /api/v1/system/config` | Yes |
| System/browser summary | `GET /api/v1/system/status` | Yes |
| List profiles | `GET /api/v1/profiles` | Yes |
| Get profile | `GET /api/v1/profiles/{profile_id}` | Yes |
| Create profile | `POST /api/v1/profiles` | Yes |
| Launch profile browser | `POST /api/v1/profiles/{profile_id}/launch` | Yes |
| Close profile browser | `POST /api/v1/profiles/{profile_id}/close` | Yes |
| List running browsers | `GET /api/v1/browsers/active` | Yes |
| Check a saved profile proxy | `POST /api/v1/profiles/{profile_id}/check-proxy` | Yes |
| Check an unsaved proxy configuration | `POST /api/v1/proxy/check` | Yes |
| Browser/profile state by profile | `GET /api/v1/browsers/active` plus launch response | Partial |
| Acquire/release a lease explicitly | — | No |
| Get a lease token/handle | — | No |
| Connect to launched browser using CDP/WS | — | No |
| Obtain a Playwright page/context from a launch | — | No |

## Critical boundary finding

`POST /api/v1/profiles/{profile_id}/launch` returns only `profile_id`, a
deprecated random `browser_session_id` that no endpoint accepts, status,
message, process ID, and opaque options. It does **not** return a CDP endpoint,
WebSocket URL, Playwright connection URL, browser-context handle, or page.

Therefore the public API currently cannot satisfy Social Pod's
`UpstreamOnboardingGateway`, `UpstreamHealthGateway`, or
`UpstreamExecutionGateway`: all three require a page compatible with
`ExecutionContext`. `CamoufoxHttpGateway` intentionally exposes only the HTTP
operations above and does not claim protocol conformance.

## Minimum public-API addition needed

Expose a documented, authenticated connection contract as part of launch (or a
subsequent session endpoint): a short-lived CDP/WebSocket endpoint or a public
remote-page RPC, tied to the launched profile and with a documented close
operation. Social Pod can then connect through Playwright without importing
upstream implementation classes. No fallback to internal managers is valid.
