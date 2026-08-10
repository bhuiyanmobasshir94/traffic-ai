# Deployment runbook

Deploys this stack to a single Linux server with Docker and Docker Compose
(v2 plugin) installed. Traefik terminates TLS on one domain, routing `/` to
the Streamlit UI and `/api` to the inference worker. See `compose.yaml` for
the full service topology and `CLAUDE.md` for what is and is not real in
this codebase.

## Prerequisites

- A Linux server reachable on ports 80 and 443, with Docker Engine and the
  Docker Compose plugin (`docker compose version` — v2) installed.
- A domain name you control.
- Roughly 4GB RAM and 2 CPU cores free for CPU-only YOLO inference over two
  video streams, plus Traefik and Redis. See "Resource expectations" below
  before deploying to anything smaller.

## First deploy

1. **DNS.** Create an A record for your domain pointing at the server's
   public IP. Let's Encrypt's HTTP-01 challenge (used here) requires this to
   resolve correctly *before* the first `make up` — it validates ownership
   by fetching a token back over port 80 on that hostname.

2. **Firewall.** Open inbound TCP 80 and 443. Port 80 stays open permanently
   — it is not just for the initial challenge, it is also where Traefik
   redirects HTTP to HTTPS and where certificate renewal re-validates.

3. **Clone and configure.**

   ```bash
   git clone <this-repo> traffic-ai && cd traffic-ai
   cp .env.example .env
   ```

   Edit `.env`: set `DOMAIN` to your real domain and `ACME_EMAIL` to an
   address you actually monitor (Let's Encrypt sends expiry warnings there
   if renewal ever fails). Review the `TRAFFIC_AI_*` settings — the defaults
   are reasonable for a small server. `.env` is gitignored; it must never be
   committed, and nothing in this repository reads secrets from anywhere
   else.

4. **Fetch demo footage.**

   ```bash
   make videos
   ```

   Downloads two `.mp4` files (~65MB total) into `data/videos/`, verifying
   each against a known MD5 before keeping it (see
   `scripts/fetch_demo_videos.py` and `data/videos/ATTRIBUTION.md`). Safe to
   re-run — it skips files that are already present and valid, and cleans up
   any partial or corrupt download automatically.

5. **Bring the stack up.**

   ```bash
   make up
   ```

   This builds the `ui` and `worker` images and starts `traefik`, `redis`,
   `worker`, and `ui` in dependency order (`redis` healthy before `worker`
   starts, `worker` healthy before `ui` starts). The first build pulls a
   CPU-only torch wheel into the worker image and can take several minutes
   on a slow link.

## Verifying certificate issuance

```bash
make logs   # then look for the traefik service specifically:
docker compose logs traefik | grep -i acme
```

A successful issuance logs something like `Certificates obtained for domains
[demo.example.com]`. Once issued, `curl -vI https://<your-domain>/` should
show a certificate chain ending in a Let's Encrypt intermediate (`R-something`
under ISRG Root X1) with a `not before` timestamp from just now. Certificates
are stored in the `letsencrypt` named volume (`/letsencrypt/acme.json` inside
the traefik container) and persist across restarts and `make down`; only
`make clean` (which removes volumes) or deleting that volume forces
re-issuance.

If the certificate never appears, see Troubleshooting below.

## Logs

```bash
make logs                      # all services, follow mode
docker compose logs -f ui      # one service
docker compose logs --tail 200 worker
```

All services log to `json-file` with rotation (10MB × 3 files per
container, see `compose.yaml`'s `x-logging` anchor) — `docker compose logs`
reads through that automatically; there is nothing extra to configure.

## Rolling back

Compose does not version releases; rollback here means going back to a
previous git ref and rebuilding.

```bash
git log --oneline -- docker/ compose.yaml src/   # find the ref to return to
git checkout <previous-ref>
make up                                          # rebuilds and restarts changed services
```

Named volumes (`letsencrypt`, `model-weights`) are untouched by this — the
certificate and cached model weights survive a rollback. If a bad release
already corrupted state in Redis, `docker compose restart redis` clears it
immediately: Redis here is configured with no persistence
(`--save "" --appendonly no`), so a restart is a full, safe reset. `make
down` stops everything without deleting volumes; only `make clean` removes
them.

## Resource expectations

CPU-only YOLO inference over two simultaneous streams is the dominant cost.
On a constrained server, tune these in `.env` (all in `traffic_ai.config`):

- **`TRAFFIC_AI_TARGET_FPS`** — the most direct lever. Lower first if the
  worker falls behind; a lower value produces smoother-looking output than
  running at the target rate and dropping frames.
- **`TRAFFIC_AI_DETECT_EVERY_N_FRAMES`** — detection is the expensive stage;
  raising this runs YOLO less often and lets the tracker interpolate more,
  trading a little accuracy for materially less CPU.
- **`TRAFFIC_AI_FRAME_WIDTH`** — detection runs on frames downscaled to
  this width. Smaller frames are proportionally faster to run YOLO over.

Start by lowering `TRAFFIC_AI_TARGET_FPS`; it has the largest effect per
unit of quality lost. `docker stats` shows live CPU/memory per container
while tuning.

Model weights download once (to the `model-weights` named volume) and are
reused on every restart — expect a slower first start while YOLO fetches its
weights, and fast starts after that.

## Troubleshooting

**Let's Encrypt rate limits.** Let's Encrypt limits certificate issuance per
exact domain (currently 5 failures per hostname per hour, and a weekly limit
on duplicate certificates). If you are iterating on `.env` or DNS and hitting
failures, stop retrying blindly — check `docker compose logs traefik` for
the actual ACME error first. Consider Let's Encrypt's staging environment
while debugging (untrusted certs, much higher limits) rather than burning
production attempts; that requires a temporary Traefik command-line change
(`--certificatesresolvers.le.acme.caserver=...`) not currently wired to an
env var in `compose.yaml`.

**Certificate not issuing — `ACME_EMAIL` rejected.** Let's Encrypt validates the
contact address at account registration and refuses reserved example domains
outright. A placeholder like `you@example.com` fails the whole resolver before
any challenge is attempted:

```
Unable to obtain ACME certificate for domains ... 400 :: urn:ietf:params:acme:error:invalidContact
:: Error validating contact(s) :: contact email has forbidden domain "example.com"
```

This is not a DNS or firewall problem and the error names the real cause, so
read the traefik log rather than assuming port 80. Set `ACME_EMAIL` to a real
mailbox you control. (Observed during local verification of this stack.)

**Certificate not issuing — port 80 blocked.** The most common cause. HTTP-01
requires Let's Encrypt's servers to reach `http://<your-domain>/.well-known/acme-challenge/...`
on port 80, unredirected by any upstream firewall, load balancer, or cloud
security group. Confirm with `curl -I http://<your-domain>/` from an
external host (not the server itself — that can succeed even when the
outside world is blocked). Also confirm DNS actually resolves to this
server's IP (`dig +short <your-domain>`) — a stale or missing A record fails
the same way as a blocked port, but with a different error in the traefik
logs.

**Streamlit WebSocket failures behind the proxy.** Streamlit's UI depends on
a WebSocket connection to `/_stcore/stream` for live updates; if it can't
upgrade, the page loads but goes stale immediately or shows a
disconnected/reconnecting toast. Traefik's HTTP router already proxies the
`Upgrade`/`Connection` headers required for a WebSocket upgrade — this is
default behavior in Traefik v3, not something `compose.yaml` opts into, so
if it breaks the first thing to check is anything sitting *in front of*
Traefik (a cloud load balancer, CDN, or corporate proxy) that strips those
headers or terminates idle connections early. Confirm from the browser
devtools Network tab: a healthy connection shows a `101 Switching Protocols`
response for `/_stcore/stream`; anything else confirms the upgrade is being
blocked upstream of this stack.

The upgrade was verified through this stack's own Traefik and does work:

```
$ curl -sk -i --http1.1 -H "Host: <domain>" -H "Connection: Upgrade" \
    -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" \
    -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
    https://<domain>/_stcore/stream
HTTP/1.1 101 Switching Protocols
```

Note the `--http1.1`. Without it curl negotiates HTTP/2 via ALPN and the
upgrade headers are silently ignored, returning `200` and the page HTML —
which looks like a failure but is an artifact of the test, not of the proxy.
Browsers use HTTP/1.1 for WebSocket handshakes.

## Security notes

- The Traefik container mounts `/var/run/docker.sock` read-only
  (`:ro` in `compose.yaml`), but that is a mitigation, not a boundary — a
  container with any access to the Docker socket can still ask the daemon to
  launch a new, unrestricted container, which is equivalent to root on the
  host. Treat the machine running this stack as being at the trust level of
  "anyone who can compromise the traefik container," and keep the Traefik
  image itself up to date.
- Redis and the worker are never published to the host — `compose.yaml` has
  no `ports:` entry for either, only Traefik binds 80/443. Do not add one
  without re-reading `CLAUDE.md`'s non-negotiables.
- No secret in this repository has a default value in `.env.example`; `.env`
  itself is gitignored. If you ever see a credential, RTSP URL, or API key
  proposed as a hardcoded default anywhere, that is a bug — stop and flag it.
