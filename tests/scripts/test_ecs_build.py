from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile

import pytest


@pytest.fixture
def module():
    path = Path(__file__).resolve().parents[2] / "scripts/ecs/build.py"
    spec = importlib.util.spec_from_file_location("ecs_build_under_test", path)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for directory in (
        "deeptutor",
        "deeptutor_cli",
        "web/public",
        "web/node_modules",
        "requirements",
        "scripts",
    ):
        (root / directory).mkdir(parents=True)
    (root / "deeptutor/__version__.py").write_text('__version__ = "9.8.7"\n')
    (root / "deeptutor/new_untracked.py").write_text("UNCOMMITTED = True\n")
    (root / "web/package.json").write_text('{"scripts":{"build":"fake build"}}')
    (root / "web/node_modules/marker.txt").write_text("original dependency")
    (root / "web/public/logo.png").write_bytes(b"image")
    (root / "web/tsconfig.json").write_text('{"original":true}')
    (root / "pyproject.toml").write_text('[project]\nname="test"\n')
    for name in (
        "requirements.txt",
        "README.md",
        "LICENSE",
        "setup.py",
        "build_metadata.py",
        "build_support.py",
    ):
        (root / name).write_text(name)
    (root / "web/package-lock.json").write_text(
        json.dumps(
            {
                "packages": {
                    "node_modules/sharp": {"version": "0.99.2"},
                    "node_modules/@img/sharp-linux-x64": {
                        "version": "0.99.2",
                        "integrity": "sha512-sharp",
                    },
                    "node_modules/@img/sharp-libvips-linux-x64": {
                        "version": "7.8.9",
                        "integrity": "sha512-vips",
                    },
                }
            }
        )
    )
    return root


def _mock_frontend(monkeypatch, module, *, native_file: str | None = None) -> list[dict]:
    calls = []

    def run(args, **kwargs):
        calls.append({"args": args, **kwargs})
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="no git")
        if args == ["node", "--version"]:
            return subprocess.CompletedProcess(args, 0, stdout="v22.13.1\n")
        if args[:2] == ["npm", "ci"]:
            dependencies = Path(kwargs["cwd"]) / "node_modules"
            dependencies.mkdir()
            (dependencies / "marker.txt").write_text("isolated install")
        if args[:3] == ["npm", "run", "build"]:
            web = Path(kwargs["cwd"])
            (web / "node_modules/marker.txt").write_text("build changed its local copy")
            (web / "tsconfig.json").write_text('{"built":true}')
            standalone = web / ".next/standalone"
            for name in ("node_modules/sharp", "node_modules/@img/sharp-darwin-arm64", ".next"):
                (standalone / name).mkdir(parents=True, exist_ok=True)
            (standalone / "server.js").write_text("// production server")
            (standalone / "node_modules/sharp/package.json").write_text('{"version":"0.99.2"}')
            (standalone / "node_modules/@img/sharp-darwin-arm64/sharp.node").write_bytes(
                b"\xcf\xfa\xed\xfe"
            )
            if native_file:
                path = standalone / native_file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 16)
            (web / ".next/static").mkdir(parents=True)
            (web / ".next/static/chunk.js").write_text("// static")
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(module.subprocess, "run", run)
    return calls


def test_build_isolates_worktree_and_excludes_runtime_secrets(module, repo, tmp_path, monkeypatch):
    (repo / "web/.next").mkdir()
    (repo / "web/.next/developer-cache").write_text("keep me")
    (repo / "web/.env.production").write_text("SECRET=provider-key")
    (repo / "web/.npmrc").write_text("//registry:_authToken=secret")
    (repo / "deeptutor/credentials.json").write_text('{"password":"secret"}')
    (repo / "data/user/settings").mkdir(parents=True)
    (repo / "data/user/settings/model_catalog.json").write_text('{"api_key":"secret"}')
    (repo / "deeptutor/__pycache__").mkdir()
    (repo / "deeptutor/__pycache__/cache.pyc").write_bytes(b"cache")
    monkeypatch.setenv("NEXT_PUBLIC_API_KEY", "must-not-reach-build")
    calls = _mock_frontend(monkeypatch, module)

    staging = module.build_release(repo, tmp_path / "output", "test-release")
    manifest = json.loads((staging / "manifest.json").read_text())

    assert (repo / "web/.next/developer-cache").read_text() == "keep me"
    assert (repo / "web/tsconfig.json").read_text() == '{"original":true}'
    assert (repo / "web/node_modules/marker.txt").read_text() == "original dependency"
    assert (staging / "deeptutor/new_untracked.py").exists()
    for name in ("setup.py", "build_metadata.py", "build_support.py"):
        assert (staging / name).read_text() == name
    assert (staging / "web-runtime/.next/static/chunk.js").exists()
    assert (staging / "web-runtime/public/logo.png").exists()
    for name in (
        "data",
        "web/.env.production",
        "web/.npmrc",
        "web/node_modules",
        "web/.next",
        "deeptutor/credentials.json",
        "deeptutor/__pycache__",
    ):
        assert not (staging / name).exists()
    assert not (staging / "web-runtime/node_modules/@img/sharp-darwin-arm64").exists()
    assert manifest["version"] == "9.8.7"
    assert manifest["target_platform"] == "linux-x64"
    assert manifest["native_packages"] == [
        {"name": "@img/sharp-linux-x64", "version": "0.99.2", "integrity": "sha512-sharp"},
        {"name": "@img/sharp-libvips-linux-x64", "version": "7.8.9", "integrity": "sha512-vips"},
    ]
    build = next(call for call in calls if call["args"] == ["npm", "run", "build"])
    assert not Path(build["cwd"]).is_relative_to(repo)
    assert "NEXT_PUBLIC_API_KEY" not in build["env"]
    assert (
        manifest["files"]["deeptutor/new_untracked.py"]
        == hashlib.sha256(b"UNCOMMITTED = True\n").hexdigest()
    )


def test_build_rejects_a_symlink_to_another_checkout(module, repo, tmp_path, monkeypatch):
    outside = tmp_path / "outside.js"
    outside.write_text("private source")
    (repo / "web/linked.js").symlink_to(outside)
    _mock_frontend(monkeypatch, module)
    with pytest.raises(ValueError, match="Symlink escapes"):
        module.build_release(repo, tmp_path / "output", "release")
    assert not (tmp_path / "output/release").exists()


def test_install_option_installs_build_dependencies_only_in_snapshot(
    module, repo, tmp_path, monkeypatch
):
    import shutil

    shutil.rmtree(repo / "web/node_modules")
    calls = _mock_frontend(monkeypatch, module)
    module.build_release(repo, tmp_path / "output", "release", install=True)
    install = next(call for call in calls if call["args"][:2] == ["npm", "ci"])
    assert "--include=dev" in install["args"]
    assert not Path(install["cwd"]).is_relative_to(repo)
    assert not (repo / "web/node_modules").exists()


def test_build_materializes_a_safe_internal_file_symlink(module, repo, tmp_path, monkeypatch):
    (repo / "deeptutor/alias.py").symlink_to("new_untracked.py")
    _mock_frontend(monkeypatch, module)
    staging = module.build_release(repo, tmp_path / "output", "release")
    alias = staging / "deeptutor/alias.py"
    assert not alias.is_symlink()
    assert alias.read_text() == "UNCOMMITTED = True\n"


def test_build_rejects_unhandled_native_dependencies(module, repo, tmp_path, monkeypatch):
    _mock_frontend(monkeypatch, module, native_file="node_modules/other/native.node")
    with pytest.raises(ValueError, match="Unsupported native dependency"):
        module.build_release(repo, tmp_path / "output", "release")


def test_package_checks_payload_and_produces_clean_reproducible_archive(
    module, repo, tmp_path, monkeypatch
):
    _mock_frontend(monkeypatch, module)
    staging = module.build_release(repo, tmp_path / "output", "release")
    first = module.package_release(staging, tmp_path / "release.tar.gz")
    second = module.package_release(staging, tmp_path / "same-release.tar.gz")

    assert first.read_bytes() == second.read_bytes()
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    assert first.with_name(first.name + ".sha256").read_text() == f"{digest}  {first.name}\n"
    with tarfile.open(first) as bundle:
        members = bundle.getmembers()
        assert "manifest.json" in bundle.getnames()
        assert "web-runtime/server.js" in bundle.getnames()
        assert all(not item.issym() and not item.islnk() for item in members)
        assert all(item.uid == item.gid == item.mtime == 0 for item in members)
        assert all(
            not any(key.startswith("LIBARCHIVE") for key in item.pax_headers) for item in members
        )

    (staging / "deeptutor/new_untracked.py").write_text("tampered")
    with pytest.raises(ValueError, match="differs from manifest"):
        module.package_release(staging, tmp_path / "tampered.tar.gz")
    assert not (tmp_path / "tampered.tar.gz").exists()


def test_package_refuses_an_external_link_even_with_a_manifest(module, repo, tmp_path, monkeypatch):
    _mock_frontend(monkeypatch, module)
    staging = module.build_release(repo, tmp_path / "output", "release")
    outside = tmp_path / "private"
    outside.write_text("private")
    (staging / "linked-private").symlink_to(outside)
    with pytest.raises(ValueError, match="Unsafe release symlink"):
        module.package_release(staging, tmp_path / "unsafe.tar.gz")


def test_release_id_cannot_escape_output(module, repo, tmp_path):
    with pytest.raises(ValueError, match="release_id"):
        module.build_release(repo, tmp_path / "output", "../escaped")
