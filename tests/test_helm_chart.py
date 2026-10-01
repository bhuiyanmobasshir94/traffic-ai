"""Structural checks on the Helm chart in deploy/helm/traffic-ai.

These shell out to `helm template` / `helm lint` — no cluster is contacted, and
nothing is installed — then parse the rendered manifests and assert the
invariants docs/KUBERNETES.md and CLAUDE.md's non-negotiables depend on: both
Deployments exist on the right ports, every container is non-root with a
read-only root filesystem and has requests AND limits, probes hit the paths the
worker and UI actually serve, `/api` is declared before `/` on the ingress, edge
authentication is wired per controller (ingress-nginx annotations, or an ordered
Traefik middleware chain) and refuses to render half-configured, and no secret
value is ever rendered when an existing Secret is used. The rendered ConfigMaps
are also fed into the real `Settings` production gate.

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
_PRODUCTION = ["-f", str(PRODUCTION_VALUES), "--set", "ingress.host=traffic.example.org"]
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
    "production": _PRODUCTION,
    # Production values with Traefik as the ingress controller (edge auth via
    # user-created Middleware CRs). Every structural test below runs against it too.
    "traefik": [*_PRODUCTION, *_TRAEFIK],
}

NGINX_AUTH_PREFIX = "nginx.ingress.kubernetes.io/auth-"
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

    def test_production_values_turn_on_ingress_nginx_basic_auth(self) -> None:
        annotations = _annotations(_ingress(*_PRODUCTION))
        assert annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"
        assert annotations["nginx.ingress.kubernetes.io/auth-secret"] == "traffic-ai-basic-auth"
        assert annotations["nginx.ingress.kubernetes.io/auth-realm"]

    def test_production_nginx_edge_auth_keeps_the_streaming_annotations(self) -> None:
        annotations = _annotations(_ingress(*_PRODUCTION))
        assert annotations["nginx.ingress.kubernetes.io/proxy-read-timeout"] == "3600"
        assert annotations["nginx.ingress.kubernetes.io/proxy-buffering"] == "off"
        assert annotations["nginx.ingress.kubernetes.io/ssl-redirect"] == "true"

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

    def test_hpa_renders_autoscaling_v2_targeting_each_deployment(self) -> None:
        docs = _template("--set", "autoscaling.enabled=true")
        hpas = _of_kind(docs, "HorizontalPodAutoscaler")
        assert {h["spec"]["scaleTargetRef"]["name"] for h in hpas} == {WORKER, UI}
        for hpa in hpas:
            assert hpa["apiVersion"] == "autoscaling/v2"
            assert hpa["spec"]["scaleTargetRef"]["kind"] == "Deployment"
            assert hpa["spec"]["minReplicas"] <= hpa["spec"]["maxReplicas"]
            (metric,) = hpa["spec"]["metrics"]
            assert metric["resource"]["target"]["type"] == "Utilization"

    def test_hpa_owns_replicas_when_enabled(self) -> None:
        docs = _template("--set", "autoscaling.enabled=true")
        for name in (WORKER, UI):
            assert "replicas" not in _named(docs, "Deployment", name)["spec"]
        for name in (WORKER, UI):
            assert "replicas" in _named(_template(), "Deployment", name)["spec"]

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
        assert len(_of_kind(docs, "HorizontalPodAutoscaler")) == 2


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
