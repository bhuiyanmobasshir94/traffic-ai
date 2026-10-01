"""Structural checks on the Helm chart in deploy/helm/traffic-ai.

These shell out to `helm template` / `helm lint` — no cluster is contacted, and
nothing is installed — then parse the rendered manifests and assert the
invariants docs/KUBERNETES.md and CLAUDE.md's non-negotiables depend on: both
Deployments exist on the right ports, every container is non-root with a
read-only root filesystem and has requests AND limits, probes hit the paths the
worker and UI actually serve, `/api` is declared before `/` on the ingress, and
no secret value is ever rendered when an existing Secret is used.

Skipped cleanly where `helm` is not installed, so the suite still passes on a
laptop or CI runner without it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

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

# The same two scenarios the chart is documented to be deployed in.
SCENARIOS: dict[str, list[str]] = {
    "default": [],
    "production": ["-f", str(PRODUCTION_VALUES), "--set", "ingress.host=traffic.example.org"],
}


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

    def test_ui_holds_no_secret(self, ui: dict) -> None:
        # compose.yaml gives the UI neither a token nor a database URL: it is a
        # thin viewer over HTTP and never connects to the database.
        (container,) = _containers(ui)
        assert "secretKeyRef" not in str(container.get("env", []))

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
