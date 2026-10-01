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

Verified against this branch (`production-hardening`) on 2026-10-01, with Helm
v4.3.0. The settings contract it mirrors is `src/traffic_ai/config.py` and
`compose.yaml` as of that state. Anything not exercised by `helm lint`,
`helm template`, or `tests/test_helm_chart.py` is marked unverified where it
appears. "Known gaps" lists what the chart does not solve.

## What the chart deploys

| Object | Purpose | Compose equivalent |
| --- | --- | --- |
| `Deployment` `<release>-worker` | FastAPI + CPU inference, port 8000. **One replica, `Recreate` rollout**: see "Worker replicas" | `worker` |
| `Deployment` `<release>-ui` | Streamlit, port 8501 | `ui` |
| `Job` `<release>-migrate` | `alembic upgrade head`, as a pre-install/pre-upgrade **Helm hook** (see "Migrations") | `migrate` |
| `Service` x2 (`ClusterIP`) | In-cluster addressing; nothing is exposed directly | the compose network |
| `Ingress` | One host; `/api` to the worker, `/` to the UI; optional edge login (see "Edge authentication") | Traefik routers and middlewares |
| `ConfigMap` x2 | Non-secret `TRAFFIC_AI_*` settings | `environment:` blocks |
| `Secret` (optional) | Only if you do not name an existing one; a hook object while migrations are on | `.env` |
| `ServiceAccount` | Identity only; no token mounted, no RBAC | n/a |
| `HorizontalPodAutoscaler` (UI only), `PodDisruptionBudget`, `NetworkPolicy` | Opt-in | n/a |

**Not deployed: Redis and Postgres.** Both are external and configured by URL
(see "Redis and Postgres"). Traefik and Let's Encrypt are replaced by your
ingress controller and your certificate process.

## Prerequisites

- A Kubernetes cluster and `kubectl` / `helm` (v3 or v4) pointed at it.
- An ingress controller. `values.yaml` defaults to `ingressClassName: nginx`;
  **`values-production.yaml` defaults to Traefik**, the only controller where the
  whole edge login works (including the live video). Any controller serves the
  routes, but see "Ingress" for how `/api` priority differs between them and read
  "Edge authentication" before choosing: the controller decides how much of the
  login can work. With Traefik you also need its `Middleware` CRDs installed.
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
camera feed to anyone. The migration Job (see "Migrations") builds the same
`Settings`, so with a missing token or the development password the *install* fails at
the pre-install hook, before any pod exists.

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
`auth.existingSecretDatabaseUrlKey`. The worker receives both. The UI receives
**only the token**, from the same Secret and key: it calls the worker
server-to-server, bypassing the ingress, so it has to present the bearer itself.
It is given no database URL (matching `compose.yaml`: the UI never connects to
the database and reads history over HTTP), and its ConfigMap sets
`TRAFFIC_AI_PERSISTENCE_ENABLED=false` so the production gate does not ask it for
one. Without the token the UI would refuse to start, exactly as the worker does.

When `existingSecret` is set the references are *required*: a wrong Secret name
or key leaves the pod in `CreateContainerConfigError` with the missing name in
`kubectl describe pod`, rather than starting without credentials.

**Lesser path: generate a Secret from values.** For a scratch cluster you can
pass `--set auth.apiToken=... --set auth.databaseUrl=...` and the chart renders
a Secret. Values files get committed and shared, `--set` lands in shell
history, and `helm get values` prints it back — treat anything supplied this
way as already leaked. The defaults in `values.yaml` for both are empty, and
`tests/test_helm_chart.py` fails if either values file ever carries a value.

There is one more cost to this path, a consequence of the migration hook (see
"Migrations"): Helm creates a release's ordinary objects only *after* its
pre-install hooks, so a generated Secret would not exist yet when the migration Job
starts. While `migrations.enabled` (the default) the generated Secret is therefore
itself a pre-install/pre-upgrade hook, created first (weight `-10`, the Job is `0`)
and recreated on every upgrade. Helm does not track hook objects as part of the
release, so **`helm uninstall` leaves that Secret, credentials included, behind**:
delete it yourself (`kubectl -n traffic-ai delete secret traffic-ai` for a release
named `traffic-ai`; `kubectl get secret` shows the name). An `existingSecret` has
neither problem, which is a further reason to use one.

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

   `values-production.yaml` selects Traefik and names three Middleware CRs it
   expects to exist (`traffic-ai-ratelimit`, `traffic-ai-basicauth`,
   `traffic-ai-api-bearer`). **Create them before installing**: YAML and the Secrets
   they need are in "Edge authentication", "Traefik". (On ingress-nginx instead, create
   the BasicAuth Secret described under "ingress-nginx" and pass `--set
   ingress.className=nginx`.)

2. **Install.** `ingress.host` has no default in `values-production.yaml`: the
   chart refuses to render without it, so a forgotten flag fails loudly instead
   of deploying a placeholder hostname. Before the first Deployment is created
   Helm runs the database migration Job (see "Migrations"); the command returns
   only after it succeeds, and fails, leaving nothing running, if it does not.

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

   Both probe paths are exempt from the API *token* (a load balancer cannot
   present credentials). Everything else under `/api` is token-protected. If you
   turned on edge authentication (`values-production.yaml` does), the ingress
   gates the whole host first, probe paths included, so add `-u admin` (curl
   prompts for the password). The kubelet's own probes hit the pod directly and
   are unaffected; an *external* uptime monitor needs the BasicAuth credentials
   or must be pointed at something else.

## Migrations

The worker never migrates the database on its own (`create_app` only builds a
`Database`), so without a migration step nothing creates the history tables. The
chart runs one: a `Job` (`<release>-migrate`) that executes `alembic upgrade head`
from the worker image, as a Helm hook on **pre-install and pre-upgrade**
(`migrations.enabled`, default `true`; `templates/job-migrate.yaml`). It is
`compose.yaml`'s `migrate` service for Kubernetes.

- **A failed migration fails the release** and leaves the running pods alone: the new
  Deployments are not applied. A failed Job is kept so you can read it —
  `kubectl -n traffic-ai logs job/traffic-ai-migrate` — and is cleared on the next
  attempt (`hook-delete-policy: before-hook-creation,hook-succeeded`: a successful
  Job is removed, so a clean release leaves nothing behind). Retries are bounded
  (`migrations.backoffLimit`, default 2) and the Job has a hard deadline
  (`migrations.activeDeadlineSeconds`, default 240, under Helm's 5-minute hook
  timeout, so the Job fails with its own reason).
- **It runs the same production gate as the worker**, with the same Secret references:
  a missing token or a database URL carrying the development password fails *here*,
  before any pod is replaced. A wrong `existingSecret` name or key leaves the Job's pod
  in `CreateContainerConfigError` until the deadline.
- **It cannot use anything the chart creates after the hooks run.** Helm applies
  ordinary objects only after pre-install hooks finish, so on the first install the
  ConfigMap and the chart's ServiceAccount do not exist yet. The Job therefore sets
  its own environment (production, log settings, the `allowUnauthenticated` mirror, the
  Secret references) instead of loading the ConfigMap, and runs as the namespace's
  `default` ServiceAccount (or `serviceAccount.name` when `serviceAccount.create` is
  false), with no token mounted. It has the same hardened security context as the
  workloads, a read-only root filesystem with a `/tmp` emptyDir, and requests and
  limits. It is not selected by either Service.
- **The generated-Secret path needs a hook Secret** (see "Secrets"), which outlives
  `helm uninstall`. With `auth.existingSecret` the Secret is yours and exists already.
- **Upgrades migrate before the old pods stop**, so for a moment the new schema is
  live under the previous release's code. Keep each revision backward-compatible within
  a release: add, do not rename or drop. `alembic downgrade` is not a rollback (the
  initial revision's downgrade drops the history tables); the safe rollback of a bad
  migration is a database backup.
- `migrations.enabled=false` renders no Job (and the generated Secret becomes an
  ordinary object again). Then you own the schema: run `alembic upgrade head` yourself
  against the same database before the worker starts. Without it `/api/readyz` reports
  `database: false` and history is unavailable; the live dashboard is unaffected.

`tests/test_helm_chart.py` renders the Job and asserts the hook annotations, the command,
the hardening, the Secret ordering, and that its environment passes (and, with a missing
token or a development-password URL, is refused by) the real `Settings` production gate.
**Not verified:** that the Job runs to completion against a real Postgres, and Helm's
hook ordering and cleanup on a live cluster.

## Worker replicas

**The worker runs as exactly one replica.** Every worker replica runs *every* camera
pipeline and writes its own copy of each crossing to Postgres, so a second replica does
not share the work: it duplicates history and overwrites the other's live counters in
Redis. It is neither scale-out nor safe redundancy, so the chart enforces what it can:

- The worker Deployment uses `strategy: Recreate`, so a rollout never runs the old and
  the new pod together. **The price is a gap** while the old pod terminates (up to its
  grace period) and the new one starts and downloads weights (the weights volume is an
  `emptyDir` unless you set a PVC); the dashboard reports stale in between.
- `autoscaling.enabled` creates an HPA for the **UI only**. Asking for a worker HPA
  (`autoscaling.worker.enabled=true`) fails the render with a message saying why, rather
  than rendering something that corrupts history.
- `worker.replicaCount` is `1`. The chart does **not** refuse a higher value: it only
  prints a warning in `NOTES.txt`. That is a gap (see "Known gaps"), so do not set it.
  To add capacity, raise `worker.resources` and tune `worker.env` (see "Resource
  expectations").

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
shows up as a UI that loads and then goes stale, or a video that freezes. With
ingress-nginx, raise its timeouts and turn proxy buffering off through
`ingress.annotations`: the exact annotations are in the commented "Using
ingress-nginx instead" block of `values-production.yaml`, which no longer applies
them itself now that it defaults to Traefik. For Traefik, which
`values-production.yaml` selects, no annotation is set; **unverified:** that
Traefik's default entrypoint timeouts leave the WebSocket and the long-lived MJPEG
streams alone (no live controller was available). If either is cut off, tune
`respondingTimeouts` on Traefik's entrypoint, which is Traefik's own configuration,
not this chart's. The WebSocket upgrade itself is passed through by default
on ingress-nginx; if it fails, see the Streamlit WebSocket section of
`docs/DEPLOYMENT.md` — the diagnosis (`101 Switching Protocols` in the browser
Network tab, anything in front of the proxy stripping `Upgrade`) is identical.

TLS is `ingress.tls.enabled` plus `ingress.tls.secretName`. The ingress template
refuses to render with TLS enabled and no secret name.

## Edge authentication

**The problem.** The worker's `AuthMiddleware` requires
`Authorization: Bearer <token>` on every `/api` path except the two probe paths.
That includes the MJPEG video stream, which the browser loads directly through an
`<img>` tag, and an `<img>` cannot attach a header. `src/traffic_ai/config.py`
describes the intended model: a human logs in **once at the edge**, and the edge
supplies the bearer toward the worker; the token is for scripts and for the UI's
own server-to-server calls. `compose.yaml` does exactly that with Traefik
(BasicAuth, then inject `Authorization: Bearer ...`), so the browser never sees
the token.

**What this chart can do about it depends on the controller.**

| | ingress-nginx | Traefik |
| --- | --- | --- |
| Human login at the edge (BasicAuth) | yes | yes |
| UI server-side calls to the worker (token from the Secret) | yes | yes |
| Browser-direct `/api`, i.e. the live video | **no, 401** unless you take option (a) or (b) below | yes |

The token is injected toward the worker only where a controller can do it without
a configuration snippet. ingress-nginx disables snippets by default, and an
Ingress annotation would put the token in plain sight (annotations are not
secret and are printed by `helm template` and `kubectl get ingress -o yaml`), so
this chart never does it.

### ingress-nginx

```yaml
ingress:
  className: nginx
  edgeAuth:
    enabled: true
    secretName: traffic-ai-basic-auth   # a Secret you create; the chart never renders it
    realm: "Traffic AI - Authentication Required"
    limitRps: 20                        # per-client request rate; 0 leaves the annotation out
  tls:
    enabled: true       # required: BasicAuth over plain HTTP sends the password in clear
    secretName: traffic-ai-tls
```

`values-production.yaml` carries this pair of choices as a documented alternative
(it defaults to Traefik): pass `--set ingress.className=nginx`, add the streaming
annotations from its commented block, and point `networkPolicy` at ingress-nginx's
namespace and pods (its defaults there are Traefik's). Create the Secret first,
with the htpasswd lines under the key **`auth`**, hashed with bcrypt cost 12 and a
long random password (the bcrypt CPU note in `docs/DEPLOYMENT.md`, "First deploy"
step 4, applies here too):

```bash
kubectl -n traffic-ai create secret generic traffic-ai-basic-auth \
  --from-file=auth=<(htpasswd -nbB -C 12 admin '<password>')
```

This sets `nginx.ingress.kubernetes.io/auth-type: basic`, `auth-secret`, and
`auth-realm` on the Ingress, plus `nginx.ingress.kubernetes.io/limit-rps` (from
`ingress.edgeAuth.limitRps`, default 20, rendered as a string) because BasicAuth has
no lockout of its own and an unthrottled login can be guessed at wire speed. An
annotation you set yourself under `ingress.annotations` wins over the chart's value.
nginx's burst allowance is its own default. They apply to the **whole host**, `/api`
included, so an unauthenticated request to any path is a 401 from the controller
before it reaches a pod. The chart refuses to render if the Secret name is missing,
or if TLS is off.

**The gap.** nginx forwards the browser's `Authorization: Basic ...` header to the
worker unchanged. The worker wants a bearer, so it answers the browser-direct
`/api` requests with 401, and the dashboard's video does not play (the rest of
the page works: its numbers come through the UI pod's server-side calls). Pick one
before putting this in front of real users:

- **(a) Accept an edge-only API.** The login is then the only gate on `/api`.
  Create the existing Secret with an **empty** `api-token` value
  (`--from-literal=api-token=`) and set `auth.allowUnauthenticated=true`. A blank
  token counts as no token, so `AuthMiddleware` is not installed, and the worker
  ignores the Basic header it receives. The tradeoff is real: anything that can
  reach the worker's port without going through the ingress is unauthenticated,
  which is why this is only reasonable with `networkPolicy.enabled=true` (UI pods
  and the ingress controller only). Programmatic clients lose their bearer-token
  path. This is the opposite of fail-closed by default; it has to be chosen.
- **(b) Put a forward-auth or reverse proxy in front of `/api`** that you operate,
  which authenticates the human and sets `Authorization: Bearer <token>` toward
  the worker. This chart does not provide one, and **this option was not tried**.
- **(c) Use Traefik as the ingress controller**, below. It is the only option here
  that keeps the worker token-protected *and* the video working, and it is the
  same pattern `compose.yaml` runs.

A configuration snippet that sets the header would also work on a controller that
allows snippets. It is deliberately not offered: the token would live in an
Ingress annotation, visible to anyone who can read Ingress objects and printed by
`helm template`.

### Traefik

Set the class and name the Middleware CRs you create. The chart **does not
template CRDs**; it only references them, so it renders on a cluster without the
Traefik CRDs.

This is what `values-production.yaml` ships (it sets all of the below). The three
names are the ones it expects; the namespace in them is `traffic-ai`:

```yaml
ingress:
  className: traefik
  edgeAuth:
    enabled: true
  tls:
    enabled: true
    secretName: traffic-ai-tls
  traefik:
    # <namespace>-<name>@kubernetescrd, in execution order.
    middlewares:
      - traffic-ai-ratelimit@kubernetescrd
      - traffic-ai-basicauth@kubernetescrd
      - traffic-ai-api-bearer@kubernetescrd
```

That renders `traefik.ingress.kubernetes.io/router.middlewares:
traffic-ai-ratelimit@kubernetescrd,traffic-ai-basicauth@kubernetescrd,traffic-ai-api-bearer@kubernetescrd`,
and the chart refuses to render if `edgeAuth.enabled` is set with an empty list.
**Order matters**, for the same reasons as in `compose.yaml`: the rate limit runs
first so that login guesses are throttled (429) before BasicAuth checks them
(BasicAuth has no lockout); BasicAuth consumes the browser's `Authorization` header
next; and only a request that has passed is given the service token in its place.
Reversed, the login check would run against a header that had already been
overwritten, and a limiter after BasicAuth would never see the guesses it exists to
slow down.

The three Middleware CRs, created out of band in the release namespace (this YAML
is **unverified**: no cluster was available, and Traefik's CRD fields are as
documented for Traefik v3 at the time of writing. Run
`kubectl apply --dry-run=server` first). Leave the `ratelimit` one out of the list
only knowingly: nothing in the chart makes you have one.

```yaml
# ratelimit.yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: ratelimit
  namespace: traffic-ai
spec:
  rateLimit:
    average: 20          # requests per period (default period: 1s), per source address
    burst: 40
    sourceCriterion:
      ipStrategy:
        depth: 0         # key on the TCP peer. Only right if client addresses reach
                         # Traefik intact: see "Rate limits and proxy hops"
---
# basicauth.yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: basicauth
  namespace: traffic-ai
spec:
  basicAuth:
    secret: traffic-ai-traefik-users   # Secret with the htpasswd lines under the key `users`
    realm: Traffic AI
    removeHeader: true                 # do not forward the browser's Basic credentials
---
# api-bearer.yaml  (keep out of git: it carries the token)
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: api-bearer
  namespace: traffic-ai
spec:
  headers:
    customRequestHeaders:
      Authorization: "Bearer REPLACE_WITH_TOKEN"
```

```bash
# Traefik's BasicAuth Secret uses the key `users` (ingress-nginx uses `auth`).
# bcrypt cost 12 and a long random password; see docs/DEPLOYMENT.md, step 4.
kubectl -n traffic-ai create secret generic traffic-ai-traefik-users \
  --from-file=users=<(htpasswd -nbB -C 12 admin '<password>')

kubectl apply -f ratelimit.yaml
kubectl apply -f basicauth.yaml

# Substitute the real token at create time; never commit it. `create` (and, on
# rotation, `replace`) rather than `apply`: see the next paragraph.
TOKEN="$(kubectl -n traffic-ai get secret traffic-ai-secrets \
  -o jsonpath='{.data.api-token}' | base64 -d)"
sed "s|REPLACE_WITH_TOKEN|${TOKEN}|" api-bearer.yaml | kubectl create -f -
```

**The token sits in clear text in the `api-bearer` Middleware.** To our knowledge
Traefik's `headers` middleware takes literal values and has no Secret reference
for them, so this is the one place the token is not in a Secret. Anyone who can
`get` `middlewares.traefik.io` in that namespace can read it. Restrict that RBAC,
do not commit the manifest, and when you rotate the token, update the Secret,
`kubectl replace` the Middleware, and restart both Deployments (`kubectl -n
traffic-ai rollout restart deploy/traffic-ai-worker deploy/traffic-ai-ui`). It is
the same exposure `compose.yaml` has (the token appears in the worker's container
labels), and it is why the chart does not render it.

**A second copy of the token is easy to leave behind: `last-applied-configuration`.**
`kubectl apply` (client-side) stores the whole object it applied in the
`kubectl.kubernetes.io/last-applied-configuration` annotation, so applying the
Middleware with the token in it would put the token in that annotation as well,
visible to anyone who can read the Middleware and printed by `kubectl get -o yaml`.
That is why the commands above use `create` and `replace`, which to our knowledge
do not write it. Check after creating it:
`kubectl -n traffic-ai get middleware api-bearer -o yaml | grep -c last-applied`
should print `0`. **Not verified on a cluster.** If you manage Middlewares through
GitOps (Argo CD, Flux) the token will be in whatever store holds the manifest, which
is the thing the "do not commit it" rule exists to avoid; use a sealed or
externally-injected secret mechanism for that one object. See "Known gaps".

One Ingress carries both paths, so the middleware chain also runs for the UI path:
the UI pod receives a bearer token it already holds from its own Secret. That is
harmless, but if you want the token on `/api` only, create two Ingress objects of
your own instead of using this value.

With Traefik as the controller, the NetworkPolicy must admit it. `values-production.yaml`
already does (these are Traefik's selectors; `values.yaml`'s defaults are
ingress-nginx's), but they are guesses at where **your** Traefik runs and how it is
labelled, and the policy is default-deny, so a mismatch locks Traefik out of `/api`:

```yaml
networkPolicy:
  ingressControllerNamespaceSelector:
    kubernetes.io/metadata.name: traefik       # wherever Traefik runs
  ingressControllerPodSelector:
    app.kubernetes.io/name: traefik
```

Check with `kubectl get pods -A -l app.kubernetes.io/name=traefik`.

### Check that it is enforced

The check that matters is the negative one: a request with no credentials must be
refused, and a controller that ignores the annotation fails open silently.

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://traffic.example.org/           # expect 401
curl -s -o /dev/null -w '%{http_code}\n' https://traffic.example.org/api/cameras  # expect 401
curl -s -o /dev/null -w '%{http_code}\n' -u admin https://traffic.example.org/api/cameras
#   Traefik: 200.   ingress-nginx: 401 (the gap above, from the worker, not the edge)
```

A `200` on the first two means the controller is not enforcing the annotations.
That happens when `ingressClassName` names a controller that does not understand
them, which is why the chart cannot make this check for you.

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

**The worker is one replica; the UI can scale.** See "Worker replicas": a second
worker replica duplicates history rather than adding capacity.
`values-production.yaml` keeps the worker at one replica and runs two UI replicas, and
`autoscaling.enabled` creates an HPA for the UI only.

## Graceful shutdown

When a pod is deleted, Kubernetes sends `SIGTERM` and then `SIGKILL` after
`terminationGracePeriodSeconds`. The worker's shutdown is bounded by the code: uvicorn
first waits up to 5 seconds for open requests (`GRACEFUL_SHUTDOWN_SECONDS`, in
`api/__main__.py`; open MJPEG streams are told to end), then the lifespan waits up to 10
seconds for the pipelines to stop and up to 10 more for the history writer to flush its
buffer to Postgres (`_SHUTDOWN_TIMEOUT_SECONDS`, in `api/app.py`): 25 seconds worst case.

- **worker: `worker.terminationGracePeriodSeconds: 30`.** Kubernetes' own default is
  also 30; it is set explicitly (and matches `compose.yaml`'s `stop_grace_period`) so it
  cannot be lowered by accident. Do not set it below 25 while persistence is on, or the
  final flush can be cut off and those crossings lost.
- **ui: `ui.terminationGracePeriodSeconds: 15`**, as in Compose.

A pod that is killed hard (node loss, OOM) loses the crossings still in its buffer; the
`history_events_lost` signals cannot see that (see "Observability").

## Rate limits and proxy hops

Two limiters, for two jobs.

- **At the edge**, in front of the login: a RateLimit Middleware on Traefik (see "Edge
  authentication", "Traefik"), or on ingress-nginx
  `nginx.ingress.kubernetes.io/limit-rps` (`ingress.edgeAuth.limitRps`, default 20,
  added when edge auth is on). BasicAuth has no lockout, so this is what slows online
  password guessing. The figures are estimates, not measurements.
- **In the worker**: `worker.env.rateLimitEnabled`, `rateLimitRequests` (120 per client
  per 60-second window) and `rateLimitStreamRequests` (10), which become
  `TRAFFIC_AI_RATE_LIMIT_ENABLED`, `TRAFFIC_AI_RATE_LIMIT_REQUESTS` and
  `TRAFFIC_AI_RATE_LIMIT_STREAM_REQUESTS`. It does not count the two probe paths or
  ordinary requests that carry the valid API token; a wrong or missing token is still
  counted (`src/traffic_ai/api/middleware.py`).

**`worker.env.trustedProxyHops`** (`TRAFFIC_AI_TRUSTED_PROXY_HOPS`, default `1`) is how
many reverse proxies sit between the client and the worker pod, so the worker's limiter
can find the real client address in `X-Forwarded-For`: the client is the entry that many
places from the **right**, because each proxy appends the address it received the
request from and anything further left was written by the caller. `1` means one ingress
controller. A cloud load balancer in front of the controller that also appends to
`X-Forwarded-For` makes it `2`. **Too low** and every client shares the proxy's bucket;
**too high** and the entry it picks was not supplied by a proxy. `0` ignores the header
and uses the socket peer. When the header is absent, short, or the chosen entry is not an
IP address, the limiter falls back to the socket peer. The same question decides the
`ipStrategy.depth` of the edge RateLimit Middleware. If the load balancer in front of
your ingress controller replaces the client address (SNAT) instead of passing it on,
every client arrives from the same address and **shares one bucket**, edge and worker
alike; `externalTrafficPolicy: Local` or the PROXY protocol are the usual ways to keep
the address. That behaviour depends on your cluster and was **not verified**.

## Observability

`/api/readyz` carries two fields that are reported but never gate readiness (they do not
take the pod out of the Service):

- `database`: `true` when Postgres answered **and** has the history table; `false` when
  it is unreachable or not migrated; `null` when persistence is off.
- `history_events_lost`: crossings counted live but never persisted since the worker
  started (buffer overflow during a database outage, or a batch the database refused).
  `null`, not `0`, when there is no history writer.

The same count is the Prometheus counter `history_events_lost_total{reason}` with reasons
`buffer_full` and `flush_failed`, both present at 0 from the first scrape, so
`increase(history_events_lost_total[15m]) > 0` is the alert (standard PromQL, **not run**
against a Prometheus). `docs/DEPLOYMENT.md` has the detail. Scraping `/api/metrics` needs
the bearer token, and a Prometheus pod is blocked by the NetworkPolicy until you allow it
(`networkPolicy.extraIngressFrom`).

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
- **The migration Job is held to the same standard**: same security context, a
  read-only root filesystem with a `/tmp` emptyDir, requests and limits, no token
  mounted, and no ConfigMap or chart-created ServiceAccount (see "Migrations").
- **`NetworkPolicy`** (opt-in, on in `values-production.yaml`): default-deny
  ingress to the worker except from the UI pods and the ingress controller.
  Check `networkPolicy.ingressControllerNamespaceSelector` and
  `ingressControllerPodSelector` match your controller (the `values.yaml` defaults
  are ingress-nginx's; `values-production.yaml` sets Traefik's), and that your CNI
  actually enforces NetworkPolicy — on one that does not, the object is accepted
  and does nothing. A Prometheus scraping `/api/metrics` is blocked by this policy
  until you allow it with `networkPolicy.extraIngressFrom`. The policy governs
  ingress only: the migration Job's pod is not selected by it.

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

Every upgrade runs the migration hook first (see "Migrations"); if it fails the
release fails and the running pods are untouched. A rollout of the worker is
`Recreate`: the old pod stops (up to its grace period) before the new one starts,
so expect a short stale dashboard, not two workers writing at once. `helm rollback`
neither runs the migration hook (it is `pre-install,pre-upgrade` only) nor undoes a
migration: the schema stays where it is, which is why migrations must stay
backward-compatible.

Redis state is TTL'd and counters reset whenever the worker restarts, by design
(`CLAUDE.md`), so a rollback or restart is always safe with respect to Redis.
`helm uninstall` leaves the Secret you created, any PVCs, and your Redis and
Postgres untouched, and **also a chart-generated Secret** (a hook object while
migrations are on; see "Secrets").

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

`helm template` with the defaults, with `values-production.yaml` (Traefik), and with
`values-production.yaml` plus `--set ingress.className=nginx` are the three shapes the
tests render; HPA, PDB, and NetworkPolicy are rendered and checked too. `helm template`
does not print `NOTES.txt`; `helm install ... --dry-run=client` does, with no cluster.

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

**`helm install` / `helm upgrade` fails at the `pre-install` / `pre-upgrade` hook.**
The migration Job failed (see "Migrations"). `kubectl -n traffic-ai logs
job/traffic-ai-migrate` shows why: the production-gate refusal (token unset, or the
development database password), Postgres unreachable or rejecting the credentials, or an
Alembic error. A pod stuck in `CreateContainerConfigError` means `auth.existingSecret`
names a Secret or key that does not exist (`kubectl describe pod -l
app.kubernetes.io/component=migrate`). The Job is kept for you to read and cleared by the
next attempt. With **no** Secret configured at all, a plain `helm install` now fails here
instead of crash-looping the worker, for the same reason.

**`helm template` fails with "autoscaling.worker.enabled is not supported".** Deliberate:
each worker replica runs every camera pipeline and writes duplicate history. Only the UI
can be autoscaled (`autoscaling.enabled`); give the worker more `resources` instead.

**Rolling the worker leaves the dashboard stale for a minute or more.** The worker rolls
out with `Recreate` (never two at once), so there is a gap while the old pod terminates
(up to its grace period) and the new one starts. With the default `emptyDir` weights
volume the new pod also downloads the weights again; a PVC
(`worker.weightsVolume.existingClaim`) shortens that.

**`429 Too Many Requests` from the edge.** The rate limit in front of the login (see "Rate
limits and proxy hops"). Expected after a run of wrong passwords. If legitimate users
hit it, suspect many of them sharing one address, or a load balancer that makes every
client look like one, before raising the limit.

**The UI loads but `/api/...` returns 404 or the dashboard shows no data.** The
ingress is sending `/api` to the UI. Check
`kubectl get ingress -n traffic-ai -o yaml`: `/api` must be listed first and
route to the worker Service. Confirm your controller's behaviour for overlapping
`Prefix` paths.

**The UI goes stale after a minute, or video freezes.** An idle timeout between
the browser and the pods is closing the WebSocket or MJPEG connection. Raise the
controller's read/send timeouts (see "Ingress").

**`/api/...` returns 401 from the browser, and the video does not play.** On
ingress-nginx with edge authentication this is the known gap: the controller
passes the BasicAuth header through and the worker wants a bearer. See "Edge
authentication" for options (a) to (c). Check where the 401 comes from: the
worker's body is `{"detail":"Unauthorized"}` with `WWW-Authenticate: Bearer`; the
controller's is an HTML page with `WWW-Authenticate: Basic`.

**`helm template` fails with "ingress.edgeAuth.secretName is required",
"requires ingress.tls.enabled", or "ingress.traefik.middlewares is required".**
Edge authentication was enabled without what would enforce it. Each is a deliberate
refusal to render an Ingress that looks protected and is not.

**Everything under `/` and `/api` is 200 with no credentials although edge
authentication is on.** The controller is not enforcing the annotations, usually
because `ingressClassName` names a different controller than the one the
annotations are written for. See "Check that it is enforced".

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

## Known gaps

What this chart does **not** solve. None is hidden by a default.

- **The API token in the Traefik `api-bearer` Middleware.** The token sits in clear text
  in a Middleware CR (Traefik's `headers` middleware takes literal values), readable by
  anyone who can `get` it, and `kubectl apply` would add a second copy in
  `kubectl.kubernetes.io/last-applied-configuration`. The commands in this runbook use
  `create`/`replace` to avoid the second copy; a GitOps controller that applies the
  manifest brings it back, and puts the token in whatever store holds the manifest.
  Restrict RBAC on `middlewares.traefik.io`. Not fixed, because nothing in Traefik's CRD
  that we know of takes a Secret reference for a header value. **Unverified on a cluster.**
- **The chart does not refuse `worker.replicaCount` above 1.** It warns in `NOTES.txt` and
  never autoscales the worker, but a manual `--set worker.replicaCount=2` renders, and the
  second replica duplicates history. Failing closed there is a possible follow-up.
- **The generated Secret outlives `helm uninstall`** while migrations are on (a hook
  object). Prefer `auth.existingSecret`.
- **Synchronous work on the worker's event loop.** Video decode and detection run on
  threads, but each frame's resize, tracker update, and annotation with JPEG encoding run
  directly on the event loop (`worker/pipeline.py`, `_process_frame`). With two cameras
  that is fine; as the camera count grows these add up on one loop and can delay every
  pipeline, the probes, and the MJPEG streams together (and a probe that times out
  restarts the pod). Read from the code, **not profiled**. Lower
  `worker.env.frameWidth` and `worker.env.targetFps` first; a structural fix means moving
  that work off the loop.
- **The Docker socket** gap in `docs/DEPLOYMENT.md` is a Compose concern (Traefik's Docker
  provider); it does not apply to the Kubernetes provider, which uses the API server with
  RBAC the Traefik install brings.
- **BasicAuth has no lockout.** It is rate-limited and bcrypt-hashed, not locked out per
  user, and there is no alerting on failed logins.
- **Middleware CRs and the ingress controller are yours.** The chart does not install,
  template, or validate them; a mis-typed name is caught by Traefik at runtime, not at
  render.

## What is and is not verified

Pinned to this branch (`production-hardening`), 2026-10-01, Helm v4.3.0.

Verified here: `helm lint`, `helm template` for the default values, for
`values-production.yaml` (Traefik), with `ingress.className=nginx`, and with
`autoscaling.enabled`, `podDisruptionBudget.enabled`, and `networkPolicy.enabled`,
plus the structural assertions in `tests/test_helm_chart.py`: the annotations each
controller mode renders, the middleware order (rate limit, login, bearer), that no
token or snippet reaches the Ingress, that the rendered UI and worker ConfigMaps (plus
their Secret-backed variables) are accepted by the real `Settings` production gate, and
that the same holds for the migration Job's environment (which the gate refuses with no
token or the development password). The migration hook's annotations, its ordering
against a generated Secret, its hardening, and that it depends on nothing created after
pre-install hooks were asserted on the rendered manifests. The worker's single replica,
`Recreate` rollout, grace period, and the refusal to render a worker HPA were rendered
and asserted. Option (a)'s claim (an empty token with `allowUnauthenticated` starts both
processes with the API open) was checked against `Settings` directly. The chart's rate
limit and proxy-hop settings were checked against the real `Settings` fields.

**Not verified:**

- Installation on a real cluster. No cluster was available; nothing was applied.
- **Helm's hook behavior on a live cluster**: that the Job runs before the rest of the
  chart on first install, that the generated Secret hook is created before it, that a
  failed Job fails the release and leaves the old pods running, and that
  `before-hook-creation,hook-succeeded` cleans up as described. This follows Helm's
  documented hook semantics and was not observed.
- The migration Job running `alembic upgrade head` against a real Postgres under
  `readOnlyRootFilesystem: true` (the same flag, and the same caveat, as below).
- **The rate limits** (the Traefik RateLimit Middleware and `limit-rps`), the effect of
  `trustedProxyHops` through a real load balancer, and whether source addresses survive
  your load balancer at all.
- The grace period's effect on a real pod deletion.
- `readOnlyRootFilesystem: true` against the built images. It is set on both
  containers, and the writable paths were derived from the Dockerfiles and the
  code (weights, the torch and ultralytics caches, `/tmp`), but no Docker daemon
  was available to run the images with `--read-only`. If a container fails with a
  "read-only file system" error, the log names the path to add as another
  `emptyDir`. Do not set the flag to `false` as the fix.
- NetworkPolicy enforcement, which depends on your CNI.
- **Edge authentication on a live controller.** No ingress-nginx or Traefik was
  running, so neither the BasicAuth challenge, the 401 on unauthenticated
  requests, nor the header injection was observed. The annotation names are
  ingress-nginx's and Traefik's documented ones; the ingress-nginx and Traefik
  Secret key names (`auth` and `users`) and the Middleware CR schema are from
  their documentation, not from a cluster. Run "Check that it is enforced" after
  installing.
- **The ingress-nginx video gap** is derived from reading the worker's
  `AuthMiddleware` and from nginx forwarding the `Authorization` header by
  default; it was not reproduced in a cluster.
- **Option (b)** (a forward-auth or reverse proxy that injects the bearer) is a
  suggestion, not something this chart provides or that was tried.

### Decision for the operator: how browsers reach `/api`

`values-production.yaml` now defaults to Traefik with the full middleware chain (rate
limit, BasicAuth, bearer injection): option (c), the complete equivalent of
`compose.yaml`, and the only one where the live video works with the worker still
token-protected. If you choose ingress-nginx instead (`--set ingress.className=nginx`),
the dashboard is gated but the live video returns 401, and you need one of the other two
before real users see it: accept an edge-only API (option (a), with the tradeoff stated
above) or front `/api` with your own proxy (option (b)). The chart will not pick one for
you, and it will not put the token in an annotation.
