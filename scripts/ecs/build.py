"""Build an isolated source release and package its verified payload.

The local frontend build may run on macOS. Platform-specific sharp modules are
removed from its standalone output; the release manifest pins the Linux x64
packages that the ECS installer must add.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any

SOURCE_DIRS = ("deeptutor", "deeptutor_cli", "requirements", "scripts", "web")
SOURCE_FILES = (
    "pyproject.toml",
    "setup.py",
    "build_metadata.py",
    "build_support.py",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "MANIFEST.in",
)
NATIVE_PACKAGES = ("@img/sharp-linux-x64", "@img/sharp-libvips-linux-x64")
_SKIP_DIRS = {
    "data",
    "node_modules",
    ".git",
    ".ssh",
    ".aws",
    ".aliyun",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".cache",
    "cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".turbo",
    ".vercel",
    "test-results",
    "playwright-report",
    "tmp",
}
_SECRET_NAMES = {
    "credentials.json",
    "secrets.json",
    "secret.json",
    "id_rsa",
    "id_ed25519",
    ".npmrc",
    ".pypirc",
    ".netrc",
}


def _sensitive_file(path: Path) -> bool:
    return (
        path.name in _SECRET_NAMES
        or path.name.startswith(".env")
        or path.name.endswith((".env", ".pem", ".key", ".p12", ".pfx"))
    )


def _excluded(path: Path) -> bool:
    name = path.name
    return (
        name in _SKIP_DIRS
        or _sensitive_file(path)
        or name.startswith((".env", ".next", "._", "tsconfig.deeptutor-"))
        or name == ".DS_Store"
        or name.endswith(
            (
                ".pyc",
                ".pyo",
                ".log",
                ".tsbuildinfo",
                ".egg-info",
                ".env",
                ".pem",
                ".key",
                ".p12",
                ".pfx",
            )
        )
        or (path.parts[-3:] == ("web", "public", "pdfjs"))
    )


def _safe_copy_tree(source: Path, target: Path, *, filtered: bool = False) -> None:
    """Materialize a tree without importing links to another checkout or secrets."""
    source = source.resolve(strict=True)

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped: set[str] = set()
        for name in names:
            path = Path(directory) / name
            if filtered and _excluded(path):
                skipped.add(name)
                continue
            if path.is_symlink():
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(source):
                    raise ValueError(f"Symlink escapes the copied tree: {path}")
                if resolved.is_dir():
                    raise ValueError(
                        f"Directory symlink is unsupported: {path}; use --install for dependencies"
                    )
                if filtered and any(
                    _excluded(part)
                    for part in [resolved, *resolved.parents]
                    if part != source and part.is_relative_to(source)
                ):
                    raise ValueError(f"Symlink points at excluded content: {path}")
        return skipped

    shutil.copytree(source, target, symlinks=False, ignore=ignore)


def _copy_source(repo: Path, target: Path) -> None:
    target.mkdir()
    for name in SOURCE_DIRS:
        path = repo / name
        if path.exists():
            if path.is_symlink():
                raise ValueError(f"Source root must not be a symlink: {path}")
            _safe_copy_tree(path, target / name, filtered=True)
    for name in SOURCE_FILES:
        path = repo / name
        if path.exists():
            if path.is_symlink():
                raise ValueError(f"Source file must not be a symlink: {path}")
            shutil.copyfile(path, target / name)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload_files(staging: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(staging.rglob("*")):
        if path.name == "manifest.json" and path.parent == staging:
            continue
        if _sensitive_file(path):
            raise ValueError(f"Sensitive file must not be included in a release: {path}")
        if path.is_symlink():
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(staging) or resolved.is_dir():
                raise ValueError(f"Unsafe release symlink: {path}")
        if path.is_file():
            files[path.relative_to(staging).as_posix()] = _sha256(path)
        elif not path.is_dir():
            raise ValueError(f"Unsupported release file: {path}")
    return files


def _git_metadata(repo: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
    )
    if result.returncode:
        return {"commit": None, "dirty": True}
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        text=True,
        capture_output=True,
    )
    return {"commit": result.stdout.strip(), "dirty": bool(status.stdout.strip())}


def _native_metadata(lock_path: Path) -> tuple[str, list[dict[str, str]]]:
    packages = json.loads(lock_path.read_text(encoding="utf-8"))["packages"]
    sharp_version = packages["node_modules/sharp"]["version"]
    native = []
    for name in NATIVE_PACKAGES:
        locked = packages[f"node_modules/{name}"]
        item = {"name": name, "version": locked["version"]}
        if locked.get("integrity"):
            item["integrity"] = locked["integrity"]
        native.append(item)
    return sharp_version, native


def _prepare_runtime(runtime: Path, sharp_version: str) -> None:
    sharp = runtime / "node_modules/sharp/package.json"
    if not sharp.is_file() or json.loads(sharp.read_text())["version"] != sharp_version:
        raise ValueError(
            "Standalone sharp does not match package-lock.json; rebuild with --install"
        )
    for directory in list(runtime.rglob("@img")):
        if not directory.is_dir():
            continue
        for child in directory.iterdir():
            if child.name.startswith("sharp-") and f"@img/{child.name}" not in NATIVE_PACKAGES:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
    for path in runtime.rglob("*"):
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            magic = stream.read(20)
        if magic[:4] == b"\x7fELF":
            if (
                len(magic) < 20
                or magic[4:6] != b"\x02\x01"
                or int.from_bytes(magic[18:20], "little") != 62
            ):
                raise ValueError(f"Native module is not Linux x64: {path}")
        elif magic[:4] in {
            b"\xfe\xed\xfa\xce",
            b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xcf\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe",
            b"\xca\xfe\xba\xbf",
            b"\xbe\xba\xfe\xca",
            b"\xbf\xba\xfe\xca",
        } or path.suffix in {".node", ".dylib", ".dll", ".exe"}:
            raise ValueError(
                f"Unsupported native dependency in standalone output: {path}; build on Linux x64"
            )


def build_release(repo: Path, output: Path, release_id: str, install: bool = False) -> Path:
    """Build into ``output/release_id`` without touching the developer's build cache."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", release_id):
        raise ValueError(
            "release_id must contain only letters, numbers, dots, underscores and hyphens"
        )
    repo = repo.resolve(strict=True)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / release_id
    if destination.exists():
        raise FileExistsError(f"Release already exists: {destination}")
    git = _git_metadata(repo)
    with tempfile.TemporaryDirectory(prefix="deeptutor-ecs-build-") as temporary:
        work = Path(temporary) / "source"
        _copy_source(repo, work)
        version_match = re.search(
            r'__version__\s*=\s*[\'"]([^\'"]+)[\'"]',
            (work / "deeptutor/__version__.py").read_text(encoding="utf-8"),
        )
        if not version_match:
            raise ValueError("Cannot read the application version")
        web = work / "web"
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("NEXT_PUBLIC_")
            and key
            not in {
                "BACKEND_PORT",
                "AUTH_ENABLED",
                "DEEPTUTOR_AUTH_ENABLED",
                "VERCEL",
                "DEEPTUTOR_NEXT_DIST_DIR",
                "DEEPTUTOR_NEXT_TSCONFIG",
                "NODE_ENV",
            }
        }
        env["NODE_ENV"] = "production"
        if install:
            # Build dependencies include TypeScript and must be installed in production mode too.
            subprocess.run(
                ["npm", "ci", "--legacy-peer-deps", "--include=dev"], cwd=web, env=env, check=True
            )
        else:
            dependencies = repo / "web/node_modules"
            if not dependencies.is_dir():
                raise ValueError("web/node_modules is missing; run with --install")
            _safe_copy_tree(dependencies, web / "node_modules")
        subprocess.run(["npm", "run", "build"], cwd=web, env=env, check=True)
        standalone = web / ".next/standalone"
        if not (standalone / "server.js").is_file():
            raise ValueError("Frontend build did not produce .next/standalone/server.js")
        sharp_version, native = _native_metadata(web / "package-lock.json")
        node = subprocess.run(
            ["node", "--version"], check=True, text=True, capture_output=True
        ).stdout.strip()
        with tempfile.TemporaryDirectory(prefix=f".{release_id}-", dir=output) as prepared:
            staging = Path(prepared) / "release"
            _copy_source(work, staging)
            runtime = staging / "web-runtime"
            _safe_copy_tree(standalone, runtime)
            for source, target in [
                (web / ".next/static", runtime / ".next/static"),
                (web / "public", runtime / "public"),
            ]:
                if target.exists():
                    shutil.rmtree(target)
                _safe_copy_tree(source, target)
            _prepare_runtime(runtime, sharp_version)
            manifest = {
                "schema_version": 1,
                "release_id": release_id,
                "version": version_match[1],
                "git": git,
                "target_platform": "linux-x64",
                "build_platform": {
                    "system": platform.system(),
                    "machine": platform.machine(),
                    "node": node,
                },
                "sharp_version": sharp_version,
                "native_packages": native,
                "files": _payload_files(staging),
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            staging.rename(destination)
    return destination


def package_release(staging: Path, archive: Path) -> Path:
    """Verify a completed release and create a reproducible gzip tar plus SHA256."""
    staging = staging.resolve(strict=True)
    archive = archive.resolve()
    if archive.is_relative_to(staging):
        raise ValueError("The archive must be outside the release directory")
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("files") != _payload_files(staging):
        raise ValueError("Release payload differs from manifest.json; rebuild before packaging")
    archive.parent.mkdir(parents=True, exist_ok=True)

    def normalized(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
        info.pax_headers = {}
        return info

    with tempfile.NamedTemporaryFile(
        prefix=f".{archive.name}-", dir=archive.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with (
            temporary_path.open("wb") as raw,
            gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0) as compressed,
        ):
            with tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT, dereference=True
            ) as bundle:
                for child in sorted(staging.iterdir()):
                    bundle.add(child, arcname=child.name, filter=normalized)
        temporary_path.replace(archive)
    finally:
        temporary_path.unlink(missing_ok=True)
    checksum = archive.with_name(archive.name + ".sha256")
    checksum.write_text(f"{_sha256(archive)}  {archive.name}\n", encoding="utf-8")
    return archive
