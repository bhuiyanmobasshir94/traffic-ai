---
name: uv-lock-ci-docker-facts
description: Verified facts about uv.lock, the Dockerfiles, and CI/release workflows in traffic-ai (extras sufficiency, action pins, opencv collision, how to simulate builds without Docker)
metadata:
  type: project
---

Verified 2026-10-01 while moving the repo from pip to uv (pyproject is PEP 621 + hatchling; `uv.lock` is committed).

- **`test` extra is NOT enough for the suite.** `tests/ui/*` import streamlit and folium (the `ui` extra). Full suite needs `uv sync --locked --extra ui --extra test` (or `pip install -e ".[ui,test]"`). With `test` alone, 3 modules fail collection.
- **Locked env result:** with ui+test extras and a Postgres wired via `TRAFFIC_AI_TEST_DATABASE_URL`, suite is 369 passed / 2 skipped (ultralytics absent). Without Postgres, 21 db tests skip.
- **Alembic URL** comes from `Settings.database_url`, i.e. env `TRAFFIC_AI_DATABASE_URL` (alembic.ini has no URL). Run `alembic upgrade head` from the repo root (script_location is cwd-relative).
- **torch pairing is in the lock:** every torch/torchvision entry has `source = { registry = "https://download.pytorch.org/whl/cpu" }`. linux/amd64 resolves to `2.5.1+cpu`, linux/arm64 (py<3.13) to plain `2.5.1`; wheel URLs use `download-r2.pytorch.org`, so locked-down networks need egress to both hosts.
- **Open hazard:** ultralytics pulls `opencv-python` (GUI wheel, resolved 5.0.0.93) alongside pinned `opencv-python-headless` 4.14; both write `cv2/`, and `import cv2` returned 5.0.0 in a simulated install. The pyproject "hold on 4.x" comment is therefore not effective for the worker image. Candidate fix (not applied, needs a decision): `[tool.uv] override-dependencies = ["opencv-python; sys_platform == 'never'"]`.
- **Action pins:** `astral-sh/setup-uv` publishes NO floating major tag (use exact `v10.2.0`); docker/* and actions/* have floating majors (checkout v7, setup-python v7, buildx v4, qemu v4, login v4, metadata v6, build-push v7, codeql-action v4). trivy-action is SHA-pinned (v0.36.0 = ed142fd...). Re-check all of these when touching the workflows; they move.
- **Dockerfiles:** `uv sync --locked --no-install-project` replaces the old stub-package trick; uv image tag `ghcr.io/astral-sh/uv:0.11.25` exists (checked via ghcr token API). Builder-stage guards: worker runs a real `torchvision.ops.nms`; UI asserts torch/torchvision/ultralytics/cv2 are absent.
- **No-daemon verification recipe:** `UV_PROJECT_ENVIRONMENT=/tmp/x uv --directory /tmp/build sync --locked --no-dev --no-install-project --extra worker`, then again with `--no-editable` after copying README.md + src, reproduces both Dockerfile layers on the host. `uvx --from actionlint-py actionlint <abs paths>` lints workflows. Homebrew Postgres 14 (`initdb`, `pg_ctl -o "-p 54329 -k /tmp"`) stands in for the CI service container.
