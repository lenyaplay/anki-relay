#!/usr/bin/env python3
"""Run the complete validation suite. Run ALL of it at the end of every change.

    python scripts/validate.py

Steps: ruff lint, ruff format check, the whole pytest suite (including the
integration test against a real local anki.syncserver), repository hygiene
(no secrets / user data tracked), docker compose config for both deployment
modes and port publishing, docker build, and container smoke tests (fails clearly
without required settings, runs as non-root, serves /health, stops gracefully).

Docker steps are reported as SKIPPED (not passed) when Docker is unavailable.
Exit code is non-zero if any step fails.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "anki-relay:validate"
PY = sys.executable


class Skip(Exception):
    pass


class Fail(Exception):
    pass


def run(cmd: list[str], *, check: bool = True, env: dict | None = None, timeout: int = 1800):
    print(f"$ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        tail = (proc.stdout + proc.stderr)[-4000:]
        raise Fail(f"exit {proc.returncode}\n{tail}")
    return proc


def stream(cmd: list[str], timeout: int = 1800) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=ROOT, timeout=timeout)
    if proc.returncode != 0:
        raise Fail(f"exit {proc.returncode}")


# ---------------------------------------------------------------- steps


def ruff_lint() -> str:
    stream([PY, "-m", "ruff", "check", "src", "tests", "scripts"])
    return "clean"


def ruff_format() -> str:
    stream([PY, "-m", "ruff", "format", "--check", "src", "tests", "scripts"])
    return "formatted"


def pytest_all() -> str:
    proc = subprocess.run(
        [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    print(proc.stdout[-6000:], proc.stderr[-2000:], sep="\n")
    summary = [line for line in proc.stdout.splitlines() if " passed" in line or " failed" in line]
    if proc.returncode != 0:
        raise Fail(summary[-1] if summary else f"pytest exit {proc.returncode}")
    return summary[-1].strip("= ") if summary else "ok"


FORBIDDEN_PATHS = [
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)data/"),
    re.compile(r"(^|/)logs/"),
    re.compile(r"\.anki2(-wal|-shm)?$"),
    re.compile(r"(^|/)oauth\.json$"),
    re.compile(r"(^|/)sync_auth\.json$"),
    re.compile(r"(^|/)state\.json$"),
]
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r'"hkey"\s*:\s*"[A-Za-z0-9]{16,}"'),
]
REQUIRED_IGNORES = ["data/", "logs/", ".env", "__pycache__", ".venv", "*.anki2"]


def repo_hygiene() -> str:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        raise Skip("not a git checkout")
    tracked = run(["git", "ls-files"]).stdout.splitlines()
    untracked = run(["git", "ls-files", "--others", "--exclude-standard"]).stdout.splitlines()
    candidates = sorted(set(tracked) | set(untracked))
    bad = [p for p in candidates if any(r.search(p) for r in FORBIDDEN_PATHS)]
    if bad:
        raise Fail("user data or secrets would be committed: " + ", ".join(bad))
    leaks = []
    for rel in candidates:
        path = ROOT / rel
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        leaks += [f"{rel}: {r.pattern}" for r in SECRET_PATTERNS if r.search(text)]
    if leaks:
        raise Fail("possible secrets: " + "; ".join(leaks))
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    entries = {line.strip().rstrip("/") for line in ignore}
    missing = [p for p in REQUIRED_IGNORES if p.rstrip("/") not in entries]
    if missing:
        raise Fail(".gitignore lacks: " + ", ".join(missing))
    probe = run(
        ["git", "check-ignore", ".env", "data/x", "logs/x", "x.anki2"], check=False
    ).stdout.split()
    if len(probe) != 4:
        raise Fail("git does not ignore .env / data/ / logs/ / *.anki2")
    return f"{len(candidates)} files checked"


def docker_available() -> None:
    if shutil.which("docker") is None:
        raise Skip("docker not installed")
    if run(["docker", "info"], check=False).returncode != 0:
        raise Skip("docker daemon not running")


def compose_config(env: dict[str, str]) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
        for key, value in env.items():
            fh.write(f"{key}={value}\n")
        env_file = fh.name
    try:
        proc = run(
            [
                "docker",
                "compose",
                "--env-file",
                env_file,
                "-f",
                "docker-compose.yml",
                "config",
                "--format",
                "json",
            ],
            env={"COMPOSE_PROFILES": env.get("COMPOSE_PROFILES", "")},
        )
        return json.loads(proc.stdout)
    finally:
        os.unlink(env_file)


def published(config: dict, service: str) -> list[tuple[str, str, str]]:
    ports = config["services"][service].get("ports", [])
    return [
        (p.get("host_ip", ""), str(p.get("published")), f"{p.get('target')}/{p.get('protocol')}")
        for p in ports
    ]


def compose_modes() -> str:
    docker_available()
    base = {"PUBLIC_URL": "https://anki-relay.example.com", "ALLOWED_EMAILS": "me@example.com"}

    caddy = compose_config({**base, "COMPOSE_PROFILES": "caddy"})
    if set(caddy["services"]) != {"anki-relay", "caddy"}:
        raise Fail(f"caddy mode services: {sorted(caddy['services'])}")
    if published(caddy, "anki-relay") != [("127.0.0.1", "8000", "8000/tcp")]:
        raise Fail(f"default app port: {published(caddy, 'anki-relay')}")
    caddy_ports = sorted(published(caddy, "caddy"))
    if caddy_ports != sorted(
        [("", "80", "80/tcp"), ("", "443", "443/tcp"), ("", "443", "443/udp")]
    ):
        raise Fail(f"caddy ports: {caddy_ports}")
    if caddy["services"]["caddy"]["environment"]["PUBLIC_URL"] != base["PUBLIC_URL"]:
        raise Fail("PUBLIC_URL not passed to Caddy")

    plain = compose_config({**base, "COMPOSE_PROFILES": ""})
    if set(plain["services"]) != {"anki-relay"}:
        raise Fail(f"nginx mode services: {sorted(plain['services'])}")

    moved = compose_config(
        {
            **base,
            "COMPOSE_PROFILES": "caddy",
            "APP_PORT": "9123",
            "BIND_ADDRESS": "0.0.0.0",  # noqa: S104 - checking the published binding
            "HTTP_PORT": "8080",
            "HTTPS_PORT": "8443",
            "PORT": "8111",
        }
    )
    if published(moved, "anki-relay") != [("0.0.0.0", "9123", "8111/tcp")]:  # noqa: S104
        raise Fail(f"APP_PORT/BIND_ADDRESS/PORT not applied: {published(moved, 'anki-relay')}")
    if sorted(published(moved, "caddy")) != sorted(
        [("", "8080", "80/tcp"), ("", "8443", "443/tcp"), ("", "8443", "443/udp")]
    ):
        raise Fail(f"HTTP_PORT/HTTPS_PORT not applied: {published(moved, 'caddy')}")
    if moved["services"]["caddy"]["environment"]["UPSTREAM"] != "anki-relay:8111":
        raise Fail("Caddy upstream does not follow PORT")
    return "caddy + nginx modes, port overrides OK"


def docker_build() -> str:
    docker_available()
    stream(["docker", "build", "-t", IMAGE, "."], timeout=3600)
    return IMAGE


def container_without_settings() -> str:
    docker_available()
    proc = run(["docker", "run", "--rm", IMAGE], check=False, timeout=120)
    output = proc.stdout + proc.stderr
    if proc.returncode == 0 or "PUBLIC_URL" not in output or "ALLOWED_EMAILS" not in output:
        raise Fail(f"expected a clear failure, got exit {proc.returncode}: {output[-500:]}")
    return f"exit {proc.returncode}: {output.strip().splitlines()[1].strip()}"


def container_smoke() -> str:
    docker_available()
    name = f"anki-relay-validate-{os.getpid()}"
    port = "18765"
    data = tempfile.mkdtemp(prefix="anki-relay-validate-")
    logs = tempfile.mkdtemp(prefix="anki-relay-validate-logs-")
    try:
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{port}:8000",
                "-e",
                f"PUBLIC_URL=http://localhost:{port}",
                "-e",
                "ALLOWED_EMAILS=me@example.com",
                "-v",
                f"{data}:/data",
                "-v",
                f"{logs}:/logs",
                IMAGE,
            ],
            env={"MSYS_NO_PATHCONV": "1"},
        )
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                    if json.load(r).get("status") == "ok":
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                logs = run(["docker", "logs", name], check=False)
                raise Fail("no /health: " + (logs.stdout + logs.stderr)[-1500:])
            time.sleep(0.5)
        uid = run(["docker", "exec", name, "sh", "-c", "grep ^Uid /proc/1/status"]).stdout
        if uid.split()[1] == "0":
            raise Fail("app runs as root")
        # REQ-004: JSON log file in the mounted /logs and a readable debug bundle.
        lines = run(["docker", "exec", name, "head", "-n", "20", "/logs/anki-relay.log"]).stdout
        entries = [json.loads(line) for line in lines.splitlines() if line.strip()]
        if not any("Anki Relay ready" in e.get("msg", "") for e in entries):
            raise Fail("no startup record in /logs/anki-relay.log")
        bundle = run(["docker", "exec", name, "anki-relay", "debug-bundle"]).stdout.strip()
        listing = run(["docker", "exec", name, "tar", "-tzf", bundle]).stdout.split()
        if "system.json" not in listing or "logs/anki-relay.log" not in listing:
            raise Fail(f"debug bundle incomplete: {listing}")
        start = time.monotonic()
        run(["docker", "stop", name])
        code = run(["docker", "inspect", name, "--format", "{{.State.ExitCode}}"]).stdout.strip()
        if code != "0":
            raise Fail(f"unclean stop, exit code {code}")
        output = run(["docker", "logs", name], check=False)
        if "sending unsynced changes" not in output.stdout + output.stderr:
            raise Fail("graceful shutdown hook did not run")
        return (
            f"uid {uid.split()[1]}, /health ok, JSON log + debug bundle ok, "
            f"stopped cleanly in {time.monotonic() - start:.1f}s"
        )
    finally:
        run(["docker", "rm", "-f", name], check=False)
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(logs, ignore_errors=True)


STEPS: list[tuple[str, Callable[[], str]]] = [
    ("ruff check", ruff_lint),
    ("ruff format", ruff_format),
    ("pytest (all tests)", pytest_all),
    ("repo hygiene / secrets", repo_hygiene),
    ("docker compose config", compose_modes),
    ("docker build", docker_build),
    ("container w/o settings", container_without_settings),
    ("container smoke test", container_smoke),
]


def main() -> int:
    results: list[tuple[str, str, str]] = []
    build_failed = False
    for title, step in STEPS:
        print(f"\n=== {title} ===", flush=True)
        if build_failed and step in (container_without_settings, container_smoke):
            results.append((title, "SKIPPED", "image was not built"))
            continue
        start = time.monotonic()
        try:
            detail = step()
            status = "PASS"
        except Skip as exc:
            status, detail = "SKIPPED", str(exc)
        except (Fail, subprocess.TimeoutExpired) as exc:
            status, detail = "FAIL", str(exc).strip().splitlines()[-1] if str(exc).strip() else ""
            print(str(exc))
            if step is docker_build:
                build_failed = True
        results.append((title, status, f"{detail} ({time.monotonic() - start:.1f}s)"))

    width = max(len(t) for t, _, _ in results)
    print("\n" + "=" * 72 + "\nVALIDATION SUMMARY")
    for title, status, detail in results:
        print(f"  {title.ljust(width)}  {status:<7}  {detail}")
    failed = [t for t, s, _ in results if s == "FAIL"]
    skipped = [t for t, s, _ in results if s == "SKIPPED"]
    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
        return 1
    if skipped:
        print(f"\nPASSED, but SKIPPED (not verified): {', '.join(skipped)}")
        return 0
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
