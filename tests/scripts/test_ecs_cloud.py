from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


@pytest.fixture
def module():
    path = Path(__file__).resolve().parents[2] / "scripts/ecs/cloud.py"
    spec = importlib.util.spec_from_file_location("ecs_cloud_under_test", path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    try:
        spec.loader.exec_module(loaded)
        yield loaded
    finally:
        sys.modules.pop(spec.name, None)


def bundle(tmp_path: Path) -> Path:
    manifest = json.dumps(
        {"schema_version": 1, "release_id": "release-1", "target_platform": "linux-x64"}
    ).encode()
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("manifest.json")
        member.size = len(manifest)
        output.addfile(member, io.BytesIO(manifest))
    Path(str(archive) + ".sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
    return archive


def test_bundle_rejects_tampering_before_any_cloud_calls(module, tmp_path, monkeypatch):
    archive = bundle(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"tampered")
    monkeypatch.setattr(module, "Aliyun", lambda _: pytest.fail("must not contact Aliyun"))
    with pytest.raises(module.DeployError, match="SHA256 mismatch"):
        module.remote_action(module.Target("i-test", "203.0.113.1"), "install", archive=archive)


def test_dry_run_is_offline_and_does_not_create_upload_keys(module, tmp_path, monkeypatch, capsys):
    archive = bundle(tmp_path)
    monkeypatch.setattr(module, "Aliyun", lambda _: pytest.fail("dry run must remain offline"))
    module.remote_action(
        module.Target("i-test", "203.0.113.1"), "install", archive=archive, dry_run=True
    )
    assert '"release_id": "release-1"' in capsys.readouterr().out


def test_success_status_with_failed_exit_is_not_reported_as_success(module, monkeypatch):
    client = module.Aliyun(module.Target("i-test", "203.0.113.1"))
    responses = iter(
        [
            {"InvokeId": "t-test"},
            {
                "Invocation": {
                    "InvocationResults": {
                        "InvocationResult": [
                            {
                                "InstanceId": "i-test",
                                "InvocationStatus": "Success",
                                "ExitCode": 7,
                                "Output": base64.b64encode(b"preflight failed\n").decode(),
                            },
                        ]
                    }
                }
            },
        ]
    )
    monkeypatch.setattr(client, "api", lambda *args: next(responses))
    with pytest.raises(module.DeployError, match="exit 7"):
        client.run("exit 7")


def test_upload_failure_removes_key_and_incoming_directory(module, tmp_path, monkeypatch):
    calls = []

    class Cloud:
        target = module.Target("i-test", "203.0.113.1")

        def run(self, script, **kwargs):
            calls.append(kwargs["name"])

    monkeypatch.setattr(module.shutil, "which", lambda name: f"/usr/bin/{name}")

    def run(command, **kwargs):
        if command[0] == "ssh-keygen":
            Path(command[command.index("-f") + 1] + ".pub").write_text("ssh-ed25519 AAAA marker")
        else:
            raise subprocess.CalledProcessError(255, command)

    monkeypatch.setattr(module.subprocess, "run", run)
    source = tmp_path / "release.tar.gz"
    source.write_bytes(b"release")
    with pytest.raises(subprocess.CalledProcessError):
        module.upload(Cloud(), [source], "ssh-ed25519 HOSTKEY", "abc123")
    assert "deeptutor-upload-key-add" in calls
    assert "deeptutor-upload-key-remove" in calls
    assert "deeptutor-upload-cleanup" in calls


def test_status_never_uploads_or_changes_authorized_keys(module, monkeypatch):
    calls = []

    class Cloud:
        def __init__(self, target):
            pass

        def preflight(self):
            return {}

        def run(self, script, **kwargs):
            calls.append(kwargs["name"])

    monkeypatch.setattr(module, "Aliyun", Cloud)
    monkeypatch.setattr(module, "upload", lambda *args: pytest.fail("status must not upload"))
    module.remote_action(module.Target("i-test", "203.0.113.1"), "status")
    assert calls == ["deeptutor-release-status"]


def test_preflight_rejects_wrong_instance_ip_before_remote_access(module, monkeypatch):
    client = module.Aliyun(module.Target("i-test", "203.0.113.1"))
    monkeypatch.setattr(
        client,
        "api",
        lambda *args: {
            "Instances": {
                "Instance": [
                    {"Status": "Running", "PublicIpAddress": {"IpAddress": ["203.0.113.2"]}},
                ]
            }
        },
    )
    monkeypatch.setattr(
        client, "run", lambda *args: pytest.fail("must not run on an unexpected host")
    )
    with pytest.raises(module.DeployError, match="does not match"):
        client.preflight()


def test_generated_key_removal_preserves_other_authorizations(module, tmp_path):
    home = tmp_path / "home"
    authorized = home / ".ssh/authorized_keys"
    authorized.parent.mkdir(parents=True)
    original = 'command="/bin/false" ssh-ed25519 AAAA permanent-tunnel\n'
    second = "ssh-ed25519 BBBB another-user-key\n"
    authorized.write_text(original + "ssh-ed25519 CCCC one-time-marker\n" + second)

    class Cloud:
        target = module.Target("i-test", "203.0.113.1", user="test-user")

        def run(self, script, **kwargs):
            body = script.partition("\n")[2].rsplit("\nDEEPTUTOR_TRANSPORT", 1)[0]
            body = body.replace(
                "Path('/var/lib/deeptutor-deploy')", f"Path({str(tmp_path / 'private')!r})"
            ).replace("Path(pwd.getpwnam('test-user').pw_dir)", f"Path({str(home)!r})")
            assert "pwd.getpwnam(" not in body
            exec(compile(body, "generated-cloud-command", "exec"), {})

    module._remove_key(Cloud(), "one-time-marker")
    assert authorized.read_text() == original + second
