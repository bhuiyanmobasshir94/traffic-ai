"""Structural checks on the Helm chart in deploy/helm/traffic-ai.

These shell out to `helm template` / `helm lint` — no cluster is contacted, and
nothing is installed — then parse the rendered manifests and assert the
invariants docs/KUBERNETES.md and CLAUDE.md's non-negotiables depend on: both
Deployments exist on the right ports, every container is non-root with a
read-only root filesystem and has requests AND limits, probes hit the paths the
worker and UI actually serve, `/api` is declared before `/` on the ingress, edge
authentication is wired per controller (ingress-nginx annotations, or an ordered
Traefik middleware chain with the rate limit first) and refuses to render
half-configured, and no secret value is ever rendered when an existing Secret is
used. The rendered ConfigMaps are also fed into the real `Settings` production gate.

The migration hook Job is held to the same standard as the workloads (hardened,
read-only root, requests and limits) plus the constraints of a pre-install hook:
it may depend on nothing the chart creates after it. The worker is pinned to one
replica that rolls out with `Recreate` and is never autoscaled, because every
replica would write its own copy of each crossing.

Skipped cleanly where `helm` is not installed, so the suite still passes on a
laptop or CI runner without it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from traffic_ai.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART = REPO_ROOT / "deploy" / "helm" / "traffic-ai"
PRODUCTION_VALUES = CHART / "values-production.yaml"
DEFAULT_VALUES = CHART / "values.yaml"

HELM = shutil.which("helm")

pytestmark = pytest.mark.skipif(HELM is None, reason="helm is not installed")

RELEASE = "traffic-ai"
WORKER = "traffic-ai-worker"
UI = "traffic-ai-ui"
MIGRATE = "traffic-ai-migrate"

# The scratch path inside the container (an emptyDir mount) — not a host temp file.
TMP_MOUNT = "/tmp"  # noqa: S108
WEIGHTS_MOUNT = "/app/weights"

RECOMMENDED_LABELS = frozenset(
    {
        "app.kubernetes.io/name",
        "app.kubernetes.io/instance",
        "app.kubernetes.io/version",
        "app.kubernetes.io/managed-by",
        "helm.sh/chart",
    }
)

# Distinctive enough that finding it in rendered output can only mean a leak.
TOKEN_SENTINEL = "sentinel-api-token-DO-NOT-RENDER"  # noqa: S105 - a test canary, not a credential
DB_URL_SENTINEL = "postgresql+asyncpg://u:sentinel-db-password-DO-NOT-RENDER@db/traffic_ai"

# Names a Middleware CR the operator creates; the chart only references them.
TRAEFIK_MIDDLEWARES = [
    "traffic-ai-basicauth@kubernetescrd",
    "traffic-ai-api-bearer@kubernetescrd",
]
# What values-production.yaml ships: rate limit, then login, then bearer injection.
PRODUCTION_MIDDLEWARES = [
    "traffic-ai-ratelimit@kubernetescrd",
    "traffic-ai-basicauth@kubernetescrd",
    "traffic-ai-api-bearer@kubernetescrd",
]
_PRODUCTION = ["-f", str(PRODUCTION_VALUES), "--set", "ingress.host=traffic.example.org"]
_NGINX = ["--set", "ingress.className=nginx"]
_TRAEFIK = [
    "--set",
    "ingress.className=traefik",
    *(
        arg
        for i, name in enumerate(TRAEFIK_MIDDLEWARES)
        for arg in ("--set", f"ingress.traefik.middlewares[{i}]={name}")
    ),
]

# The shapes the chart is documented to be deployed in.
SCENARIOS: dict[str, list[str]] = {
    "default": [],
    # values-production.yaml defaults to Traefik: the only controller where the
    # whole edge login (including the video) works.
    "production": _PRODUCTION,
    # Production values with ingress-nginx, the documented alternative (BasicAuth
    # annotations; the video limitation applies). Every structural test below runs
    # against it too.
    "nginx": [*_PRODUCTION, *_NGINX],
}

NGINX_AUTH_PREFIX = "nginx.ingress.kubernetes.io/auth-"
NGINX_LIMIT_RPS = "nginx.ingress.kubernetes.io/limit-rps"
TRAEFIK_MIDDLEWARES_ANNOTATION = "traefik.ingress.kubernetes.io/router.middlewares"
# Enough to satisfy MIN_API_TOKEN_LENGTH; the value only has to be accepted.
GATE_TOKEN = "t" * 64
GATE_DB_URL = "postgresql+asyncpg://u:a-real-database-password@db/traffic_ai"


def _helm(*args: str) -> subprocess.CompletedProcess[str]:
    # `HELM` is the absolute path from shutil.which and every argument is a
    # literal or a repo path built in this file; there is no shell and no
    # external input, so S603 (untrusted subprocess input) does not apply.
    assert HELM is not None
    return subprocess.run(  # noqa: S603
        [HELM, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _template_raw(*extra: str) -> str:
    result = _helm("template", RELEASE, str(CHART), *extra)
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    return result.stdout


def _template(*extra: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(_template_raw(*extra)) if d]


def _of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind]


def _named(docs: list[dict], kind: str, name: str) -> dict:
    matches = [d for d in _of_kind(docs, kind) if d["metadata"]["name"] == name]
    assert len(matches) == 1, f"expected exactly one {kind} named {name!r}, got {len(matches)}"
    return matches[0]


def _containers(deployment: dict) -> list[dict]:
    return deployment["spec"]["template"]["spec"]["containers"]


def _env(container: dict) -> dict[str, dict]:
    return {e["name"]: e for e in container.get("env", [])}


def _ingress(*extra: str) -> dict:
    (ingress,) = _of_kind(_template(*extra), "Ingress")
    return ingress


def _migrate_job(docs: list[dict]) -> dict:
    return _named(docs, "Job", MIGRATE)


def _hook(doc: dict) -> dict[str, str] | None:
    """The helm.sh/hook annotations of an object, or None for an ordinary one."""
    annotations = doc["metadata"].get("annotations", {})
    if "helm.sh/hook" not in annotations:
        return None
    return {k: v for k, v in annotations.items() if k.startswith("helm.sh/hook")}


def _hook_events(doc: dict) -> set[str]:
    hook = _hook(doc)
    assert hook is not None, f"{doc['kind']}/{doc['metadata']['name']} is not a hook"
    return set(hook["helm.sh/hook"].split(","))


def _install_notes(*extra: str) -> str:
    """NOTES.txt as `helm install` would print it, without contacting a cluster."""
    result = _helm("install", RELEASE, str(CHART), "--dry-run=client", "-n", "ns", *extra)
    if result.returncode != 0 and "dry-run" in result.stderr:
        pytest.skip("this helm does not support --dry-run=client")
    assert result.returncode == 0, f"helm install --dry-run failed:\n{result.stderr}"
    return result.stdout.split("NOTES:", 1)[1]


def _annotations(ingress: dict) -> dict[str, str]:
    return ingress["metadata"].get("annotations", {})


def _gate_settings(
    monkeypatch: pytest.MonkeyPatch, configmap: dict, extra: dict[str, str]
) -> Settings:
    """Build `Settings` the way the pod would: ConfigMap plus the Secret-backed env."""
    # Start from a clean TRAFFIC_AI_* slate so ambient variables cannot change the result.
    for key in [k for k in os.environ if k.startswith("TRAFFIC_AI_")]:
        monkeypatch.delenv(key)
    for key, value in {**configmap["data"], **extra}.items():
        if key.startswith("TRAFFIC_AI_"):
            monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


@pytest.fixture(scope="module", params=list(SCENARIOS), ids=list(SCENARIOS))
def docs(request: pytest.FixtureRequest) -> list[dict]:
    return _template(*SCENARIOS[request.param])


@pytest.fixture(scope="module")
def worker(docs: list[dict]) -> dict:
    return _named(docs, "Deployment", WORKER)


@pytest.fixture(scope="module")
def ui(docs: list[dict]) -> dict:
    return _named(docs, "Deployment", UI)


@pytest.fixture(scope="module")
def all_containers(worker: dict, ui: dict) -> list[dict]:
    return _containers(worker) + _containers(ui)


class TestRender:
    @pytest.mark.parametrize("scenario", list(SCENARIOS))
    def test_helm_lint_passes(self, scenario: str) -> None:
        result = _helm("lint", str(CHART), *SCENARIOS[scenario])
        assert result.returncode == 0, f"helm lint failed:\n{result.stdout}\n{result.stderr}"

    @pytest.mark.parametrize("scenario", list(SCENARIOS))
    def test_template_renders_valid_yaml(self, scenario: str) -> None:
        rendered = _template(*SCENARIOS[scenario])
        assert rendered
        for doc in rendered:
            assert {"apiVersion", "kind", "metadata"} <= doc.keys()

    def test_production_values_without_host_refuse_to_render(self) -> None:
        # A forgotten --set ingress.host must fail loudly, not deploy a placeholder.
        result = _helm("template", RELEASE, str(CHART), "-f", str(PRODUCTION_VALUES))
        assert result.returncode != 0
        assert "ingress.host is required" in result.stderr


class TestDeployments:
    def test_worker_and_ui_exist_on_composes_ports(self, worker: dict, ui: dict) -> None:
        (worker_container,) = _containers(worker)
        (ui_container,) = _containers(ui)
        assert [p["containerPort"] for p in worker_container["ports"]] == [8000]
        assert [p["containerPort"] for p in ui_container["ports"]] == [8501]

    def test_every_container_has_requests_and_limits(self, all_containers: list[dict]) -> None:
        assert all_containers
        for container in all_containers:
            resources = container["resources"]
            for section in ("requests", "limits"):
                for resource in ("cpu", "memory"):
                    assert resources[section][resource], (
                        f"{container['name']} is missing {section}.{resource}"
                    )

    def test_worker_requests_reflect_cpu_inference(self, worker: dict) -> None:
        # Honest sizing: CPU inference needs a full core and ~2Gi of headroom.
        (container,) = _containers(worker)
        assert container["resources"]["requests"]["cpu"] == "1"
        assert container["resources"]["limits"]["memory"] == "2Gi"

    def test_every_container_is_locked_down(self, all_containers: list[dict]) -> None:
        for container in all_containers:
            ctx = container["securityContext"]
            name = container["name"]
            assert ctx["runAsNonRoot"] is True, name
            assert ctx["runAsUser"] == 1000, name
            assert ctx["runAsGroup"] == 1000, name
            assert ctx["allowPrivilegeEscalation"] is False, name
            assert ctx["capabilities"]["drop"] == ["ALL"], name
            assert ctx["readOnlyRootFilesystem"] is True, name
            assert ctx["seccompProfile"] == {"type": "RuntimeDefault"}, name

    def test_every_pod_is_non_root_with_default_seccomp(self, worker: dict, ui: dict) -> None:
        for deployment in (worker, ui):
            ctx = deployment["spec"]["template"]["spec"]["securityContext"]
            assert ctx["runAsNonRoot"] is True
            assert ctx["runAsUser"] == 1000
            assert ctx["runAsGroup"] == 1000
            assert ctx["seccompProfile"] == {"type": "RuntimeDefault"}

    def test_no_pod_mounts_a_service_account_token(self, worker: dict, ui: dict) -> None:
        for deployment in (worker, ui):
            assert deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is False

    def test_writable_paths_are_volumes_not_the_root_filesystem(
        self, worker: dict, ui: dict
    ) -> None:
        # readOnlyRootFilesystem only works if the paths that need writes are
        # mounted volumes. /app/weights holds model weights (and is where
        # YOLO_CONFIG_DIR / TORCH_HOME point); /tmp is scratch for both.
        for deployment, expected in ((worker, {TMP_MOUNT, WEIGHTS_MOUNT}), (ui, {TMP_MOUNT})):
            pod = deployment["spec"]["template"]["spec"]
            volume_names = {v["name"] for v in pod["volumes"]}
            (container,) = pod["containers"]
            mounts = {m["mountPath"]: m["name"] for m in container["volumeMounts"]}
            assert expected <= mounts.keys()
            assert set(mounts.values()) <= volume_names, "a mount references an undefined volume"

    def test_ui_does_not_mount_over_baked_in_streamlit_config(self, ui: dict) -> None:
        (container,) = _containers(ui)
        assert all(not m["mountPath"].startswith("/app") for m in container["volumeMounts"])

    def test_selectors_are_subsets_of_pod_labels(self, worker: dict, ui: dict) -> None:
        for deployment in (worker, ui):
            selector = deployment["spec"]["selector"]["matchLabels"]
            labels = deployment["spec"]["template"]["metadata"]["labels"]
            assert selector.items() <= labels.items()

    def test_images_are_pinned_not_latest(self, all_containers: list[dict]) -> None:
        for container in all_containers:
            image = container["image"]
            assert ":" in image
            assert not image.endswith(":latest")


class TestWorkerIsASingleton:
    """Each worker replica runs every pipeline and writes its own copy of each crossing."""

    def test_worker_is_one_replica_in_every_shape_the_chart_ships(self, worker: dict) -> None:
        assert worker["spec"]["replicas"] == 1

    def test_worker_rolls_out_with_recreate_never_two_at_once(self, worker: dict) -> None:
        # RollingUpdate would run the old and the new worker together for the whole
        # rollout, and both would write every crossing.
        assert worker["spec"]["strategy"] == {"type": "Recreate"}

    def test_ui_keeps_the_default_rolling_update(self, ui: dict) -> None:
        # The UI is stateless; only the worker needs the stricter strategy.
        assert "strategy" not in ui["spec"]

    def test_worker_replicas_always_render_even_with_autoscaling_on(self) -> None:
        docs = _template("--set", "autoscaling.enabled=true")
        assert _named(docs, "Deployment", WORKER)["spec"]["replicas"] == 1
        # ...while the UI's count is handed to its HPA.
        assert "replicas" not in _named(docs, "Deployment", UI)["spec"]

    def test_worker_autoscaling_is_refused_with_the_reason(self) -> None:
        result = _helm("template", RELEASE, str(CHART), "--set", "autoscaling.worker.enabled=true")
        assert result.returncode != 0
        assert "autoscaling.worker.enabled is not supported" in result.stderr
        assert "duplicate history" in result.stderr
        assert "worker.replicaCount=1" in result.stderr

    def test_worker_autoscaling_is_refused_in_production_too(self) -> None:
        result = _helm(
            "template",
            RELEASE,
            str(CHART),
            *SCENARIOS["production"],
            "--set",
            "autoscaling.enabled=true",
            "--set",
            "autoscaling.worker.enabled=true",
        )
        assert result.returncode != 0
        assert "duplicate history" in result.stderr

    def test_worker_hpa_is_never_rendered(self) -> None:
        docs = _template("--set", "autoscaling.enabled=true")
        targets = {
            h["spec"]["scaleTargetRef"]["name"] for h in _of_kind(docs, "HorizontalPodAutoscaler")
        }
        assert WORKER not in targets

    def test_a_second_replica_is_called_out_in_the_install_notes(self) -> None:
        notes = _install_notes("--set", "worker.replicaCount=2")
        assert "WARNING: worker.replicaCount is 2" in notes
        assert "WARNING: worker.replicaCount" not in _install_notes()


class TestTerminationGracePeriods:
    """Kubernetes' 30s default is only a default; the worker's shutdown budget is real."""

    def test_worker_grace_covers_the_applications_shutdown_budget(self, worker: dict) -> None:
        # Request drain (GRACEFUL_SHUTDOWN_SECONDS=5) + pipelines (10s) + history
        # writer drain (10s): src/traffic_ai/api/__main__.py and app.py.
        grace = worker["spec"]["template"]["spec"]["terminationGracePeriodSeconds"]
        assert grace == 30
        assert grace >= 5 + 10 + 10

    def test_ui_grace_period_matches_compose(self, ui: dict) -> None:
        assert ui["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 15

    def test_grace_periods_are_configurable(self) -> None:
        docs = _template(
            "--set",
            "worker.terminationGracePeriodSeconds=45",
            "--set",
            "ui.terminationGracePeriodSeconds=20",
        )
        worker = _named(docs, "Deployment", WORKER)
        ui = _named(docs, "Deployment", UI)
        assert worker["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 45
        assert ui["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 20


class TestProbes:
    def test_worker_probes_match_the_api(self, worker: dict) -> None:
        (container,) = _containers(worker)
        assert container["livenessProbe"]["httpGet"]["path"] == "/api/healthz"
        assert container["readinessProbe"]["httpGet"]["path"] == "/api/readyz"

    def test_worker_has_a_startup_probe_for_the_cold_weights_download(self, worker: dict) -> None:
        (container,) = _containers(worker)
        probe = container["startupProbe"]
        assert probe["httpGet"]["path"] == "/api/healthz"
        # periodSeconds * failureThreshold is the cold-start budget: at least a few minutes.
        assert probe["periodSeconds"] * probe["failureThreshold"] >= 180

    def test_worker_readiness_tolerates_503_during_startup(self, worker: dict) -> None:
        # /api/readyz returns 503 until a pipeline is RUNNING; the probe must not
        # give up (and the kubelet must not restart anything) for being not-ready.
        (container,) = _containers(worker)
        assert container["readinessProbe"]["failureThreshold"] >= 10
        assert "restart" not in str(container["readinessProbe"]).lower()

    def test_ui_probes_use_streamlit_health(self, ui: dict) -> None:
        (container,) = _containers(ui)
        assert container["livenessProbe"]["httpGet"]["path"] == "/_stcore/health"
        assert container["readinessProbe"]["httpGet"]["path"] == "/_stcore/health"

    def test_probes_target_the_declared_named_port(self, all_containers: list[dict]) -> None:
        for container in all_containers:
            port_names = {p["name"] for p in container["ports"]}
            for key in ("livenessProbe", "readinessProbe", "startupProbe"):
                if key in container:
                    assert container[key]["httpGet"]["port"] in port_names


class TestEnvironmentContract:
    """Mirrors compose.yaml and src/traffic_ai/config.py."""

    def test_worker_env_matches_compose(self, docs: list[dict]) -> None:
        data = _named(docs, "ConfigMap", WORKER)["data"]
        assert data["TRAFFIC_AI_ENVIRONMENT"] == "production"
        assert data["TRAFFIC_AI_DEVICE"] == "cpu"
        assert data["TRAFFIC_AI_TARGET_FPS"] == "12"
        assert data["TRAFFIC_AI_DETECT_EVERY_N_FRAMES"] == "2"
        assert data["TRAFFIC_AI_FRAME_WIDTH"] == "960"
        assert data["TRAFFIC_AI_VIDEO_DIR"] == "/data/videos"
        assert data["TRAFFIC_AI_MODEL_WEIGHTS"] == "/app/weights/yolov8n.pt"
        assert data["TRAFFIC_AI_ALLOW_UNAUTHENTICATED"] == "false"
        assert data["TRAFFIC_AI_REDIS_URL"].startswith("redis://")

    def test_writable_cache_dirs_point_at_the_weights_volume(self, docs: list[dict]) -> None:
        data = _named(docs, "ConfigMap", WORKER)["data"]
        assert data["YOLO_CONFIG_DIR"] == "/app/weights"
        assert data["TORCH_HOME"] == "/app/weights"

    def test_ui_env_matches_compose(self, docs: list[dict]) -> None:
        data = _named(docs, "ConfigMap", UI)["data"]
        assert data["TRAFFIC_AI_ENVIRONMENT"] == "production"
        assert data["TRAFFIC_AI_API_INTERNAL_URL"] == f"http://{WORKER}:8000"
        assert data["TRAFFIC_AI_API_PUBLIC_URL"] == "/api"
        # No database URL reaches the UI, so persistence must be off for it or the
        # production gate would reject the development-password default.
        assert data["TRAFFIC_AI_PERSISTENCE_ENABLED"] == "false"
        assert data["TRAFFIC_AI_ALLOW_UNAUTHENTICATED"] == "false"

    def test_workloads_load_their_own_configmap(self, worker: dict, ui: dict) -> None:
        for deployment, name in ((worker, WORKER), (ui, UI)):
            (container,) = _containers(deployment)
            assert container["envFrom"] == [{"configMapRef": {"name": name}}]

    def test_worker_token_and_database_url_come_from_a_secret(self, worker: dict) -> None:
        # Production Settings refuses to build without both, so the worker must
        # always be wired to receive them.
        (container,) = _containers(worker)
        env = _env(container)
        for name in ("TRAFFIC_AI_API_TOKEN", "TRAFFIC_AI_DATABASE_URL"):
            assert "secretKeyRef" in env[name]["valueFrom"], name
            assert "value" not in env[name], f"{name} must not be a literal"

    def test_ui_references_only_the_api_token_secret_key(self, ui: dict) -> None:
        # compose.yaml gives the UI the token (its server-side calls to the
        # worker present it) and no database URL: it is a thin viewer over HTTP.
        # So the ONLY Secret reference the UI pod may carry is the token key, and
        # it is always a secretKeyRef, never a literal.
        (container,) = _containers(ui)
        env = _env(container)
        token = env["TRAFFIC_AI_API_TOKEN"]
        assert "value" not in token, "the token must not be a literal"
        assert token["valueFrom"]["secretKeyRef"]["key"] == "api-token"

        secret_refs = {
            e["name"]: e["valueFrom"]["secretKeyRef"]["key"]
            for e in container.get("env", [])
            if "secretKeyRef" in e.get("valueFrom", {})
        }
        assert secret_refs == {"TRAFFIC_AI_API_TOKEN": "api-token"}

        # No database URL by any route: not as an env var, not via a Secret key,
        # not through envFrom (which here is the ConfigMap only).
        assert "TRAFFIC_AI_DATABASE_URL" not in env
        assert "database-url" not in str(ui["spec"]["template"]["spec"])
        assert container["envFrom"] == [{"configMapRef": {"name": UI}}]

    def test_ui_and_worker_read_the_token_from_the_same_secret_key(
        self, worker: dict, ui: dict
    ) -> None:
        (worker_container,) = _containers(worker)
        (ui_container,) = _containers(ui)
        worker_ref = _env(worker_container)["TRAFFIC_AI_API_TOKEN"]["valueFrom"]["secretKeyRef"]
        ui_ref = _env(ui_container)["TRAFFIC_AI_API_TOKEN"]["valueFrom"]["secretKeyRef"]
        assert ui_ref == worker_ref

    def test_ui_token_reference_is_required_with_an_existing_secret(self) -> None:
        docs = _template(*SCENARIOS["production"])
        (container,) = _containers(_named(docs, "Deployment", UI))
        ref = _env(container)["TRAFFIC_AI_API_TOKEN"]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "traffic-ai-secrets"
        assert ref["optional"] is False, "a wrong secret name must stop the pod loudly"

    def test_rendered_ui_environment_passes_the_production_gate(
        self, docs: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ConfigMap plus the one Secret-backed variable is everything the UI
        # pod gets. Hold it to the real Settings validation, not to its own strings.
        configmap = _named(docs, "ConfigMap", UI)
        settings = _gate_settings(monkeypatch, configmap, {"TRAFFIC_AI_API_TOKEN": GATE_TOKEN})
        assert settings.is_production
        assert settings.auth_enabled
        assert not settings.persistence_enabled

    def test_rendered_ui_environment_without_the_token_is_refused(
        self, docs: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Proves the token wiring above is load-bearing, not decoration.
        configmap = _named(docs, "ConfigMap", UI)
        with pytest.raises(ValueError, match="TRAFFIC_AI_API_TOKEN is unset"):
            _gate_settings(monkeypatch, configmap, {})

    def test_rendered_worker_environment_passes_the_production_gate(
        self, docs: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configmap = _named(docs, "ConfigMap", WORKER)
        extra = {"TRAFFIC_AI_API_TOKEN": GATE_TOKEN, "TRAFFIC_AI_DATABASE_URL": GATE_DB_URL}
        settings = _gate_settings(monkeypatch, configmap, extra)
        assert settings.is_production
        assert settings.persistence_enabled

    def test_production_values_reference_an_existing_secret(self) -> None:
        docs = _template(*SCENARIOS["production"])
        (container,) = _containers(_named(docs, "Deployment", WORKER))
        env = _env(container)
        for name in ("TRAFFIC_AI_API_TOKEN", "TRAFFIC_AI_DATABASE_URL"):
            ref = env[name]["valueFrom"]["secretKeyRef"]
            assert ref["name"] == "traffic-ai-secrets"
            assert ref["optional"] is False, "a wrong secret name must stop the pod loudly"

    def test_worker_configmap_carries_the_limiter_and_proxy_settings(
        self, docs: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configmap = _named(docs, "ConfigMap", WORKER)
        assert configmap["data"]["TRAFFIC_AI_RATE_LIMIT_ENABLED"] == "true"
        assert configmap["data"]["TRAFFIC_AI_RATE_LIMIT_REQUESTS"] == "120"
        assert configmap["data"]["TRAFFIC_AI_RATE_LIMIT_STREAM_REQUESTS"] == "10"
        assert configmap["data"]["TRAFFIC_AI_TRUSTED_PROXY_HOPS"] == "1"
        # ...and the real Settings accepts them, with the code's own defaults.
        extra = {"TRAFFIC_AI_API_TOKEN": GATE_TOKEN, "TRAFFIC_AI_DATABASE_URL": GATE_DB_URL}
        settings = _gate_settings(monkeypatch, configmap, extra)
        for field in (
            "rate_limit_enabled",
            "rate_limit_requests",
            "rate_limit_stream_requests",
            "trusted_proxy_hops",
        ):
            assert getattr(settings, field) == Settings.model_fields[field].default, field

    def test_limiter_and_proxy_settings_are_configurable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        docs = _template(
            "--set-string",
            "worker.env.rateLimitEnabled=false",
            "--set-string",
            "worker.env.rateLimitRequests=30",
            "--set-string",
            "worker.env.rateLimitStreamRequests=3",
            "--set-string",
            "worker.env.trustedProxyHops=2",
        )
        extra = {"TRAFFIC_AI_API_TOKEN": GATE_TOKEN, "TRAFFIC_AI_DATABASE_URL": GATE_DB_URL}
        settings = _gate_settings(monkeypatch, _named(docs, "ConfigMap", WORKER), extra)
        assert settings.rate_limit_enabled is False
        assert settings.rate_limit_requests == 30
        assert settings.rate_limit_stream_requests == 3
        assert settings.trusted_proxy_hops == 2

    def test_ui_configmap_does_not_carry_the_limiter_settings(self, docs: list[dict]) -> None:
        # The limiter is the worker's: the UI process serves no /api.
        data = _named(docs, "ConfigMap", UI)["data"]
        assert not [k for k in data if "RATE_LIMIT" in k or "PROXY" in k]

    def test_every_traffic_ai_variable_names_a_real_setting(self, docs: list[dict]) -> None:
        # Settings ignores unknown variables (extra="ignore"), so a typo would leave the
        # default in force while the chart claimed otherwise.
        for name in (WORKER, UI):
            for key in _named(docs, "ConfigMap", name)["data"]:
                if key.startswith("TRAFFIC_AI_"):
                    field = key.removeprefix("TRAFFIC_AI_").lower()
                    assert field in Settings.model_fields, f"{name}: {key} is not a setting"


class TestSecrets:
    def test_existing_secret_renders_no_secret_and_leaks_nothing(self) -> None:
        raw = _template_raw(
            "--set",
            "auth.existingSecret=my-secret",
            "--set",
            f"auth.apiToken={TOKEN_SENTINEL}",
            "--set",
            f"auth.databaseUrl={DB_URL_SENTINEL}",
        )
        assert TOKEN_SENTINEL not in raw
        assert "sentinel-db-password" not in raw
        docs = [d for d in yaml.safe_load_all(raw) if d]
        assert not _of_kind(docs, "Secret")
        (container,) = _containers(_named(docs, "Deployment", WORKER))
        refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]}
        assert refs["TRAFFIC_AI_API_TOKEN"]["name"] == "my-secret"
        assert refs["TRAFFIC_AI_DATABASE_URL"]["name"] == "my-secret"
        # The UI pod takes the token from the same Secret, and only the token.
        (ui_container,) = _containers(_named(docs, "Deployment", UI))
        ui_refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in ui_container["env"]}
        assert ui_refs == {"TRAFFIC_AI_API_TOKEN": refs["TRAFFIC_AI_API_TOKEN"]}

    def test_existing_secret_key_names_are_configurable(self) -> None:
        docs = _template(
            "--set",
            "auth.existingSecret=my-secret",
            "--set",
            "auth.existingSecretTokenKey=tok",
            "--set",
            "auth.existingSecretDatabaseUrlKey=dsn",
        )
        (container,) = _containers(_named(docs, "Deployment", WORKER))
        env = _env(container)
        assert env["TRAFFIC_AI_API_TOKEN"]["valueFrom"]["secretKeyRef"]["key"] == "tok"
        assert env["TRAFFIC_AI_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["key"] == "dsn"

    @pytest.mark.parametrize("scenario", list(SCENARIOS))
    def test_no_secret_is_rendered_by_default(self, scenario: str) -> None:
        # Nothing to put in one, and an empty-string token would count as "set".
        assert not _of_kind(_template(*SCENARIOS[scenario]), "Secret")

    def test_generated_secret_is_the_explicit_lesser_path(self) -> None:
        docs = _template(
            "--set",
            f"auth.apiToken={TOKEN_SENTINEL}",
            "--set",
            f"auth.databaseUrl={DB_URL_SENTINEL}",
        )
        (secret,) = _of_kind(docs, "Secret")
        assert secret["stringData"] == {
            "api-token": TOKEN_SENTINEL,
            "database-url": DB_URL_SENTINEL,
        }
        (container,) = _containers(_named(docs, "Deployment", WORKER))
        ref = _env(container)["TRAFFIC_AI_API_TOKEN"]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == secret["metadata"]["name"]
        assert ref["key"] == "api-token"

    def test_values_files_carry_no_secret(self) -> None:
        for path in (DEFAULT_VALUES, PRODUCTION_VALUES):
            auth = yaml.safe_load(path.read_text(encoding="utf-8"))["auth"]
            assert not auth.get("apiToken"), f"{path.name} must not contain an API token"
            assert not auth.get("databaseUrl"), f"{path.name} must not contain a database URL"

    def test_redis_url_default_carries_no_credentials(self) -> None:
        for path in (DEFAULT_VALUES, PRODUCTION_VALUES):
            url = yaml.safe_load(path.read_text(encoding="utf-8"))["redis"]["url"]
            assert "@" not in url, f"{path.name}: redis.url must not embed credentials"


class TestMigrations:
    """`alembic upgrade head` as a Helm hook: compose.yaml's `migrate`, for Kubernetes."""

    def test_job_is_a_pre_install_and_pre_upgrade_hook(self, docs: list[dict]) -> None:
        hook = _hook(_migrate_job(docs))
        assert hook is not None
        assert _hook_events(_migrate_job(docs)) == {"pre-install", "pre-upgrade"}
        # A failed Job survives for its logs until the next attempt clears it; a
        # successful one is removed, so a clean release leaves nothing behind.
        assert set(hook["helm.sh/hook-delete-policy"].split(",")) == {
            "before-hook-creation",
            "hook-succeeded",
        }
        assert isinstance(hook["helm.sh/hook-weight"], str), "annotation values must be strings"

    def test_job_runs_alembic_upgrade_head_in_the_worker_image(
        self, docs: list[dict], worker: dict
    ) -> None:
        (container,) = _migrate_job(docs)["spec"]["template"]["spec"]["containers"]
        # `command` replaces the image's ENTRYPOINT (the API server).
        assert container["command"] == ["alembic", "upgrade", "head"]
        (worker_container,) = _containers(worker)
        assert container["image"] == worker_container["image"]

    def test_job_is_locked_down_exactly_like_the_workloads(
        self, docs: list[dict], worker: dict
    ) -> None:
        pod = _migrate_job(docs)["spec"]["template"]["spec"]
        (container,) = pod["containers"]
        (worker_container,) = _containers(worker)
        assert container["securityContext"] == worker_container["securityContext"]
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert pod["securityContext"] == worker["spec"]["template"]["spec"]["securityContext"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["restartPolicy"] == "Never"

    def test_job_writes_only_to_a_tmp_emptydir(self, docs: list[dict]) -> None:
        pod = _migrate_job(docs)["spec"]["template"]["spec"]
        (container,) = pod["containers"]
        assert {m["mountPath"]: m["name"] for m in container["volumeMounts"]} == {TMP_MOUNT: "tmp"}
        assert pod["volumes"] == [{"name": "tmp", "emptyDir": {}}]

    def test_job_has_resources_and_bounded_retries(self, docs: list[dict]) -> None:
        spec = _migrate_job(docs)["spec"]
        (container,) = spec["template"]["spec"]["containers"]
        for section in ("requests", "limits"):
            for resource in ("cpu", "memory"):
                assert container["resources"][section][resource], f"{section}.{resource}"
        assert spec["backoffLimit"] >= 0
        # Below Helm's default 5-minute hook timeout, so the Job fails with its own
        # reason instead of Helm giving up on it.
        assert 0 < spec["activeDeadlineSeconds"] < 300

    def test_job_uses_the_same_secret_references_as_the_worker(
        self, docs: list[dict], worker: dict
    ) -> None:
        (container,) = _migrate_job(docs)["spec"]["template"]["spec"]["containers"]
        (worker_container,) = _containers(worker)
        for name in ("TRAFFIC_AI_API_TOKEN", "TRAFFIC_AI_DATABASE_URL"):
            ref = _env(container)[name]["valueFrom"]["secretKeyRef"]
            assert ref == _env(worker_container)[name]["valueFrom"]["secretKeyRef"], name
            assert "value" not in _env(container)[name], f"{name} must not be a literal"

    def test_job_needs_nothing_the_chart_creates_after_pre_install_hooks(
        self, docs: list[dict]
    ) -> None:
        # Helm creates ordinary objects only AFTER pre-install hooks finish, so a Job
        # that mounted the ConfigMap or ran as the chart's ServiceAccount would hang on
        # the very first install.
        pod = _migrate_job(docs)["spec"]["template"]["spec"]
        (container,) = pod["containers"]
        assert "envFrom" not in container
        assert not [v for v in pod["volumes"] if "configMap" in v or "secret" in v]
        service_accounts = {d["metadata"]["name"] for d in _of_kind(docs, "ServiceAccount")}
        assert pod.get("serviceAccountName") not in service_accounts

    def test_job_runs_as_the_namespace_default_account_when_the_chart_makes_one(self) -> None:
        pod = _migrate_job(_template())["spec"]["template"]["spec"]
        assert "serviceAccountName" not in pod

    def test_job_names_a_pre_existing_service_account(self) -> None:
        extra = ("--set", "serviceAccount.create=false", "--set", "serviceAccount.name=pre-made")
        pod = _migrate_job(_template(*extra))["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] == "pre-made"
        default_pod = _migrate_job(_template("--set", "serviceAccount.create=false"))["spec"][
            "template"
        ]["spec"]
        assert default_pod["serviceAccountName"] == "default"

    def test_job_pods_are_not_selected_by_either_service(self, docs: list[dict]) -> None:
        job_labels = _migrate_job(docs)["spec"]["template"]["metadata"]["labels"]
        for service in _of_kind(docs, "Service"):
            assert not service["spec"]["selector"].items() <= job_labels.items()

    @staticmethod
    def _job_settings(
        monkeypatch: pytest.MonkeyPatch, job: dict, extra: dict[str, str]
    ) -> Settings:
        """`Settings` as the Job's process would build it: literal env plus secret-backed."""
        (container,) = job["spec"]["template"]["spec"]["containers"]
        literal = {e["name"]: e["value"] for e in container["env"] if "value" in e}
        return _gate_settings(monkeypatch, {"data": literal}, extra)

    def test_job_environment_passes_the_production_gate(
        self, docs: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extra = {"TRAFFIC_AI_API_TOKEN": GATE_TOKEN, "TRAFFIC_AI_DATABASE_URL": GATE_DB_URL}
        settings = self._job_settings(monkeypatch, _migrate_job(docs), extra)
        assert settings.is_production
        assert settings.database_url == GATE_DB_URL

    def test_job_refuses_a_missing_token_before_any_pod_is_replaced(
        self, docs: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValueError, match="TRAFFIC_AI_API_TOKEN is unset"):
            self._job_settings(
                monkeypatch, _migrate_job(docs), {"TRAFFIC_AI_DATABASE_URL": GATE_DB_URL}
            )

    def test_job_refuses_the_development_database_password(
        self, docs: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dev_url = "postgresql+asyncpg://traffic:traffic@db:5432/traffic_ai"
        extra = {"TRAFFIC_AI_API_TOKEN": GATE_TOKEN, "TRAFFIC_AI_DATABASE_URL": dev_url}
        with pytest.raises(ValueError, match="development password"):
            self._job_settings(monkeypatch, _migrate_job(docs), extra)

    def test_job_mirrors_allow_unauthenticated_so_it_agrees_with_the_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without the mirror the Job would refuse an open API the worker accepts.
        docs = _template("--set", "auth.allowUnauthenticated=true")
        settings = self._job_settings(
            monkeypatch, _migrate_job(docs), {"TRAFFIC_AI_DATABASE_URL": GATE_DB_URL}
        )
        assert settings.allow_unauthenticated
        assert not settings.auth_enabled

    def test_job_can_be_disabled_and_no_hook_remains(self) -> None:
        docs = _template(
            "--set",
            "migrations.enabled=false",
            "--set",
            f"auth.apiToken={TOKEN_SENTINEL}",
        )
        assert not _of_kind(docs, "Job")
        assert [d["kind"] for d in docs if _hook(d)] == []

    def test_retry_and_deadline_are_configurable(self) -> None:
        docs = _template(
            "--set", "migrations.backoffLimit=5", "--set", "migrations.activeDeadlineSeconds=100"
        )
        spec = _migrate_job(docs)["spec"]
        assert spec["backoffLimit"] == 5
        assert spec["activeDeadlineSeconds"] == 100

    def test_only_the_job_and_a_generated_secret_are_ever_hooks(self, docs: list[dict]) -> None:
        assert {d["kind"] for d in docs if _hook(d)} <= {"Job", "Secret"}

    # --- the generated Secret must exist BEFORE the Job starts -------------------------

    GENERATED = (
        "--set",
        f"auth.apiToken={TOKEN_SENTINEL}",
        "--set",
        f"auth.databaseUrl={DB_URL_SENTINEL}",
    )

    def test_generated_secret_is_a_hook_created_before_the_job(self) -> None:
        docs = _template(*self.GENERATED)
        (secret,) = _of_kind(docs, "Secret")
        job = _migrate_job(docs)
        # Same events, so whenever the Job runs the Secret has just been created...
        assert _hook_events(secret) == _hook_events(job) == {"pre-install", "pre-upgrade"}
        # ...and a lower weight runs first.
        secret_weight = int(_hook(secret)["helm.sh/hook-weight"])
        job_weight = int(_hook(job)["helm.sh/hook-weight"])
        assert secret_weight < job_weight
        # Recreated on every upgrade rather than failing on "already exists".
        assert _hook(secret)["helm.sh/hook-delete-policy"] == "before-hook-creation"

    def test_job_reads_the_generated_secret_by_its_name(self) -> None:
        docs = _template(*self.GENERATED)
        (secret,) = _of_kind(docs, "Secret")
        (container,) = _migrate_job(docs)["spec"]["template"]["spec"]["containers"]
        for name, key in (
            ("TRAFFIC_AI_API_TOKEN", "api-token"),
            ("TRAFFIC_AI_DATABASE_URL", "database-url"),
        ):
            ref = _env(container)[name]["valueFrom"]["secretKeyRef"]
            assert ref["name"] == secret["metadata"]["name"]
            assert ref["key"] == key
            assert key in secret["stringData"]

    def test_generated_secret_is_an_ordinary_object_when_migrations_are_off(self) -> None:
        docs = _template(*self.GENERATED, "--set", "migrations.enabled=false")
        (secret,) = _of_kind(docs, "Secret")
        assert _hook(secret) is None

    def test_existing_secret_path_needs_no_hook_secret(self) -> None:
        docs = _template("--set", "auth.existingSecret=my-secret")
        assert not _of_kind(docs, "Secret")
        (container,) = _migrate_job(docs)["spec"]["template"]["spec"]["containers"]
        refs = {
            e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"] if "valueFrom" in e
        }
        assert refs["TRAFFIC_AI_API_TOKEN"]["name"] == "my-secret"
        assert refs["TRAFFIC_AI_API_TOKEN"]["optional"] is False
        assert refs["TRAFFIC_AI_DATABASE_URL"]["optional"] is False

    def test_hook_secret_still_leaks_no_credentials_onto_other_objects(self) -> None:
        raw = _template_raw(*self.GENERATED)
        docs = [d for d in yaml.safe_load_all(raw) if d]
        for doc in docs:
            if doc["kind"] != "Secret":
                assert TOKEN_SENTINEL not in yaml.safe_dump(doc), doc["kind"]
                assert "sentinel-db-password" not in yaml.safe_dump(doc), doc["kind"]


class TestInstallNotes:
    """NOTES.txt is what an operator reads right after `helm install`."""

    def test_notes_explain_the_edge_login_and_that_healthz_needs_it(self) -> None:
        notes = _install_notes(*SCENARIOS["production"])
        assert "EDGE LOGIN" in notes
        assert "BasicAuth" in notes
        assert "/api/healthz is behind" in notes
        assert "curl -u" in notes

    def test_notes_omit_the_edge_login_when_it_is_off(self) -> None:
        assert "EDGE LOGIN" not in _install_notes()

    def test_traefik_notes_point_at_the_middleware_crs(self) -> None:
        notes = _install_notes(*SCENARIOS["production"])
        assert "Middleware CRs" in notes
        assert "video" not in notes.split("EDGE LOGIN")[1].split("MIGRATIONS")[0]

    def test_nginx_notes_name_the_video_limitation(self) -> None:
        notes = _install_notes(*SCENARIOS["nginx"])
        assert "EDGE LOGIN" in notes
        assert "live video" in notes
        assert "get 401" in notes

    def test_notes_name_the_migration_hook_and_how_to_read_a_failure(self) -> None:
        notes = _install_notes()
        assert "MIGRATIONS" in notes
        assert "logs job/traffic-ai-migrate" in notes
        assert "alembic upgrade head" in notes

    def test_notes_warn_when_migrations_are_disabled(self) -> None:
        notes = _install_notes("--set", "migrations.enabled=false")
        assert "WARNING: migrations.enabled is false" in notes
        assert "MIGRATIONS:" not in notes

    def test_notes_state_the_single_worker_replica_rule(self) -> None:
        notes = _install_notes()
        assert "WORKER REPLICAS" in notes
        assert "keep worker.replicaCount at 1" in notes
        assert "Recreate" in notes

    def test_notes_warn_that_a_generated_secret_outlives_uninstall(self) -> None:
        notes = _install_notes("--set", "auth.apiToken=x" + "y" * 40, "--set", "auth.databaseUrl=u")
        assert "helm uninstall` leaves it behind" in notes


class TestIngress:
    def test_api_is_declared_before_the_ui_catch_all(self, docs: list[dict]) -> None:
        (ingress,) = _of_kind(docs, "Ingress")
        (rule,) = ingress["spec"]["rules"]
        paths = rule["http"]["paths"]
        assert [p["path"] for p in paths] == ["/api", "/"]
        assert all(p["pathType"] == "Prefix" for p in paths)

    def test_paths_route_to_the_right_services(self, docs: list[dict]) -> None:
        (ingress,) = _of_kind(docs, "Ingress")
        by_path = {p["path"]: p for p in ingress["spec"]["rules"][0]["http"]["paths"]}
        assert by_path["/api"]["backend"]["service"]["name"] == WORKER
        assert by_path["/"]["backend"]["service"]["name"] == UI

    def test_ingress_backends_resolve_to_a_service_port(self, docs: list[dict]) -> None:
        # The port name an ingress references must exist on the target Service.
        (ingress,) = _of_kind(docs, "Ingress")
        for path in ingress["spec"]["rules"][0]["http"]["paths"]:
            backend = path["backend"]["service"]
            service = _named(docs, "Service", backend["name"])
            assert backend["port"]["name"] in {p["name"] for p in service["spec"]["ports"]}

    def test_production_ingress_terminates_tls_on_the_host(self) -> None:
        docs = _template(*SCENARIOS["production"])
        (ingress,) = _of_kind(docs, "Ingress")
        assert ingress["spec"]["tls"] == [
            {"hosts": ["traffic.example.org"], "secretName": "traffic-ai-tls"}
        ]
        assert ingress["spec"]["rules"][0]["host"] == "traffic.example.org"

    def test_ingress_can_be_disabled(self) -> None:
        assert not _of_kind(_template("--set", "ingress.enabled=false"), "Ingress")


class TestEdgeAuth:
    """Edge authentication on the ingress, the Kubernetes form of compose's BasicAuth."""

    NGINX_EDGE = (
        "--set",
        "ingress.edgeAuth.enabled=true",
        "--set",
        "ingress.edgeAuth.secretName=basic-auth-users",
        "--set",
        "ingress.tls.enabled=true",
        "--set",
        "ingress.tls.secretName=traffic-ai-tls",
    )
    TRAEFIK_EDGE = (
        "--set",
        "ingress.edgeAuth.enabled=true",
        "--set",
        "ingress.tls.enabled=true",
        "--set",
        "ingress.tls.secretName=traffic-ai-tls",
        *_TRAEFIK,
    )

    def test_edge_auth_is_off_by_default(self) -> None:
        annotations = _annotations(_ingress())
        assert not [k for k in annotations if k.startswith(NGINX_AUTH_PREFIX)]
        assert TRAEFIK_MIDDLEWARES_ANNOTATION not in annotations
        assert NGINX_LIMIT_RPS not in annotations

    def test_production_values_default_to_traefik_with_the_full_middleware_chain(self) -> None:
        # ingress-nginx cannot inject the API token, so under it the video is 401.
        # Traefik is the one controller where the whole login works, so it is the default.
        ingress = _ingress(*_PRODUCTION)
        assert ingress["spec"]["ingressClassName"] == "traefik"
        chain = _annotations(ingress)[TRAEFIK_MIDDLEWARES_ANNOTATION].split(",")
        assert chain == PRODUCTION_MIDDLEWARES

    def test_production_chain_limits_the_rate_before_the_login_before_the_bearer(self) -> None:
        chain = _annotations(_ingress(*_PRODUCTION))[TRAEFIK_MIDDLEWARES_ANNOTATION].split(",")
        limiter, login, bearer = (
            chain.index(f"traffic-ai-{name}@kubernetescrd")
            for name in ("ratelimit", "basicauth", "api-bearer")
        )
        # A limiter after BasicAuth would never see the guesses it exists to slow down;
        # the bearer must replace the browser's header only after the login has checked it.
        assert limiter < login < bearer

    def test_production_traefik_ingress_carries_no_nginx_annotations(self) -> None:
        annotations = _annotations(_ingress(*_PRODUCTION))
        assert not [k for k in annotations if k.startswith("nginx.ingress.kubernetes.io/")]

    def test_production_network_policy_admits_the_default_controller_traefik(self) -> None:
        # The policy is default-deny: selectors left on ingress-nginx's labels would
        # lock the controller the overlay actually selects out of /api.
        docs = _template(*SCENARIOS["production"])
        (policy,) = _of_kind(docs, "NetworkPolicy")
        _, controller = policy["spec"]["ingress"][0]["from"]
        assert controller["namespaceSelector"]["matchLabels"] == {
            "kubernetes.io/metadata.name": "traefik"
        }
        assert controller["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "traefik"}

    def test_production_values_with_nginx_still_gate_the_whole_host(self) -> None:
        # The documented alternative: nginx stays a choice, with its video limitation.
        annotations = _annotations(_ingress(*SCENARIOS["nginx"]))
        assert annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"
        assert annotations["nginx.ingress.kubernetes.io/auth-secret"] == "traffic-ai-basic-auth"
        assert annotations["nginx.ingress.kubernetes.io/auth-realm"]
        assert TRAEFIK_MIDDLEWARES_ANNOTATION not in annotations

    def test_values_production_keeps_the_nginx_alternative_documented(self) -> None:
        # The streaming annotations moved out of the active values with the default
        # controller; they must stay written down for whoever picks nginx.
        text = PRODUCTION_VALUES.read_text(encoding="utf-8")
        for needle in (
            "Using ingress-nginx instead",
            "proxy-read-timeout",
            "proxy-buffering",
            "does not play",
        ):
            assert needle in text, f"values-production.yaml no longer documents: {needle}"

    # --- ingress-nginx: BasicAuth has no lockout, so cap the guessing rate ------------

    def test_nginx_edge_auth_sets_a_per_client_rate_limit_as_a_string(self) -> None:
        annotations = _annotations(_ingress(*self.NGINX_EDGE))
        # An annotation cannot hold a number: it has to be the string "20".
        assert annotations[NGINX_LIMIT_RPS] == "20"
        assert isinstance(annotations[NGINX_LIMIT_RPS], str)

    def test_nginx_rate_limit_is_configurable(self) -> None:
        ingress = _ingress(*self.NGINX_EDGE, "--set", "ingress.edgeAuth.limitRps=5")
        assert _annotations(ingress)[NGINX_LIMIT_RPS] == "5"

    def test_nginx_rate_limit_can_be_left_out_with_zero(self) -> None:
        ingress = _ingress(*self.NGINX_EDGE, "--set", "ingress.edgeAuth.limitRps=0")
        assert NGINX_LIMIT_RPS not in _annotations(ingress)

    def test_an_operators_own_limit_rps_annotation_wins(self) -> None:
        ingress = _ingress(
            *self.NGINX_EDGE,
            "--set-string",
            "ingress.annotations.nginx\\.ingress\\.kubernetes\\.io/limit-rps=3",
        )
        assert _annotations(ingress)[NGINX_LIMIT_RPS] == "3"

    def test_no_nginx_rate_limit_without_edge_auth(self) -> None:
        assert NGINX_LIMIT_RPS not in _annotations(_ingress("--set", "ingress.edgeAuth.limitRps=9"))

    def test_traefik_gets_no_nginx_rate_limit_annotation(self) -> None:
        # On Traefik the limit is a RateLimit Middleware CR first in the chain.
        assert NGINX_LIMIT_RPS not in _annotations(_ingress(*self.TRAEFIK_EDGE))

    def test_nginx_auth_references_a_secret_by_name_and_renders_none(self) -> None:
        docs = _template(*self.NGINX_EDGE)
        (ingress,) = _of_kind(docs, "Ingress")
        assert _annotations(ingress)["nginx.ingress.kubernetes.io/auth-secret"] == (
            "basic-auth-users"
        )
        assert not _of_kind(docs, "Secret"), "htpasswd hashes are created out of band"

    def test_nginx_edge_auth_adds_no_traefik_annotation(self) -> None:
        assert TRAEFIK_MIDDLEWARES_ANNOTATION not in _annotations(_ingress(*self.NGINX_EDGE))

    def test_realm_is_configurable(self) -> None:
        ingress = _ingress(*self.NGINX_EDGE, "--set", "ingress.edgeAuth.realm=Cameras")
        assert _annotations(ingress)["nginx.ingress.kubernetes.io/auth-realm"] == "Cameras"

    def test_user_annotations_are_kept_alongside_the_auth_ones(self) -> None:
        ingress = _ingress(
            *self.NGINX_EDGE,
            "--set-string",
            "ingress.annotations.example\\.org/keep=yes",
        )
        annotations = _annotations(ingress)
        assert annotations["example.org/keep"] == "yes"
        assert annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"

    def test_nginx_edge_auth_without_a_secret_name_is_refused(self) -> None:
        result = _helm(
            "template",
            RELEASE,
            str(CHART),
            "--set",
            "ingress.edgeAuth.enabled=true",
            "--set",
            "ingress.tls.enabled=true",
            "--set",
            "ingress.tls.secretName=traffic-ai-tls",
        )
        assert result.returncode != 0
        assert "ingress.edgeAuth.secretName is required" in result.stderr

    def test_edge_auth_without_tls_is_refused(self) -> None:
        # BasicAuth over plain HTTP sends the password in clear text.
        result = _helm(
            "template",
            RELEASE,
            str(CHART),
            "--set",
            "ingress.edgeAuth.enabled=true",
            "--set",
            "ingress.edgeAuth.secretName=basic-auth-users",
        )
        assert result.returncode != 0
        assert "requires ingress.tls.enabled" in result.stderr

    def test_traefik_attaches_the_middlewares_in_the_given_order(self) -> None:
        annotations = _annotations(_ingress(*self.TRAEFIK_EDGE))
        chain = annotations[TRAEFIK_MIDDLEWARES_ANNOTATION].split(",")
        # BasicAuth must run before the bearer is injected, as in compose.yaml.
        assert chain == TRAEFIK_MIDDLEWARES
        assert chain.index("traffic-ai-basicauth@kubernetescrd") < chain.index(
            "traffic-ai-api-bearer@kubernetescrd"
        )

    def test_traefik_edge_auth_adds_no_nginx_annotations(self) -> None:
        annotations = _annotations(_ingress(*self.TRAEFIK_EDGE))
        assert not [k for k in annotations if k.startswith("nginx.ingress.kubernetes.io/auth-")]

    def test_traefik_ingress_class_is_set(self) -> None:
        assert _ingress(*self.TRAEFIK_EDGE)["spec"]["ingressClassName"] == "traefik"

    def test_traefik_edge_auth_without_middlewares_is_refused(self) -> None:
        # Edge auth was asked for and nothing would enforce it: fail, never render
        # an Ingress that looks protected and is not.
        result = _helm(
            "template",
            RELEASE,
            str(CHART),
            "--set",
            "ingress.className=traefik",
            "--set",
            "ingress.edgeAuth.enabled=true",
            "--set",
            "ingress.tls.enabled=true",
            "--set",
            "ingress.tls.secretName=traffic-ai-tls",
        )
        assert result.returncode != 0
        assert "ingress.traefik.middlewares is required" in result.stderr

    def test_traefik_without_edge_auth_and_without_middlewares_adds_nothing(self) -> None:
        annotations = _annotations(_ingress("--set", "ingress.className=traefik"))
        assert TRAEFIK_MIDDLEWARES_ANNOTATION not in annotations

    def test_middlewares_are_ignored_for_other_controllers(self) -> None:
        # The annotation is Traefik's; on another controller it would be noise.
        annotations = _annotations(_ingress(*_TRAEFIK[2:]))  # middlewares, default nginx class
        assert TRAEFIK_MIDDLEWARES_ANNOTATION not in annotations

    def test_the_chart_templates_no_crds(self) -> None:
        # Middleware CRs are user-created: the chart must render cleanly on a
        # cluster that has no Traefik CRDs installed.
        docs = _template(*self.TRAEFIK_EDGE)
        for doc in docs:
            assert not doc["apiVersion"].startswith("traefik."), doc["kind"]

    def test_the_token_never_reaches_the_ingress(self) -> None:
        # Annotations are not secret, and ingress-nginx disables configuration
        # snippets by default. Neither the token nor a header-injecting snippet
        # may appear on the Ingress, however it is configured.
        for extra in (self.NGINX_EDGE, self.TRAEFIK_EDGE):
            ingress = _ingress(*extra, "--set", f"auth.apiToken={TOKEN_SENTINEL}")
            assert TOKEN_SENTINEL not in yaml.safe_dump(ingress)
            for key, value in _annotations(ingress).items():
                assert "snippet" not in key
                assert "Bearer" not in value
                assert "Authorization" not in value


class TestServices:
    def test_services_select_their_deployment_pods(self, docs: list[dict]) -> None:
        for service_name, deployment_name in ((WORKER, WORKER), (UI, UI)):
            service = _named(docs, "Service", service_name)
            deployment = _named(docs, "Deployment", deployment_name)
            labels = deployment["spec"]["template"]["metadata"]["labels"]
            assert service["spec"]["selector"].items() <= labels.items()

    def test_services_are_cluster_internal(self, docs: list[dict]) -> None:
        # Only the ingress is exposed — the Compose rule that the worker and
        # redis are never published, carried over.
        for service in _of_kind(docs, "Service"):
            assert service["spec"]["type"] == "ClusterIP"


class TestLabels:
    def test_every_object_carries_the_recommended_labels(self, docs: list[dict]) -> None:
        for doc in docs:
            missing = RECOMMENDED_LABELS - doc["metadata"]["labels"].keys()
            assert not missing, f"{doc['kind']}/{doc['metadata']['name']} lacks {missing}"

    def test_components_are_labelled(self, worker: dict, ui: dict) -> None:
        assert worker["metadata"]["labels"]["app.kubernetes.io/component"] == "worker"
        assert ui["metadata"]["labels"]["app.kubernetes.io/component"] == "ui"


class TestOptInResources:
    def test_hpa_and_pdb_and_networkpolicy_are_off_by_default(self) -> None:
        docs = _template()
        for kind in ("HorizontalPodAutoscaler", "PodDisruptionBudget", "NetworkPolicy"):
            assert not _of_kind(docs, kind), f"{kind} must be opt-in"

    def test_hpa_renders_autoscaling_v2_for_the_ui_only(self) -> None:
        docs = _template("--set", "autoscaling.enabled=true")
        hpas = _of_kind(docs, "HorizontalPodAutoscaler")
        # The worker is never autoscaled: each replica would write duplicate history.
        assert {h["spec"]["scaleTargetRef"]["name"] for h in hpas} == {UI}
        for hpa in hpas:
            assert hpa["apiVersion"] == "autoscaling/v2"
            assert hpa["spec"]["scaleTargetRef"]["kind"] == "Deployment"
            assert hpa["spec"]["minReplicas"] <= hpa["spec"]["maxReplicas"]
            (metric,) = hpa["spec"]["metrics"]
            assert metric["resource"]["target"]["type"] == "Utilization"

    def test_hpa_owns_the_ui_replicas_when_enabled(self) -> None:
        docs = _template("--set", "autoscaling.enabled=true")
        assert "replicas" not in _named(docs, "Deployment", UI)["spec"]
        assert "replicas" in _named(_template(), "Deployment", UI)["spec"]
        # The worker's count is never handed to an HPA.
        assert "replicas" in _named(docs, "Deployment", WORKER)["spec"]

    def test_pdb_defaults_to_a_budget_that_cannot_block_drains(self) -> None:
        docs = _template("--set", "podDisruptionBudget.enabled=true")
        pdbs = _of_kind(docs, "PodDisruptionBudget")
        assert {p["metadata"]["name"] for p in pdbs} == {WORKER, UI}
        for pdb in pdbs:
            assert pdb["apiVersion"] == "policy/v1"
            # The chart ships one replica per Deployment: minAvailable: 1 would
            # make every pod unevictable and block `kubectl drain` forever.
            assert pdb["spec"] == {
                "maxUnavailable": 1,
                "selector": pdb["spec"]["selector"],
            }

    def test_pdb_selectors_match_pod_labels(self) -> None:
        docs = _template("--set", "podDisruptionBudget.enabled=true")
        for pdb in _of_kind(docs, "PodDisruptionBudget"):
            deployment = _named(docs, "Deployment", pdb["metadata"]["name"])
            labels = deployment["spec"]["template"]["metadata"]["labels"]
            assert pdb["spec"]["selector"]["matchLabels"].items() <= labels.items()

    def test_pdb_min_available_that_would_block_drains_is_refused(self) -> None:
        result = _helm(
            "template",
            RELEASE,
            str(CHART),
            "--set",
            "podDisruptionBudget.enabled=true",
            "--set",
            "podDisruptionBudget.minAvailable=1",
        )
        assert result.returncode != 0
        assert "block forever" in result.stderr

    def test_pdb_min_available_is_refused_when_the_single_worker_alone_would_block(self) -> None:
        # The worker is never autoscaled, so its floor is always worker.replicaCount.
        # With the UI comfortably above 1, only the worker's single replica is at issue.
        result = _helm(
            "template",
            RELEASE,
            str(CHART),
            "--set",
            "podDisruptionBudget.enabled=true",
            "--set",
            "podDisruptionBudget.minAvailable=1",
            "--set",
            "ui.replicaCount=3",
        )
        assert result.returncode != 0
        assert "worker=1, ui=3" in result.stderr

    def test_pdb_min_available_is_allowed_with_enough_replicas(self) -> None:
        docs = _template(
            "--set",
            "podDisruptionBudget.enabled=true",
            "--set",
            "podDisruptionBudget.minAvailable=1",
            "--set",
            "worker.replicaCount=2",
            "--set",
            "ui.replicaCount=2",
        )
        for pdb in _of_kind(docs, "PodDisruptionBudget"):
            assert pdb["spec"]["minAvailable"] == 1
            assert "maxUnavailable" not in pdb["spec"]

    def test_networkpolicy_default_denies_worker_except_ui_and_ingress_controller(self) -> None:
        docs = _template("--set", "networkPolicy.enabled=true")
        (policy,) = _of_kind(docs, "NetworkPolicy")
        worker = _named(docs, "Deployment", WORKER)
        ui = _named(docs, "Deployment", UI)

        # Selects the worker pods (and only them) and governs ingress.
        selected = policy["spec"]["podSelector"]["matchLabels"]
        assert selected.items() <= worker["spec"]["template"]["metadata"]["labels"].items()
        assert not selected.items() <= ui["spec"]["template"]["metadata"]["labels"].items()
        assert policy["spec"]["policyTypes"] == ["Ingress"]

        (rule,) = policy["spec"]["ingress"]
        ui_peer, controller_peer = rule["from"]
        assert ui_peer["podSelector"]["matchLabels"].items() <= (
            ui["spec"]["template"]["metadata"]["labels"].items()
        )
        # Namespace AND pod selector in one peer: ANDed, not two permissive peers.
        assert set(controller_peer) == {"namespaceSelector", "podSelector"}
        assert rule["ports"] == [{"protocol": "TCP", "port": 8000}]

    def test_production_values_enable_the_hardening_options(self) -> None:
        docs = _template(*SCENARIOS["production"])
        assert _of_kind(docs, "NetworkPolicy")
        assert _of_kind(docs, "PodDisruptionBudget")
        # One worker replica on purpose: replicas are redundancy, not scale-out.
        assert _named(docs, "Deployment", WORKER)["spec"]["replicas"] == 1
        assert _named(docs, "Deployment", UI)["spec"]["replicas"] == 2

    def test_production_values_with_every_opt_in_still_render(self) -> None:
        docs = _template(
            *SCENARIOS["production"],
            "--set",
            "autoscaling.enabled=true",
        )
        assert len(_of_kind(docs, "HorizontalPodAutoscaler")) == 1  # the UI's

    def test_pdb_min_available_is_checked_against_the_ui_hpa_floor(self) -> None:
        # With the UI's HPA on, its minReplicas (not the static count) is the floor.
        base = (
            "--set",
            "autoscaling.enabled=true",
            "--set",
            "podDisruptionBudget.enabled=true",
            "--set",
            "podDisruptionBudget.minAvailable=1",
            "--set",
            "worker.replicaCount=2",
            "--set",
            "ui.replicaCount=5",
        )
        refused = _helm(
            "template", RELEASE, str(CHART), *base, "--set", "autoscaling.ui.minReplicas=1"
        )
        assert refused.returncode != 0
        assert "block forever" in refused.stderr
        allowed = _template(*base, "--set", "autoscaling.ui.minReplicas=2")
        assert _of_kind(allowed, "PodDisruptionBudget")


class TestVideosVolume:
    def test_videos_volume_is_read_only_when_enabled(self) -> None:
        docs = _template(
            "--set",
            "worker.videosVolume.enabled=true",
            "--set",
            "worker.videosVolume.existingClaim=demo-videos",
        )
        pod = _named(docs, "Deployment", WORKER)["spec"]["template"]["spec"]
        (container,) = pod["containers"]
        (mount,) = [m for m in container["volumeMounts"] if m["name"] == "videos"]
        assert mount["readOnly"] is True
        assert mount["mountPath"] == "/data/videos"
        (volume,) = [v for v in pod["volumes"] if v["name"] == "videos"]
        assert volume["persistentVolumeClaim"] == {"claimName": "demo-videos", "readOnly": True}

    def test_enabling_videos_without_a_claim_is_refused(self) -> None:
        result = _helm("template", RELEASE, str(CHART), "--set", "worker.videosVolume.enabled=true")
        assert result.returncode != 0
        assert "existingClaim" in result.stderr


def test_scenarios_reference_files_that_exist() -> None:
    assert CHART.is_dir()
    assert PRODUCTION_VALUES.is_file()
    assert DEFAULT_VALUES.is_file()
