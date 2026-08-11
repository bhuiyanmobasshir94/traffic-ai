---
name: deployment-docker
description: Docker/Traefik/compose deployment layout added to traffic-ai (docker/, compose.yaml, scripts/fetch_demo_videos.py) and the pip-caching pattern used for a local poetry-core package
metadata:
  type: project
---

Verified 2026-08-10, session that added the deployment stack.

**Layout.** `docker/Dockerfile.ui` (installs `pyproject.toml`'s `ui` extra
only) and `docker/Dockerfile.worker` (installs the `worker` extra + CPU-only
torch via `--extra-index-url https://download.pytorch.org/whl/cpu`, then
`libgl1`/`libglib2.0-0` at runtime for opencv-headless). `compose.yaml` wires
`traefik` (v3, HTTP-01 ACME, dashboard disabled, docker socket `:ro`),
`redis` (no persistence — `--save "" --appendonly no`, matches the project's
stale-vs-live design in `store.py`), `worker`, `ui`. `worker`/`redis` publish
no host ports — only traefik does. The `/api` Traefik router needs an
explicit `priority` label higher than the UI catch-all router's, or
`PathPrefix(/api)` does not reliably win — verified via `docker compose
config` and asserted in `tests/test_deployment_config.py`.

**Local-package Docker caching trick** (used in both Dockerfiles): since
`pyproject.toml` uses `poetry-core` as the build backend with `packages =
[{include = "traffic_ai", from = "src"}]`, `pip install .` needs the actual
`src/traffic_ai/` directory to exist to build wheel metadata — you can't
split "install deps" from "install local package" the way a pure
requirements.txt project can. Fix: `mkdir -p src/traffic_ai && touch
src/traffic_ai/__init__.py` as a stub, `pip install ".[extra]"` against that
stub (this is the cacheable, network-heavy layer), then `COPY src ./src` for
real and `pip install --no-deps --force-reinstall .` (fast, no network).
`--force-reinstall` is required on that second step — plain `pip install .`
sees the same name+version already "installed" from the stub and silently
skips reinstalling, so the real source never lands without it.

**Named volume ownership gotcha:** a fresh Docker named volume mounted into
a non-root container defaults to root:root, so a non-root `USER app`
container can't write into it unless the image already created that
directory (with correct ownership) at the same path before the volume is
first attached — Docker copies the image directory's contents/permissions
into a volume on its first use. Used for the worker's YOLO weights cache
(`/app/weights`, `chown app:app` in `Dockerfile.worker` before `USER app`).

**Test harness for compose.yaml:** `tests/test_deployment_config.py` parses
`compose.yaml` with `yaml.safe_load` — no Docker daemon needed, `${VAR}`
strings are left un-interpolated by a raw YAML parse and that's fine since
the assertions only check keys/labels/tags, never variable values.

**pyproject.toml note:** dev group is installed by default with `poetry
install` (no `--with dev` needed); CI only adds `--extras ui` since the
`worker` extra pulls torch transitively through `ultralytics` even though
torch isn't a direct pyproject dependency — there's no way to get
fastapi/uvicorn out of the `worker` extra without also getting torch, so CI
skips the whole extra.
