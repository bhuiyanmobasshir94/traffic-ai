# Kubernetes runbook

Deploys this stack to a Kubernetes cluster with the Helm chart in
`deploy/helm/traffic-ai`. It reproduces the topology `compose.yaml` already
proves — a worker (FastAPI + inference) and a UI (Streamlit) behind **one**
ingress host, `/api` to the worker and everything else to the UI — so the
single-domain, no-CORS design in `CLAUDE.md` carries over unchanged. For the
single-server Docker Compose path, see `docs/DEPLOYMENT.md`.

The chart was verified with `helm lint`, `helm template`, and the structural
tests in `tests/test_helm_chart.py`. It has **not** been installed on a live
cluster; see "What is and is not verified" at the end before relying on it.

Verified against branch `production-hardening` (base commit `a70d210` plus
uncommitted working-tree changes) on 2026-10-01, with Helm v4.3.0. The
settings contract it mirrors is `src/traffic_ai/config.py` and
`compose.yaml` as of that commit.

## What the chart deploys

| Object | Purpose | Compose equivalent |
| --- | --- | --- |
| `Deployment` `<release>-worker` | FastAPI + CPU inference, port 8000 | `worker` |
| `Deployment` `<release>-ui` | Streamlit, port 8501 | `ui` |
| `Service` x2 (`ClusterIP`) | In-cluster addressing; nothing is exposed directly | the compose network |
| `Ingress` | One host; `/api` to the worker, `/` to the UI | Traefik routers |
| `ConfigMap` x2 | Non-secret `TRAFFIC_AI_*` settings | `environment:` blocks |
| `Secret` (optional) | Only if you do not name an existing one | `.env` |
| `ServiceAccount` | Identity only; no token mounted, no RBAC | n/a |
| `HorizontalPodAutoscaler`, `PodDisruptionBudget`, `NetworkPolicy` | Opt-in | n/a |

**Not deployed: Redis and Postgres.** Both are external and configured by URL
(see "Redis and Postgres"). Traefik and Let's Encrypt are replaced by your
ingress controller and your certificate process.

## Prerequisites

- A Kubernetes cluster and `kubectl` / `helm` (v3 or v4) pointed at it.
- An ingress controller. The chart defaults to `ingressClassName: nginx` and
  `values-production.yaml` carries ingress-nginx annotations; any controller
  works, but see "Ingress" for how `/api` priority differs between them.
- A TLS certificate for your host as a `kubernetes.io/tls` Secret, or a
  certificate manager that produces one. The chart installs neither cert-manager
  nor its CRDs.
- A Redis and a Postgres reachable from the cluster.
- Roughly 1 CPU core and 1Gi RAM *requested* (2 cores / 2Gi *limit*) free for
  the worker, plus a little for the UI. See "Resource expectations".
- The two images, pushed to a registry the cluster can pull from.

## Build and push the images

The chart references `traffic-ai-worker` and `traffic-ai-ui`, tagged with the
app version (`1.1.0`). Build from the repository root, as `compose.yaml` does:

```bash
docker build -f docker/Dockerfile.worker -t registry.example.com/traffic-ai/traffic-ai-worker:1.1.0 .
docker build -f docker/Dockerfile.ui     -t registry.example.com/traffic-ai/traffic-ai-ui:1.1.0 .
docker push registry.example.com/traffic-ai/traffic-ai-worker:1.1.0
docker push registry.example.com/traffic-ai/traffic-ai-ui:1.1.0
```

Set `image.registry` to `registry.example.com/traffic-ai`. Tags are pinned;
never deploy `:latest`. If the registry is private, create an image pull Secret
and list it under `imagePullSecrets`.

## Secrets: the API token and the database URL

In production the worker will **not start** without both of these. This is
`Settings._production_requires_hardening` in `src/traffic_ai/config.py`:
`TRAFFIC_AI_ENVIRONMENT=production` (which the chart sets, exactly as
`compose.yaml` does) refuses to build unless `TRAFFIC_AI_API_TOKEN` is set and
`TRAFFIC_AI_DATABASE_URL` is not the development default. Failing at startup is
the point: the alternative is a public deployment that silently serves a live
camera feed to anyone.

**Preferred: an existing Secret, referenced by name.** Create it yourself, out
of band, so the values never appear in a values file, in `helm template`
output, or in `helm get values`:

```bash
kubectl create namespace traffic-ai

kubectl -n traffic-ai create secret generic traffic-ai-secrets \
  --from-literal=api-token="$(openssl rand -hex 32)" \
  --from-literal=database-url="postgresql+asyncpg://USER:PASSWORD@HOST:5432/traffic_ai"
```

`values-production.yaml` already sets `auth.existingSecret: traffic-ai-secrets`
with the keys `api-token` and `database-url`. Different key names are
configurable via `auth.existingSecretTokenKey` and
`auth.existingSecretDatabaseUrlKey`. Only the worker receives these; the UI
holds neither (matching `compose.yaml`: the UI never connects to the database
and reads history over HTTP).

When `existingSecret` is set the references are *required*: a wrong Secret name
or key leaves the pod in `CreateContainerConfigError` with the missing name in
`kubectl describe pod`, rather than starting without credentials.

**Lesser path: generate a Secret from values.** For a scratch cluster you can
pass `--set auth.apiToken=... --set auth.databaseUrl=...` and the chart renders
a Secret. Values files get committed and shared, `--set` lands in shell
history, and `helm get values` prints it back — treat anything supplied this
way as already leaked. The defaults in `values.yaml` for both are empty, and
`tests/test_helm_chart.py` fails if either values file ever carries a value.

A Redis URL that embeds a password is also a secret. Do not put it in
`redis.url`; override `TRAFFIC_AI_REDIS_URL` on the pods with a `secretKeyRef`
through `worker.extraEnv` and `ui.extraEnv`. A pod's own `env:` entries take
precedence over the ConfigMap the chart loads with `envFrom`.

## Redis and Postgres

The chart does not vendor database charts. The only contract is a URL.

- **Managed service** (ElastiCache, Cloud SQL, RDS, ...): use the endpoint it
  gives you. Make sure the cluster's network can reach it.
- **In-cluster release** of any Redis or PostgreSQL chart you already operate:
  use its Service DNS name, `<service>.<namespace>.svc.cluster.local`.

| Setting | Where | Shape |
| --- | --- | --- |
| Redis | `redis.url` in values | `redis://redis-master.redis.svc.cluster.local:6379/0` |
| Postgres | `database-url` key of the Secret | `postgresql+asyncpg://USER:PASSWORD@HOST:5432/traffic_ai` |

The Postgres URL must use the `postgresql+asyncpg://` driver prefix, and its
password must not be `traffic` (the development default the worker refuses).
Redis is a TTL'd cache in this design (`CLAUDE.md`), so it needs no persistence;
`compose.yaml` runs it with `--save "" --appendonly no`, and the same settings
suit an in-cluster release.

## First deploy

1. **Create the namespace and the Secret** as above.

2. **Install.** `ingress.host` has no default in `values-production.yaml`: the
   chart refuses to render without it, so a forgotten flag fails loudly instead
   of deploying a placeholder hostname.

   ```bash
   helm upgrade --install traffic-ai deploy/helm/traffic-ai \
     --namespace traffic-ai \
     -f deploy/helm/traffic-ai/values-production.yaml \
     --set image.registry=registry.example.com/traffic-ai \
     --set ingress.host=traffic.example.org \
     --set redis.url=redis://redis-master.redis.svc.cluster.local:6379/0
   ```

3. **DNS.** Point `traffic.example.org` at your ingress controller's address
   (`kubectl get ingress -n traffic-ai`).

4. **Watch it come up.**

   ```bash
   kubectl -n traffic-ai get pods -w
   kubectl -n traffic-ai rollout status deploy/traffic-ai-worker --timeout=10m
   ```

   The first start can take several minutes: the worker downloads model weights
   before its pipelines reach RUNNING. The pod shows `Running` but `0/1` Ready
   until then. That is expected — see "Probes".

5. **Check it.**

   ```bash
   curl -fsS https://traffic.example.org/api/healthz
   curl -fsS https://traffic.example.org/api/readyz
   ```

   Both probe paths are exempt from the API token (a load balancer cannot
   present credentials). Everything else under `/api` is token-protected.

## Probes

| | Worker | UI |
| --- | --- | --- |
| startup | `/api/healthz`, up to 60 x 5s (5 minutes) | none |
| liveness | `/api/healthz` | `/_stcore/health` |
| readiness | `/api/readyz` | `/_stcore/health` |

`/api/readyz` returns **503 until a pipeline is RUNNING**, which is correct, not
a fault: it keeps the pod out of the Service without restarting it. Two things
make a slow first start safe. The `startupProbe` holds liveness and readiness
off until the process first answers `/api/healthz`, so a cold weights download
cannot trip the liveness probe and put the pod in a restart loop. Readiness then
has a generous `failureThreshold` (30 x 10s) so a pipeline that is merely slow to
reach RUNNING is never mistaken for a broken one. If your registry or network is
slow, raise `worker.startupProbe.failureThreshold` rather than lowering the
checks.

## Ingress

`/api` must win over the UI catch-all `/`. The chart declares both as
`pathType: Prefix` with **`/api` first**. Controllers resolve overlapping
prefixes differently — some choose the longest matching path regardless of the
order you write them, others take the first match in declaration order — and
listing `/api` first is correct under both. It is the Kubernetes equivalent of
`compose.yaml` giving the worker's Traefik router `priority=100` over the UI's
`1`. `tests/test_helm_chart.py` asserts the order.

Both Streamlit and the worker hold connections open for a long time:
Streamlit's `/_stcore/stream` WebSocket, and the worker's MJPEG streams. A
controller's default idle timeout (60s on ingress-nginx) will cut them, which
shows up as a UI that loads and then goes stale, or a video that freezes.
`values-production.yaml` raises the nginx timeouts and turns proxy buffering off;
if you use a different controller, set its equivalents through
`ingress.annotations`. The WebSocket upgrade itself is passed through by default
on ingress-nginx; if it fails, see the Streamlit WebSocket section of
`docs/DEPLOYMENT.md` — the diagnosis (`101 Switching Protocols` in the browser
Network tab, anything in front of the proxy stripping `Upgrade`) is identical.

TLS is `ingress.tls.enabled` plus `ingress.tls.secretName`. The ingress template
refuses to render with TLS enabled and no secret name.

## Resource expectations

CPU-only detection is the dominant cost (`docs/DEPLOYMENT.md`, "Resource
expectations"), so the worker requests and limits are sized for it rather than
left small:

| | CPU request / limit | Memory request / limit |
| --- | --- | --- |
| worker | 1 / 2 cores | 1Gi / 2Gi |
| ui | 100m / 500m | 256Mi / 512Mi |

Every container has both requests and limits. Tune the pipeline, not the limits,
if the worker falls behind: lower `worker.env.targetFps` first, then raise
`worker.env.detectEveryNFrames`, then lower `worker.env.frameWidth`. These map to
`TRAFFIC_AI_TARGET_FPS`, `TRAFFIC_AI_DETECT_EVERY_N_FRAMES`, and
`TRAFFIC_AI_FRAME_WIDTH`, with the same meaning as in `docs/DEPLOYMENT.md`.

**Worker replicas are redundancy, not scale-out.** Each worker replica runs every
camera pipeline and publishes to the same Redis keys, so a second replica doubles
the CPU bill without adding capacity or sharing work. `values-production.yaml`
keeps the worker at one replica and runs two UI replicas. Enabling
`autoscaling.enabled` creates an HPA for each Deployment; for the worker, treat
that as a deliberate choice rather than a default.

## Demo footage

`compose.yaml` mounts `./data/videos` read-only into the worker. The chart does
not provision storage for it. Create a PersistentVolumeClaim, populate it with
the files `make videos` fetches, and reference it:

```bash
--set worker.videosVolume.enabled=true \
--set worker.videosVolume.existingClaim=traffic-ai-videos
```

It is mounted read-only at `/data/videos`, as in Compose. Enabling it without a
claim name fails at render time. Without footage, expect the pipelines to report
a fault visibly (`CameraState.error`) and the worker to stay not-ready, since
readiness waits for a RUNNING pipeline.

## Security posture

Secure by default, and each item below is asserted by `tests/test_helm_chart.py`:

- **Non-root.** Pod and container run as uid/gid 1000 with `runAsNonRoot: true`,
  matching the `app` user baked into both Dockerfiles.
- **No privilege escalation.** `allowPrivilegeEscalation: false`, every
  capability dropped, `seccompProfile: RuntimeDefault`.
- **Read-only root filesystem on both containers.** The paths that need writes
  are `emptyDir` volumes: `/tmp` on both, and `/app/weights` on the worker.
  `YOLO_CONFIG_DIR` (ultralytics' settings file, already set by the worker
  Dockerfile) and `TORCH_HOME` (where torchvision — the default detector — caches
  its pretrained weights) both point at `/app/weights`; `HOME` points at `/tmp`.
  The UI's baked-in `/app/.streamlit` config is deliberately not mounted over.
- **No Kubernetes API access.** No ServiceAccount token is mounted and the chart
  creates no RBAC.
- **Not exposed.** Both Services are `ClusterIP`; only the ingress is reachable
  from outside, the Kubernetes form of "Redis and the worker are never published
  to the host".
- **`NetworkPolicy`** (opt-in, on in `values-production.yaml`): default-deny
  ingress to the worker except from the UI pods and the ingress controller.
  Check `networkPolicy.ingressControllerNamespaceSelector` and
  `ingressControllerPodSelector` match your controller (the defaults are
  ingress-nginx's), and that your CNI actually enforces NetworkPolicy — on one
  that does not, the object is accepted and does nothing. A Prometheus scraping
  `/api/metrics` is blocked by this policy until you allow it with
  `networkPolicy.extraIngressFrom`.

The weights volume is an `emptyDir`, so weights are downloaded again whenever the
pod is rescheduled. Compose keeps them in a named volume. If that costs too much
on your link, set `worker.weightsVolume.existingClaim` to a PVC.

## Upgrading, rolling back, uninstalling

```bash
# Upgrade: the same command as the first deploy, with new image tags or values.
helm upgrade --install traffic-ai deploy/helm/traffic-ai -n traffic-ai \
  -f deploy/helm/traffic-ai/values-production.yaml \
  --set image.registry=registry.example.com/traffic-ai \
  --set ingress.host=traffic.example.org

helm history traffic-ai -n traffic-ai
helm rollback traffic-ai <revision> -n traffic-ai
helm uninstall traffic-ai -n traffic-ai
```

Pods roll automatically when the ConfigMap, or a Secret the chart generated,
changes (a checksum annotation). A Secret **you** created is not watched:
after rotating `traffic-ai-secrets`, restart the worker so it re-reads it —

```bash
kubectl -n traffic-ai rollout restart deploy/traffic-ai-worker
```

Redis state is TTL'd and counters reset whenever the worker restarts, by design
(`CLAUDE.md`), so a rollback or restart is always safe with respect to Redis.
`helm uninstall` leaves the Secret you created, any PVCs, and your Redis and
Postgres untouched.

## Verifying without a cluster

None of this needs cluster access:

```bash
helm lint deploy/helm/traffic-ai
helm template traffic-ai deploy/helm/traffic-ai > /dev/null
helm template traffic-ai deploy/helm/traffic-ai \
  -f deploy/helm/traffic-ai/values-production.yaml \
  --set ingress.host=traffic.example.org
pytest tests/test_helm_chart.py        # skipped automatically if helm is absent
```

`helm template` with the defaults and with `values-production.yaml` are the two
shapes the tests render; HPA, PDB, and NetworkPolicy are rendered and checked
too.

## Troubleshooting

**Worker pod in `CrashLoopBackOff`, log says "Refusing to start in production".**
This is the safety check working. The log names the problem: either
`TRAFFIC_AI_API_TOKEN is unset` or `TRAFFIC_AI_DATABASE_URL still carries the
development password`. Supply the Secret (see above). With the generated-Secret
path, a key whose value was empty is simply absent, so it reaches the worker as
"unset" rather than as an empty token that would count as set.

**Worker pod in `CreateContainerConfigError`.** `auth.existingSecret` names a
Secret or key that does not exist in the release namespace.
`kubectl -n traffic-ai describe pod <pod>` names which.

**Worker `Running` but `0/1` Ready for a long time.** Expected for the first few
minutes (weights download, pipeline start). Past that, `kubectl logs` the worker:
a Redis or Postgres it cannot reach, or missing demo footage, are the usual
causes. `/api/readyz` is 503 until a pipeline is RUNNING.

**Worker restarts repeatedly during first start.** The startup budget is too
small for your network. Raise `worker.startupProbe.failureThreshold`.

**The UI loads but `/api/...` returns 404 or the dashboard shows no data.** The
ingress is sending `/api` to the UI. Check
`kubectl get ingress -n traffic-ai -o yaml`: `/api` must be listed first and
route to the worker Service. Confirm your controller's behaviour for overlapping
`Prefix` paths.

**The UI goes stale after a minute, or video freezes.** An idle timeout between
the browser and the pods is closing the WebSocket or MJPEG connection. Raise the
controller's read/send timeouts (see "Ingress").

**`/api/...` returns 401 from the browser.** See "Open question: browser access
with an API token" below.

**`helm template` fails with "ingress.host is required".** You used
`values-production.yaml` without `--set ingress.host=...`. That is intentional.

**`helm template` fails with "block forever".** `podDisruptionBudget.minAvailable`
is at or above a Deployment's replica count, which would make its pods
unevictable and block `kubectl drain`. Use `maxUnavailable` (the default), or raise
the replica counts.

**Node drain hangs.** A PodDisruptionBudget whose `minAvailable` equals the
replica count. The chart defaults to `maxUnavailable: 1`, which cannot block a
drain at any replica count, but it does mean a single-replica worker can be
evicted (briefly unavailable) during maintenance.

## What is and is not verified

Verified here: `helm lint`, `helm template` for the default values, for
`values-production.yaml`, and with `autoscaling.enabled`,
`podDisruptionBudget.enabled`, and `networkPolicy.enabled`, plus the structural
assertions in `tests/test_helm_chart.py`.

**Not verified:**

- Installation on a real cluster. No cluster was available; nothing was applied.
- `readOnlyRootFilesystem: true` against the built images. It is set on both
  containers, and the writable paths were derived from the Dockerfiles and the
  code (weights, the torch and ultralytics caches, `/tmp`), but no Docker daemon
  was available to run the images with `--read-only`. If a container fails with a
  "read-only file system" error, the log names the path to add as another
  `emptyDir`. Do not set the flag to `false` as the fix.
- NetworkPolicy enforcement, which depends on your CNI.

### Open question: browser access with an API token

The worker's `AuthMiddleware` requires the bearer token on every `/api` path
except the two probe paths, including the MJPEG stream the browser loads
directly. The UI does not send a token (`compose.yaml` gives it none), and a
browser `<img>` cannot attach an `Authorization` header. So with a token set and
nothing at the edge adding the header, a browser-direct `/api` request is
rejected with 401. `src/traffic_ai/config.py` describes the intended model as
edge authentication for browsers (Traefik BasicAuth in Compose) and the token
for scripts. This chart does not add an equivalent edge-auth layer; configure
one on your ingress controller, and do not put the token itself into an Ingress
annotation (annotations are not secret). This needs a decision before exposing
the dashboard to real users.
