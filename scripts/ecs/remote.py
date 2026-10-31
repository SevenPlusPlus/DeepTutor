#!/usr/bin/env python3
"""Install an already uploaded DeepTutor source release on a provisioned ECS host.

This file is intentionally standalone: the local CLI uploads it alongside the
bundle, verifies both SHA-256 values, and invokes it with Cloud Assistant.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import ProxyHandler, build_opener
import uuid


class DeploymentError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise DeploymentError("Invalid release ID")
    return value


def payload_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != value.rstrip("/")
        or any(part.startswith(".env") for part in path.parts)
        or path.parts[0] in {"data", ".git", ".venv", "runtime.env", "current", "previous"}
    ):
        raise DeploymentError(f"Unsafe release path: {value!r}")
    return path


def verify_manifest(root: Path, expected_id: str) -> dict:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("release_id") != expected_id
        or manifest.get("target_platform") != "linux-x64"
        or not isinstance(manifest.get("version"), str)
    ):
        raise DeploymentError("Unsupported or mismatched release manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise DeploymentError("Manifest has no payload file checksums")
    for name, expected in files.items():
        payload_path(name)
        if name == "manifest.json" or not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
            raise DeploymentError("Invalid manifest checksum entry")
        path = root / name
        if not path.is_file() or path.is_symlink() or sha256(path) != expected:
            raise DeploymentError(f"Payload checksum mismatch: {name}")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != set(files) | {"manifest.json"}:
        raise DeploymentError("Release contains files absent from its manifest")
    required = {
        "pyproject.toml",
        "requirements.txt",
        "deeptutor/__version__.py",
        "web-runtime/server.js",
    }
    if not required.issubset(files):
        raise DeploymentError("Release is missing required runtime files")
    return manifest


def unpack_release(archive: Path, destination: Path, expected_sha: str, expected_id: str) -> dict:
    """Validate the entire archive before writing anything into a new release."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or sha256(archive) != expected_sha:
        raise DeploymentError("Archive SHA-256 mismatch")
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        names: set[str] = set()
        for member in members:
            name = member.name.rstrip("/")
            payload_path(name)
            if name in names or not (member.isdir() or member.isfile()):
                raise DeploymentError(f"Duplicate or unsupported archive member: {name}")
            names.add(name)
        destination.mkdir(parents=False, exist_ok=False)
        for member in members:
            path = destination / member.name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(0o755)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                assert source is not None
                with source, path.open("xb") as output:
                    shutil.copyfileobj(source, output)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
    return verify_manifest(destination, expected_id)


@contextmanager
def deployment_lock(base: Path):
    name = hashlib.sha256(str(base).encode()).hexdigest()[:16]
    private = Path("/var/lib/deeptutor-deploy")
    if private.is_symlink():
        raise DeploymentError("Root deployment state directory cannot be a symlink")
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    if private.stat().st_uid != 0 or private.stat().st_mode & 0o077:
        raise DeploymentError("Root deployment state directory must be root-owned and private")
    path = private / f"deployment-{name}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError("Another deployment is already running") from exc
        yield


def atomic_link(path: Path, target: Path) -> None:
    if path.exists() and not path.is_symlink():
        raise DeploymentError(f"Refusing to replace a non-symlink: {path}")
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path: Path, content: str, mode: int | None = None) -> None:
    previous = path.stat() if path.exists() else None
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content.encode())
            stream.flush()
            os.fsync(stream.fileno())
            permissions = (
                mode if mode is not None else (previous.st_mode & 0o777 if previous else 0o600)
            )
            os.chmod(temporary, permissions)
            if previous is not None and os.geteuid() == 0:
                os.chown(temporary, previous.st_uid, previous.st_gid)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def backend_script(base: Path) -> str:
    return f"""#!/bin/bash
set -eu
DEEPTUTOR_DEPLOY_BASE={shlex.quote(str(base))}
PYTHON="$DEEPTUTOR_DEPLOY_BASE/current/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then PYTHON="$DEEPTUTOR_DEPLOY_BASE/venv/bin/python"; fi
WS_MAX_SIZE=$("$PYTHON" -c "from deeptutor.services.config import get_ws_max_size; print(get_ws_max_size())")
BACKEND_PORT=$("$PYTHON" -c "from deeptutor.services.config import load_system_settings; print(load_system_settings()['backend_port'])")
exec "$PYTHON" -m uvicorn deeptutor.api.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --workers 1 --no-access-log --no-proxy-headers --ws-max-size "$WS_MAX_SIZE" --timeout-keep-alive 300
"""


class Installer:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.base = Path(args.base).resolve()
        self.current = self.base / "current"
        self.previous = self.base / "previous"

    def validate_target(self) -> None:
        if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
            raise DeploymentError("This bundle requires Linux x86_64")
        if not (self.base / "runtime.env").is_file() or not (self.base / "data").is_dir():
            raise DeploymentError("Target is not a provisioned DeepTutor source deployment")
        if not self.current.is_symlink() or not self.current.resolve().is_dir():
            raise DeploymentError("Target current symlink is missing or invalid")
        for link in [self.current, self.previous]:
            if link.exists() and not link.is_symlink():
                raise DeploymentError(f"Expected a symlink: {link}")
        for executable in [self.args.python, self.args.uv, self.args.node]:
            if not Path(executable).is_file():
                raise DeploymentError(f"Required target executable is missing: {executable}")

    def as_user(self, command: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
        environment = {
            "PATH": f"{Path(self.args.node).parent}:/usr/local/bin:/usr/bin:/bin",
            "UV_CACHE_DIR": str(self.base / ".cache/uv"),
            "UV_CONCURRENT_DOWNLOADS": "12",
            "UV_CONCURRENT_BUILDS": "2",
            "UV_HTTP_TIMEOUT": "120",
            "PYTHONUNBUFFERED": "1",
        }
        invocation = ["runuser", "-u", self.args.user, "--", "env"]
        invocation += [f"{key}={value}" for key, value in environment.items()]
        return subprocess.run(invocation + command, cwd=cwd, check=True, **kwargs)

    def source_extra_requirements(
        self, candidate: Path, python: Path, extras: list[str]
    ) -> list[str]:
        # Evaluate release-owned build metadata as the deployment user, before
        # installing dependencies. Source extras must never pull feature wheels
        # over the module code already present in this candidate.
        result = self.as_user(
            [
                str(python),
                "-c",
                "import json; from build_metadata import source_optional_dependencies; "
                "print(json.dumps(source_optional_dependencies()))",
            ],
            candidate,
            capture_output=True,
            text=True,
        )
        available = json.loads(result.stdout)
        unknown = sorted(set(extras) - available.keys())
        if unknown:
            raise DeploymentError(f"Unknown source extra(s): {', '.join(unknown)}")
        return list(dict.fromkeys(raw for name in extras for raw in available[name]))

    def prepare(self, candidate: Path, manifest: dict) -> None:
        user = pwd.getpwnam(self.args.user)
        for path in [candidate, *candidate.rglob("*")]:
            os.chown(path, user.pw_uid, user.pw_gid)
        python = candidate / ".venv/bin/python"
        self.as_user(
            [self.args.uv, "venv", "--python", self.args.python, str(candidate / ".venv")],
            candidate,
        )
        extras = self.args.extra if self.args.extra is not None else ["parse-markitdown"]
        extra_requirements = self.source_extra_requirements(candidate, python, extras)
        self.as_user(
            [
                self.args.uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--index-url",
                self.args.pip_index,
                "-r",
                str(candidate / "requirements.txt"),
                *extra_requirements,
            ],
            candidate,
        )
        self.as_user(
            [
                self.args.uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                "--index-url",
                self.args.pip_index,
                "-e",
                str(candidate),
            ],
            candidate,
        )
        self.as_user([self.args.uv, "pip", "check", "--python", str(python)], candidate)
        frozen = self.as_user(
            [self.args.uv, "pip", "freeze", "--python", str(python)],
            candidate,
            capture_output=True,
            text=True,
        )
        (candidate / "requirements-resolved.txt").write_text(frozen.stdout, encoding="utf-8")
        self.install_native(candidate, manifest, user.pw_uid, user.pw_gid)
        probe = (
            "import fastapi, uvicorn, openai, yaml, aiosqlite; "
            "from deeptutor.__version__ import __version__; "
            f"assert __version__ == {manifest['version']!r}; print('Python runtime preflight passed')"
        )
        self.as_user([str(python), "-c", probe], candidate)
        self.as_user(
            [self.args.node, "-e", "require('sharp'); console.log('Linux sharp preflight passed')"],
            candidate / "web-runtime",
        )

    def install_native(self, candidate: Path, manifest: dict, uid: int, gid: int) -> None:
        packages = manifest.get("native_packages", [])
        required = {"@img/sharp-linux-x64", "@img/sharp-libvips-linux-x64"}
        if {item.get("name") for item in packages} != required or len(packages) != 2:
            raise DeploymentError("Manifest must pin both Linux x64 sharp native packages")
        for item in packages:
            if not re.fullmatch(
                r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", str(item.get("version", ""))
            ):
                raise DeploymentError("Invalid native package version")
        npm = str(Path(self.args.node).with_name("npm"))
        with tempfile.TemporaryDirectory(prefix=".native-", dir=candidate) as temporary:
            prefix = Path(temporary)
            os.chown(prefix, uid, gid)
            self.as_user(
                [
                    npm,
                    "install",
                    "--prefix",
                    str(prefix),
                    "--registry",
                    self.args.npm_registry,
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--save-exact",
                    *[f"{item['name']}@{item['version']}" for item in packages],
                ],
                candidate,
            )
            lock = json.loads((prefix / "package-lock.json").read_text(encoding="utf-8"))
            for item in packages:
                entry = lock["packages"][f"node_modules/{item['name']}"]
                if entry.get("version") != item["version"] or (
                    item.get("integrity") and entry.get("integrity") != item["integrity"]
                ):
                    raise DeploymentError(f"Native package integrity mismatch: {item['name']}")
                target = candidate / "web-runtime/node_modules" / item["name"]
                if target.exists():
                    shutil.rmtree(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(prefix / "node_modules" / item["name"], target)
            shutil.copy2(prefix / "package-lock.json", candidate / "native-package-lock.json")

    def prepare_launcher(self) -> None:
        """A stable launcher supports both new per-release and legacy venvs."""
        backup = self.base / ".deployment-backups"
        backup.mkdir(exist_ok=True, mode=0o700)
        for path in [self.base / "start-backend.sh", self.base / "runtime.env"]:
            saved = backup / path.name
            if path.exists() and not saved.exists():
                shutil.copy2(path, saved)
        launcher = self.base / "start-backend.sh"
        atomic_text(launcher, backend_script(self.base), mode=0o755)
        environment = self.base / "runtime.env"
        lines = environment.read_text(encoding="utf-8").splitlines()
        prefix = str(self.current / ".venv/bin")
        for index, line in enumerate(lines):
            if line.startswith("PATH="):
                value = line[5:].strip().strip('"').strip("'")
                if not value.startswith(prefix + ":"):
                    lines[index] = f"PATH={prefix}:{value}"
                break
        else:
            lines.append(f"PATH={prefix}:/usr/local/bin:/usr/bin:/bin")
        atomic_text(environment, "\n".join(lines) + "\n")

    def restart(self) -> None:
        subprocess.run(["systemctl", "restart", self.args.service], check=True)

    def healthy(self) -> None:
        opener = build_opener(ProxyHandler({}))
        endpoints = [
            (f"http://127.0.0.1:{self.args.backend_port}/health/ready", "ready"),
            (f"http://127.0.0.1:{self.args.frontend_port}/api/auth/status", "auth"),
            (
                f"http://127.0.0.1:{self.args.frontend_port}/_next/image?url=%2Flogo.png&w=32&q=75",
                "image",
            ),
        ]
        deadline = time.monotonic() + self.args.health_timeout
        last = "not checked"
        while time.monotonic() < deadline:
            try:
                for url, kind in endpoints:
                    with opener.open(url, timeout=5) as response:
                        payload = response.read()
                        if kind == "ready" and json.loads(payload).get("status") != "ready":
                            raise DeploymentError("Backend is not ready")
                        if kind == "auth" and "enabled" not in json.loads(payload):
                            raise DeploymentError("Frontend API proxy is not ready")
                        if kind == "image" and (
                            not response.headers.get_content_type().startswith("image/")
                            or not payload
                        ):
                            raise DeploymentError("Image optimization is not ready")
                return
            except (OSError, ValueError, DeploymentError) as exc:
                last = type(exc).__name__
                time.sleep(2)
        raise DeploymentError(f"Release health check timed out ({last})")

    def activate(self, candidate: Path) -> None:
        old = self.current.resolve(strict=True)
        atomic_link(self.current, candidate)
        try:
            self.restart()
            self.healthy()
            atomic_link(self.previous, old)
        except Exception as exc:
            atomic_link(self.current, old)
            try:
                self.restart()
                self.healthy()
            except Exception as recovery:
                raise DeploymentError(
                    "Deployment failed; previous link restored but recovery health check failed"
                ) from recovery
            raise DeploymentError(
                "Deployment failed; previous release restored and healthy"
            ) from exc

    def install(self) -> None:
        self.validate_target()
        identifier = release_id(self.args.release_id)
        releases = self.base / "releases"
        releases.mkdir(exist_ok=True)
        candidate = releases / identifier
        if candidate.exists() or candidate.is_symlink():
            raise DeploymentError(f"Release already exists: {candidate}; choose a new release ID")
        manifest = unpack_release(Path(self.args.archive), candidate, self.args.sha256, identifier)
        print(f"Preparing release {identifier}", flush=True)
        self.prepare(candidate, manifest)
        self.prepare_launcher()
        self.activate(candidate)
        print(
            json.dumps(
                {"result": "deployed", "current": str(candidate), "version": manifest["version"]}
            ),
            flush=True,
        )

    def rollback(self) -> None:
        self.validate_target()
        if not self.previous.is_symlink() or not self.previous.resolve().is_dir():
            raise DeploymentError("No previous successful release is available")
        candidate = self.previous.resolve(strict=True)
        if candidate.parent != self.base / "releases":
            raise DeploymentError("Previous release is outside the deployment releases directory")
        self.activate(candidate)
        print(json.dumps({"result": "rolled_back", "current": str(candidate)}), flush=True)

    def status(self) -> None:
        result = subprocess.run(
            ["systemctl", "is-active", self.args.service], capture_output=True, text=True
        )
        print(
            json.dumps(
                {
                    "current": str(self.current.resolve()) if self.current.is_symlink() else None,
                    "previous": (
                        str(self.previous.resolve()) if self.previous.is_symlink() else None
                    ),
                    "service": self.args.service,
                    "state": result.stdout.strip(),
                }
            )
        )


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ["install", "rollback", "status"]:
        command = commands.add_parser(action)
        command.add_argument("--base", default="/opt/deeptutor")
        command.add_argument("--user", default="deeptutor-deploy")
        command.add_argument("--service", default="deeptutor-ecs.service")
        command.add_argument("--python", default="/usr/bin/python3.11")
        command.add_argument("--uv", default="/opt/deeptutor/bin/uv")
        command.add_argument("--node", default="/usr/local/bin/node")
        command.add_argument("--pip-index", default="https://mirrors.aliyun.com/pypi/simple/")
        command.add_argument("--npm-registry", default="https://registry.npmmirror.com")
        command.add_argument("--health-timeout", type=int, default=90)
        command.add_argument("--backend-port", type=int, default=8001)
        command.add_argument("--frontend-port", type=int, default=3782)
        if action == "install":
            command.add_argument("--archive", required=True)
            command.add_argument("--sha256", required=True)
            command.add_argument("--release-id", required=True)
            command.add_argument("--extra", action="append")
    return parser


def main() -> int:
    args = argument_parser().parse_args()
    if args.action != "status" and os.geteuid() != 0:
        raise DeploymentError("Install and rollback must run through root Cloud Assistant")
    deployment = Installer(args)
    if args.action == "status":
        deployment.status()
    else:
        with deployment_lock(deployment.base):
            getattr(deployment, args.action)()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DeploymentError, subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f"Deployment error: {error}", file=sys.stderr)
        raise SystemExit(1)
