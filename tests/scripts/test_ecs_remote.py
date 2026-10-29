from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


def load_remote():
    path = Path(__file__).resolve().parents[2] / "scripts/ecs/remote.py"
    spec = importlib.util.spec_from_file_location("ecs_remote_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_bundle(tmp_path: Path, *, extra: str | None = None) -> Path:
    files = {
        "pyproject.toml": b"[project]\nname='deeptutor'\n",
        "requirements.txt": b"",
        "deeptutor/__version__.py": b"__version__ = '1.0.0'\n",
        "web-runtime/server.js": b"console.log('test')\n",
    }
    manifest = {
        "schema_version": 1,
        "release_id": "new",
        "version": "1.0.0",
        "target_platform": "linux-x64",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    files["manifest.json"] = json.dumps(manifest).encode()
    if extra:
        files[extra] = b"unlisted"
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))
    return archive


def make_installer(module, tmp_path: Path):
    base = tmp_path / "deployment"
    old = base / "releases/legacy"
    old.mkdir(parents=True)
    (base / "current").symlink_to(old)
    (base / "data").mkdir()
    (base / "runtime.env").write_text("DEEPTUTOR_HOME=/target\nPATH=/legacy/bin:/usr/bin\n")
    args = argparse.Namespace(
        base=str(base),
        user="unused",
        service="deeptutor-ecs.service",
        python="/unused/python",
        uv="/unused/uv",
        node="/unused/node",
        extra=None,
        pip_index="https://example.com/simple",
        npm_registry="https://example.com",
        health_timeout=1,
        backend_port=8001,
        frontend_port=3782,
    )
    return module.Installer(args), old


@pytest.mark.parametrize(
    "member_name",
    [".", "../escape", "/tmp/escape", "nested/../../escape", "data/private.json", "web/.env.local"],
)
def test_archive_rejects_unsafe_paths_before_creating_release(tmp_path, member_name):
    module = load_remote()
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo(member_name)
        member.size = 1
        bundle.addfile(member, io.BytesIO(b"x"))
    target = tmp_path / "release"
    with pytest.raises(module.DeploymentError, match="Unsafe release path"):
        module.unpack_release(archive, target, module.sha256(archive), "new")
    assert not target.exists()


def test_archive_rejects_symlinks_even_with_safe_names(tmp_path):
    module = load_remote()
    archive = tmp_path / "link.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("web-runtime/node_modules")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc"
        bundle.addfile(member)
    with pytest.raises(module.DeploymentError, match="unsupported archive member"):
        module.unpack_release(archive, tmp_path / "release", module.sha256(archive), "new")


def test_manifest_checks_payload_bytes_and_unlisted_files(tmp_path):
    module = load_remote()
    archive = make_bundle(tmp_path, extra="web-runtime/secret.txt")
    with pytest.raises(module.DeploymentError, match="absent from its manifest"):
        module.unpack_release(archive, tmp_path / "extra", module.sha256(archive), "new")
    archive = make_bundle(tmp_path)
    root = tmp_path / "release"
    module.unpack_release(archive, root, module.sha256(archive), "new")
    (root / "web-runtime/server.js").write_text("modified")
    with pytest.raises(module.DeploymentError, match="checksum mismatch"):
        module.verify_manifest(root, "new")


def test_preparation_failure_does_not_change_live_release(tmp_path, monkeypatch):
    module = load_remote()
    installer, old = make_installer(module, tmp_path)
    archive = make_bundle(tmp_path)
    installer.args.archive = str(archive)
    installer.args.sha256 = module.sha256(archive)
    installer.args.release_id = "new"
    monkeypatch.setattr(installer, "validate_target", lambda: None)

    def fail(*_):
        raise module.DeploymentError("dependency installation failed")

    monkeypatch.setattr(installer, "prepare", fail)
    monkeypatch.setattr(
        installer, "restart", lambda: pytest.fail("Must not restart before preparation")
    )
    with pytest.raises(module.DeploymentError, match="dependency installation failed"):
        installer.install()
    assert installer.current.resolve() == old
    assert not installer.previous.exists()


def test_failed_candidate_restores_old_link_and_checks_recovery(tmp_path, monkeypatch):
    module = load_remote()
    installer, old = make_installer(module, tmp_path)
    new = installer.base / "releases/new"
    new.mkdir()
    restarted = []
    checked = []
    monkeypatch.setattr(installer, "restart", lambda: restarted.append(installer.current.resolve()))

    def health():
        checked.append(installer.current.resolve())
        if installer.current.resolve() == new:
            raise module.DeploymentError("candidate failed")

    monkeypatch.setattr(installer, "healthy", health)
    with pytest.raises(module.DeploymentError, match="previous release restored and healthy"):
        installer.activate(new)
    assert installer.current.resolve() == old
    assert restarted == checked == [new, old]
    assert not installer.previous.exists()


def test_successful_rollback_records_the_release_it_replaced(tmp_path, monkeypatch):
    module = load_remote()
    installer, old = make_installer(module, tmp_path)
    previous = installer.base / "releases/previous"
    previous.mkdir()
    installer.previous.symlink_to(previous)
    monkeypatch.setattr(installer, "validate_target", lambda: None)
    monkeypatch.setattr(installer, "restart", lambda: None)
    monkeypatch.setattr(installer, "healthy", lambda: None)
    installer.rollback()
    assert installer.current.resolve() == previous
    assert installer.previous.resolve() == old


def test_launcher_keeps_legacy_fallback_and_preserves_other_environment(tmp_path):
    module = load_remote()
    installer, _ = make_installer(module, tmp_path)
    before = (installer.base / "runtime.env").read_text()
    installer.prepare_launcher()
    installer.prepare_launcher()
    script = (installer.base / "start-backend.sh").read_text()
    assert "current/.venv/bin/python" in script
    assert (
        'if [ ! -x "$PYTHON" ]; then PYTHON="$DEEPTUTOR_DEPLOY_BASE/venv/bin/python"; fi' in script
    )
    environment = (installer.base / "runtime.env").read_text()
    assert "DEEPTUTOR_HOME=/target\n" in environment
    assert environment.count(str(installer.current / ".venv/bin")) == 1
    assert (installer.base / ".deployment-backups/runtime.env").read_text() == before


def test_launcher_executes_the_current_release_python_or_legacy_fallback(tmp_path):
    module = load_remote()
    installer, old = make_installer(module, tmp_path)
    installer.prepare_launcher()
    launcher = installer.base / "start-backend.sh"

    def fake_python(path, label):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '#!/bin/sh\ncase "$*" in\n'
            "*get_ws_max_size*) echo 1024;;\n"
            "*load_system_settings*) echo 8001;;\n"
            f"*) echo {label};;\nesac\n"
        )
        path.chmod(0o755)

    fake_python(installer.base / "venv/bin/python", "legacy")
    assert subprocess.check_output(["bash", str(launcher)], text=True).strip() == "legacy"
    fake_python(old / ".venv/bin/python", "current-release")
    assert subprocess.check_output(["bash", str(launcher)], text=True).strip() == "current-release"


def test_installer_provides_node_path_to_user_subprocesses(tmp_path, monkeypatch):
    module = load_remote()
    installer, old = make_installer(module, tmp_path)
    installer.args.node = "/opt/node/bin/node"
    captured = []

    def run(command, **_):
        captured.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    installer.as_user(["/opt/node/bin/npm", "--version"], old)
    assert "PATH=/opt/node/bin:/usr/local/bin:/usr/bin:/bin" in captured[0]
    assert not any(value.startswith("HOME=") for value in captured[0])


def test_environment_file_is_preserved_when_atomic_replace_fails(tmp_path, monkeypatch):
    module = load_remote()
    environment = tmp_path / "runtime.env"
    environment.write_text("DEEPTUTOR_HOME=/existing\n")

    def fail_replace(*args):
        raise OSError("simulated write failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated write failure"):
        module.atomic_text(environment, "DEEPTUTOR_HOME=/new\n")
    assert environment.read_text() == "DEEPTUTOR_HOME=/existing\n"
    assert list(tmp_path.iterdir()) == [environment]
