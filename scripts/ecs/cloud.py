"""Aliyun Cloud Assistant transport for an existing DeepTutor ECS installation."""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid


class DeployError(RuntimeError):
    pass


@dataclass
class Target:
    instance_id: str
    host: str
    region: str = "cn-hangzhou"
    profile: str = "default"
    base: str = "/opt/deeptutor"
    user: str = "deeptutor-deploy"
    service: str = "deeptutor-ecs.service"
    python: str = "/usr/bin/python3.11"
    node: str = "/usr/local/bin/node"
    uv: str = "/opt/deeptutor/bin/uv"
    pip_index: str = "https://mirrors.aliyun.com/pypi/simple/"
    npm_registry: str = "https://registry.npmmirror.com"
    public_url: str = ""

    def __post_init__(self) -> None:
        ipaddress.IPv4Address(self.host)
        for value in (self.instance_id, self.region, self.user, self.service):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", value):
                raise ValueError(f"Invalid target identifier: {value!r}")
        for value in (self.base, self.python, self.node, self.uv):
            if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in Path(value).parts:
                raise ValueError(f"Expected a simple absolute target path: {value!r}")
        if self.base in ("/", "/opt", "/usr", "/etc", "/var"):
            raise ValueError("base must name the application's own directory")
        for value in (self.pip_index, self.npm_registry):
            if not value.startswith("https://") or "@" in value:
                raise ValueError("Package indexes must use HTTPS without embedded credentials")

    @classmethod
    def load(cls, path: Path) -> Target:
        if not path.is_file():
            raise DeployError(
                f"Missing {path}; copy deploy/ecs.example.json and fill in the target"
            )
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except TypeError as exc:
            raise DeployError(f"Invalid ECS target configuration: {exc}") from exc


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_bundle(archive: Path) -> tuple[dict, str]:
    checksum = Path(str(archive) + ".sha256")
    if not archive.is_file() or not checksum.is_file():
        raise DeployError("Both the release .tar.gz and its .sha256 file are required")
    digest = sha256(archive)
    checksum_parts = checksum.read_text().split()
    if not checksum_parts or checksum_parts[0].lower() != digest:
        raise DeployError("Release archive SHA256 mismatch")
    with tarfile.open(archive, "r:gz") as package:
        try:
            member = package.getmember("manifest.json")
        except KeyError as exc:
            raise DeployError("Release archive has no manifest.json") from exc
        if not member.isfile() or member.size > 8 * 1024 * 1024:
            raise DeployError("Invalid release manifest")
        source = package.extractfile(member)
        assert source is not None
        manifest = json.load(source)
    release_id = manifest.get("release_id", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", release_id):
        raise DeployError("Invalid release_id in manifest")
    if manifest.get("schema_version") != 1 or manifest.get("target_platform") != "linux-x64":
        raise DeployError("This deployer requires a schema 1 linux-x64 release")
    return manifest, digest


class Aliyun:
    def __init__(self, target: Target):
        self.target = target

    def api(self, action: str, *args: str) -> dict:
        cmd = [
            "aliyun",
            "ecs",
            action,
            "--profile",
            self.target.profile,
            "--RegionId",
            self.target.region,
            *args,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode:
            raise DeployError(
                f"Aliyun {action} failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        return json.loads(result.stdout)

    def run(self, script: str, *, timeout: int = 120, name: str = "deeptutor-deploy") -> str:
        encoded = base64.b64encode(script.encode()).decode()
        if len(encoded) > 24 * 1024:
            raise DeployError("Cloud Assistant command exceeds its 24 KiB limit")
        started = self.api(
            "RunCommand",
            "--InstanceId.1",
            self.target.instance_id,
            "--Type",
            "RunShellScript",
            "--ContentEncoding",
            "Base64",
            "--CommandContent",
            encoded,
            "--Timeout",
            str(timeout),
            "--TerminationMode",
            "ProcessTree",
            "--KeepCommand",
            "false",
            "--Name",
            name,
        )
        invoke_id = started["InvokeId"]
        print(f"Cloud Assistant: {invoke_id}", flush=True)
        deadline, previous = time.monotonic() + timeout + 45, ""
        while time.monotonic() < deadline:
            response = self.api("DescribeInvocationResults", "--InvokeId", invoke_id)
            rows = (
                response.get("Invocation", {})
                .get("InvocationResults", {})
                .get("InvocationResult", [])
            )
            row = next((r for r in rows if r.get("InstanceId") == self.target.instance_id), None)
            if row:
                output = base64.b64decode(row.get("Output", "")).decode(errors="replace")
                if output != previous:
                    print(
                        output[len(previous) :] if output.startswith(previous) else output,
                        end="",
                        flush=True,
                    )
                    previous = output
                if row.get("InvocationStatus") not in (
                    "Pending",
                    "Running",
                    "Scheduled",
                    "Stopping",
                ):
                    if row.get("InvocationStatus") != "Success" or row.get("ExitCode", 0) != 0:
                        raise DeployError(
                            f"Remote command failed ({invoke_id}, {row.get('InvocationStatus')}, "
                            f"exit {row.get('ExitCode')})"
                        )
                    return output
            time.sleep(2)
        raise DeployError(f"Timed out waiting for {invoke_id}; check its status before retrying")

    def preflight(self) -> dict:
        response = self.api(
            "DescribeInstances", "--InstanceIds", json.dumps([self.target.instance_id])
        )
        instances = response.get("Instances", {}).get("Instance", [])
        if len(instances) != 1 or instances[0].get("Status") != "Running":
            raise DeployError("The configured ECS instance is not running or not accessible")
        instance = instances[0]
        addresses = instance.get("PublicIpAddress", {}).get("IpAddress", [])
        addresses += [instance.get("EipAddress", {}).get("IpAddress", "")]
        if self.target.host not in addresses:
            raise DeployError("Configured SSH host does not match the ECS instance's public IP")
        settings = asdict(self.target)
        script = (
            "python3.11 - <<'PREFLIGHT'\n"
            + f"settings = {settings!r}\n"
            + """
import json, os, pathlib, platform, pwd, subprocess
base = pathlib.Path(settings['base'])
if platform.machine() not in ('x86_64', 'AMD64'):
    raise SystemExit('This bundle targets Linux x86_64')
account = pwd.getpwnam(settings['user'])
if account.pw_uid == 0:
    raise SystemExit('The application deployment user must be non-root')
for name in ('runtime.env', 'start-backend.sh', 'current'):
    if not (base / name).exists():
        raise SystemExit('Existing source deployment is missing: ' + name)
for key in ('python', 'node', 'uv'):
    subprocess.run([settings[key], '--version'], check=True)
unit = subprocess.run(['systemctl', 'show', settings['service'], '--property=LoadState,ActiveState'],
                      check=True, capture_output=True, text=True)
print(unit.stdout, end='')
if 'LoadState=loaded' not in unit.stdout.splitlines():
    raise SystemExit('The configured systemd service is not installed')
print('PREFLIGHT_JSON=' + json.dumps({
    'host_key': pathlib.Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().strip(),
    'current': str((base / 'current').resolve()),
    'free_bytes': __import__('shutil').disk_usage(base).free,
}))
PREFLIGHT
"""
        )
        output = self.run(script, name="deeptutor-deploy-preflight")
        for line in output.splitlines():
            if line.startswith("PREFLIGHT_JSON="):
                return json.loads(line.removeprefix("PREFLIGHT_JSON="))
        raise DeployError("Preflight did not return the ECS host key")


def _python(script: str) -> str:
    return "python3.11 - <<'DEEPTUTOR_TRANSPORT'\n" + script + "\nDEEPTUTOR_TRANSPORT\n"


def _remove_key(cloud: Aliyun, marker: str) -> None:
    cloud.run(
        _python(f"""
from pathlib import Path
import fcntl, pwd
root = Path('/var/lib/deeptutor-deploy')
root.mkdir(mode=0o700, exist_ok=True)
lock = (root / 'ssh-keys.lock').open('a')
fcntl.flock(lock, fcntl.LOCK_EX)
p = Path(pwd.getpwnam({cloud.target.user!r}).pw_dir) / '.ssh/authorized_keys'
if p.exists():
    lines = [line for line in p.read_text().splitlines() if line.split()[-1:] != [{marker!r}]]
    p.write_text(chr(10).join(lines) + chr(10))
"""),
        name="deeptutor-upload-key-remove",
    )


def _cleanup_upload(cloud: Aliyun, remote_dir: str) -> None:
    try:
        cloud.run(
            _python(f"import shutil\nshutil.rmtree({remote_dir!r}, ignore_errors=True)"),
            name="deeptutor-upload-cleanup",
        )
    except Exception as exc:
        print(f"Could not clean temporary upload directory {remote_dir}: {exc}", file=sys.stderr)


def upload(cloud: Aliyun, files: list[Path], host_key: str, nonce: str) -> str:
    """Use an expiring SFTP-only key; never loosen the permanent tunnel key."""
    for program in ("ssh-keygen", "sftp"):
        if not shutil.which(program):
            raise DeployError(f"Required local program is missing: {program}")
    remote_dir = f"{cloud.target.base}/incoming/{nonce}"
    marker = f"deeptutor-release-{nonce}"
    with tempfile.TemporaryDirectory(prefix="deeptutor-upload-") as temporary:
        work = Path(temporary)
        key = work / "key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key), "-C", marker],
            check=True,
        )
        public_key = key.with_suffix(".pub").read_text().strip()
        known_hosts = work / "known_hosts"
        known_hosts.write_text(f"{cloud.target.host} {host_key}\n")
        grant = f"""
from pathlib import Path
import datetime, fcntl, os, pwd
root = Path('/var/lib/deeptutor-deploy')
root.mkdir(mode=0o700, exist_ok=True)
lock = (root / 'ssh-keys.lock').open('a')
fcntl.flock(lock, fcntl.LOCK_EX)
u = pwd.getpwnam({cloud.target.user!r})
incoming = Path({remote_dir!r})
incoming.mkdir(parents=True, exist_ok=False)
os.chown(incoming, u.pw_uid, u.pw_gid)
ssh = Path(u.pw_dir) / '.ssh'
ssh.mkdir(mode=0o700, exist_ok=True)
os.chown(ssh, u.pw_uid, u.pw_gid)
auth = ssh / 'authorized_keys'
expiry = (datetime.datetime.now().astimezone() + datetime.timedelta(minutes=20)).strftime('%Y%m%d%H%M%S')
line = 'restrict,command="internal-sftp",expiry-time="' + expiry + '" ' + {public_key!r}
with auth.open('a') as stream:
    stream.write(chr(10) + line + chr(10))
os.chmod(auth, 0o600)
os.chown(auth, u.pw_uid, u.pw_gid)
"""
        # Even an ambiguous/failed grant is followed by a removal attempt.
        try:
            cloud.run(_python(grant), name="deeptutor-upload-key-add")

            def quote(value: str) -> str:
                if "\n" in value or "\r" in value:
                    raise DeployError("SFTP paths must not contain newlines")
                return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

            batch = work / "sftp.batch"
            batch.write_text(
                "\n".join(
                    f"put {quote(str(p.resolve()))} {quote(remote_dir + '/' + p.name)}"
                    for p in files
                )
                + "\n"
            )
            subprocess.run(
                [
                    "sftp",
                    "-q",
                    "-b",
                    str(batch),
                    "-i",
                    str(key),
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    f"UserKnownHostsFile={known_hosts}",
                    "-o",
                    "GlobalKnownHostsFile=/dev/null",
                    "-o",
                    "HostKeyAlgorithms=ssh-ed25519",
                    "-o",
                    "ConnectTimeout=15",
                    "-o",
                    "ServerAliveInterval=15",
                    "-o",
                    "ServerAliveCountMax=3",
                    f"{cloud.target.user}@{cloud.target.host}",
                ],
                check=True,
            )
        except BaseException:
            _cleanup_upload(cloud, remote_dir)
            raise
        finally:
            already_failing = sys.exc_info()[0] is not None
            try:
                _remove_key(cloud, marker)
            except Exception as exc:
                _cleanup_upload(cloud, remote_dir)
                if not already_failing:
                    raise
                print(
                    f"Temporary SFTP key removal failed; it expires after 20 minutes: {exc}",
                    file=sys.stderr,
                )
    return remote_dir


def remote_action(
    target: Target, action: str, *, archive: Path | None = None, dry_run: bool = False
) -> None:
    manifest, digest = inspect_bundle(archive) if archive else ({}, "")
    plan = {
        "action": action,
        "instance_id": target.instance_id,
        "host": target.host,
        "base": target.base,
        "service": target.service,
        "release_id": manifest.get("release_id"),
        "archive_sha256": digest or None,
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    if dry_run:
        print("Dry run: no cloud commands, uploads or release switches were performed.")
        return
    cloud = Aliyun(target)
    preflight = cloud.preflight()
    if action == "status":
        cloud.run(
            _python(f"""
from pathlib import Path
import json, subprocess
base = Path({target.base!r})
print(json.dumps({{name: str((base / name).resolve()) if (base / name).exists() else None
                  for name in ('current', 'previous')}}, indent=2))
subprocess.run(['systemctl', 'show', {target.service!r}, 'deeptutor-backend.service',
                'deeptutor-frontend.service', '--property=Id,ActiveState,SubState,MemoryCurrent,NRestarts'], check=True)
"""),
            name="deeptutor-release-status",
        )
        return
    if archive and preflight["free_bytes"] < 2 * 1024**3:
        raise DeployError(
            "At least 2 GiB of free ECS disk space is required before preparing a release"
        )
    helper = Path(__file__).with_name("remote.py")
    files = [helper] + ([archive] if archive else [])
    nonce = uuid.uuid4().hex
    remote_dir = upload(cloud, files, preflight["host_key"], nonce)
    args = [
        action,
        "--base",
        target.base,
        "--user",
        target.user,
        "--service",
        target.service,
        "--python",
        target.python,
        "--node",
        target.node,
        "--uv",
        target.uv,
        "--pip-index",
        target.pip_index,
        "--npm-registry",
        target.npm_registry,
    ]
    if archive:
        args += [
            "--archive",
            f"/var/lib/deeptutor-deploy/{nonce}/{archive.name}",
            "--sha256",
            digest,
            "--release-id",
            manifest["release_id"],
        ]
    hashes = {p.name: sha256(p) for p in files}
    worker = f"""
from pathlib import Path
import hashlib, os, shutil, subprocess
incoming = Path({remote_dir!r})
protected = Path('/var/lib/deeptutor-deploy') / {nonce!r}
protected.parent.mkdir(mode=0o700, exist_ok=True)
protected.mkdir(mode=0o700)
try:
    for name, digest in {hashes!r}.items():
        destination = protected / name
        shutil.copyfile(incoming / name, destination)
        os.chmod(destination, 0o600)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise SystemExit('Uploaded file SHA256 mismatch: ' + name)
    result = subprocess.run([{target.python!r}, '-u', str(protected / 'remote.py')] + {args!r})
    raise SystemExit(result.returncode)
finally:
    shutil.rmtree(protected)
    shutil.rmtree(incoming)
"""
    try:
        cloud.run(_python(worker), timeout=1800, name=f"deeptutor-{action}")
    except BaseException:
        _cleanup_upload(cloud, remote_dir)
        raise
