#!/usr/bin/env python3
"""Stage and join native release assets; building and legal packaging stay in native.py."""

import argparse
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

from native import ROOT, PLATFORMS, canonical, content, digest


def version_matches(version):
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError("Release version must be a stable semantic version")
    cargo = tomllib.loads(content(ROOT / "Cargo.toml").decode())
    extension = tomllib.loads(content(ROOT / "extension.toml").decode())
    if cargo["package"]["version"] != version or extension["version"] != version:
        raise ValueError("Release version must match Cargo.toml and extension.toml")


def clean_source(commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected an exact source commit")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT)
    if head != commit or dirty:
        raise ValueError("Release assets require the exact clean committed source")


def identity_matches(identity, version, commit):
    required = {"product": "argo-cd", "serverVersion": version,
                "adapterContractVersion": 1, "analysisReady": True,
                "sourceCommit": commit, "dirty": False, "buildMode": "release"}
    if any(identity.get(key) != value for key, value in required.items()):
        raise ValueError("Native identity must match the clean pinned release and source commit")
    if identity.get("configSchemaVersions") != [1] or identity.get("reportSchemaVersions") != [1]:
        raise ValueError("Unsupported native release capabilities")


def asset_name(version, platform):
    if platform not in PLATFORMS:
        raise ValueError("Unsupported release platform")
    return f"argocd-ls-{version}-{platform.replace('/', '-')}" + (".exe" if platform.startswith("windows/") else "")


def output_directory(output):
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output must not already exist")
    try:
        relative = output.relative_to(ROOT)
    except ValueError:
        relative = None
    if relative is not None and (not relative.parts or relative.parts[0] not in ("artifacts", "build", "dist")):
        raise ValueError("In-checkout outputs must be under artifacts, build or dist")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def publish_directory(output, files):
    output = output_directory(output)
    with tempfile.TemporaryDirectory(prefix=".release-stage-", dir=output.parent) as temporary:
        stage = Path(temporary) / "assets"
        stage.mkdir()
        for name, value in sorted(files.items()):
            path = stage / name
            path.write_bytes(value)
            path.chmod(0o755 if name.startswith("argocd-ls-") and not name.endswith((".zip", ".tar.gz")) else 0o644)
        stage.rename(output)


def stage_assets(manifest, output, version, commit):
    version_matches(version)
    clean_source(commit)
    manifest = manifest.absolute()
    build = json.loads(content(manifest, 4 * 1024 * 1024))
    identity_matches(build.get("identity", {}), version, commit)
    platform = build.get("platform")
    binary_name = asset_name(version, platform)
    # The existing packager validates actual Go metadata, runs the native identity
    # command, and checks the complete legal inventory. Do not duplicate it here.
    with tempfile.TemporaryDirectory(prefix="argocd-release-package-") as temporary:
        package_dir = Path(temporary) / "package"
        command = [sys.executable, str(ROOT / "tools/native.py"), "package", "--manifest", str(manifest), "--output", str(package_dir)]
        process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=180)
        if process.returncode:
            raise ValueError("Existing native package verification failed: " + process.stderr.strip())
        package = json.loads(content(package_dir / "package.json", 4 * 1024 * 1024))
        binary = content(manifest.parent / build["binary"])
        archive = content(package_dir / package["archive"])
        if digest(binary) != package["binarySHA256"] or digest(archive) != package["archiveSHA256"]:
            raise ValueError("Release inputs changed during staging")
        record = {"schemaVersion": 1, "product": "argo-cd", "platform": platform,
                  "version": version, "sourceCommit": commit, "binaryAsset": binary_name,
                  "archiveAsset": package["archive"], "package": package}
        record_name = f"release-{platform.replace('/', '-')}.json"
        files = {binary_name: binary, package["archive"]: archive, record_name: canonical(record)}
        # Ensure the staged raw download and preserved legal archive are exactly
        # the same native build before making any output directory visible.
        validate_record(record, files, version, commit)
        publish_directory(output, files)
    return {"platform": platform, "version": version, "files": sorted(files)}


def archive_files(data, archive_name, root):
    files = {}
    total = 0

    def add(name, value):
        nonlocal total
        relative = PurePosixPath(name)
        if not name.startswith(root + "/") or relative.is_absolute() or ".." in relative.parts or "\\" in name or name in files:
            raise ValueError("Unsafe or duplicate release archive member")
        total += len(value)
        if total > 256 * 1024 * 1024:
            raise ValueError("Release archive content exceeds its size limit")
        files[name] = value

    if archive_name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.infolist():
                if entry.is_dir() or (entry.external_attr >> 16) & 0o170000 != 0o100000 or entry.file_size > 128 * 1024 * 1024:
                    raise ValueError("Release archive must contain bounded regular files")
                add(entry.filename, archive.read(entry))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for entry in archive:
                if not entry.isfile() or entry.size > 128 * 1024 * 1024:
                    raise ValueError("Release archive must contain bounded regular files")
                stream = archive.extractfile(entry)
                if stream is None:
                    raise ValueError("Missing release archive file data")
                add(entry.name, stream.read())
    return {name[len(root) + 1:]: value for name, value in files.items()}


def validate_record(record, files, version, commit):
    platform = record.get("platform")
    binary_name = asset_name(version, platform)
    root = f"argocd-ls-{version}-{platform.replace('/', '-')}"
    archive_name = root + (".zip" if platform.startswith("windows/") else ".tar.gz")
    if record.get("schemaVersion") != 1 or record.get("product") != "argo-cd" or record.get("version") != version or record.get("sourceCommit") != commit:
        raise ValueError("Release record version, product or source commit mismatch")
    if record.get("binaryAsset") != binary_name or record.get("archiveAsset") != archive_name:
        raise ValueError("Release asset name or platform mismatch")
    package = record["package"]
    identity_matches(package.get("identity", {}), version, commit)
    if package.get("root") != root or package.get("archive") != archive_name or package.get("platform") != platform:
        raise ValueError("Native package platform or layout mismatch")
    binary, archive = files[binary_name], files[archive_name]
    if not binary or digest(binary) != package.get("binarySHA256") or digest(archive) != package.get("archiveSHA256"):
        raise ValueError("Release binary or archive SHA-256 mismatch")
    members = archive_files(archive, archive_name, root)
    if {name: digest(value) for name, value in members.items()} != package.get("files"):
        raise ValueError("Release archive file inventory or checksum mismatch")
    required = {"LICENSE", "NOTICE.txt", "licenses/go/LICENSE", "licenses/go/PATENTS", "provenance/build.json"}
    if not required.issubset(members) or not any(name.startswith("licenses/argo-cd/") for name in members):
        raise ValueError("Release archive is missing required legal or provenance files")
    executable = "argocd-ls.exe" if platform.startswith("windows/") else "argocd-ls"
    if members.get(executable) != binary:
        raise ValueError("Raw release binary differs from the legal archive")
    provenance_bytes = members["provenance/build.json"]
    provenance = json.loads(provenance_bytes)
    if digest(provenance_bytes) != package.get("buildManifestSHA256"):
        raise ValueError("Release build manifest checksum mismatch")
    if provenance.get("identity") != package["identity"] or provenance.get("platform") != platform or provenance.get("binarySHA256") != digest(binary):
        raise ValueError("Archived build provenance differs from release assets")


def collect_assets(inputs, output, version, commit):
    version_matches(version)
    clean_source(commit)
    records = sorted(inputs.glob("*/release-*.json"))
    platforms, files = set(), {}
    for path in records:
        record = json.loads(content(path, 4 * 1024 * 1024))
        platform = record.get("platform")
        if platform in platforms:
            raise ValueError("Duplicate release platform")
        expected_names = {record.get("binaryAsset"), record.get("archiveAsset"), path.name}
        if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name) for name in expected_names):
            raise ValueError("Unsafe release asset filename")
        if {p.name for p in path.parent.iterdir()} != expected_names:
            raise ValueError("Unexpected or missing platform release files")
        current = {name: content(path.parent / name) for name in expected_names}
        validate_record(record, current, version, commit)
        if set(files) & set(current):
            raise ValueError("Duplicate release asset filename")
        platforms.add(platform)
        files.update(current)
    if platforms != PLATFORMS or len(records) != len(PLATFORMS):
        raise ValueError("Release requires all five native platforms")
    files["SHA256SUMS"] = "".join(f"{digest(value)}  {name}\n" for name, value in sorted(files.items())).encode()
    publish_directory(output, files)
    return {"version": version, "sourceCommit": commit, "platforms": sorted(platforms), "files": sorted(files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    tag = sub.add_parser("version")
    tag.add_argument("--tag", required=True)
    tag.add_argument("--source-commit", required=True)
    for name in ("stage", "collect"):
        command = sub.add_parser(name)
        command.add_argument("--version", required=True)
        command.add_argument("--source-commit", required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--manifest" if name == "stage" else "--inputs", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "version":
            if not args.tag.startswith("v"):
                raise ValueError("Release tag must start with v")
            version_matches(args.tag[1:])
            clean_source(args.source_commit)
            print("version=" + args.tag[1:])
        elif args.command == "stage":
            print(json.dumps(stage_assets(args.manifest, args.output, args.version, args.source_commit)))
        else:
            print(json.dumps(collect_assets(args.inputs, args.output, args.version, args.source_commit)))
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
