#!/usr/bin/env python3
"""Phoenix runtime 预检：版本固定、SDK、服务可达性与 Docker fallback。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import urllib.request
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PINNED_SERVER_VERSION = "20.4.0"
PINNED_IMAGE = f"arizephoenix/phoenix:version-{PINNED_SERVER_VERSION}"


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _http_reachable(base_url: str, *, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/", timeout=timeout) as response:
            return 200 <= response.status < 500
    except Exception:
        return False


def _docker_ready() -> tuple[bool, str]:
    executable = shutil.which("docker")
    if not executable:
        return False, "docker-command-missing"
    try:
        result = subprocess.run([executable, "info", "--format", "{{.ServerVersion}}"],
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, "docker-daemon-unreachable"
    return ((True, result.stdout.strip()) if result.returncode == 0
            else (False, "docker-daemon-unreachable"))


def phoenix_preflight(*, compose_path: str | Path = "docker-compose.phoenix.yml",
                      base_url: str = "http://localhost:6006",
                      http_probe=_http_reachable, docker_probe=_docker_ready,
                      package_probe=_package_version) -> dict:
    path = Path(compose_path)
    compose = path.read_text(encoding="utf-8") if path.exists() else ""
    match = re.search(r"^\s*image:\s*(\S+)\s*$", compose, flags=re.MULTILINE)
    image = match.group(1) if match else None
    image_pinned = image == PINNED_IMAGE and ":latest" not in (image or "")
    client = package_probe("arize-phoenix-client")
    otel = package_probe("arize-phoenix-otel")
    service_reachable = http_probe(base_url)
    docker_ready, docker_detail = docker_probe()
    failures = []
    if not image_pinned:
        failures.append("phoenix-image-not-pinned-to-policy")
    if client is None:
        failures.append("phoenix-client-missing")
    if otel is None:
        failures.append("phoenix-otel-missing")
    if not service_reachable and not docker_ready:
        failures.append("no-reachable-server-or-docker-runtime")
    return {"ready": not failures, "failures": failures,
            "server": {"baseUrl": base_url, "reachable": service_reachable,
                       "policyVersion": PINNED_SERVER_VERSION},
            "compose": {"path": str(path), "image": image, "pinned": image_pinned},
            "sdk": {"arizePhoenixClient": client, "arizePhoenixOtel": otel},
            "docker": {"ready": docker_ready, "detail": docker_detail},
            "interpretation": "runtime readiness only; not an end-to-end write/read proof"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose", default="docker-compose.phoenix.yml")
    parser.add_argument("--base-url", default="http://localhost:6006")
    args = parser.parse_args(argv)
    report = phoenix_preflight(compose_path=args.compose, base_url=args.base_url)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
