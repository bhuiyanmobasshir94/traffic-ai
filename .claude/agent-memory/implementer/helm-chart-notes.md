---
name: helm-chart-notes
description: Helm chart (deploy/helm/traffic-ai) design decisions, helm 4 verification tricks, and tooling traps hit while building it and tests/test_helm_chart.py
metadata:
  type: project
---

Chart lives in `deploy/helm/traffic-ai`; tests in `tests/test_helm_chart.py` (shell out to `helm template`, skip if no helm).

Verified facts:
- `helm install <rel> <chart> --dry-run=client` works with NO cluster and prints NOTES.txt; plain `helm template` does not show NOTES.
- Settings validator only checks API token + non-dev DB URL for `environment=production`; compose.yaml hardcodes production, so the chart does too (ConfigMap, not a value). Without secrets the worker crash-loops by design.
- Default detector is torchvision, which downloads weights to `TORCH_HOME` (default under `$HOME/.cache`). With readOnlyRootFilesystem that must point at the writable `/app/weights` emptyDir; the chart sets `TORCH_HOME` and `YOLO_CONFIG_DIR` there and `HOME=/tmp`.
- `AuthMiddleware` (api/middleware.py) gates EVERY non-exempt `/api` path incl. the MJPEG stream; the UI sends no token. Browser access with a token is an unresolved app-level question (flagged in docs/KUBERNETES.md).
- Mounting an emptyDir over `/app/.streamlit` would hide the baked-in config; the UI only mounts `/tmp`.

Traps:
- Secret refs: `optional: {{ not existingSecret }}` — generated Secret path is optional so the app validator names the failure; existingSecret path is required so typos stop the pod.
- Secret keys are emitted only when non-empty (an empty-string token would be "set").
- Test lint: `"/tmp"` literals trip S108 (use a noqa'd constant); class-level set constants trip RUF012 (use module frozenset).
- In worktree sessions a Bash command of the form `cd <worktree> && cat >> file <<EOF` is refused as "too complex"; use Edit/Write. `.env.example` is permission-denied for Read and cat. Write/Edit paths must include the `.claude/worktrees/production-hardening/` segment.
- Docker daemon was not running, so readOnlyRootFilesystem was never exercised against the real images.
