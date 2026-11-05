#!/usr/bin/env python3
"""Real native release staging plus explicitly synthetic cross-platform aggregation controls."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {"linux/amd64", "linux/arm64", "darwin/amd64", "darwin/arm64", "windows/amd64"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def synthetic_platform(folder, platform, version, commit, omit=None, different_binary=False, unsafe=False):
    """These are parser controls, never evidence that another architecture ran."""
    folder.mkdir(parents=True)
    suffix = platform.replace("/", "-")
    root = f"argocd-ls-{version}-{suffix}"
    executable = "argocd-ls.exe" if platform.startswith("windows/") else "argocd-ls"
    asset = root + (".exe" if platform.startswith("windows/") else "")
    archive_name = root + (".zip" if platform.startswith("windows/") else ".tar.gz")
    binary = b"Synthetic aggregate fixture, not a native executable: " + platform.encode()
    identity = {"product": "argo-cd", "serverVersion": version, "adapterContractVersion": 1,
                "analysisReady": True, "configSchemaVersions": [1], "reportSchemaVersions": [1],
                "toolkitVersion": "fixture", "catalogHash": "a" * 64,
                "sourceCommit": commit, "dirty": False, "buildMode": "release"}
    provenance = encoded({"identity": identity, "platform": platform, "binarySHA256": sha(binary)})
    files = {executable: b"different bytes" if different_binary else binary,
             "LICENSE": b"fixture license", "NOTICE.txt": b"fixture notice",
             "licenses/go/LICENSE": b"fixture Go license", "licenses/go/PATENTS": b"fixture patent grant",
             "licenses/argo-cd/fixture/LICENSE": b"fixture upstream license", "provenance/build.json": provenance}
    if omit:
        del files[omit]
    if unsafe:
        files["../escape"] = b"unsafe"
    buffer = io.BytesIO()
    if platform.startswith("windows/"):
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo(root + "/" + name)
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
    else:
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, data in sorted(files.items()):
                info = tarfile.TarInfo(root + "/" + name)
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
    archive = buffer.getvalue()
    package = {"platform": platform, "root": root, "archive": archive_name,
               "archiveSHA256": sha(archive), "binarySHA256": sha(binary), "identity": identity,
               "buildManifestSHA256": sha(provenance), "files": {name: sha(value) for name, value in files.items()}}
    record = {"schemaVersion": 1, "product": "argo-cd", "platform": platform, "version": version,
              "sourceCommit": commit, "binaryAsset": asset, "archiveAsset": archive_name, "package": package}
    (folder / asset).write_bytes(binary)
    (folder / archive_name).write_bytes(archive)
    (folder / f"release-{suffix}.json").write_bytes(encoded(record))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    rows, completed = [], False
    fixture = output / "synthetic-source-history"

    def record(name, checks, **details):
        rows.append({"id": name, "checks": checks, "passed": bool(checks) and all(checks.values()), **details})

    def invoke(name, command, expected_error=None, cwd=None):
        process = subprocess.run([str(x) for x in command], cwd=cwd or fixture,
                                 capture_output=True, timeout=240)
        log = output / "logs" / name
        log.mkdir(parents=True)
        (log / "stdout").write_bytes(process.stdout)
        (log / "stderr").write_bytes(process.stderr)
        checks = {"expectedExit": process.returncode == (2 if expected_error else 0)}
        if expected_error:
            checks["specificRefusal"] = expected_error.lower() in process.stderr.decode(errors="replace").lower()
        record(name, checks, command=[str(x) for x in command], exit=process.returncode)
        if not all(checks.values()):
            raise AssertionError(f"{name}: {process.stderr.decode(errors='replace')[-2500:]}")
        return process

    try:
        fixture.mkdir()
        names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
        for name in sorted(set(names) - {""}):
            source = ROOT / name
            # A tracked file deleted by the work under test is not part of the snapshot.
            if not source.exists():
                continue
            if source.is_symlink() or not source.is_file():
                raise ValueError("Source fixture requires regular files")
            destination = fixture / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        env = os.environ | {"GIT_AUTHOR_NAME": "Release E2E fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                            "GIT_COMMITTER_NAME": "Release E2E fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}
        for command in (["git", "init"], ["git", "-c", "core.autocrlf=false", "add", "."],
                        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Synthetic release E2E source history only"]):
            subprocess.run(command, cwd=fixture, env=env, check=True, capture_output=True)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=fixture, text=True).strip()
        version = (fixture / "VERSION").read_text().strip()
        tool = [sys.executable, fixture / "tools/release.py"]
        common = ["--version", version, "--source-commit", commit]
        invoke("stable-tag", tool + ["version", "--tag", "v" + version, "--source-commit", commit])
        invoke("tag-version-mismatch", tool + ["version", "--tag", "v999.0.0", "--source-commit", commit], "must match")
        invoke("unsafe-tag", tool + ["version", "--tag", "v../escape", "--source-commit", commit], "stable semantic version")
        build = output / "native-release"
        invoke("build-real-native-release", [sys.executable, fixture / "tools/native.py", "build", "--platform", args.platform,
                                             "--output", build, "--mode", "release", "--version", version, "--source-commit", commit])
        manifest = build / "manifest.json"
        stage = output / "stage-one"
        stage_args = ["stage", "--manifest", manifest, *common]
        invoke("stage-real-native-release", tool + stage_args + ["--output", stage])
        second = output / "stage-two"
        invoke("repeat-staging", tool + stage_args + ["--output", second])
        first_bytes = {path.name: path.read_bytes() for path in stage.iterdir()}
        record("deterministic-staging", {"sameInventoryAndBytes": first_bytes == {path.name: path.read_bytes() for path in second.iterdir()}})
        stage_record = json.loads(next(stage.glob("release-*.json")).read_bytes())
        executable = stage / stage_record["binaryAsset"]
        identity = invoke("execute-staged-native", [executable, "version", "--json"])
        record("exact-native-identity", {"sameIdentity": json.loads(identity.stdout) == stage_record["package"]["identity"],
                                          "release": json.loads(identity.stdout)["buildMode"] == "release"})
        invoke("existing-output-refused", tool + stage_args + ["--output", stage], "already exist")
        invoke("source-commit-mismatch", tool + ["stage", "--manifest", manifest, "--version", version, "--source-commit", "0" * 40,
                                                 "--output", output / "bad-commit"], "exact clean committed source")
        readme = fixture / "README.md"
        original = readme.read_bytes()
        readme.write_bytes(original + b"\nchanged\n")
        invoke("dirty-source-refused", tool + stage_args + ["--output", output / "dirty"], "exact clean committed source")
        readme.write_bytes(original)
        build_record = json.loads(manifest.read_bytes())
        invalid = json.loads(manifest.read_bytes())
        invalid["identity"]["buildMode"] = "development"
        bad_manifest = build / "development.json"
        bad_manifest.write_bytes(encoded(invalid))
        invoke("development-identity-refused", tool + ["stage", "--manifest", bad_manifest, *common, "--output", output / "development"], "clean pinned release")
        native = build / build_record["binary"]
        original_binary = native.read_bytes()
        native.write_bytes(original_binary + b"tampered")
        invoke("tampered-native-refused", tool + stage_args + ["--output", output / "tampered"], "binary hash differs")
        native.write_bytes(original_binary)
        inputs = output / "aggregate-controls"
        inputs.mkdir()
        for platform in sorted(PLATFORMS):
            target = inputs / platform.replace("/", "-")
            if platform == args.platform:
                shutil.copytree(stage, target)
            else:
                synthetic_platform(target, platform, version, commit)
        combined = output / "combined"
        collect = ["collect", "--inputs", inputs, *common]
        invoke("aggregate-five-platform-controls", tool + collect + ["--output", combined])
        lines = (combined / "SHA256SUMS").read_text().splitlines()
        values = {name: digest for digest, name in (line.split("  ") for line in lines)}
        record("complete-checksum-manifest", {"allFifteenAssets": len(lines) == len(values) == 15,
                                               "everyDigestMatches": all(sha((combined / name).read_bytes()) == digest for name, digest in values.items()),
                                               "noSelfDigest": "SHA256SUMS" not in values})
        chosen = inputs / "linux-amd64"
        held = output / "held-platform"
        chosen.rename(held)
        invoke("missing-platform-refused", tool + collect + ["--output", output / "missing-platform"], "all five")
        held.rename(chosen)
        duplicate = inputs / "duplicate"
        shutil.copytree(chosen, duplicate)
        invoke("duplicate-platform-refused", tool + collect + ["--output", output / "duplicate-platform"], "Duplicate release platform")
        shutil.rmtree(duplicate)
        record_file = next(chosen.glob("release-*.json"))
        saved = record_file.read_bytes()
        altered = json.loads(saved)
        altered["sourceCommit"] = "0" * 40
        record_file.write_bytes(encoded(altered))
        invoke("mixed-source-provenance-refused", tool + collect + ["--output", output / "mixed-source"], "source commit mismatch")
        record_file.write_bytes(saved)
        extra = chosen / "unexpected.bin"
        extra.write_bytes(b"unexpected")
        invoke("extra-asset-refused", tool + collect + ["--output", output / "extra"], "Unexpected or missing")
        extra.unlink()
        for name, options, reason in (("missing-notice", {"omit": "NOTICE.txt"}, "missing required legal"),
                                       ("different-archive-native", {"different_binary": True}, "differs from the legal archive"),
                                       ("unsafe-archive", {"unsafe": True}, "Unsafe or duplicate")):
            chosen.rename(held)
            synthetic_platform(chosen, "linux/amd64", version, commit, **options)
            invoke(name + "-refused", tool + collect + ["--output", output / name], reason)
            shutil.rmtree(chosen)
            held.rename(chosen)
        completed = True
    except Exception as error:
        record("runner", {"completed": False}, error=repr(error))
    result = {"scope": "Real current-host native release staging; other-architecture aggregation records are synthetic controls, not native-platform validation or a product release.",
              "platform": args.platform, "completed": completed, "rows": rows,
              "passed": completed and bool(rows) and all(row["passed"] for row in rows)}
    (output / "report.json").write_bytes(encoded(result))
    print(json.dumps({"cases": len(rows), "passed": result["passed"], "failed": [row["id"] for row in rows if not row["passed"]]}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
