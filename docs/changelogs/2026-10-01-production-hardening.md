# 2026-10-01 — Production hardening: auth, persistence, observability, Helm, reproducible builds

Branch `production-hardening`, built on `worktree-production-demo` (`3e90abf`).

## What

- **Config gate** (`src/traffic_ai/config.py`): new settings for persistence, auth, metrics,
  rate limiting, proxy hops. Production refuses to start with no API token, a token under 32
  chars or outside visible ASCII, or the dev database password. Blank tokens collapse to unset.
  Settings errors no longer echo input.
- **Persistence** (`src/traffic_ai/db/`, `migrations/`, `alembic.ini`): Postgres tables
  `crossing_events` and `camera_count_snapshots` (schema only; nothing writes snapshots yet),
  async repository, batching `CrossingWriter` with a 10k buffer, one split retry, and loss
  counting. DB errors are logged without bound parameters (`db/errors.py`).
- **API** (`src/traffic_ai/api/`): bearer-token `AuthMiddleware` (constant-time byte compare),
  security headers (HSTS in production only), per-IP rate limiting (service token and probes
  exempt, trusted-hop XFF, capped table), Prometheus at `/api/metrics`, request-id validation,
  `/api/history/{events,counts,hourly}`, readiness fields `database` (schema-aware) and
  `history_events_lost`. MJPEG streams end on shutdown; uvicorn graceful timeout 5s.
- **Pipeline** (`worker/pipeline.py`): submits each crossing to the writer and exports
  per-camera metrics; both fail open.
- **UI**: server-side bearer token (never rendered or logged), new `pages/Analytics.py`,
  distinct unauthorized / unavailable / empty / failed states, incomplete-totals warning,
  looped-demo-footage disclosure on every page.
- **Compose**: `postgres`, one-shot `migrate`, per-router Traefik ratelimit → BasicAuth
  (usersfile `config/traefik/users.htpasswd`) → bearer injection, UI security headers,
  resource limits, stop grace periods.
- **Helm** (`deploy/helm/traffic-ai/`): new chart — hardened pods (non-root, read-only rootfs,
  dropped caps), probes, migrate hook Job, worker singleton (`Recreate`, no worker HPA),
  opt-in HPA/PDB/NetworkPolicy, edge auth for Traefik (production default) or ingress-nginx.
- **Build/CI/CD**: PEP 621 + `uv.lock` (torch/torchvision on the CPU index, `opencv-python`
  excluded), Dockerfiles install `--locked`, CI fixed (it could not collect six test modules
  before), Postgres-backed tests in CI, helm lint, stale-lock check; `release.yml` builds
  amd64+arm64 to GHCR after CI, with SBOM, provenance, Trivy; all actions SHA-pinned.
  `make clean` keeps volumes; `make clean-volumes CONFIRM=yes` destroys them.
- **Docs**: `docs/DEPLOYMENT.md` (Compose runbook), `docs/KUBERNETES.md`, `README.md`
  (capabilities table: ANPR, speed, toll charging, fines listed as not implemented),
  `CLAUDE.md`, four decision-log entries.

## Why

The maintainer asked to complete the application to production grade and make it
deployment-ready, choosing BasicAuth + bearer token, Postgres + Alembic, and both Compose
and Helm. The prior state had no authentication, no persistence, no metrics, no rate
limiting, no lockfile, and a CI job that could not collect the suite.

## How

Shared contract first (`config.py`, `pyproject.toml`), then parallel workers on disjoint
file sets (persistence, API hardening, Helm, builds/CI), then an integration wave
(pipeline/API wiring, UI, deployment surface), then a full-diff `reviewer` pass. The review
found 0 critical, 2 high, 8 medium, 12 low; all high/medium and most low findings were
fixed with tests that fail when the fix is reverted. Decisions and rejected alternatives are
in `docs/decisions/DECISIONS.md` (2026-10-01 entries).

## Affected modules and behaviors

Auth now gates every `/api` path except `/api/healthz` and `/api/readyz`; scripts must send
`Authorization: Bearer`, and Prometheus must too. A production deployment without the
required secrets now fails at startup by design. History is new durable data: counts and
metadata only — no frames, no plates (`plate_text` stays NULL; ANPR remains a disabled seam).

## Intended outcome

`docker compose up` (or `helm install`) with the documented secrets yields a TLS dashboard
behind a login, live video and counts, and history that survives restarts. Signals: login
prompt, `/api/readyz` → `database: true`, `history_events_lost` 0, Analytics totals rising.
Failure signals: startup refusal naming the missing secret, `database: false`, a non-zero
`history_events_lost_total`.

## Verification

- `pytest -o addopts=""` → 916 passed, 24 skipped (21+1 `requires_postgres`, 2 ultralytics).
  The build/CD worker also ran the locked env against a throwaway local Postgres 14:
  369 passed incl. all `requires_postgres` tests (before the integration wave).
- `ruff check .` and `ruff format --check .` → clean.
- `docker compose config -q` → exit 0. `helm lint` and `helm template` (default, production,
  nginx, traefik) → exit 0; unsafe combos fail at render. `actionlint` → clean.
- `uv lock --check` → current.

**Not verified:** no image was built and the stack was never run (Docker daemon down); no
cluster install; Traefik BasicAuth/header swap/429s, ACME issuance, real graceful shutdown,
the migrate hook against a live cluster, and the post-integration Postgres SQL paths
(skipped locally; run in CI). CI and release workflows have not run on GitHub.

## Critical notes

- **Required before first deploy:** `.env` with `DOMAIN`, `ACME_EMAIL`,
  `TRAFFIC_AI_API_TOKEN` (`openssl rand -hex 32`), `POSTGRES_PASSWORD`; and
  `config/traefik/users.htpasswd` (`htpasswd -nbB -C 12`). `.env.example` was **not**
  updated this session (`.env*` access was denied) — sync it from `docs/DEPLOYMENT.md`.
- Rollback: redeploy the previous image; migrations are forward-only (0001 has a downgrade).
- `make clean-volumes` deletes all history and ACME certs.
- Known gaps: worker `replicaCount>1` renders with a warning only; Docker socket mounted on
  Traefik; Traefik bearer token lives in a Middleware CR on K8s; snapshot rollups unused;
  cv2/tracker work is synchronous on the event loop; ultralytics AGPL decision still open.
