#!/usr/bin/env python3
"""Validate CI candidates without creating a tag, Release, or installation channel."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

from release import (ROOT, PLATFORMS, asset_name, canonical, clean_source,
                     collect_assets, content, digest, identity_matches,
                     output_directory, publish_directory, version_matches)


WASM_VARIANTS = {"developer-package"}


def extension_manifest():
    extension = tomllib.loads(content(ROOT / "extension.toml").decode())
    if extension["id"] != "argocd-lsp":
        raise ValueError("Candidate extension must be argocd-lsp")
    return extension


def candidate_version(commit, requested=""):
    version = tomllib.loads(content(ROOT / "Cargo.toml").decode())["package"]["version"]
    version_matches(version)
    clean_source(commit)
    if requested and requested != version:
        raise ValueError("Requested release version must match the candidate version")
    return version


def source_files():
    names = {"Cargo.toml", "Cargo.lock", "extension.toml", "rust-toolchain.toml", "LICENSE"}
    for folder in ("src", "snippets"):
        names.update(path.relative_to(ROOT).as_posix() for path in (ROOT / folder).rglob("*") if path.is_file())
    return {name: digest(content(ROOT / name)) for name in sorted(names)}


def native_binary(folder, version, commit):
    records = list(folder.glob("release-*.json"))
    if len(records) != 1:
        raise ValueError("Candidate needs one staged native release record per host")
    record = json.loads(content(records[0], 4 * 1024 * 1024))
    platform = record.get("platform")
    if record.get("version") != version or record.get("sourceCommit") != commit:
        raise ValueError("Candidate native version or source commit mismatch")
    identity_matches(record["package"]["identity"], version, commit)
    name = asset_name(version, platform)
    if record.get("binaryAsset") != name:
        raise ValueError("Candidate native asset name mismatch")
    binary = content(folder / name)
    expected = record.get("binarySha256", record["package"].get("binarySHA256"))
    if not binary or digest(binary) != expected:
        raise ValueError("Candidate native SHA-256 mismatch")
    return platform, binary


def check_wasm(data, native_binaries):
    if len(data) <= 8 or data[:8] != b"\0asm\x0d\0\x01\0":
        raise ValueError("Candidate adapter must be a wasm32-wasip2 component")
    # Detect the actual raw native payload, including an include_bytes! payload
    # inside a valid component section; the aggregate checks all five binaries.
    if any(binary[:64] in data for binary in native_binaries):
        raise ValueError("Native payload found inside candidate adapter")
    offset = 8
    while offset < len(data):
        section = data[offset]
        offset += 1
        size = 0
        for index in range(5):
            if offset >= len(data):
                raise ValueError("Truncated candidate WASM section")
            byte = data[offset]
            offset += 1
            size |= (byte & 0x7f) << (7 * index)
            if not byte & 0x80:
                break
        else:
            raise ValueError("Invalid candidate WASM section length")
        if section > 11 or size > len(data) - offset:
            raise ValueError("Invalid candidate WASM component framing")
        offset += size


def adapter_metadata(version, commit, platform):
    extension = extension_manifest()
    return {"schemaVersion": 1, "kind": "adapter-ci-candidate", "extensionId": extension["id"],
            "repository": extension["repository"], "version": version, "sourceCommit": commit,
            "buildHost": platform, "target": "wasm32-wasip2", "sourceFiles": source_files(),
            "rustToolchain": tomllib.loads(content(ROOT / "rust-toolchain.toml").decode())["toolchain"],
            "editorAcceptance": "not-executed", "installationChannel": "none"}


def stage_adapter(native_assets, wasm_inputs, output, version, commit, platform):
    version_matches(version)
    clean_source(commit)
    native_platform, binary = native_binary(native_assets, version, commit)
    if platform != native_platform:
        raise ValueError("Adapter build host differs from its native release platform")
    paths = {}
    for item in wasm_inputs:
        name, separator, path = item.partition("=")
        if not separator or name not in WASM_VARIANTS or name in paths:
            raise ValueError("Unexpected or duplicate candidate WASM variant")
        paths[name] = Path(path)
    if set(paths) != WASM_VARIANTS:
        raise ValueError("Missing required candidate WASM variant")
    files, inventory = {}, {}
    for name, path in sorted(paths.items()):
        data = content(path, 16 * 1024 * 1024)
        check_wasm(data, [binary])
        files[name + ".wasm"] = data
        inventory[name + ".wasm"] = {"sha256": digest(data), "bytes": len(data)}
    record = adapter_metadata(version, commit, platform)
    record.update(wasm=inventory, nativeBinarySha256=digest(binary))
    files["adapter.json"] = canonical(record)
    clean_source(commit)
    publish_directory(output, files)
    return record


def collect_candidate(native_inputs, adapter_inputs, output, version, commit):
    version_matches(version)
    clean_source(commit)
    output = output_directory(output)
    with tempfile.TemporaryDirectory(prefix=".candidate-", dir=output.parent) as temporary:
        stage = Path(temporary) / "candidate"
        stage.mkdir()
        # The existing strict collector owns native hashes, all legal archives,
        # provenance and the five-platform completeness gate.
        native = collect_assets(native_inputs, stage / "native", version, commit)
        binaries = {platform: content(stage / "native" / asset_name(version, platform)) for platform in PLATFORMS}
        seen = set()
        for path in sorted(adapter_inputs.glob("*/adapter.json")):
            record = json.loads(content(path, 4 * 1024 * 1024))
            platform = record.get("buildHost")
            if platform not in PLATFORMS or platform in seen:
                raise ValueError("Unsupported or duplicate candidate adapter host")
            expected = adapter_metadata(version, commit, platform)
            if any(record.get(key) != value for key, value in expected.items()):
                raise ValueError("Candidate adapter source identity or metadata mismatch")
            if record.get("nativeBinarySha256") != digest(binaries[platform]):
                raise ValueError("Candidate adapter/native build mismatch")
            names = {name + ".wasm" for name in WASM_VARIANTS}
            if (set(record.get("wasm", {})) != names
                    or {item.name for item in path.parent.iterdir()} != names | {"adapter.json"}):
                raise ValueError("Unexpected or missing candidate adapter files; native payloads are forbidden")
            destination = stage / "adapters" / platform.replace("/", "-")
            destination.mkdir(parents=True)
            for name in sorted(names):
                data = content(path.parent / name, 16 * 1024 * 1024)
                if record["wasm"][name] != {"sha256": digest(data), "bytes": len(data)}:
                    raise ValueError("Candidate adapter SHA-256 or size mismatch")
                check_wasm(data, binaries.values())
                (destination / name).write_bytes(data)
            (destination / "adapter.json").write_bytes(canonical(record))
            seen.add(platform)
        if seen != PLATFORMS:
            raise ValueError("Candidate requires adapter evidence from all five native hosts")
        files = {path.relative_to(stage).as_posix(): digest(content(path))
                 for path in sorted(stage.rglob("*")) if path.is_file()}
        extension = extension_manifest()
        report = {"schemaVersion": 1, "kind": "distribution-ci-candidate", "extensionId": extension["id"],
                  "version": version, "sourceCommit": commit, "platforms": native["platforms"],
                  "nativeBuildMode": "release", "files": dict(files),
                  "editorAcceptance": "not-executed", "installationChannel": "none",
                  "publication": "not-published", "nativePayloadCheck": "strict adapter file inventory and all five native binary signatures"}
        (stage / "candidate.json").write_bytes(canonical(report))
        files["candidate.json"] = digest(content(stage / "candidate.json"))
        (stage / "SHA256SUMS").write_text("".join(f"{value}  {name}\n" for name, value in sorted(files.items())), encoding="utf-8")
        clean_source(commit)
        stage.rename(output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    version = sub.add_parser("version")
    version.add_argument("--requested-version", default="")
    version.add_argument("--source-commit", required=True)
    for name in ("adapter", "collect"):
        command = sub.add_parser(name)
        command.add_argument("--version", required=True)
        command.add_argument("--source-commit", required=True)
        command.add_argument("--output", type=Path, required=True)
        if name == "adapter":
            command.add_argument("--native-assets", type=Path, required=True)
            command.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
            command.add_argument("--wasm", action="append", required=True, help="variant=path to an existing compiled component")
        else:
            command.add_argument("--native-inputs", type=Path, required=True)
            command.add_argument("--adapter-inputs", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "version":
            print("version=" + candidate_version(args.source_commit, args.requested_version))
        elif args.command == "adapter":
            print(json.dumps(stage_adapter(args.native_assets, args.wasm, args.output, args.version, args.source_commit, args.platform)))
        else:
            print(json.dumps(collect_candidate(args.native_inputs, args.adapter_inputs, args.output, args.version, args.source_commit)))
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
