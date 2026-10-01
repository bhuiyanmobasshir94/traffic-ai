---
name: deployment-edge-auth-facts
description: Verified facts about compose/Helm edge auth, the production gate on the UI process, and compose/helm render quirks in traffic-ai
metadata:
  type: project
---

Verified 2026-10-01 (branch production-hardening, base 5c67d11) while building the Postgres/migrate/Traefik-edge-auth deployment surface.

- **The UI process runs the production gate too.** `Settings` is built in the UI with `TRAFFIC_AI_ENVIRONMENT=production`, so the UI needs `TRAFFIC_AI_API_TOKEN` AND `TRAFFIC_AI_PERSISTENCE_ENABLED=false` (it has no DB URL, and the default URL carries `:traffic@`, which the gate refuses). The Helm chart's UI ConfigMap lacked both before this slice, so the UI would have crash-looped. `ALLOW_UNAUTHENTICATED` must be mirrored on the UI too.
- **Blank token == no token** (`_blank_token_is_absent`), so compose `${TRAFFIC_AI_API_TOKEN:-}` is a safe default: production refuses. The UI client also rejects a token with whitespace/control chars (trailing newline in .env).
- **Traefik middleware definitions die with their container.** A router referencing a middleware defined on another service 404s when that container is down. Each router (worker, ui) therefore defines its own BasicAuth middleware in its own labels. Order on the worker router is basicauth then api-bearer.
- **ingress-nginx cannot do the compose pattern.** It forwards the browser's `Authorization: Basic` to the worker, which wants Bearer, so browser-direct `/api` (the MJPEG `<img>`) gets 401. Snippets are off by default and annotations are not secret. Only Traefik (user-created Middleware CRs, referenced by `ingress.traefik.middlewares`) gives parity. Chart does not template CRDs.
- **Compose long-syntax bind mounts do not auto-create a missing source** (short syntax does). `docker compose config` omits `create_host_path: false` (renders `bind: {}`) but shows `true` for short-syntax mounts. YAML folds the `Bearer <token>` label onto two lines in `config` output; it is one value once parsed.
- **Migrate service:** worker image ENTRYPOINT is `python -m traffic_ai.api`, so `migrate` needs `entrypoint: ["alembic"]` plus `command: ["upgrade","head"]`. WORKDIR is /app, which holds alembic.ini and migrations/. Both `migrate` and `worker` keep a `build:` block with the same `image:` so `up --build` cannot race a pull.
- **The app never migrates.** `create_app` only builds a `Database`; a bad URL turns history off (`db.init_failed`), an unreachable server surfaces at flush time. `/api/readyz` reports `database` but never gates on it.
- **`camera_count_snapshots` is schema-only:** `upsert_snapshots` exists in the repository and nothing calls it. Do not document it as a feature.
- **Tests that exercise the real gate:** both deployment test files resolve `${VAR:-default}` / the rendered ConfigMap and build `Settings` with a cleaned `TRAFFIC_AI_*` env (monkeypatch.delenv). Cheaper and truer than string-matching.
- ruff here: `S105` fires on `== "${...}"` comparisons against keys named PASSWORD/TOKEN (hoist the literal into a differently named variable); a `noqa: S105` on `TEST_TOKEN = "t" * 64` is flagged unused (RUF100). `RUF012` fires on a mutable class attribute in a test class (use a module constant).

Added 2026-10-01 (hardening round 2, verified by render/test):
- **Helm migrate hook:** pre-install hooks run BEFORE the chart's ConfigMap, Secret and ServiceAccount exist. `templates/job-migrate.yaml` therefore carries its own env, no envFrom, and names a ServiceAccount only when `serviceAccount.create=false`. A chart-GENERATED Secret becomes a hook too (weight -10 vs the Job's 0; `before-hook-creation`) and then survives `helm uninstall`. `helm template` does NOT print NOTES.txt; `helm install --dry-run=client -n ns` does (`result.stdout.split("NOTES:")`).
- **Worker singleton:** `strategy: Recreate`, `replicas` always rendered, worker HPA refused with `fail` in `hpa.yaml` (`autoscaling.worker.enabled`); `worker.replicaCount>1` is only a NOTES warning, not a refusal.
- **Release gating:** `ci.yml` has no tag trigger, so `workflow_run` would never fire for a tag. `release.yml` calls `ci.yml` via `workflow_call` (`needs: ci`). Pre-release = tag containing `-`; `latest` and `major.minor` are gated in `docker/metadata-action` tags with `flavor: latest=false`. YAML parses a bare `on:` as `True` in tests.
- **Action SHAs** come from `https://api.github.com/repos/<o>/<r>/commits/<tag>` with `Accept: application/vnd.github.sha` (plain curl works; `git ls-remote` is refused by the sandbox). Pinned 2026-10-01: checkout v7.0.1, setup-uv v10.2.0, setup-python v7.0.0, qemu v4.4.0, buildx v4.4.1, login v4.6.0, metadata v6.2.0, build-push v7.4.0, codeql-action v4.38.2.
- **Test seam for Makefile:** `make -C <root> --no-print-directory <target> COMPOSE=echo` prints the compose command instead of running it. Worker shutdown budget is read from source text (`GRACEFUL_SHUTDOWN_SECONDS` in api/__main__.py, `_SHUTDOWN_TIMEOUT_SECONDS` in api/app.py) = 5 + 10 + 10.
- A first full-suite run hung 10 min at low CPU while a sibling worker was mid-edit; a rerun with `-v > log` finished in 24s. If it hangs, kill it and rerun to a log file.
