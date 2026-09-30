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
| Launch persistent browser with limited remote page control | `POST /api/v1/profiles/{profile_id}/launch-remote` | Yes |
| Run allowlisted operation on its live page | `POST /api/v1/profiles/{profile_id}/remote/page` | Yes |

## Critical boundary finding

The ordinary launch endpoint still returns no CDP or WebSocket endpoint. The
new `launch-remote` endpoint instead launches the same persistent profile and
returns one temporary `http_page_rpc` handle. The authenticated RPC accepts
only `url`, `goto`, `count`, `fill`, `click`, and `get_attribute`; it never
returns cookies, storage state, or an unrestricted browser object. Handles are
profile-bound, in-memory, and invalidated as part of browser close.

`CamoufoxHttpGateway` adapts that small page surface to the existing
onboarding, health, and execution gateway protocols. It never imports an
upstream manager or opens another browser process.

## Minimum public-API addition needed

The public remote-page RPC is now that contract. CDP/WebSocket remains absent
by design: Camoufox's `launch_server()` cannot serve a persistent context.
