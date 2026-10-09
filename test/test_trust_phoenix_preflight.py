from trust.phoenix_preflight import PINNED_IMAGE, phoenix_preflight


def _packages(name):
    return {"arize-phoenix-client": "3.3.0", "arize-phoenix-otel": "0.17.1"}.get(name)


def test_preflight_accepts_pinned_compose_and_reachable_server(tmp_path):
    compose = tmp_path / "compose.yml"
    compose.write_text(f"services:\n  phoenix:\n    image: {PINNED_IMAGE}\n")
    report = phoenix_preflight(compose_path=compose, http_probe=lambda _: True,
                               docker_probe=lambda: (False, "missing"),
                               package_probe=_packages)
    assert report["ready"] is True
    assert report["compose"]["pinned"] is True


def test_preflight_rejects_latest_and_missing_runtime(tmp_path):
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  phoenix:\n    image: arizephoenix/phoenix:latest\n")
    report = phoenix_preflight(compose_path=compose, http_probe=lambda _: False,
                               docker_probe=lambda: (False, "docker-command-missing"),
                               package_probe=_packages)
    assert report["ready"] is False
    assert report["failures"] == ["phoenix-image-not-pinned-to-policy",
                                  "no-reachable-server-or-docker-runtime"]


def test_docker_is_an_acceptable_startup_fallback_but_not_write_read_proof(tmp_path):
    compose = tmp_path / "compose.yml"
    compose.write_text(f"services:\n  phoenix:\n    image: {PINNED_IMAGE}\n")
    report = phoenix_preflight(compose_path=compose, http_probe=lambda _: False,
                               docker_probe=lambda: (True, "27.0.0"),
                               package_probe=_packages)
    assert report["ready"] is True
    assert report["server"]["reachable"] is False
    assert report["interpretation"].endswith("not an end-to-end write/read proof")
