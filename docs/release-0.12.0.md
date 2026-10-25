# Laminus 0.12.0 — pre-alpha API retirement

Breaking pre-alpha change: versionless `/api/*` routes are removed immediately
from Laminus and laminus-mock. They return 404. Use `/api/v1/*` exclusively.

The owner explicitly waived the six-month sunset and traffic waiting gates on
October 24, 2026 for this migration across all repositories. This replaces the
previous April 24, 2027 sunset. Future stable deprecations retain normal policy.

- Use `POST /api/v1/jobs` (202 with `id`), cursor-paginated profiles/jobs, typed
  SSE events, and `/api/v1/jobs/{id}/artifact` downloads.
- The bundled dashboard now uses these contracts, including UUID profile
  selection, all catalog pages, and artifact availability after download.
- Themis, Ordinus, and Concordia active integrations/healthchecks already use v1.
  Update any external scripts before upgrading. Rebuild/deploy the Laminus and
  mock images together with this dashboard.
- `openapi.json` now describes only v1; `openapi-v1.json` remains available.
- `/`, `/docs`, and OpenAPI document URLs remain available. The internal
  known-profile test helper moves to `/api/v1/test/known-profile`.
- No compatibility flag or redirects restore retired endpoints. Roll back the
  image to restore them if an external caller was missed.
