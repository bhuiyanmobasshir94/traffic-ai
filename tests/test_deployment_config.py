"""Structural checks on the deployment config.

These do not build or start anything — no Docker daemon required. They parse
compose.yaml and the two Dockerfiles as text/YAML and assert the invariants
docs/DEPLOYMENT.md and CLAUDE.md's non-negotiables depend on: only traefik is
published to the host (redis, postgres and the worker never are), the /api
router always wins over the UI catch-all, both routers sit behind BasicAuth with
the bearer token injected AFTER it on /api and a rate limit in FRONT of the login,
the schema migrates before the worker starts, every service has resource limits
and a stop grace period that covers the worker's shutdown, no image floats on
`:latest`, and the Traefik docker-socket mount is read-only.

A few tests go one step further and feed the compose environment, with its
`${VAR:-default}` interpolation resolved, into the real `Settings` production
gate: that is the contract the compose comments promise, so it is checked against
the code rather than against more strings.

The same file also holds the checks on the rest of the deployment surface that
is not Helm: the Makefile's destructive target, the CI and release workflows
(SHA-pinned actions, images only after CI, no `latest` for pre-releases), and the
runbooks' agreement with the code.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from traffic_ai.config import Settings
from traffic_ai.domain import ReadinessResponse
from traffic_ai.metrics import render

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "compose.yaml"
GITIGNORE_PATH = REPO_ROOT / ".gitignore"
HTPASSWD_EXAMPLE = REPO_ROOT / "config" / "traefik" / "users.htpasswd.example"
MAKEFILE = REPO_ROOT / "Makefile"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "DEPLOYMENT.md"
KUBERNETES_DOC = REPO_ROOT / "docs" / "KUBERNETES.md"
HELM_VALUES = [
    REPO_ROOT / "deploy" / "helm" / "traffic-ai" / "values.yaml",
    REPO_ROOT / "deploy" / "helm" / "traffic-ai" / "values-production.yaml",
]
APP_MODULE = REPO_ROOT / "src" / "traffic_ai" / "api" / "app.py"
APP_MAIN_MODULE = REPO_ROOT / "src" / "traffic_ai" / "api" / "__main__.py"
DEMO_VIDEO_FILENAMES = {"toll-plaza-a.mp4", "toll-plaza-b.mp4"}

# Distinctive, and long enough to satisfy MIN_API_TOKEN_LENGTH.
TEST_TOKEN = "t" * 64
TEST_DB_PASSWORD = "a-real-database-password"  # noqa: S105 - a test value, not a credential
# The two values the production gate demands of the worker; everything else is under test.
LIMITER_ENV = {"TRAFFIC_AI_API_TOKEN": TEST_TOKEN, "POSTGRES_PASSWORD": TEST_DB_PASSWORD}

# `${NAME}` or `${NAME:-default}` — the only interpolation forms compose.yaml uses.
_INTERPOLATION = re.compile(r"\$\{(?P<name>\w+)(?::-(?P<default>[^}]*))?\}")


@pytest.fixture(scope="module")
def compose() -> dict:
    with COMPOSE_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def services(compose: dict) -> dict:
    return compose["services"]


def _label_value(labels: list[str], key: str) -> str | None:
    prefix = f"{key}="
    for label in labels:
        if label.startswith(prefix):
            return label[len(prefix) :]
    return None


def _router_priority(labels: list[str], router: str) -> int:
    value = _label_value(labels, f"traefik.http.routers.{router}.priority")
    assert value is not None, f"no priority label for router {router!r}"
    return int(value)


def _router_middlewares(labels: list[str], router: str) -> list[str]:
    value = _label_value(labels, f"traefik.http.routers.{router}.middlewares")
    assert value is not None, f"router {router!r} has no middlewares label"
    return value.split(",")


def _middleware_name(reference: str) -> str:
    """`worker-basicauth@docker` -> `worker-basicauth`."""
    name, _, provider = reference.partition("@")
    assert provider == "docker", f"{reference!r} must name the docker provider"
    return name


def _interpolate(value: object, env: dict[str, str]) -> str:
    """Resolve compose's `${NAME:-default}` the way `docker compose` would."""

    def replace(match: re.Match[str]) -> str:
        name, default = match.group("name"), match.group("default")
        found = env.get(name, "")
        # `:-` substitutes the default for unset AND empty, like the shell.
        return found if found else (default or "")

    return _INTERPOLATION.sub(replace, str(value))


def _resolved_environment(service: dict, env: dict[str, str]) -> dict[str, str]:
    return {k: _interpolate(v, env) for k, v in service["environment"].items()}


def _build_production_settings(
    monkeypatch: pytest.MonkeyPatch, resolved: dict[str, str]
) -> Settings:
    # Settings reads os.environ; start from a clean TRAFFIC_AI_* slate so an
    # ambient variable on the machine running the tests cannot change the result.
    for key in [k for k in os.environ if k.startswith("TRAFFIC_AI_")]:
        monkeypatch.delenv(key)
    for key, value in resolved.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def _memory_bytes(value: str) -> int:
    match = re.fullmatch(r"(\d+)([KMG])", value)
    assert match, f"unparseable memory value {value!r}"
    return int(match.group(1)) * {"K": 1024, "M": 1024**2, "G": 1024**3}[match.group(2)]


def _seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)(s|m)", value)
    assert match, f"unparseable duration {value!r}"
    return int(match.group(1)) * {"s": 1, "m": 60}[match.group(2)]


def _source_number(path: Path, name: str) -> float:
    """Read `NAME = <number>` out of a source file without importing it.

    The API modules build the app at import time; the shutdown budgets are read
    from the text so the compose grace period is held to the real constants.
    """
    match = re.search(rf"^{name}\s*=\s*([0-9.]+)", path.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{name} not found in {path}"
    return float(match.group(1))


def _workflow(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _workflow_triggers(workflow: dict) -> dict:
    # YAML 1.1 reads a bare `on:` key as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    return triggers


def _workflow_steps(workflow: dict) -> list[dict]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


class TestNoPublishedInternalPorts:
    @pytest.mark.parametrize("service_name", ["redis", "postgres", "migrate", "worker", "ui"])
    def test_service_has_no_ports(self, services: dict, service_name: str) -> None:
        assert "ports" not in services[service_name], (
            f"{service_name} must not publish ports to the host — only traefik does"
        )

    def test_only_traefik_publishes_ports(self, services: dict) -> None:
        publishers = sorted(name for name, definition in services.items() if "ports" in definition)
        assert publishers == ["traefik"]

    def test_traefik_binds_only_80_and_443(self, services: dict) -> None:
        assert sorted(services["traefik"]["ports"]) == ["443:443", "80:80"]

    def test_no_service_uses_host_networking_or_expose_to_host(self, services: dict) -> None:
        for name, definition in services.items():
            assert definition.get("network_mode") != "host", f"{name} uses host networking"


class TestRoutingPriority:
    def test_api_router_outranks_ui_router(self, services: dict) -> None:
        worker_priority = _router_priority(services["worker"]["labels"], "worker")
        ui_priority = _router_priority(services["ui"]["labels"], "ui")
        assert worker_priority > ui_priority, (
            f"worker router priority ({worker_priority}) must exceed "
            f"ui router priority ({ui_priority}) so PathPrefix(/api) always wins"
        )

    def test_worker_rule_matches_api_path_prefix(self, services: dict) -> None:
        rule = _label_value(services["worker"]["labels"], "traefik.http.routers.worker.rule")
        assert rule is not None
        assert "PathPrefix(`/api`)" in rule

    def test_ui_rule_is_host_only_catch_all(self, services: dict) -> None:
        rule = _label_value(services["ui"]["labels"], "traefik.http.routers.ui.rule")
        assert rule is not None
        assert "PathPrefix" not in rule

    def test_each_service_defines_exactly_one_router(self, services: dict) -> None:
        # Compose healthchecks hit the container directly, so there is no
        # reason to publish a second, unauthenticated route (e.g. for
        # /api/healthz) through the edge. A new router has to be a deliberate edit.
        for name in ("worker", "ui"):
            routers = [
                label.split("=", 1)[0].split(".")[3]
                for label in services[name]["labels"]
                if label.startswith("traefik.http.routers.")
            ]
            assert set(routers) == {name}, f"{name} defines extra routers: {set(routers)}"


class TestEdgeAuthentication:
    def test_both_routers_sit_behind_basic_auth(self, services: dict) -> None:
        for service_name, router in (("worker", "worker"), ("ui", "ui")):
            labels = services[service_name]["labels"]
            names = [_middleware_name(m) for m in _router_middlewares(labels, router)]
            with_basic_auth = [
                n
                for n in names
                if _label_value(labels, f"traefik.http.middlewares.{n}.basicauth.usersfile")
            ]
            assert with_basic_auth, f"{router} router has no BasicAuth middleware"

    def test_every_referenced_middleware_is_defined_on_the_same_service(
        self, services: dict
    ) -> None:
        # A middleware definition lives only as long as its container. A router
        # that referenced one defined elsewhere would 404 whenever that other
        # container was down — taking the dashboard's stale banner with it.
        for service_name, router in (("worker", "worker"), ("ui", "ui")):
            labels = services[service_name]["labels"]
            for reference in _router_middlewares(labels, router):
                name = _middleware_name(reference)
                defined = any(
                    label.startswith(f"traefik.http.middlewares.{name}.") for label in labels
                )
                assert defined, f"{router} references {name!r}, not defined in {service_name}"

    def test_basic_auth_reads_the_mounted_users_file(self, services: dict) -> None:
        mount = next(
            v
            for v in services["traefik"]["volumes"]
            if isinstance(v, dict) and v["target"].endswith("users.htpasswd")
        )
        assert mount["type"] == "bind"
        assert mount["read_only"] is True
        assert mount["source"] == "./config/traefik/users.htpasswd"
        # The file is a prerequisite the operator creates: a missing path must
        # stop `up`, not be silently created as an empty directory.
        assert mount["bind"]["create_host_path"] is False
        for service_name, router in (("worker", "worker"), ("ui", "ui")):
            labels = services[service_name]["labels"]
            for reference in _router_middlewares(labels, router):
                name = _middleware_name(reference)
                usersfile = _label_value(
                    labels, f"traefik.http.middlewares.{name}.basicauth.usersfile"
                )
                if usersfile is not None:
                    assert usersfile == mount["target"]

    def test_bearer_token_is_injected_after_basic_auth_on_the_worker_router(
        self, services: dict
    ) -> None:
        labels = services["worker"]["labels"]
        chain = [_middleware_name(m) for m in _router_middlewares(labels, "worker")]

        def has(name: str, suffix: str) -> bool:
            return _label_value(labels, f"traefik.http.middlewares.{name}.{suffix}") is not None

        auth_at = next(i for i, n in enumerate(chain) if has(n, "basicauth.usersfile"))
        bearer_at = next(
            i for i, n in enumerate(chain) if has(n, "headers.customrequestheaders.Authorization")
        )
        assert auth_at < bearer_at, (
            "the login check must run before the browser's Authorization header is replaced"
        )

    def test_bearer_header_comes_from_the_environment_never_a_literal(self, services: dict) -> None:
        labels = services["worker"]["labels"]
        values = [
            label.split("=", 1)[1]
            for label in labels
            if "customrequestheaders.Authorization=" in label
        ]
        assert values == ["Bearer ${TRAFFIC_AI_API_TOKEN:-}"]

    def test_ui_router_does_not_inject_the_token(self, services: dict) -> None:
        assert not any("customrequestheaders" in label for label in services["ui"]["labels"])

    def test_basic_auth_does_not_forward_the_browsers_credentials(self, services: dict) -> None:
        for service_name in ("worker", "ui"):
            labels = services[service_name]["labels"]
            removing = [label for label in labels if label.endswith(".basicauth.removeheader=true")]
            assert removing, f"{service_name}: BasicAuth would forward the Basic header upstream"


class TestUiSecurityHeaders:
    @pytest.fixture
    def headers(self, services: dict) -> dict[str, str]:
        prefix = "traefik.http.middlewares.ui-security-headers.headers."
        return {
            label[len(prefix) :].split("=", 1)[0]: label.split("=", 1)[1]
            for label in services["ui"]["labels"]
            if label.startswith(prefix)
        }

    def test_headers_middleware_is_on_the_ui_router(self, services: dict) -> None:
        chain = _router_middlewares(services["ui"]["labels"], "ui")
        assert "ui-security-headers@docker" in chain

    def test_hsts_is_a_year_with_subdomains(self, headers: dict[str, str]) -> None:
        assert int(headers["stsseconds"]) >= 31_536_000
        assert headers["stsincludesubdomains"] == "true"

    def test_nosniff_and_referrer_policy_are_set(self, headers: dict[str, str]) -> None:
        assert headers["contenttypenosniff"] == "true"
        assert headers["referrerpolicy"] == "no-referrer"

    def test_framing_is_restricted_to_same_origin(self, headers: dict[str, str]) -> None:
        # SAMEORIGIN, not DENY: Streamlit components (the Folium map) render in
        # same-origin iframes. Anything but a restriction would be a regression.
        assert headers["customframeoptionsvalue"] in {"DENY", "SAMEORIGIN"}

    def test_no_content_security_policy_is_set(self, headers: dict[str, str]) -> None:
        # Streamlit needs inline scripts; a strict CSP breaks the dashboard.
        assert not [k for k in headers if "contentsecuritypolicy" in k]


class TestPostgresAndMigrations:
    def test_postgres_service_is_pinned_and_unpublished(self, services: dict) -> None:
        assert services["postgres"]["image"] == "postgres:17-alpine"
        assert "ports" not in services["postgres"]

    def test_postgres_is_health_checked_with_pg_isready(self, services: dict) -> None:
        test = " ".join(services["postgres"]["healthcheck"]["test"])
        assert "pg_isready" in test

    def test_postgres_data_lives_on_a_declared_named_volume(
        self, services: dict, compose: dict
    ) -> None:
        mounts = [v for v in services["postgres"]["volumes"] if "/var/lib/postgresql/data" in v]
        assert len(mounts) == 1
        volume_name = mounts[0].split(":", 1)[0]
        assert not volume_name.startswith((".", "/")), "postgres data must be a named volume"
        assert volume_name in compose["volumes"]

    def test_dev_password_is_only_a_default_for_the_postgres_role(self, services: dict) -> None:
        dev_default = "${POSTGRES_PASSWORD:-traffic}"  # the gate refuses this at startup
        assert services["postgres"]["environment"]["POSTGRES_PASSWORD"] == dev_default

    def test_migrate_is_a_one_shot_that_waits_for_postgres(self, services: dict) -> None:
        migrate = services["migrate"]
        assert migrate["restart"] == "no"
        assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"

    def test_migrate_runs_alembic_upgrade_head_in_the_worker_image(self, services: dict) -> None:
        migrate, worker = services["migrate"], services["worker"]
        # The image's ENTRYPOINT is the API server, so the entrypoint is replaced
        # rather than `command:` being appended to it.
        assert migrate["entrypoint"] == ["alembic"]
        assert migrate["command"] == ["upgrade", "head"]
        assert migrate["image"] == worker["image"]
        assert migrate["build"] == worker["build"]
        assert migrate["build"]["dockerfile"] == "docker/Dockerfile.worker"

    def test_worker_waits_for_migrate_to_complete_successfully(self, services: dict) -> None:
        depends_on = services["worker"]["depends_on"]
        assert depends_on["migrate"]["condition"] == "service_completed_successfully"
        assert depends_on["postgres"]["condition"] == "service_healthy"
        assert depends_on["redis"]["condition"] == "service_healthy"

    def test_only_the_worker_stack_depends_on_the_database(self, services: dict) -> None:
        # The UI is a thin viewer over HTTP; it never connects to the database.
        assert "postgres" not in services["ui"]["depends_on"]
        assert "migrate" not in services["ui"]["depends_on"]

    def test_worker_and_migrate_build_the_same_database_url(self, services: dict) -> None:
        worker_url = services["worker"]["environment"]["TRAFFIC_AI_DATABASE_URL"]
        migrate_url = services["migrate"]["environment"]["TRAFFIC_AI_DATABASE_URL"]
        assert worker_url == migrate_url
        assert worker_url == (
            "postgresql+asyncpg://${POSTGRES_USER:-traffic}:${POSTGRES_PASSWORD:-traffic}"
            "@postgres:5432/${POSTGRES_DB:-traffic_ai}"
        )

    def test_ui_is_given_no_database_url(self, services: dict) -> None:
        assert "TRAFFIC_AI_DATABASE_URL" not in services["ui"]["environment"]


class TestProductionGateAgainstTheComposeEnvironment:
    """The compose comments make claims about `Settings`; hold them to the code."""

    def test_unset_postgres_password_cannot_ship(
        self, services: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = {"TRAFFIC_AI_API_TOKEN": TEST_TOKEN}  # POSTGRES_PASSWORD left unset
        resolved = _resolved_environment(services["worker"], env)
        with pytest.raises(ValueError, match="development password"):
            _build_production_settings(monkeypatch, resolved)

    def test_a_real_postgres_password_passes(
        self, services: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = {"TRAFFIC_AI_API_TOKEN": TEST_TOKEN, "POSTGRES_PASSWORD": TEST_DB_PASSWORD}
        resolved = _resolved_environment(services["worker"], env)
        settings = _build_production_settings(monkeypatch, resolved)
        assert settings.is_production
        assert settings.persistence_enabled
        assert TEST_DB_PASSWORD in settings.database_url

    def test_migrate_applies_the_same_gate(
        self, services: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = {"TRAFFIC_AI_API_TOKEN": TEST_TOKEN}
        resolved = _resolved_environment(services["migrate"], env)
        with pytest.raises(ValueError, match="development password"):
            _build_production_settings(monkeypatch, resolved)

    @pytest.mark.parametrize("service_name", ["worker", "migrate", "ui"])
    def test_unset_token_refuses_to_start(
        self, services: dict, monkeypatch: pytest.MonkeyPatch, service_name: str
    ) -> None:
        # compose passes `${TRAFFIC_AI_API_TOKEN:-}`: blank, not absent.
        resolved = _resolved_environment(services[service_name], {"POSTGRES_PASSWORD": "x" * 24})
        assert resolved["TRAFFIC_AI_API_TOKEN"] == ""
        with pytest.raises(ValueError, match="TRAFFIC_AI_API_TOKEN is unset"):
            _build_production_settings(monkeypatch, resolved)

    def test_token_is_never_defaulted_to_a_value(self, services: dict) -> None:
        blank_when_unset = "${TRAFFIC_AI_API_TOKEN:-}"
        for name in ("worker", "migrate", "ui"):
            assert services[name]["environment"]["TRAFFIC_AI_API_TOKEN"] == blank_when_unset

    def test_ui_starts_with_only_the_token(
        self, services: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The UI has no database URL, so it must switch persistence off or the
        # production gate would reject the development-password default.
        resolved = _resolved_environment(services["ui"], {"TRAFFIC_AI_API_TOKEN": TEST_TOKEN})
        settings = _build_production_settings(monkeypatch, resolved)
        assert settings.is_production
        assert not settings.persistence_enabled
        assert settings.auth_enabled
        assert settings.api_internal_url == "http://worker:8000"
        assert settings.api_public_url == "/api"

    def test_worker_persistence_can_be_switched_off_from_the_environment(
        self, services: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = {"TRAFFIC_AI_API_TOKEN": TEST_TOKEN, "TRAFFIC_AI_PERSISTENCE_ENABLED": "false"}
        resolved = _resolved_environment(services["worker"], env)
        assert not _build_production_settings(monkeypatch, resolved).persistence_enabled


class TestResourceLimits:
    @pytest.mark.parametrize(
        "service_name", ["traefik", "redis", "postgres", "migrate", "worker", "ui"]
    )
    def test_service_has_limits_and_reservations(self, services: dict, service_name: str) -> None:
        resources = services[service_name]["deploy"]["resources"]
        for section in ("limits", "reservations"):
            for resource in ("cpus", "memory"):
                assert resources[section][resource], f"{service_name} lacks {section}.{resource}"

    def test_every_service_is_covered(self, services: dict) -> None:
        # A service added later must come with limits, not be forgotten here.
        for name, definition in services.items():
            assert definition.get("deploy", {}).get("resources", {}).get("limits"), name

    def test_reservations_never_exceed_limits(self, services: dict) -> None:
        for name, definition in services.items():
            resources = definition["deploy"]["resources"]
            assert float(resources["reservations"]["cpus"]) <= float(resources["limits"]["cpus"])
            assert _memory_bytes(resources["reservations"]["memory"]) <= _memory_bytes(
                resources["limits"]["memory"]
            ), name

    def test_worker_is_sized_for_cpu_inference(self, services: dict) -> None:
        limits = services["worker"]["deploy"]["resources"]["limits"]
        assert float(limits["cpus"]) >= 2
        assert _memory_bytes(limits["memory"]) >= 3 * 1024**3

    def test_redis_limit_leaves_headroom_over_its_dataset_cap(self, services: dict) -> None:
        # --maxmemory bounds the dataset only; the container needs room above it.
        assert "--maxmemory 256mb" in services["redis"]["command"]
        assert _memory_bytes(services["redis"]["deploy"]["resources"]["limits"]["memory"]) > (
            256 * 1024**2
        )


class TestImageTags:
    def test_no_service_pins_latest(self, services: dict) -> None:
        for name, definition in services.items():
            image = definition.get("image")
            if image is None:
                continue
            assert not image.endswith(":latest"), f"{name} pins image {image!r} to :latest"
            assert ":" in image, f"{name} image {image!r} has no tag at all"

    @pytest.mark.parametrize("dockerfile", ["docker/Dockerfile.ui", "docker/Dockerfile.worker"])
    def test_dockerfile_base_image_not_latest(self, dockerfile: str) -> None:
        text = (REPO_ROOT / dockerfile).read_text(encoding="utf-8")
        from_lines = [line for line in text.splitlines() if line.strip().upper().startswith("FROM")]
        assert from_lines, f"{dockerfile} has no FROM line"
        for line in from_lines:
            assert ":latest" not in line, f"{dockerfile} pins {line!r} to :latest"


class TestTraefikSocketMount:
    def test_docker_socket_mounted_read_only(self, services: dict) -> None:
        volumes = services["traefik"]["volumes"]
        socket_mounts = [v for v in volumes if isinstance(v, str) and "docker.sock" in v]
        assert socket_mounts, "traefik has no docker.sock mount"
        for mount in socket_mounts:
            assert mount.endswith(":ro"), f"docker.sock mount {mount!r} is not read-only"


class TestRedisEphemeralByDesign:
    def test_redis_persistence_disabled(self, services: dict) -> None:
        command = services["redis"]["command"]
        assert '--save ""' in command
        assert "--appendonly no" in command


class TestWorkerVideoMountReadOnly:
    def test_video_volume_is_read_only(self, services: dict) -> None:
        volumes = services["worker"]["volumes"]
        video_mounts = [v for v in volumes if "data/videos" in v]
        assert video_mounts, "worker has no data/videos mount"
        for mount in video_mounts:
            assert mount.endswith(":ro"), f"data/videos mount {mount!r} is not read-only"


class TestSecretsStayOutOfTheRepository:
    def test_gitignore_excludes_the_real_users_file_and_env_variants(self) -> None:
        ordered = [line.strip() for line in GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()]
        assert "config/traefik/users.htpasswd" in ordered
        assert ".env" in ordered
        assert ".env.*" in ordered
        assert "!.env.example" in ordered
        # The negation must come AFTER the pattern it carves out, or it does nothing.
        assert ordered.index(".env.*") < ordered.index("!.env.example")

    def test_htpasswd_example_is_comment_only(self) -> None:
        # A real-looking `user:hash` line, even a sample, is a credential in
        # waiting. The template explains how to create the file and nothing more.
        assert HTPASSWD_EXAMPLE.is_file()
        body = [
            line
            for line in HTPASSWD_EXAMPLE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert body == []

    def test_compose_contains_no_literal_credentials(self) -> None:
        text = COMPOSE_PATH.read_text(encoding="utf-8")
        assert "$2y$" not in text, "a bcrypt hash must live in the htpasswd file, not compose.yaml"
        # Nothing shaped like the `openssl rand -hex 32` token (or longer).
        assert re.search(r"\b[0-9a-fA-F]{32,}\b", text) is None


class TestDemoVideoFilenamesMatchFetchScript:
    def test_fetch_script_targets_expected_filenames(self) -> None:
        script_path = REPO_ROOT / "scripts" / "fetch_demo_videos.py"
        text = script_path.read_text(encoding="utf-8")
        filenames = set(re.findall(r'filename="([^"]+\.mp4)"', text))
        assert filenames == DEMO_VIDEO_FILENAMES


class TestStopGracePeriods:
    """Docker's 10s default would SIGKILL the worker mid-way through its history flush."""

    def test_worker_grace_covers_the_applications_own_shutdown_budget(self, services: dict) -> None:
        # uvicorn drains requests (GRACEFUL_SHUTDOWN_SECONDS), then the lifespan waits
        # up to _SHUTDOWN_TIMEOUT_SECONDS for the pipelines and again for the history
        # writer. Held to the constants in the code, not to a copy of their values.
        needed = _source_number(APP_MAIN_MODULE, "GRACEFUL_SHUTDOWN_SECONDS") + 2 * _source_number(
            APP_MODULE, "_SHUTDOWN_TIMEOUT_SECONDS"
        )
        granted = _seconds(services["worker"]["stop_grace_period"])
        assert granted >= needed, f"stop_grace_period {granted}s is below the {needed}s budget"

    def test_worker_and_ui_grace_periods_are_the_documented_values(self, services: dict) -> None:
        assert services["worker"]["stop_grace_period"] == "30s"
        assert services["ui"]["stop_grace_period"] == "15s"

    def test_ui_grace_is_not_below_dockers_default(self, services: dict) -> None:
        assert _seconds(services["ui"]["stop_grace_period"]) >= 10


class TestEdgeRateLimit:
    """BasicAuth has no lockout, so a Traefik rate limit must run in FRONT of it."""

    ROUTERS = (("worker", "worker"), ("ui", "ui"))

    @staticmethod
    def _chain(services: dict, service_name: str, router: str) -> list[str]:
        labels = services[service_name]["labels"]
        return [_middleware_name(m) for m in _router_middlewares(labels, router)]

    def _limiter(self, services: dict, service_name: str, router: str) -> str:
        labels = services[service_name]["labels"]
        limiters = [
            name
            for name in self._chain(services, service_name, router)
            if _label_value(labels, f"traefik.http.middlewares.{name}.ratelimit.average")
        ]
        assert len(limiters) == 1, f"{router} router needs exactly one rate limit: {limiters}"
        return limiters[0]

    def test_both_routers_rate_limit_before_basic_auth(self, services: dict) -> None:
        for service_name, router in self.ROUTERS:
            labels = services[service_name]["labels"]
            chain = self._chain(services, service_name, router)
            limiter = self._limiter(services, service_name, router)
            login = next(
                n
                for n in chain
                if _label_value(labels, f"traefik.http.middlewares.{n}.basicauth.usersfile")
            )
            assert chain.index(limiter) < chain.index(login), (
                f"{router}: a limiter after BasicAuth never sees the guesses it exists to slow"
            )

    def test_worker_chain_is_limit_then_login_then_bearer(self, services: dict) -> None:
        assert self._chain(services, "worker", "worker") == [
            "worker-ratelimit",
            "worker-basicauth",
            "api-bearer",
        ]

    def test_ui_chain_keeps_headers_first_so_a_429_carries_them(self, services: dict) -> None:
        assert self._chain(services, "ui", "ui") == [
            "ui-security-headers",
            "ui-ratelimit",
            "ui-basicauth",
        ]

    def test_each_router_has_its_own_limiter(self, services: dict) -> None:
        # Shared with another container's labels, a limiter would vanish (404) with it.
        worker = self._limiter(services, "worker", "worker")
        ui = self._limiter(services, "ui", "ui")
        assert worker != ui

    def test_limits_are_set_and_the_burst_covers_the_average(self, services: dict) -> None:
        for service_name, router in self.ROUTERS:
            labels = services[service_name]["labels"]
            name = self._limiter(services, service_name, router)
            average = int(
                _label_value(labels, f"traefik.http.middlewares.{name}.ratelimit.average")
            )
            burst = int(_label_value(labels, f"traefik.http.middlewares.{name}.ratelimit.burst"))
            assert average >= 1
            assert burst >= average

    def test_bucket_key_is_the_peer_address_with_traefik_as_the_edge(self, services: dict) -> None:
        # depth 0 ignores X-Forwarded-For: right while Traefik is the edge, wrong
        # behind a load balancer (docs/DEPLOYMENT.md). The label must be explicit so
        # the choice is visible rather than a default nobody looked at.
        for service_name, router in self.ROUTERS:
            labels = services[service_name]["labels"]
            name = self._limiter(services, service_name, router)
            key = f"traefik.http.middlewares.{name}.ratelimit.sourcecriterion.ipstrategy.depth"
            assert _label_value(labels, key) == "0"


class TestWorkerRateLimitAndProxyEnvironment:
    """The worker's own limiter settings, and the proxy count it trusts."""

    NAMES = (
        "TRAFFIC_AI_RATE_LIMIT_ENABLED",
        "TRAFFIC_AI_RATE_LIMIT_REQUESTS",
        "TRAFFIC_AI_RATE_LIMIT_STREAM_REQUESTS",
        "TRAFFIC_AI_TRUSTED_PROXY_HOPS",
    )

    def test_worker_passes_each_setting_with_an_overridable_default(self, services: dict) -> None:
        env = services["worker"]["environment"]
        for name in self.NAMES:
            assert re.fullmatch(rf"\$\{{{name}:-[^}}]+\}}", env[name]), (
                f"{name} must be `${{{name}:-default}}`, got {env[name]!r}"
            )

    def test_compose_defaults_equal_the_settings_defaults(
        self, services: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resolved = _resolved_environment(services["worker"], LIMITER_ENV)
        settings = _build_production_settings(monkeypatch, resolved)
        for name in self.NAMES:
            field = name.removeprefix("TRAFFIC_AI_").lower()
            assert getattr(settings, field) == Settings.model_fields[field].default, field

    def test_one_proxy_is_trusted_because_traefik_is_the_only_one(self, services: dict) -> None:
        resolved = _resolved_environment(services["worker"], LIMITER_ENV)
        assert resolved["TRAFFIC_AI_TRUSTED_PROXY_HOPS"] == "1"
        proxies = [n for n, d in services.items() if "traefik" in str(d.get("image", ""))]
        assert proxies == ["traefik"]

    def test_overrides_from_the_environment_reach_settings(
        self, services: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = {
            **LIMITER_ENV,
            "TRAFFIC_AI_RATE_LIMIT_ENABLED": "false",
            "TRAFFIC_AI_RATE_LIMIT_REQUESTS": "30",
            "TRAFFIC_AI_RATE_LIMIT_STREAM_REQUESTS": "3",
            "TRAFFIC_AI_TRUSTED_PROXY_HOPS": "2",
        }
        settings = _build_production_settings(
            monkeypatch, _resolved_environment(services["worker"], env)
        )
        assert settings.rate_limit_enabled is False
        assert settings.rate_limit_requests == 30
        assert settings.rate_limit_stream_requests == 3
        assert settings.trusted_proxy_hops == 2

    @pytest.mark.parametrize("service_name", ["worker", "migrate", "ui"])
    def test_every_traffic_ai_variable_names_a_real_setting(
        self, services: dict, service_name: str
    ) -> None:
        # A typo'd name is silently ignored by Settings (extra="ignore"), which would
        # leave the default in force while the compose file claims otherwise.
        for name in services[service_name]["environment"]:
            if name.startswith("TRAFFIC_AI_"):
                field = name.removeprefix("TRAFFIC_AI_").lower()
                assert field in Settings.model_fields, f"{service_name}: {name} is not a setting"


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
class TestMakefileClean:
    """`make clean` used to run `down -v` and silently delete history and certificates."""

    @staticmethod
    def _make(*args: str) -> subprocess.CompletedProcess[str]:
        make = shutil.which("make")
        assert make is not None
        # COMPOSE=echo on every call: even a recipe that did reach `down -v` would
        # only print it. `make` is an absolute path from shutil.which and every
        # argument is a literal, so S603 does not apply.
        return subprocess.run(  # noqa: S603
            [make, "-C", str(REPO_ROOT), "--no-print-directory", *args, "COMPOSE=echo"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_clean_keeps_the_volumes(self) -> None:
        result = self._make("clean")
        assert result.returncode == 0, result.stderr
        assert "down" in result.stdout
        assert "-v" not in result.stdout.split(), "clean must not remove volumes"
        assert "--volumes" not in result.stdout

    def test_clean_volumes_refuses_without_confirmation(self) -> None:
        result = self._make("clean-volumes")
        assert result.returncode != 0
        assert "down -v" not in result.stdout, "the destructive command ran unconfirmed"
        assert "CONFIRM=yes" in result.stdout

    def test_clean_volumes_says_what_it_will_destroy(self) -> None:
        output = self._make("clean-volumes").stdout
        for volume in ("postgres-data", "letsencrypt"):
            assert volume in output

    @pytest.mark.parametrize("value", ["no", "y", "YES", "true", "1", ""])
    def test_only_the_exact_word_yes_confirms(self, value: str) -> None:
        result = self._make("clean-volumes", f"CONFIRM={value}")
        assert result.returncode != 0
        assert "down -v" not in result.stdout

    def test_clean_volumes_proceeds_with_confirmation(self) -> None:
        result = self._make("clean-volumes", "CONFIRM=yes")
        assert result.returncode == 0, result.stderr
        assert "down -v" in result.stdout

    def test_down_dash_v_appears_only_under_clean_volumes(self) -> None:
        recipe_lines = [
            line
            for line in MAKEFILE.read_text(encoding="utf-8").splitlines()
            if line.startswith("\t") and re.search(r"down\s+(-v|--volumes)\b", line)
        ]
        assert recipe_lines == ["\t$(COMPOSE) down -v"]

    def test_both_targets_are_declared_phony(self) -> None:
        phony = re.search(r"^\.PHONY:(.*)$", MAKEFILE.read_text(encoding="utf-8"), re.MULTILINE)
        assert phony is not None
        assert {"clean", "clean-volumes"} <= set(phony.group(1).split())


class TestWorkflows:
    def test_every_third_party_action_is_pinned_to_a_commit_sha(self) -> None:
        # A tag can be moved to different code; a commit SHA cannot. These jobs hold
        # `packages: write`, so a mutable reference is a supply-chain hole.
        sha_pinned = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
        for path in (CI_WORKFLOW, RELEASE_WORKFLOW):
            for step in _workflow_steps(_workflow(path)):
                uses = step.get("uses")
                if uses is None or uses.startswith("./"):
                    continue
                assert sha_pinned.match(uses), f"{path.name}: {uses!r} is not SHA-pinned"

    def test_every_pinned_action_names_its_release_in_a_comment(self) -> None:
        # The SHA alone is unreadable; the comment is how a human knows what to bump.
        pattern = re.compile(r"uses:\s+\S+@[0-9a-f]{40}\s+#\s+v\d")
        for path in (CI_WORKFLOW, RELEASE_WORKFLOW):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "uses:" in line and "@" in line and not line.strip().startswith("#"):
                    assert pattern.search(line), f"{path.name}: no version comment on {line!r}"

    def test_ci_can_be_called_by_the_release_workflow(self) -> None:
        assert "workflow_call" in _workflow_triggers(_workflow(CI_WORKFLOW))

    def test_images_are_built_only_after_ci_passes(self) -> None:
        # CI does not trigger on tag pushes, so `workflow_run` would never fire for a
        # release; the reusable-workflow call is what puts CI in front of the images.
        jobs = _workflow(RELEASE_WORKFLOW)["jobs"]
        assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
        needs = jobs["images"]["needs"]
        assert "ci" in ([needs] if isinstance(needs, str) else needs)
        assert not any("if" in jobs[name] for name in ("ci", "images")), (
            "a job-level `if` could let the images run when CI did not"
        )

    def test_ci_does_not_run_on_tags_itself(self) -> None:
        # Pins the reason for the design above: if CI ever triggered on tags, the
        # release would run it twice.
        push = _workflow_triggers(_workflow(CI_WORKFLOW))["push"]
        assert "tags" not in push

    def test_the_called_workflow_needs_no_more_than_read_access(self) -> None:
        jobs = _workflow(RELEASE_WORKFLOW)["jobs"]
        assert jobs["ci"]["permissions"] == {"contents": "read"}
        assert _workflow(CI_WORKFLOW)["permissions"] == {"contents": "read"}

    def test_images_job_keeps_its_narrow_permissions(self) -> None:
        permissions = _workflow(RELEASE_WORKFLOW)["jobs"]["images"]["permissions"]
        assert permissions == {
            "contents": "read",
            "packages": "write",
            "security-events": "write",
        }

    def test_latest_is_never_derived_for_a_pre_release_tag(self) -> None:
        steps = _workflow_steps(_workflow(RELEASE_WORKFLOW))
        (meta,) = [s for s in steps if s.get("id") == "meta"]
        # The action's own heuristic is switched off; the rule lives in this file.
        assert "latest=false" in meta["with"]["flavor"]
        latest_lines = [
            line.strip() for line in meta["with"]["tags"].splitlines() if "value=latest" in line
        ]
        assert len(latest_lines) == 1
        (latest,) = latest_lines
        assert "enable=" in latest, "`latest` must be conditional"
        assert "github.ref_type == 'tag'" in latest, "a branch dispatch must not tag latest"
        assert "!contains(github.ref_name, '-')" in latest, "a pre-release (v1.2.3-rc.1) must not"

    def test_the_minor_alias_is_not_moved_by_a_pre_release_either(self) -> None:
        steps = _workflow_steps(_workflow(RELEASE_WORKFLOW))
        (meta,) = [s for s in steps if s.get("id") == "meta"]
        (minor,) = [
            line for line in meta["with"]["tags"].splitlines() if "{{major}}.{{minor}}" in line
        ]
        assert "!contains(github.ref_name, '-')" in minor

    def test_full_version_and_sha_tags_are_still_produced(self) -> None:
        steps = _workflow_steps(_workflow(RELEASE_WORKFLOW))
        (meta,) = [s for s in steps if s.get("id") == "meta"]
        tags = meta["with"]["tags"]
        assert "type=semver,pattern={{version}}" in tags
        assert "type=sha" in tags

    def test_ci_postgres_matches_the_compose_postgres_image(self, services: dict) -> None:
        ci_image = _workflow(CI_WORKFLOW)["jobs"]["test"]["services"]["postgres"]["image"]
        assert ci_image == services["postgres"]["image"] == "postgres:17-alpine"


class TestDocsAgreeWithTheCode:
    def test_htpasswd_creation_commands_use_bcrypt_cost_12(self) -> None:
        # htpasswd's default bcrypt cost is 5; every command that creates a hash must
        # say -C 12, in the template, the runbooks, and the Helm values comments.
        creating = re.compile(r"htpasswd\s+-nb?B\b")
        for path in (HTPASSWD_EXAMPLE, DEPLOYMENT_DOC, KUBERNETES_DOC, *HELM_VALUES):
            for line in path.read_text(encoding="utf-8").splitlines():
                if creating.search(line):
                    assert "-C 12" in line, f"{path.name}: htpasswd without -C 12: {line.strip()!r}"

    def test_runbooks_do_not_pin_to_a_base_commit_plus_uncommitted_changes(self) -> None:
        for path in (DEPLOYMENT_DOC, KUBERNETES_DOC):
            text = path.read_text(encoding="utf-8")
            assert "uncommitted" not in text, f"{path.name} still has the stale pin"
            assert "base commit" not in text, f"{path.name} still has the stale pin"

    def test_documented_history_signals_exist_in_the_code(self) -> None:
        assert "history_events_lost_total" in render()[0].decode()
        assert "history_events_lost" in ReadinessResponse.model_fields
        for path in (DEPLOYMENT_DOC, KUBERNETES_DOC):
            text = path.read_text(encoding="utf-8")
            assert "history_events_lost_total" in text, path.name
            assert "history_events_lost" in text, path.name

    def test_documented_proxy_hops_setting_exists_and_is_documented(self) -> None:
        assert "trusted_proxy_hops" in Settings.model_fields
        for path in (DEPLOYMENT_DOC, KUBERNETES_DOC):
            assert "TRAFFIC_AI_TRUSTED_PROXY_HOPS" in path.read_text(encoding="utf-8"), path.name

    @pytest.mark.parametrize(
        ("path", "gap"),
        [
            (DEPLOYMENT_DOC, "docker-socket-proxy"),
            (DEPLOYMENT_DOC, "event loop"),
            (KUBERNETES_DOC, "last-applied-configuration"),
            (KUBERNETES_DOC, "event loop"),
        ],
        ids=lambda v: v.name if isinstance(v, Path) else v,
    )
    def test_known_gaps_are_named_in_the_runbooks(self, path: Path, gap: str) -> None:
        text = path.read_text(encoding="utf-8")
        assert "Known gaps" in text, f"{path.name} has no Known gaps section"
        assert gap in text, f"{path.name} does not name the gap: {gap}"
