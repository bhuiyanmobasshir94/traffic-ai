# Deployment runbook (Docker Compose)

Deploys this stack to a single Linux server with Docker and Docker Compose
(v2 plugin). One domain, one certificate: Traefik terminates TLS, asks a human
to log in once with BasicAuth, and routes `/` to the Streamlit UI and `/api` to
the inference worker. Postgres keeps crossing history; Redis keeps the live
state. For Kubernetes, see `docs/KUBERNETES.md`. `compose.yaml` is the source of
truth for topology; `CLAUDE.md` says what is and is not real in this codebase.

```
              :80 / :443
                  |
             +----v----+   BasicAuth on BOTH routers (users file you create)
             | traefik |   /api: after login, Authorization is replaced with
             +----+----+         "Bearer <TRAFFIC_AI_API_TOKEN>"
        /api |         | /        (a rate limit runs BEFORE the login on both)
      +------v---+  +--v-----+
      |  worker  |<-|   ui   |  the UI calls the worker directly, with the token
      +--+----+--+  +--------+
         |    |
   +-----v-+ +v---------+      postgres <- migrate (one-shot, alembic upgrade head)
   | redis | | postgres |      starts first; the worker waits for it to finish
   +-------+ +----------+
```

Verified against this branch (`production-hardening`) on 2026-10-01. See "What
is and is not verified" at the end: no Docker daemon was available, so nothing
below was run against a live stack. "Known gaps" lists what this design does not
solve.

## Prerequisites

- A Linux server reachable on ports 80 and 443, with Docker Engine and the
  Docker Compose plugin (`docker compose version` shows v2).
- A domain name you control, and `htpasswd` (`apache2-utils` on Debian/Ubuntu,
  `httpd-tools` on RHEL) or Docker to run it from a container.
- `openssl`, to generate secrets.
- Roughly 4 CPU cores and 6GB RAM. CPU-only YOLO inference over two video
  streams is the dominant cost; the `deploy.resources` figures in `compose.yaml`
  are **estimates, not measurements** (worker capped at 2 CPUs / 3G, Postgres 1 CPU
  / 512M, Redis 0.5 CPU / 384M, UI 0.5 CPU / 512M, Traefik 0.5 CPU / 256M). They are
  caps, not typical use. Watch `docker stats` under real load and adjust. See
  "Resource expectations" before deploying to anything smaller.

## Required .env keys

> **For whoever maintains `.env.example`:** this section is the list to sync into
> it. `.env.example` was not edited by the change that wrote this runbook. Per
> `CLAUDE.md` it carries no values for `DOMAIN` or `ACME_EMAIL`, and it must carry
> no value for either secret below.

`docker compose` reads `.env` from the project directory for the `${...}`
substitutions in `compose.yaml`. `.env` is gitignored, and so is every
`.env.<name>` variant; only `.env.example` is tracked.

**Required: the stack will not come up without these.**

| Key | What it is | How to set it |
| --- | --- | --- |
| `DOMAIN` | The hostname Traefik serves and requests a certificate for | Your real domain, e.g. `traffic.example.org` |
| `ACME_EMAIL` | Let's Encrypt contact; expiry warnings go here | A mailbox you monitor. Reserved domains such as `example.com` are rejected (see Troubleshooting) |
| `TRAFFIC_AI_API_TOKEN` | Bearer token the worker requires, the UI presents, and Traefik injects on `/api` | `openssl rand -hex 32`. At least 32 characters, visible ASCII, no whitespace |
| `POSTGRES_PASSWORD` | Password of the Postgres role | `openssl rand -hex 24`. Hex avoids characters that need escaping in the database URL |

Two of these have a failure mode worth knowing in advance, because both fail
closed on purpose:

- Leave `TRAFFIC_AI_API_TOKEN` blank or unset and the **worker, the migrate step,
  and the UI all refuse to start**. Compose passes `${TRAFFIC_AI_API_TOKEN:-}`, an
  empty string, and `Settings` treats a blank token as no token; in production that
  is a startup error (`src/traffic_ai/config.py`, `_production_requires_hardening`).
  A token shorter than 32 characters is refused the same way.
- Leave `POSTGRES_PASSWORD` unset and it defaults to `traffic`, the
  development password. That default exists so the compose file parses without a
  `.env`; the worker and `migrate` then **refuse to start** because the database
  URL contains `:traffic@`. A forgotten password stops the deployment instead of
  shipping a guessable one.

**Optional: defaults in `compose.yaml`.**

| Key | Default | Notes |
| --- | --- | --- |
| `POSTGRES_USER` | `traffic` | Only read when Postgres first initialises its volume |
| `POSTGRES_DB` | `traffic_ai` | Same |
| `TRAFFIC_AI_PERSISTENCE_ENABLED` | `true` | `false` keeps the live dashboard and drops history. The UI always runs with it off, since it never opens the database |
| `TRAFFIC_AI_TARGET_FPS` | `12` | The first knob to lower on a slow server |
| `TRAFFIC_AI_DETECT_EVERY_N_FRAMES` | `2` | Raise to run detection less often |
| `TRAFFIC_AI_FRAME_WIDTH` | `960` | Lower for faster detection |
| `TRAFFIC_AI_WORKER_CPUS` | `2.0` | The worker container's CPU limit. Directly caps processed frames per second; set it to the cores the host can spare (must not exceed the host's core count) |
| `TRAFFIC_AI_DEVICE` | `cpu` | |
| `TRAFFIC_AI_MODEL_WEIGHTS` | `/app/weights/yolov8n.pt` | |
| `TRAFFIC_AI_LOG_LEVEL` | `INFO` | |
| `TRAFFIC_AI_LOG_FORMAT` | `json` | |
| `TRAFFIC_AI_RATE_LIMIT_ENABLED` | `true` | The worker's own per-client limiter (not the Traefik one; see "Rate limits and proxy hops") |
| `TRAFFIC_AI_RATE_LIMIT_REQUESTS` | `120` | Ordinary requests allowed per client address per 60-second window |
| `TRAFFIC_AI_RATE_LIMIT_STREAM_REQUESTS` | `10` | MJPEG stream requests per client address per window |
| `TRAFFIC_AI_TRUSTED_PROXY_HOPS` | `1` | Reverse proxies in front of the worker. `1` is Traefik alone; change it only if you add another proxy or a load balancer in front. See "Rate limits and proxy hops" |
| `TRAEFIK_LOG_LEVEL` | `INFO` | |

**Not in `.env`, but required files:**

- `config/traefik/users.htpasswd`: the BasicAuth users (step 4 below). Traefik
  will not start without it.
- `data/videos/*.mp4`: demo footage (`make videos`).

## First deploy

1. **DNS.** Create an A record for your domain pointing at the server's public
   IP. Let's Encrypt's HTTP-01 challenge (used here) requires this to resolve
   correctly *before* the first `make up`: it validates ownership by fetching a
   token back over port 80 on that hostname.

2. **Firewall.** Open inbound TCP 80 and 443. Port 80 stays open permanently: it
   is where Traefik redirects HTTP to HTTPS and where certificate renewal
   re-validates. Nothing else is published: Redis, Postgres, and the worker have
   no `ports:` entry, and only Traefik binds to the host.

3. **Clone and configure.**

   ```bash
   git clone <this-repo> traffic-ai && cd traffic-ai
   cp .env.example .env
   chmod 600 .env
   ```

   Edit `.env` and set the four required keys above. Generate the secrets on the
   server so they never travel:

   ```bash
   openssl rand -hex 32   # -> TRAFFIC_AI_API_TOKEN
   openssl rand -hex 24   # -> POSTGRES_PASSWORD
   ```

   Paste each value with no trailing space or newline: the UI client refuses a
   token containing whitespace or control characters. Never commit `.env`;
   nothing in this repository reads secrets from anywhere else.

4. **Create the BasicAuth users file.** This is a required step. The file is
   gitignored and mounted read-only into Traefik; `compose.yaml` mounts it so that
   a missing file stops `docker compose up` with an error rather than letting Docker
   create an empty directory in its place.

   ```bash
   htpasswd -nbB -C 12 admin '<password>' > config/traefik/users.htpasswd
   # more users: use >> so the first is kept
   htpasswd -nbB -C 12 second-user '<password>' >> config/traefik/users.htpasswd
   chmod 600 config/traefik/users.htpasswd
   ```

   `-B` is bcrypt and **`-C 12` is its cost**: `htpasswd` defaults to 5, which is
   far cheaper for an attacker to brute-force. Use a long random password per
   person (for example `openssl rand -base64 18`), never one reused from
   elsewhere: this login is the only thing between the internet and the dashboard
   and the live video, and Traefik's rate limit (see "Rate limits and proxy
   hops") slows guessing without preventing it.

   **The cost is paid in CPU, and the browser sends the credentials with every
   request.** Hashing one password at cost 12 took about 0.3 s of CPU on the
   developer laptop this was written on (cost 10: about 0.08 s; cost 5: about
   0.01 s; measured with `htpasswd`, not on a server). If Traefik re-checks the
   hash on each request, which was **not observed here** (no Docker daemon), a page
   that loads dozens of assets spends that much CPU on each one, against the 0.5
   CPU limit `compose.yaml` gives Traefik. After the first deploy, watch
   `docker stats traefik` while loading the dashboard. If it is pinned or the page
   loads slowly, regenerate the file with `-C 10` rather than going back to the
   default of 5.

   No `htpasswd`? `docker run --rm httpd:2 htpasswd -nbB -C 12 admin '<password>' >
   config/traefik/users.htpasswd`. A password typed on the command line is in your
   shell history; use `htpasswd -nB -C 12 admin` and type it at the prompt instead.
   `config/traefik/users.htpasswd.example` is a comment-only template. The file
   lives on the host, not in a Compose label, because a bcrypt hash is full of `$`
   characters that Compose would try to interpolate.

5. **Fetch demo footage.**

   ```bash
   make videos
   ```

   Downloads two `.mp4` files (~65MB total) into `data/videos/`, verifying each
   against a known MD5 before keeping it (see `scripts/fetch_demo_videos.py` and
   `data/videos/ATTRIBUTION.md`). Safe to re-run.

6. **Bring the stack up.**

   ```bash
   make up        # docker compose up -d --build
   ```

   Start-up order, enforced by `depends_on`: Postgres healthy, then `migrate` runs
   `alembic upgrade head` and exits 0, then the worker starts (it also waits for
   Redis healthy), then the UI (waits for the worker healthy). The first build
   pulls a CPU-only torch wheel into the worker image and can take several minutes
   on a slow link. The worker's first start downloads model weights, so it can take
   a few minutes to become healthy.

7. **Log in.** Open `https://<your-domain>/`. The browser asks for the BasicAuth
   user and password once; the dashboard and the live video then work. The browser
   never sees the API token.

## Migrations

The schema is created and updated by the one-shot `migrate` service, not by the
application: the worker does not run Alembic, so a database that was never
migrated has no tables to hold history. `migrate` uses the worker image (so schema and
code are built together), overrides its entrypoint to `alembic`, and runs
`upgrade head`. It sets the same production environment as the worker, so a bad
token or the development password fails **there**, before the worker starts.

```bash
docker compose ps -a migrate                  # Exited (0) is success
docker compose logs migrate                   # Alembic output, or the refusal reason
docker compose run --rm migrate upgrade head  # re-run by hand; safe, idempotent
docker compose run --rm migrate current       # which revision the database is at
docker compose run --rm migrate history       # known revisions
```

(`migrate` has `entrypoint: ["alembic"]`, so what follows the service name is the
Alembic subcommand.) If `migrate` exits non-zero, the worker never starts and
`docker compose up` reports the dependency failure; fix the cause shown in the
`migrate` log and re-run `make up`.

## Verifying the deployment

Everything is behind the edge login, **probe paths included**, so every request
below carries `-u admin` (curl prompts for the password).

```bash
export DOMAIN=traffic.example.org

# 1. The edge refuses anonymous requests. Both must be 401.
curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/
curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/api/cameras

# 2. Liveness and readiness (exempt from the API token, not from the edge login).
curl -fsS -u admin https://$DOMAIN/api/healthz
curl -sS  -u admin https://$DOMAIN/api/readyz
```

`/api/healthz` answers `{"status":"ok","version":...,"environment":"production"}`.
`/api/readyz` answers 200 only when Redis responds **and** at least one camera
pipeline is RUNNING, and 503 otherwise, with a `detail` of `redis unreachable` or
`no camera pipeline running`. 503 is expected for the first minutes while model
weights download. The body also carries two fields that are reported but never
gate readiness: with Postgres down the dashboard keeps serving live counts and
video and only history is lost.

- `database`: `true` when Postgres answered **and** has the history table,
  `false` when it is unreachable or reachable but not migrated (the usual cause is
  a skipped `migrate`), and `null` when persistence is off.
- `history_events_lost`: how many crossings were counted live but will never reach
  Postgres since the worker started (the in-memory buffer overflowed during a
  database outage, or the database refused a batch). It is `null`, **not** `0`,
  when there is no history writer to ask (persistence off, or its setup failed). A
  number above zero means the history totals are short by at least that much; the
  live dashboard is unaffected. The counter resets when the worker restarts.

```bash
# 3. Metrics. Through the edge, Traefik supplies the bearer for you.
curl -fsS -u admin https://$DOMAIN/api/metrics | head
```

The series are `http_requests_total`, `http_request_duration_seconds`,
`pipeline_frames_processed_total`, `pipeline_fps`, `pipeline_active_tracks`,
`pipeline_status`, `crossings_total`, and `history_events_lost_total`
(`traffic_ai/metrics.py`). `/api/metrics` is under the API token like every other
`/api` route except the two probes.

`history_events_lost_total{reason}` counts crossings that were counted live but
never persisted, by reason: `buffer_full` (evicted from the in-memory buffer during
a database outage) or `flush_failed` (a batch the database refused). History is
best-effort by design, so the loss itself is the thing to alert on. Both series
exist at 0 from the first scrape, so this works from the start:

```
increase(history_events_lost_total[15m]) > 0
```

(`/api/readyz` and the Analytics page show the same total; see the
`history_events_lost` field above.) The alert expression is standard PromQL and
was **not run** against a Prometheus.

To check the **worker's own** token enforcement, which the edge otherwise hides,
go around Traefik, from inside the container (the token is read from its
environment, so you never paste it):

```bash
docker compose exec worker python -c "
import os, urllib.request as u
req = u.Request('http://127.0.0.1:8000/api/metrics',
                headers={'Authorization': 'Bearer ' + os.environ['TRAFFIC_AI_API_TOKEN']})
print('with token:', u.urlopen(req, timeout=5).status)
try:
    u.urlopen('http://127.0.0.1:8000/api/metrics', timeout=5)
except Exception as exc:
    print('without token:', exc)
"
```

Expect `with token: 200` and `without token: HTTP Error 401: Unauthorized`.

```bash
# 4. Service state and certificate.
docker compose ps
docker compose logs traefik | grep -i acme
curl -vI https://$DOMAIN/ 2>&1 | grep -i 'issuer\|HTTP/'
```

`docker compose ps` should show `traefik`, `redis`, `postgres`, `worker`, `ui`
healthy and `migrate` exited with code 0. Finally log in through a browser, open
the dashboard, and confirm the video plays: that exercises the whole chain (edge
login, then the bearer injected on the browser's `<img>` request).

## Verifying certificate issuance

```bash
docker compose logs traefik | grep -i acme
```

A successful issuance logs something like `Certificates obtained for domains
[demo.example.com]`. Once issued, `curl -vI https://<your-domain>/` should show a
certificate chain ending in a Let's Encrypt intermediate (`R-something` under ISRG
Root X1) with a `not before` timestamp from just now. Certificates are stored in
the `letsencrypt` named volume (`/letsencrypt/acme.json` inside the traefik
container) and persist across restarts, `make down` and `make clean`; only
`make clean-volumes CONFIRM=yes` or deleting that volume by hand forces
re-issuance. If the certificate never appears, see Troubleshooting.

## Logs

```bash
make logs                      # all services, follow mode
docker compose logs -f ui      # one service
docker compose logs --tail 200 worker
docker compose logs migrate    # the last migration run
```

All services log to `json-file` with rotation (10MB x 3 files per container, see
`compose.yaml`'s `x-logging` anchor); `docker compose logs` reads through that
automatically.

## Backups and restore

Postgres is the only service whose data matters. Redis is a TTL'd cache with
persistence off and is rebuilt by the worker. Model weights re-download, and the
certificate re-issues (rate limits permitting; see Troubleshooting). Back up the
`.env` file and `config/traefik/users.htpasswd` separately and securely: they are
not in the repository and not in any volume.

**Back up** (a compressed custom-format dump, taken from inside the container so
no database port is ever opened):

```bash
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "traffic_ai-$(date +%F).dump"
```

`-T` stops Compose allocating a TTY, which would corrupt the binary output. The
variables are expanded by the container's shell, so the dump follows whatever
`POSTGRES_USER` and `POSTGRES_DB` the container was started with. Copy the file off
the server, and **test a restore**; an untested backup is a hope. A nightly cron entry
running the command above from the project directory is enough for a demo-scale
history table.

**Restore** into the running database (this replaces the objects in the dump):

```bash
docker compose stop worker ui                 # stop writers and readers first
docker compose exec -T postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' \
  < traffic_ai-2026-10-01.dump
docker compose run --rm migrate upgrade head  # bring the schema forward if the dump was older
docker compose up -d
```

To restore into a clean database instead, remove the volume first. That deletes
all history, so only do it deliberately: `docker compose down`, then
`docker volume rm <project>_postgres-data` (find the exact name with
`docker volume ls`), then `docker compose up -d` and run the `pg_restore` above
once Postgres is healthy.

Check it worked: `docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d
"$POSTGRES_DB" -c "SELECT count(*) FROM crossing_events"'`.

## Upgrades and rollback

**Upgrade.**

```bash
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "pre-upgrade-$(date +%F).dump"       # always, before changing anything
git pull
make up                                   # rebuilds changed images, re-runs migrate, then the worker
docker compose ps -a
```

`migrate` runs `alembic upgrade head` on the new code before the new worker starts,
so the worker never meets a schema older than it expects. If `docker compose up`
leaves an already-exited `migrate` alone, force it:
`docker compose run --rm migrate upgrade head`.

**Rollback.** Compose does not version releases; rolling back the code means going
back to a previous git ref and rebuilding.

```bash
git log --oneline -- docker/ compose.yaml src/ migrations/   # find the ref
git checkout <previous-ref>
make up
```

**Migrations are forward-only in practice.** Going back to older code does not
undo a migration, and `alembic downgrade` is not a safe rollback: the initial
revision's downgrade drops the history tables outright. Whether older code
tolerates a newer schema depends on the migration, which is usually fine for an
additive one (a new table or nullable column) and not for anything else. The safe
rollback for a bad migration is the pre-upgrade dump: restore it (above), then
check out the previous ref and `make up`. That is why the upgrade step takes the dump
first.

Named volumes (`letsencrypt`, `model-weights`, `postgres-data`) are untouched by
a code rollback. If a bad release corrupted state in Redis,
`docker compose restart redis` clears it immediately: Redis runs with no
persistence (`--save "" --appendonly no`), so a restart is a full, safe reset, and
the dashboard reports stale until the worker repopulates it. `make down` stops
everything without deleting volumes, and `make clean` is the same thing (it used to
delete them; it no longer does). **`make clean-volumes` removes them, Postgres
history and the issued certificate included.** It prints what it will destroy and
refuses to run unless you pass `CONFIRM=yes` (`make clean-volumes CONFIRM=yes`);
take a dump first (see "Backups and restore").

## Metrics: scraping with Prometheus

Prometheus is not part of this stack. Point yours at the worker one of two ways.

**Through the edge**, from a Prometheus anywhere. Traefik swaps the token in, so
Prometheus holds only a BasicAuth login (create a dedicated user for it in
`config/traefik/users.htpasswd`):

```yaml
scrape_configs:
  - job_name: traffic-ai
    scheme: https
    metrics_path: /api/metrics
    basic_auth:
      username: prometheus
      password_file: /etc/prometheus/traffic-ai-basic-auth-password
    static_configs:
      - targets: ["traffic.example.org"]
```

**On the compose network**, bypassing the edge, with the bearer token. The worker
is not published to the host, so this needs Prometheus attached to the project's
network (for example a second Compose file you own that joins it):

```yaml
scrape_configs:
  - job_name: traffic-ai
    metrics_path: /api/metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/traffic-ai-api-token
    static_configs:
      - targets: ["worker:8000"]
```

Put the token or password in a file readable only by Prometheus, not in the YAML.
Both snippets are standard Prometheus configuration and were **not run** against
this stack. The worker's in-process rate limiter does not count requests that
present the valid API token or that hit the two probe paths
(`src/traffic_ai/api/middleware.py`), so a scraper is not throttled by it on either
route; see "Rate limits and proxy hops".

## Rate limits and proxy hops

Two limiters, in two places, for two different jobs.

**At the edge, in Traefik (`compose.yaml` labels).** Each router (`worker`, `ui`)
has its own `ratelimit` middleware that runs **before** BasicAuth: 20 requests per
second sustained, with a burst allowance of 40, per source address. BasicAuth has
no lockout, so without this a login can be guessed at wire speed; with it, excess
requests are answered `429` and never reach the password check or the worker. The
figures are estimates, not measurements. A dashboard page load fetches a burst of
static assets, so if legitimate users see `429` (for example many people behind
one office address), raise `burst` in the labels of both services in
`compose.yaml` and run `docker compose up -d` (Compose recreates the services whose
labels changed). The two routers have separate buckets.

The bucket key is the **TCP peer address** (`ipstrategy.depth=0`, which ignores
`X-Forwarded-For`). That is correct while Traefik is the edge, as shipped. Put a
load balancer or another proxy in front of Traefik and every client would arrive
from the balancer's address and share one bucket, so set `depth` on both
`ratelimit` middlewares to the number of trusted proxies in front of Traefik (and
make Traefik trust that proxy's forwarded headers, which is Traefik configuration
this stack does not set). Not exercised here: see the unverified list.

**In the worker (`TRAFFIC_AI_RATE_LIMIT_*`).** The API's own per-client limiter:
`TRAFFIC_AI_RATE_LIMIT_REQUESTS` ordinary requests and
`TRAFFIC_AI_RATE_LIMIT_STREAM_REQUESTS` MJPEG stream requests per client address per
60-second window, switchable with `TRAFFIC_AI_RATE_LIMIT_ENABLED`. It does not count
the two probe paths, and it does not count ordinary requests that carry the valid
API token (the UI's server-to-server calls, and anything Traefik injected the bearer
into); a wrong or missing token is still counted. The stream budget applies even to
token holders. Read from `src/traffic_ai/api/middleware.py`.

**`TRAFFIC_AI_TRUSTED_PROXY_HOPS`** tells that limiter how many reverse proxies sit
between the client and the worker, so it can find the real client address in
`X-Forwarded-For` (each proxy appends the address it received the request from, so
the client is the entry that many places from the **right**; anything further left
was written by the caller and is not believed). The default `1` is this stack
(Traefik only). Set it to the true number if you add a proxy or load balancer in
front; **too low** and clients share the proxy's bucket, **too high** and the entry
it picks is not a proxy-supplied one. `0` ignores the header and uses the socket
peer, the right value if the worker were ever reachable without a proxy. The limiter
falls back to the socket peer, never to a header value, when the header is absent,
has fewer entries than the hop count, or the chosen entry is not an IP address. That
rule is from the code (`RateLimitMiddleware.client_key`); it was not exercised
against a live proxy chain.

## Graceful shutdown

`docker compose stop`, `down`, and a redeploy send `SIGTERM` and then `SIGKILL` after
the service's `stop_grace_period`. Docker's default is 10 seconds.

- **worker: `30s`.** Its shutdown is bounded by the code: uvicorn first waits up to 5
  seconds for open requests (`GRACEFUL_SHUTDOWN_SECONDS` in `api/__main__.py`; open
  MJPEG streams are told to end), then the lifespan waits up to 10 seconds for the
  camera pipelines to stop and up to 10 more for the history writer to flush its
  buffered crossings to Postgres (`_SHUTDOWN_TIMEOUT_SECONDS` in `api/app.py`): 25
  seconds worst case, so 30 leaves margin. With the Docker default the final flush
  could be cut off and those crossings lost. (`tests/test_deployment_config.py`
  checks the grace period against those constants.)
- **ui: `15s`.** Streamlit has nothing to flush; this only gives open sessions time
  to close.

If the worker is killed anyway (the host loses power, `docker kill`), the crossings
still in its buffer are lost with it. That is the same loss `history_events_lost`
reports for the failures it can observe; it cannot observe a hard kill.

## Resource expectations

CPU-only YOLO inference over two simultaneous streams is the dominant cost. On a
constrained server, tune these in `.env` (all in `traffic_ai.config`):

- **`TRAFFIC_AI_TARGET_FPS`**: the most direct lever. Lower first if the worker
  falls behind; a lower value produces smoother-looking output than running at the
  target rate and dropping frames.
- **`TRAFFIC_AI_DETECT_EVERY_N_FRAMES`**: detection is the expensive stage; raising
  this runs YOLO less often and lets the tracker interpolate more, trading a little
  accuracy for materially less CPU.
- **`TRAFFIC_AI_FRAME_WIDTH`**: detection runs on frames downscaled to this width.
  Smaller frames are proportionally faster.

- **`TRAFFIC_AI_WORKER_CPUS`**: the worker's container CPU limit. Measured on a
  4-core arm64 laptop (2026-10-01, default torchvision detector, both cameras): the
  worker saturates whatever it is given — 2 CPUs gave **~0.5 processed frames/s per
  camera**, 4 CPUs **~0.9**. The footage is 30 fps, so the annotated stream advances
  in slow motion and wall-clock throughput (`throughput_per_min`) reads far below the
  footage's real flow. Give the worker every core you can.

`pipeline_fps` (state, overlay, and the `pipeline_fps` metric) is averaged over the
last 20 ticks; it is the sustained processed rate, so read it after tuning.

Start by lowering `TRAFFIC_AI_TARGET_FPS`; it has the largest effect per unit of
quality lost. `docker stats` shows live CPU and memory per container while tuning.
If a container is being killed for memory (`docker inspect <container>` shows
`OOMKilled: true`) or throttled on CPU, raise its `deploy.resources.limits` in
`compose.yaml` rather than guessing; the shipped limits are estimates.

Model weights download once (to the `model-weights` named volume) and are reused
on every restart: expect a slower first start, and fast starts after that.

## Troubleshooting

**`docker compose up` fails before anything starts: the users file is missing.**
`compose.yaml` mounts `config/traefik/users.htpasswd` with
`create_host_path: false`, so a missing file is an error naming the path instead
of Docker silently creating a directory there (which would make Traefik fail to read
any users). Create the file (First deploy, step 4) and run `make up` again. If a
previous run *did* leave a directory at that path, remove it first
(`rmdir config/traefik/users.htpasswd`).

**A service refuses to start: "Refusing to start in production with an unsafe
configuration".** The safety check working. `docker compose logs worker`,
`migrate`, or `ui` names the problem: `TRAFFIC_AI_API_TOKEN is unset`, `is shorter
than 32 characters`, or `TRAFFIC_AI_DATABASE_URL still carries the development
password`. Set the missing key in `.env` and `make up`. The worker, `migrate`, and
the UI all share the token requirement; only the worker and `migrate` check the
database password.

**`migrate` exited non-zero and the worker is stuck waiting.** Read
`docker compose logs migrate`. The usual causes are the refusal above, or
Postgres rejecting the password (next item). `docker compose ps -a` shows the exit
code.

**I changed `POSTGRES_PASSWORD` and now the worker or `migrate` cannot log in.**
Postgres reads `POSTGRES_PASSWORD` only when it first initialises an empty data
volume; changing `.env` afterwards changes the *URL the worker uses* but not the
role's actual password. Set the role's password to match `.env`, without losing
data, from an interactive `psql` (the password is typed at a prompt and never
lands in your shell history):

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
# at the psql prompt:  \password        (enter the value now in POSTGRES_PASSWORD)
```

Local connections inside the container are typically trusted, so this works even
though the old password no longer matches. Then `make up`. If there is nothing
worth keeping, you can instead remove the volume and let it re-initialise
(`docker compose down`, `docker volume rm <project>_postgres-data`, `make up`).

**`429 Too Many Requests` from the edge.** The Traefik rate limit (20 requests/s,
burst 40, per source address; see "Rate limits and proxy hops"). Expected after a run
of wrong passwords or a flood. If legitimate users hit it, the usual cause is many of
them sharing one address (an office NAT, or a load balancer in front of Traefik that
makes every client look like one), not a limit that is too small.

**Login prompt loops, or `401` after entering the right password.** Check the
users file: one `user:$2y$...` line per user, no blank-looking trailing spaces, no
Windows line endings. `htpasswd -vb config/traefik/users.htpasswd admin '<password>'`
verifies a login offline. If an edit to the file is not picked up,
`docker compose restart traefik`.

**Logged in, but the video does not play and `/api/...` returns 401 JSON.** The
bearer Traefik injects does not match the worker's token. This happens when
`TRAFFIC_AI_API_TOKEN` in `.env` changed but the containers were not recreated:
the token reaches Traefik through the worker's *labels*, and the worker and UI
through their environment. `make up` recreates them when it detects the change;
`docker compose up -d --force-recreate worker ui` forces it. A token that was
pasted with a trailing newline or space is also refused by the UI client.

**The dashboard says history is unavailable.** Persistence is off, or the
database is down. `/api/readyz` shows `database: false`, and
`docker compose logs worker postgres` says why. Live counts and video are
unaffected by design.

**Let's Encrypt rate limits.** Let's Encrypt limits certificate issuance per exact
domain (currently 5 failures per hostname per hour, and a weekly limit on
duplicate certificates). If you are iterating on `.env` or DNS and hitting
failures, stop retrying blindly: check `docker compose logs traefik` for the actual
ACME error first. Consider Let's Encrypt's staging environment while debugging
(untrusted certs, much higher limits) rather than burning production attempts; that
requires a temporary Traefik command-line change
(`--certificatesresolvers.le.acme.caserver=...`) not currently wired to an env var
in `compose.yaml`.

**Certificate not issuing: `ACME_EMAIL` rejected.** Let's Encrypt validates the
contact address at account registration and refuses reserved example domains
outright. A placeholder like `you@example.com` fails the whole resolver before any
challenge is attempted:

```
Unable to obtain ACME certificate for domains ... 400 :: urn:ietf:params:acme:error:invalidContact
:: Error validating contact(s) :: contact email has forbidden domain "example.com"
```

This is not a DNS or firewall problem and the error names the real cause, so read
the traefik log rather than assuming port 80. Set `ACME_EMAIL` to a real mailbox you
control. (Observed during local verification of this stack.)

**Certificate not issuing: port 80 blocked.** The most common cause. HTTP-01
requires Let's Encrypt's servers to reach
`http://<your-domain>/.well-known/acme-challenge/...` on port 80, unredirected by
any upstream firewall, load balancer, or cloud security group. Confirm with
`curl -I http://<your-domain>/` from an external host (not the server itself: that
can succeed even when the outside world is blocked). Also confirm DNS actually
resolves to this server's IP (`dig +short <your-domain>`): a stale or missing A
record fails the same way as a blocked port, but with a different error in the
traefik logs. Traefik answers HTTP-01 challenges on an internal router, so the
BasicAuth middleware on the UI and worker routers should not interfere with
issuance; that was not observed here (see "What is and is not verified").

**Streamlit WebSocket failures behind the proxy.** Streamlit's UI depends on a
WebSocket connection to `/_stcore/stream` for live updates; if it can't upgrade,
the page loads but goes stale immediately or shows a disconnected/reconnecting
toast. Traefik's HTTP router already proxies the `Upgrade`/`Connection` headers
required for a WebSocket upgrade: this is default behavior in Traefik v3, not
something `compose.yaml` opts into, so if it breaks the first thing to check is
anything sitting *in front of* Traefik (a cloud load balancer, CDN, or corporate
proxy) that strips those headers or terminates idle connections early. Confirm from
the browser devtools Network tab: a healthy connection shows a `101 Switching
Protocols` response for `/_stcore/stream`; anything else confirms the upgrade is
being blocked upstream of this stack.

The upgrade was verified through this stack's own Traefik and does work:

```
$ curl -sk -i --http1.1 -H "Host: <domain>" -H "Connection: Upgrade" \
    -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" \
    -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
    https://<domain>/_stcore/stream
HTTP/1.1 101 Switching Protocols
```

Note the `--http1.1`. Without it curl negotiates HTTP/2 via ALPN and the upgrade
headers are silently ignored, returning `200` and the page HTML, which looks like
a failure but is an artifact of the test, not of the proxy. Browsers use HTTP/1.1
for WebSocket handshakes.

That output was recorded **before** edge authentication was added to the UI router.
With BasicAuth on, the same request needs credentials (add `-u admin`) or it is
answered `401`; the `401` here is the login challenge, not a proxy fault. The
authenticated form was not re-run.

## Security notes

- **Two layers, two audiences.** Humans log in at Traefik (BasicAuth) once. The
  worker separately requires `Authorization: Bearer <TRAFFIC_AI_API_TOKEN>` on every
  `/api` path except `/api/healthz` and `/api/readyz`; Traefik injects it after the
  login so a browser `<img>` (the MJPEG stream) works, and the UI sends it itself on
  its server-to-server calls. A request that reaches the worker without the edge
  (another container on the network) still has to present the token.
- **Why the order is BasicAuth, then token.** Traefik runs the router's
  middlewares left to right. BasicAuth runs first, validating the browser's
  `Authorization: Basic ...` header and (`removeheader`) not forwarding it; only a
  request that passed is given the service token. Reversed, the login check would
  run against a header already overwritten.
- **The token is in the worker container's labels** (that is how Traefik learns
  it), readable with `docker inspect` by anyone who can talk to the Docker daemon,
  who is root-equivalent on this host anyway. It is also in the environment of the
  worker and UI containers. It is not in any file in the repository.
- **The UI router has security headers** (HSTS for a year, `nosniff`,
  `no-referrer`, and `X-Frame-Options: SAMEORIGIN`) but **no Content-Security-Policy
  on purpose**: Streamlit ships inline scripts and styles, so a strict policy breaks
  the dashboard. SAMEORIGIN rather than DENY because Streamlit components, including
  the map, render in same-origin iframes. The worker sets its own, stricter headers
  on `/api` responses.
- **BasicAuth is rate-limited and has no lockout.** Traefik answers `429` to a source
  that exceeds 20 requests/s (burst 40) before BasicAuth checks anything, which makes
  online guessing slow, not impossible. Hash the passwords with bcrypt cost 12 and
  use long random ones (step 4 of "First deploy"). There is no per-user lockout and
  no alerting on failed logins.
- The worker's own rate limiter is per process and per client address; it slows a
  casual scraper and is not a hard quota. See "Rate limits and proxy hops".
- The Traefik container mounts `/var/run/docker.sock` read-only (`:ro` in
  `compose.yaml`), but that is a mitigation, not a boundary: a container with any
  access to the Docker socket can still ask the daemon to launch a new, unrestricted
  container, which is equivalent to root on the host. Treat the machine running this
  stack as being at the trust level of "anyone who can compromise the traefik
  container," and keep the Traefik image itself up to date. See "Known gaps" for the
  hardening that is not done.
- Redis, Postgres, and the worker are never published to the host: `compose.yaml`
  has no `ports:` entry for them, only Traefik binds 80/443. Do not add one without
  re-reading `CLAUDE.md`'s non-negotiables. Postgres in particular holds crossing
  history; reach it with `docker compose exec`, never a published port.
- No secret in this repository has a default value in `.env.example`; `.env`
  itself is gitignored, as is `config/traefik/users.htpasswd`. If you ever see a
  credential, RTSP URL, or API key proposed as a hardcoded default anywhere, that is
  a bug: stop and flag it. (`POSTGRES_PASSWORD` defaulting to `traffic` is the one
  deliberate exception, and it is a known-bad value the application refuses.)
- This stores crossing counts and timestamps. It stores no frames and no plates
  (`plate_text` is always `None`; there is no ANPR model). That changes the risk
  profile if it ever does; see `CLAUDE.md`.

## Known gaps

What this deployment does **not** solve. None of these is hidden by a default; each
is a decision someone should make deliberately.

- **The Docker socket on Traefik.** Traefik's Docker provider reads the socket, and
  `:ro` does not stop it asking the daemon for new containers (see Security notes), so
  a compromised Traefik is root on the host. The usual reduction is a socket proxy
  such as `docker-socket-proxy` (the `tecnativa/docker-socket-proxy` project): a small
  container that is the only thing holding the socket and exposes just the read-only
  endpoints Traefik needs, with Traefik pointed at it
  (`--providers.docker.endpoint=tcp://<proxy>:2375`) instead of the socket. **Not done
  here, and not tried**; the exact set of endpoints Traefik needs was not verified.
- **The API token is in the worker's container labels** (that is how Traefik learns
  what to inject), readable with `docker inspect`, and in the environment of the
  worker and UI containers. Anyone who can talk to the Docker daemon can read it;
  they are root-equivalent here anyway.
- **BasicAuth has no lockout and no alerting.** It is rate-limited (429) and hashed
  with bcrypt, not locked out per user. Whether Traefik re-hashes on every request,
  and so what bcrypt cost 12 does to its CPU, was not observed (step 4 of "First
  deploy").
- **Synchronous work on the worker's event loop.** Video decode and detection run on
  threads, but each frame's resize, tracker update, and annotation with JPEG encoding
  run directly on the event loop (`worker/pipeline.py`, `_process_frame`). With two
  cameras that is fine; as the camera count grows these add up on one loop and can
  delay every pipeline, `/api/healthz`, and the MJPEG streams together. Read from the
  code, **not profiled**. Lower `TRAFFIC_AI_FRAME_WIDTH` and `TRAFFIC_AI_TARGET_FPS`
  first; a structural fix means moving that work off the loop.
- **One worker, by design.** Every worker replica runs every camera and writes its own
  copy of each crossing, so the stack is not horizontally scalable and a second
  `worker` container would duplicate history. Capacity comes from CPU and the knobs
  under "Resource expectations".
- **A hard kill loses buffered history** (see "Graceful shutdown").

## What is and is not verified

Pinned to this branch (`production-hardening`), 2026-10-01.

**Checked:** `docker compose config -q` parses `compose.yaml` (with `DOMAIN` and
`ACME_EMAIL` set); `tests/test_deployment_config.py` asserts the structure
(only Traefik publishes ports, both routers carry BasicAuth with a rate limit in
front of it, the bearer is injected after it on the worker router, `migrate` gates
the worker, every service has resource limits, the worker's stop grace period covers
the shutdown budget read from the code) and feeds the resolved compose environment
into the real `Settings` production gate (an unset password and an unset token are
refused; the UI starts with only the token; the limiter and proxy-hop variables
resolve to `Settings`' own defaults and every `TRAFFIC_AI_*` variable names a real
setting). The Makefile's `clean` and `clean-volumes` targets were run with
`COMPOSE=echo`, so the commands they would issue were printed, not executed. The
workflows were parsed and checked with `actionlint`; the pinned action SHAs were
looked up through the GitHub API on 2026-10-01. The cited `config.py`, route, and
middleware behavior was read from the code.

**Verified by a local deployment (2026-10-01, Docker Desktop 20.10.23, 4-core arm64,
`DOMAIN=traffic.localhost`, Traefik's self-signed fallback certificate):** both images
build from `uv.lock` (worker 1.71 GB; UI 596 MB with no torch, cv2, ultralytics, or
SQLAlchemy); the worker image refuses `TRAFFIC_AI_ENVIRONMENT=production` with no token
and the dev DB password, naming both; `docker compose up -d` brings every service to
healthy, `migrate` applies revision `0001` and exits 0 before the worker starts, and only
Traefik publishes ports. At the edge: no credentials and a wrong password → 401, the
right login → 200, HTTP → 301 to HTTPS; with only the login, `/api/cameras` → 200, which
proves the BasicAuth→bearer swap; the MJPEG stream plays through the login (6 frames,
454 KB in 6 s, HSTS, nosniff, `X-Frame-Options`, `X-Request-ID` present) and is 401
without it. Inside the network the worker itself returns 401 for no or a wrong bearer and
200 for the right one, keeps `/api/healthz` open, and guards `/api/metrics`. A 40-way
parallel burst of 200 unauthenticated requests returned 128 × 429 and 72 × 401, so the
edge limiter runs before BasicAuth. `/api/readyz` reported `database: true`,
`history_events_lost: 0`; live crossings matched `/api/history/counts` and
`crossings_total`; history survived a worker restart while the live counters reset.

**Found by the local deployment, then contained rather than fixed (branch
`production-hardening`, 2026-10-01):** `toll-plaza-b` counted **0** crossings and
reported `standstill` on visibly moving traffic, and the dashboard presented both as
measurements. The tracker churns IDs on that footage (48–55 active tracks, IDs above 480
within ~150 frames). A follow-up measurement over 900 frames of `toll-plaza-b.mp4`, with
the real torchvision detector and ByteTrack, gave 3,932 track IDs for a scene of about 50
vehicles (median track life 3–13 frames). Scoring every horizontal counting line from
y=0.40 to y=0.95 with the real `LineCounter` gave 0 to 540 crossings for the same 30 s,
and gating on track age moved that to 6–530, against roughly 20–30 real incoming vehicles
in a slit-scan of the footage. No line placement is correct, so any count from this camera
would be invented. (These figures come from that investigation and were not re-run when the
switch below was added.)

`toll-plaza-b` is therefore configured `counting_enabled=False` in
`src/traffic_ai/cameras.py`. It still decodes, detects, tracks and streams annotated live
video, and still reports `active_tracks`. It does not count, derive throughput or
congestion, write to history, or increment `crossings_total`; the overlay reads "counting
not calibrated"; its map marker and corridor are gray; the dashboard shows the reason in
place of the metrics; and the Analytics page shows the reason instead of querying history.
The API answers **409**, with that reason as `detail`, on
`/api/cameras/toll-plaza-b/events` and on `/api/history/{events,counts,hourly}` when
`camera_id=toll-plaza-b`; the merged `/api/events` feed leaves it out. Its `/state`,
`frame.jpg` and `stream.mjpg` still answer 200. Order of answers: unknown camera 404, then
409, then a bad window or `limit` 422, then history unavailable 503. `toll-plaza-a` is
unchanged. This hides the problem and does not fix tracking on that
footage: turning counting back on needs footage or a tracker that holds identities, plus a
re-measurement, not a different line. The switch is covered by unit tests; it has not been
run against the deployed stack. Crossing rows for camera B written before the switch (the
local run above counted 0 for it) are not removed. The 409 is decided by the `camera_id` the
caller names, so `/api/history/*` without a `camera_id` (and the Analytics "All cameras"
view built on it) would still include such rows: the aggregates are SQL sums the API cannot
take one camera out of without a change to the repository.

**Not verified:** the effect of the stop grace periods on a real `docker compose stop`;
bcrypt cost 12's CPU effect on Traefik; the
missing-file error message from `create_host_path: false`; Let's Encrypt issuance, and
BasicAuth not interfering with it; the password-reset path (that `psql` over the
container's local socket is trusted); the backup, restore, and password-reset
commands; the Prometheus snippets and the `history_events_lost_total` alert; a real
`make clean-volumes CONFIRM=yes`. **Also not run:** the GitHub Actions workflows
themselves (a release run, the `workflow_call` hand-off from the release to CI, and
the image tags the metadata step produces for a final and a pre-release tag). The
`deploy.resources` numbers are estimates. Treat the first deploy as the verification
and read `docker compose ps -a` and the `migrate` and `traefik` logs.
