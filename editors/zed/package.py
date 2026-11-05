#!/usr/bin/env python3
"""Package the standalone Zed adapter, including its original dependency notices."""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
import tomllib
from urllib.parse import urlparse
import zipfile

ROOT = Path(__file__).resolve().parent
EXTENSION_ID = "argocd-lsp"
SERVER_ID = "argocd-ls"
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:alpha|beta|rc)\.(?:0|[1-9][0-9]*))?"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def content(path, limit=32 * 1024 * 1024):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Expected a bounded regular file: " + str(path))
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("File exceeds its size limit: " + str(path))
    return value


def identity(tag=None):
    cargo = tomllib.loads(content(ROOT / "Cargo.toml").decode())
    manifest = tomllib.loads(content(ROOT / "extension.toml").decode())
    version = manifest["version"]
    native = content(ROOT / "native-server-version").decode().strip()
    if not re.fullmatch(VERSION, version) or not re.fullmatch(VERSION, native):
        raise ValueError("Invalid adapter or native release version")
    if cargo["package"]["version"] != version:
        raise ValueError("Adapter Cargo and extension versions must match")
    if manifest.get("id") != EXTENSION_ID or manifest.get("schema_version") != 1:
        raise ValueError("Unexpected extension ID or manifest schema version")
    servers = manifest.get("language_servers", {})
    if set(servers) != {SERVER_ID}:
        raise ValueError("Unexpected language server ID")
    if servers[SERVER_ID].get("languages") != ["YAML", "Helm"] or servers[SERVER_ID].get("language_ids") != {"YAML": "yaml", "Helm": "helm"}:
        raise ValueError("YAML and Helm language IDs must match the native LSP protocol")
    repository = urlparse(manifest.get("repository", ""))
    parts = repository.path.strip("/").split("/")
    if (repository.scheme != "https" or repository.netloc != "github.com" or len(parts) != 2
            or manifest["repository"] != cargo["package"]["repository"]
            or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)):
        raise ValueError("Adapter repository must match its Cargo package")
    capabilities = manifest.get("capabilities", [])
    required = [
        {"kind": "process:exec", "command": "*", "args": ["version", "--json"]},
        {"kind": "download_file", "host": "github.com", "path": [*parts, "releases", "download", "**"]},
    ]
    if any(capability not in capabilities for capability in required):
        raise ValueError("Required Zed process or download capability is missing")
    if tag is not None and tag != "zed-v" + version:
        raise ValueError("Zed tag must match zed-v plus the adapter version")
    return cargo, manifest, native


def wasm_identity(data):
    """Check component framing and read API/release sections from nested modules."""
    if len(data) <= 8 or data[:8] != b"\0asm\x0d\0\x01\0":
        raise ValueError("Expected a wasm32-wasip2 component")

    def leb(value, offset):
        result = 0
        for shift in range(0, 35, 7):
            if offset >= len(value):
                raise ValueError("Truncated WASM length")
            byte = value[offset]
            offset += 1
            if shift == 28 and byte & 0xf0:
                raise ValueError("Overflowing WASM length")
            result |= (byte & 127) << shift
            if byte < 128:
                return result, offset
        raise ValueError("Invalid WASM length")

    found = {"zed:api-version": [], "argo:release": []}

    def sections(value, depth=0):
        if depth > 16:
            raise ValueError("Excessive nested WASM components")
        if len(value) < 8 or value[:4] != b"\0asm" or value[4:8] not in (b"\x01\0\0\0", b"\x0d\0\x01\0"):
            raise ValueError("Invalid nested WASM header")
        component = value[4:8] == b"\x0d\0\x01\0"
        pos = 8
        while pos < len(value):
            section = value[pos]
            size, pos = leb(value, pos + 1)
            end = pos + size
            if end > len(value) or section > (11 if component else 13):
                raise ValueError("Invalid WASM section")
            payload = value[pos:end]
            if section == 0:
                length, start = leb(payload, 0)
                if start + length > len(payload):
                    raise ValueError("Invalid WASM custom section")
                name = payload[start:start + length].decode("utf-8")
                if name in found:
                    found[name].append(payload[start + length:])
            elif component and section in (1, 4):
                sections(payload, depth + 1)
            pos = end

    sections(data)
    if any(len(values) != 1 for values in found.values()):
        raise ValueError("Missing or duplicate Zed API or release metadata section")
    version = found["zed:api-version"][0]
    if len(version) != 6:
        raise ValueError("Invalid Zed API version section")
    api = ".".join(str(int.from_bytes(version[n:n + 2], "big")) for n in (0, 2, 4))
    release = found["argo:release"][0].decode("utf-8").splitlines()
    if len(release) != 3:
        raise ValueError("Invalid adapter release metadata")
    return api, release


def dependency_notices():
    metadata = json.loads(subprocess.check_output([
        "cargo", "metadata", "--locked", "--offline", "--format-version=1",
        "--filter-platform", "wasm32-wasip2", "--manifest-path", str(ROOT / "Cargo.toml")],
        cwd=ROOT, timeout=180))
    files, packages = {}, []
    upstream_notices = json.loads(content(ROOT / "licenses/upstream.json"))
    for package in sorted(metadata["packages"], key=lambda item: (item["name"], item["version"])):
        if package["id"] == metadata["resolve"]["root"]:
            continue
        folder = Path(package["manifest_path"]).parent
        found = {}
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.name.upper().startswith(("LICENSE", "LICENCE", "NOTICE", "COPYING", "COPYRIGHT", "PATENTS")):
                found[path.name] = content(path)
        if package.get("license_file"):
            path = folder / package["license_file"]
            found[path.name] = content(path)
        origin = {"kind": "crate"}
        if not found:
            pin = upstream_notices.get(package["name"] + "-" + package["version"])
            if pin is None:
                raise ValueError("Missing original license text for " + package["name"])
            vcs = json.loads(content(folder / ".cargo_vcs_info.json"))
            if (pin["repository"] != package["repository"] or pin["commit"] != vcs["git"]["sha1"]
                    or pin["pathInVcs"] != vcs["path_in_vcs"]):
                raise ValueError("Upstream license source differs from the locked crate")
            for name, source in pin["files"].items():
                path = PurePosixPath(source["path"])
                if ".." in path.parts or path.is_absolute() or path.parts[:2] != ("licenses", "upstream"):
                    raise ValueError("Unsafe upstream license path")
                value = content(ROOT / path)
                if digest(value) != source["sha256"]:
                    raise ValueError("Pinned upstream license checksum mismatch")
                found[name] = value
            if not found:
                raise ValueError("Upstream license inventory is empty")
            origin = {"kind": "upstream", **pin}
        prefix = "licenses/" + package["name"] + "-" + package["version"] + "/"
        files.update({prefix + name: value for name, value in found.items()})
        packages.append({"name": package["name"], "version": package["version"], "license": package.get("license"),
                         "files": {name: digest(value) for name, value in sorted(found.items())}, "source": origin})
    sysroot = Path(subprocess.check_output(["rustc", "--print", "sysroot"], cwd=ROOT, text=True, timeout=30).strip())
    legal_root = sysroot / "share/doc/rust"
    for path in sorted(legal_root.glob("COPYRIGHT*.html")):
        files["licenses/rust/" + path.name] = content(path)
    for path in sorted((legal_root / "licenses").glob("*.txt")):
        files["licenses/rust/" + path.name] = content(path)
    for name in ("Apache-2.0.txt", "MIT.txt", "COPYRIGHT-library.html"):
        if "licenses/rust/" + name not in files:
            raise ValueError("Rust standard library license text is missing: " + name)
    files["licenses/THIRD_PARTY.json"] = (json.dumps(packages, indent=2, sort_keys=True) + "\n").encode()
    return files


def clean_source(commit):
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=30).strip()
    # The native LSP is independently versioned. Only this adapter's source is
    # part of this package, even when this crate lives in a larger repository.
    status = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all", "--", "."], cwd=ROOT, timeout=30)
    if head != commit or status or not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("Adapter release requires the exact clean committed source")


def package(wasm, output, commit):
    cargo, manifest, native = identity()
    clean_source(commit)
    data = content(wasm, 16 * 1024 * 1024)
    api, release = wasm_identity(data)
    dependency = cargo["dependencies"]["zed_extension_api"]
    expected_api = dependency if isinstance(dependency, str) else dependency["version"]
    if not expected_api.startswith("=") or api != expected_api[1:]:
        raise ValueError("WASM API version differs from the pinned dependency")
    if release != [manifest["id"], manifest["version"], native]:
        raise ValueError("WASM release metadata differs from the extension ID, version, or native pin")
    if output.exists() or output.is_symlink():
        raise ValueError("Output must not already exist")
    manifest_text = content(ROOT / "extension.toml").decode()
    library = {"kind": "Rust", "version": api}
    if "lib" in manifest and manifest["lib"] != library:
        raise ValueError("Extension library declaration differs from the compiled WASM API")
    if "lib" not in manifest:
        manifest_text += '\n[lib]\nkind = "Rust"\nversion = "' + api + '"\n'
    files = {"extension.toml": manifest_text.encode(), "extension.wasm": data,
             "LICENSE": content(ROOT / "LICENSE")}
    snippets = manifest.get("snippets", [])
    if not snippets:
        raise ValueError("Adapter manifest has no snippet resources")
    for name in snippets:
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or path.as_posix() != name
                or "\\" in name or ":" in name or not path.parts or path.parts[0] != "snippets"
                or path.suffix != ".json" or name in files):
            raise ValueError("Unsafe or duplicate adapter resource path")
        value = content(ROOT / name)
        if not isinstance(json.loads(value), dict):
            raise ValueError("Snippet resource must be a JSON object")
        files[name] = value
    files.update(dependency_notices())
    record = {"schemaVersion": 1, "kind": "zed-adapter", "extensionId": manifest["id"],
              "version": manifest["version"], "nativeServerVersion": native,
              "sourceCommit": commit, "target": "wasm32-wasip2", "apiVersion": api,
              "editorAcceptance": "not-executed", "files": {name: digest(value) for name, value in sorted(files.items())}}
    encoded = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode()
    files["MANIFEST.json"] = encoded
    archive_name = cargo["package"]["name"] + "-" + manifest["version"] + ".zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".zed-package-", dir=output.parent) as temporary:
        stage = Path(temporary) / "release"
        stage.mkdir()
        archive = stage / archive_name
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for name, value in sorted(files.items()):
                info = zipfile.ZipInfo(manifest["id"] + "/" + name, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, value)
        (stage / "zed-adapter.json").write_bytes(encoded)
        hashes = {archive_name: digest(content(archive)), "zed-adapter.json": digest(encoded)}
        (stage / "SHA256SUMS").write_text("".join(f"{value}  {name}\n" for name, value in sorted(hashes.items())))
        clean_source(commit)
        stage.rename(output)
    return {"archive": str(output / archive_name), **record}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--wasm", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    try:
        _, manifest, _ = identity(args.tag)
        if args.wasm is None:
            print("version=" + manifest["version"])
        else:
            if args.output is None or args.source_commit is None:
                raise ValueError("Packaging needs --output and --source-commit")
            print(json.dumps(package(args.wasm.absolute(), args.output.absolute(), args.source_commit)))
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
