from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"


def test_optional_docker_services_bind_to_loopback_only():
    config = yaml.safe_load(COMPOSE.read_text())
    services = config["services"]

    for service_name in ("redis", "ollama"):
        for port in services[service_name]["ports"]:
            assert port.startswith("127.0.0.1:"), f"{service_name} exposes {port}"


def test_optional_docker_services_use_named_volumes():
    config = yaml.safe_load(COMPOSE.read_text())

    assert set(config["volumes"]) == {"redis_data", "ollama_data"}
    assert config["services"]["redis"]["volumes"] == ["redis_data:/data"]
    assert config["services"]["ollama"]["volumes"] == ["ollama_data:/root/.ollama"]


def test_optional_docker_services_use_project_scoped_container_names():
    config = yaml.safe_load(COMPOSE.read_text())

    for service_name in ("redis", "ollama"):
        assert "container_name" not in config["services"][service_name]
