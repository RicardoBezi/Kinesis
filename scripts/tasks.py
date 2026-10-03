"""Kinesis task runner. Use as ``uv run task <name>``; ``uv run task --help`` lists tasks.

It uses only the standard library, so it behaves the same on Windows, macOS and Linux CI.
Every subprocess is run with a fixed argv list and never through a shell (ADR 0011).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / ".tools"
CLIENT_DIR = ROOT / "client" / "kotlin-desktop"

# ADR 0010: the pinned Blender LTS. Checksums come from download.blender.org/.../blender-<v>.sha256
BLENDER_VERSION = "4.5.14"
BLENDER_BASE_URL = "https://download.blender.org/release/Blender4.5"
BLENDER_ARCHIVES: dict[str, tuple[str, str]] = {
    "linux": (
        f"blender-{BLENDER_VERSION}-linux-x64.tar.xz",
        "9ba871ff2ecd36526b77432745980b7e6664ecd0c7ca11c48849073dcfe06da3",
    ),
    "windows": (
        f"blender-{BLENDER_VERSION}-windows-x64.zip",
        "b9533d2397ac1984db4466fb23a7a4649391cca93f6e84209f9bcc60d071c8b9",
    ),
}

TaskFn = Callable[[list[str]], int]
TASKS: dict[str, tuple[TaskFn, str]] = {}


def task(name: str, help_text: str) -> Callable[[TaskFn], TaskFn]:
    def register(fn: TaskFn) -> TaskFn:
        TASKS[name] = (fn, help_text)
        return fn

    return register


def run(argv: Sequence[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> int:
    print(f"$ {' '.join(argv)}", flush=True)
    merged = {**os.environ, **(env or {})}
    return subprocess.run(list(argv), cwd=cwd, env=merged, check=False).returncode


def run_all(*commands: Sequence[str]) -> int:
    """Run commands in order, report every failure, and return non-zero if any failed."""
    failed = [c for c in commands if run(c) != 0]
    for c in failed:
        print(f"FAILED: {' '.join(c)}", file=sys.stderr)
    return 1 if failed else 0


def pytest(*args: str) -> list[str]:
    return [sys.executable, "-m", "pytest", *args]


def blender_bin() -> str | None:
    configured = os.environ.get("KINESIS_BLENDER_BIN")
    if configured:
        return configured
    local = sorted(TOOLS.glob(f"blender-{BLENDER_VERSION}*/blender*"))
    executables = [p for p in local if p.name in ("blender", "blender.exe")]
    return str(executables[0]) if executables else None


# --------------------------------------------------------------------------- setup


@task("setup", "Install Python deps, create .env from .env.example, report optional tools")
def setup(_: list[str]) -> int:
    rc = run(["uv", "sync"])
    env_file = ROOT / ".env"
    if not env_file.exists():
        shutil.copyfile(ROOT / ".env.example", env_file)
        print("created .env from .env.example")
    print(f"blender {BLENDER_VERSION}: {blender_bin() or 'not found (uv run task setup-blender)'}")
    print(f"java: {shutil.which('java') or 'not found (Gradle toolchain will provision JDK 17)'}")
    print(f"docker: {shutil.which('docker') or 'not found (optional)'}")
    return rc


@task("setup-blender", f"Download portable Blender {BLENDER_VERSION} LTS into .tools/")
def setup_blender(_: list[str]) -> int:
    system = platform.system().lower()
    if system not in BLENDER_ARCHIVES:
        print(f"no portable archive configured for {system}; install Blender {BLENDER_VERSION}")
        print("manually and set KINESIS_BLENDER_BIN")
        return 1
    name, sha256 = BLENDER_ARCHIVES[system]
    TOOLS.mkdir(exist_ok=True)
    archive = TOOLS / name
    if not archive.exists():
        url = f"{BLENDER_BASE_URL}/{name}"
        print(f"downloading {url}")
        urllib.request.urlretrieve(url, archive)  # noqa: S310 - fixed https URL
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != sha256:
        archive.unlink()
        print(f"checksum mismatch for {name}: {digest}", file=sys.stderr)
        return 1
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(TOOLS)  # noqa: S202 - pinned archive, checksum verified above
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(TOOLS, filter="data")
    print(f"installed: {blender_bin()}")
    print("set KINESIS_BLENDER_BIN in .env to the path above (or leave unset to auto-detect)")
    return 0


# --------------------------------------------------------------------------- quality


@task("fmt", "Format and auto-fix with ruff")
def fmt(_: list[str]) -> int:
    return run_all(["ruff", "format", "."], ["ruff", "check", "--fix", "."])


@task("typecheck", "mypy --strict on backend/src/kinesis and scripts")
def typecheck(_: list[str]) -> int:
    return run(["mypy"])


@task("lint", "ruff format --check, ruff check, mypy")
def lint(_: list[str]) -> int:
    return run_all(["ruff", "format", "--check", "."], ["ruff", "check", "."], ["mypy"])


# --------------------------------------------------------------------------- tests


@task("test", "Full offline suite: unit, contract, fault, golden (numpy), API")
def test(args: list[str]) -> int:
    return run(pytest(*args))


@task("test-unit", "Unit tests only")
def test_unit(args: list[str]) -> int:
    return run(pytest("tests/unit", *args))


@task("test-golden", "Golden regression thresholds (numpy mirror of the fixture)")
def test_golden(args: list[str]) -> int:
    return run(pytest("tests/golden", *args))


@task("test-integration", "Integration tests that need no Blender (API + orchestration)")
def test_integration(args: list[str]) -> int:
    return run(pytest("tests/integration", *args))


@task("test-blender", f"Blender {BLENDER_VERSION} integration + golden tests")
def test_blender(args: list[str]) -> int:
    exe = blender_bin()
    if exe is None:
        print("Blender not found: run `uv run task setup-blender` or set KINESIS_BLENDER_BIN")
        return 1
    return run(pytest("-m", "blender", *args), env={"KINESIS_BLENDER_BIN": exe})


@task("live-nebius-test", "Live Token Factory tests (requires NEBIUS_API_KEY); never in CI")
def live_nebius_test(args: list[str]) -> int:
    if not os.environ.get("NEBIUS_API_KEY"):
        print("NEBIUS_API_KEY is not set")
        return 1
    return run(pytest("-m", "live_nebius", *args), env={"KINESIS_LIVE_NEBIUS": "1"})


# --------------------------------------------------------------------------- run / build


@task("run", "Start the API with auto-reload on http://127.0.0.1:8000")
def run_api(args: list[str]) -> int:
    return run(
        [sys.executable, "-m", "uvicorn", "kinesis.main:create_app", "--factory", "--reload", *args]
    )


@task("openapi", "Export docs/api/openapi.json + Kotlin test samples (--check fails on drift)")
def openapi(args: list[str]) -> int:
    return run_all(
        [sys.executable, str(ROOT / "scripts" / "export_openapi.py"), *args],
        [sys.executable, str(ROOT / "scripts" / "export_samples.py"), *args],
    )


@task("fixture", "Regenerate blender/fixtures/foot_slide_v1.blend through Blender")
def fixture(_: list[str]) -> int:
    exe = blender_bin()
    if exe is None:
        print("Blender not found: run `uv run task setup-blender` or set KINESIS_BLENDER_BIN")
        return 1
    script = ROOT / "blender" / "fixtures" / "build_fixture.py"
    out = ROOT / "blender" / "fixtures" / "foot_slide_v1.blend"
    return run(
        [
            *(exe, "--background", "--factory-startup", "-noaudio", "--python-exit-code", "3"),
            *("--python", str(script), "--", "--out", str(out)),
        ]
    )


@task("models", "List models available from Token Factory for the configured key (spike S4)")
def models(args: list[str]) -> int:
    return run([sys.executable, str(ROOT / "spikes" / "s4_token_factory.py"), "--list", *args])


@task("client", "Run the Compose Desktop review client")
def client(args: list[str]) -> int:
    gradlew = str(CLIENT_DIR / "gradlew.bat") if os.name == "nt" else "./gradlew"
    return run([gradlew, "run", *args], cwd=CLIENT_DIR)


# --------------------------------------------------------------------------- entry


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="task",
        description="Kinesis tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(f"  {n:<18} {h}" for n, (_, h) in TASKS.items()),
    )
    parser.add_argument("name", choices=sorted(TASKS), metavar="name", help="task to run")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="extra args passed through")
    ns = parser.parse_args(argv)
    fn, _ = TASKS[ns.name]
    return fn(list(ns.args))


if __name__ == "__main__":
    raise SystemExit(main())
