#!/usr/bin/env python3
"""Build, package and deploy DeepTutor source releases to an existing Aliyun ECS."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tarfile

from ecs.build import build_release, package_release
from ecs.cloud import Aliyun, DeployError, Target, remote_action

ROOT = Path(__file__).resolve().parents[1]


def default_release_id() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    commit = result.stdout.strip() if result.returncode == 0 else "source"
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + commit


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("build", "release"):
        command = commands.add_parser(
            name, help="Build in isolation" if name == "build" else "Build, package and deploy"
        )
        command.add_argument("--release-id", default=None)
        command.add_argument("--output", type=Path, default=ROOT / "dist/ecs")
        command.add_argument(
            "--install",
            action="store_true",
            help="Run npm ci instead of reusing local node_modules",
        )
    command = commands.add_parser("package", help="Package a completed release directory")
    command.add_argument("--release", type=Path, required=True)
    command.add_argument("--archive", type=Path)
    command = commands.add_parser("deploy", help="Upload and deploy a verified release archive")
    command.add_argument("--bundle", type=Path, required=True)
    commands.add_parser("rollback", help="Restore the previous release")
    commands.add_parser("status", help="Read current service and release state")
    commands.add_parser("check", help="Read-only ECS/runtime preflight")
    for name in ("release", "deploy", "rollback", "status", "check"):
        command = commands.choices[name]
        command.add_argument("--config", type=Path, default=ROOT / "deploy/ecs.local.json")
        if name in ("release", "deploy", "rollback"):
            command.add_argument(
                "--dry-run",
                action="store_true",
                help="Validate locally and print the plan without cloud writes",
            )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        target = Target.load(args.config) if hasattr(args, "config") else None
        if args.command in ("build", "release"):
            release = build_release(
                ROOT,
                args.output.resolve(),
                args.release_id or default_release_id(),
                install=args.install,
            )
            print(f"Release directory: {release}", flush=True)
            if args.command == "build":
                return 0
            archive = package_release(release, release.parent / (release.name + ".tar.gz"))
            remote_action(target, "install", archive=archive, dry_run=args.dry_run)
        elif args.command == "package":
            release = args.release.resolve()
            archive = args.archive or release.parent / (release.name + ".tar.gz")
            print(package_release(release, archive.resolve()))
        elif args.command == "deploy":
            remote_action(target, "install", archive=args.bundle.resolve(), dry_run=args.dry_run)
        elif args.command == "check":
            Aliyun(target).preflight()
        else:
            remote_action(target, args.command, dry_run=getattr(args, "dry_run", False))
        return 0
    except (
        DeployError,
        ValueError,
        OSError,
        tarfile.TarError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"ECS release failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted. A dispatched cloud command may still be running; check status before retrying.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
