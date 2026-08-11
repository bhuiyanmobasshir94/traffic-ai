"""Structural checks on the deployment config.

These do not build or start anything — no Docker daemon required. They parse
compose.yaml and the two Dockerfiles as text/YAML and assert the invariants
docs/DEPLOYMENT.md and CLAUDE.md's non-negotiables depend on: redis and the
worker are never published to the host, the /api router always wins over the
UI catch-all, no image floats on `:latest`, and the Traefik docker-socket
mount is read-only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "compose.yaml"
DEMO_VIDEO_FILENAMES = {"toll-plaza-a.mp4", "toll-plaza-b.mp4"}


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


class TestNoPublishedInternalPorts:
    @pytest.mark.parametrize("service_name", ["redis", "worker", "ui"])
    def test_service_has_no_ports(self, services: dict, service_name: str) -> None:
        assert "ports" not in services[service_name], (
            f"{service_name} must not publish ports to the host — only traefik does"
        )


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
        socket_mounts = [v for v in volumes if "docker.sock" in v]
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


class TestDemoVideoFilenamesMatchFetchScript:
    def test_fetch_script_targets_expected_filenames(self) -> None:
        script_path = REPO_ROOT / "scripts" / "fetch_demo_videos.py"
        text = script_path.read_text(encoding="utf-8")
        filenames = set(re.findall(r'filename="([^"]+\.mp4)"', text))
        assert filenames == DEMO_VIDEO_FILENAMES
