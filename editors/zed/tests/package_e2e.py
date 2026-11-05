#!/usr/bin/env python3
"""Exercise a real compiled adapter through clean-source packaging and extraction."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", "-c", "commit.gpgsign=false", *args], cwd=root, text=True, stderr=subprocess.PIPE, timeout=30).strip()


def commit(root):
    git(root, "add", ".")
    git(root, "commit", "--quiet", "--allow-empty", "-m", "Zed package acceptance fixture")
    return git(root, "rev-parse", "HEAD")


def leb(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def custom(name, payload):
    name = name.encode()
    body = leb(len(name)) + name + payload
    return b"\0" + leb(len(body)) + body


def verify_package(folder, source, wasm, sha):
    archives = list(folder.glob("*.zip"))
    assert len(archives) == 1, "Expected one installable adapter ZIP"
    index = json.loads((folder / "zed-adapter.json").read_bytes())
    extension = tomllib.loads((source / "extension.toml").read_text())
    server = next(iter(extension["language_servers"]))
    assert index["sourceCommit"] == sha
    assert index["extensionId"] == extension["id"]
    assert index["version"] == extension["version"]
    assert index["nativeServerVersion"] == (source / "native-server-version").read_text().strip()
    assert index["target"] == "wasm32-wasip2" and index["apiVersion"] == "0.7.0"
    assert index["editorAcceptance"] == "not-executed"
    sums = dict(line.split("  ", 1)[::-1] for line in (folder / "SHA256SUMS").read_text().splitlines())
    assert set(sums) == {archives[0].name, "zed-adapter.json"}
    assert all(digest((folder / name).read_bytes()) == expected for name, expected in sums.items())
    with zipfile.ZipFile(archives[0]) as archive:
        members = archive.infolist()
        names = [item.filename for item in members]
        assert len(names) == len(set(names)), "Duplicate archive member"
        prefix = extension["id"] + "/"
        assert all(name.startswith(prefix) and ".." not in PurePosixPath(name).parts for name in names)
        assert all(item.external_attr >> 16 == 0o100644 for item in members)
        files = {name.removeprefix(prefix): archive.read(name) for name in names}
        assert set(files) == set(index["files"]) | {"MANIFEST.json"}
        assert all(name in {"extension.wasm", "extension.toml", "LICENSE", "MANIFEST.json"}
                   or name.startswith(("snippets/", "licenses/")) for name in files), "Build or native source leaked into the runtime archive"
        assert files["MANIFEST.json"] == (folder / "zed-adapter.json").read_bytes()
        assert all(digest(files[name]) == expected for name, expected in index["files"].items())
        assert files["extension.wasm"] == wasm
        manifest = tomllib.loads(files["extension.toml"].decode())
        assert manifest["id"] == extension["id"]
        assert manifest["lib"] == {"kind": "Rust", "version": "0.7.0"}
        assert manifest["language_servers"][server]["language_ids"] == {"YAML": "yaml", "Helm": "helm"}
        assert files["LICENSE"] == (source / "LICENSE").read_bytes()
        for snippet in manifest["snippets"]:
            assert files[snippet] == (source / snippet).read_bytes()
            assert isinstance(json.loads(files[snippet]), dict) and json.loads(files[snippet])
        # Validate original legal bytes independently against Cargo's real source locations.
        metadata = json.loads(subprocess.check_output([
            "cargo", "metadata", "--locked", "--offline", "--format-version=1", "--filter-platform", "wasm32-wasip2"], cwd=source, timeout=180))
        expected_packages = {p["name"] + "-" + p["version"]: p for p in metadata["packages"] if p["id"] != metadata["resolve"]["root"]}
        notices = json.loads(files["licenses/THIRD_PARTY.json"])
        assert {p["name"] + "-" + p["version"] for p in notices} == set(expected_packages), "Dependency notice inventory is incomplete"
        pins = json.loads((source / "licenses/upstream.json").read_bytes())
        for notice in notices:
            key = notice["name"] + "-" + notice["version"]
            package = expected_packages[key]
            crate = Path(package["manifest_path"]).parent
            assert notice["license"] == package["license"] and notice["files"]
            for name, expected in notice["files"].items():
                if notice["source"]["kind"] == "upstream":
                    original = source / pins[key]["files"][name]["path"]
                    assert notice["source"]["commit"] == json.loads((crate / ".cargo_vcs_info.json").read_bytes())["git"]["sha1"]
                else:
                    original = crate / name
                    if not original.exists() and package.get("license_file"):
                        original = crate / package["license_file"]
                actual = files["licenses/" + key + "/" + name]
                assert actual == original.read_bytes() and digest(actual) == expected
        sysroot = Path(subprocess.check_output(["rustc", "--print", "sysroot"], cwd=source, text=True, timeout=30).strip())
        for name in ("Apache-2.0.txt", "MIT.txt"):
            assert files["licenses/rust/" + name] == (sysroot / "share/doc/rust/licenses" / name).read_bytes()
        assert files["licenses/rust/COPYRIGHT-library.html"] == (sysroot / "share/doc/rust/COPYRIGHT-library.html").read_bytes()
        installed = folder / "installed"
        archive.extractall(installed)
        assert [path.name for path in installed.iterdir()] == [extension["id"]]
        assert (installed / extension["id"] / "extension.wasm").read_bytes() == wasm
    return {"archive": archives[0].name, "sha256": digest(archives[0].read_bytes()), "files": len(index["files"]), "dependencies": len(notices)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wasm", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.absolute()
    out.mkdir(parents=True, exist_ok=False)
    report = {"command": sys.argv, "passed": False, "cases": [], "wasmSHA256": digest(args.wasm.read_bytes()),
              "testSHA256": digest(Path(__file__).read_bytes()),
              "packagerSHA256": digest((ROOT / "package.py").read_bytes())}
    try:
        original = args.wasm.read_bytes()
        with tempfile.TemporaryDirectory(prefix="zed-package-acceptance-") as temporary:
            scratch = Path(temporary)
            source = scratch / "source"
            shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns("target", ".git", "artifacts", "__pycache__"))
            git(source, "init", "--quiet")
            git(source, "config", "user.name", "Adapter acceptance")
            git(source, "config", "user.email", "adapter-acceptance@example.invalid")
            git(source, "config", "core.autocrlf", "false")
            sha = commit(source)
            wasm = scratch / "adapter.wasm"
            wasm.write_bytes(original)
            version = tomllib.loads((source / "extension.toml").read_text())["version"]

            def invoke(name, expected=0, message=None, tag=None, commit_sha=None):
                destination = scratch / ("result-" + name)
                command = [sys.executable, str(source / "package.py"), "--wasm", str(wasm),
                           "--output", str(destination), "--source-commit", commit_sha or sha,
                           "--tag", tag or "zed-v" + version]
                result = subprocess.run(command, cwd=source, capture_output=True, text=True, timeout=180)
                (out / (name + ".stdout")).write_text(result.stdout)
                (out / (name + ".stderr")).write_text(result.stderr)
                row = {"name": name, "exitCode": result.returncode, "expectedExitCode": expected,
                       "passed": result.returncode == expected and (message is None or message in result.stderr)}
                if expected:
                    row["noOutputOnFailure"] = not destination.exists()
                    row["passed"] = row["passed"] and row["noOutputOnFailure"]
                report["cases"].append(row)
                assert row["passed"], f"{name}: {result.stderr[-2000:]}"
                return destination

            first = invoke("real-wasm-package")
            report["package"] = verify_package(first, source, original, sha)
            second = invoke("repeat-package")
            assert all((second / name).read_bytes() == (first / name).read_bytes()
                       for name in [report["package"]["archive"], "zed-adapter.json", "SHA256SUMS"]), "Packaging the same inputs was not deterministic"
            invoke("wrong-source-commit", 2, "exact clean committed source", commit_sha="0" * 40)
            invoke("wrong-release-tag", 2, "Zed tag", tag="v" + version)

            manifest_path = source / "extension.toml"
            cargo_path = source / "Cargo.toml"
            native_path = source / "native-server-version"
            manifest_text = manifest_path.read_text()
            snippets = tomllib.loads(manifest_text)["snippets"]
            first_pin = next(iter(json.loads((source / "licenses/upstream.json").read_bytes()).values()))
            legal = source / next(iter(first_pin["files"].values()))["path"]
            changes = [
                ("wrong-extension-id", {manifest_path: manifest_text.replace('id = "', 'id = "other-', 1).encode()}, "Unexpected extension ID"),
                ("wrong-language-id", {manifest_path: manifest_text.replace('Helm = "helm"', 'Helm = "yaml"').encode()}, "language IDs"),
                ("wrong-cargo-version", {cargo_path: cargo_path.read_bytes().replace(version.encode(), b"9.9.8-alpha.1", 1)}, "Cargo and extension versions"),
                ("stale-native-pin", {native_path: b"9.9.8-alpha.1\n"}, "WASM release metadata"),
                ("wrong-library-api", {manifest_path: (manifest_text+'\n[lib]\nkind="Rust"\nversion="9.9.9"\n').encode()}, "library declaration"),
                ("unsafe-resource", {manifest_path: manifest_text.replace(snippets[0], "../outside.json").encode()}, "Unsafe or duplicate adapter resource"),
                ("duplicate-resource", {manifest_path: manifest_text.replace('snippets = ["'+snippets[0]+'"]', 'snippets = ["'+snippets[0]+'", "'+snippets[0]+'"]' ).encode()}, "Unsafe or duplicate adapter resource"),
                ("invalid-snippet", {source / snippets[0]: b"[]"}, "Snippet resource must be a JSON object"),
                ("tampered-original-license", {legal: legal.read_bytes()+b"changed"}, "upstream license checksum mismatch"),
            ]
            for name, updates, expected in changes:
                backups = {path: path.read_bytes() for path in updates}
                for path, value in updates.items(): path.write_bytes(value)
                sha = commit(source)
                invoke(name, 2, expected)
                for path, value in backups.items(): path.write_bytes(value)
                sha = commit(source)

            readme = source / "README.md"
            before = readme.read_bytes()
            readme.write_bytes(before + b"dirty source\n")
            invoke("dirty-adapter-source", 2, "exact clean committed source")
            readme.write_bytes(before)
            assert git(source, "status", "--porcelain") == ""
            source_snippet = source / snippets[0]
            backup = source_snippet.read_bytes()
            source_snippet.unlink()
            sha = commit(source)
            invoke("missing-snippet", 2, "bounded regular file")
            source_snippet.write_bytes(backup)
            sha = commit(source)

            api_at = original.index(b"zed:api-version") + len(b"zed:api-version")
            wrong_api = original[:api_at] + b"\0\0\0\x08\0\0" + original[api_at+6:]
            nested = original.index(b"\0asm", 4)
            extension_id = tomllib.loads(manifest_text)["id"].encode()
            release = extension_id + b"\n" + version.encode() + b"\n" + native_path.read_bytes()
            release_section = b"argo:release" + release
            assert original.count(release_section) == 1, "Expected one compiler-emitted release metadata section"
            wrong_version = (b"8" if version.startswith("9") else b"9") + version[1:].encode()
            wrong_release = extension_id + b"\n" + wrong_version + b"\n" + native_path.read_bytes()
            variants = [
                ("truncated-wasm", original[:-1], "WASM"),
                ("native-bytes", b"not a WASM component", "wasm32-wasip2"),
                ("core-module-instead-of-component", b"\0asm\x01\0\0\0", "wasm32-wasip2"),
                ("invalid-nested-header", original[:nested]+b"BAD!"+original[nested+4:], "nested WASM header"),
                ("missing-api-section", original.replace(b"zed:api-version", b"bad:api-version", 1), "Missing or duplicate"),
                ("wrong-api-section", wrong_api, "API version differs"),
                ("duplicate-api-section", original+custom("zed:api-version", b"\0\0\0\x07\0\0"), "Missing or duplicate"),
                ("invalid-api-length", original.replace(b"zed:api-version", b"bad:api-version", 1)+custom("zed:api-version", b"short"), "Invalid Zed API version section"),
                ("wrong-wasm-adapter-version", original.replace(release_section, b"argo:release" + wrong_release, 1), "WASM release metadata"),
                ("wrong-wasm-extension-id", original.replace(release_section, b"argo:release" + b"x" + release[1:], 1), "WASM release metadata"),
                ("duplicate-release-section", original+custom("argo:release", release), "Missing or duplicate"),
                ("missing-release-section", original.replace(b"argo:release", b"gone:release", 1), "Missing or duplicate"),
                ("overflowing-section-length", b"\0asm\x0d\0\x01\0\0\xff\xff\xff\xff\x1f", "Overflowing WASM length"),
            ]
            for name, data, expected in variants:
                wasm.write_bytes(data)
                invoke(name, 2, expected)
            wasm.write_bytes(original)
        report["passed"] = all(row["passed"] for row in report["cases"])
    except Exception as error:
        report["error"] = repr(error)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "cases": len(report["cases"]), "package": report.get("package"), "error": report.get("error")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
