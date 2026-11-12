#!/usr/bin/env python3
"""Exercise evidence packing and fail-closed verification through the real CLI."""

import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/ci_evidence.py"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root):
    rows = {}
    for path in [root, *sorted(root.rglob("*"))]:
        metadata = path.lstat()
        value = [metadata.st_mode, metadata.st_size, metadata.st_mtime_ns]
        if stat.S_ISLNK(metadata.st_mode):
            value.append(os.readlink(path))
        elif stat.S_ISREG(metadata.st_mode):
            value.append(digest(path))
        rows[path.relative_to(root).as_posix()] = value
    return rows


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = Path(os.path.abspath(args.output))
    artifacts = ROOT / "artifacts"
    if out == artifacts or not out.is_relative_to(artifacts) or out.resolve() != out:
        parser.error("Test output must be strictly inside this checkout's artifacts directory")
    out.mkdir(parents=True, exist_ok=False)
    rows, sockets = [], []
    applicability = {"symlinks": True, "fifo": hasattr(os, "mkfifo"), "socket": os.name != "nt"}

    def invoke(name, arguments, expected=0):
        directory = out / "commands" / name
        directory.mkdir(parents=True)
        command = [sys.executable, str(TOOL), *map(str, arguments)]
        start = time.monotonic()
        process = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=30)
        (directory / "stdout").write_bytes(process.stdout)
        (directory / "stderr").write_bytes(process.stderr)
        rows.append({"id": name, "command": command, "exit": process.returncode,
                     "elapsedSeconds": time.monotonic() - start, "passed": process.returncode == expected})
        if process.returncode != expected:
            raise AssertionError(name + ": " + process.stderr.decode(errors="replace"))
        return process

    try:
        first, second = out / "inputs/one", out / "inputs/two"
        write(first / "report.json", '{"passed":false,"reason":"retained failure"}\n')
        write(first / "failed.stderr", "a complete replay needs this failure\n")
        write(first / ".argocd-ls.yaml", "version: 3.0.0\n")
        write(first / "synthetic/.git/config", "[core]\n\trepositoryformatversion = 0\n")
        write(first / "synthetic/main.go", "package main\n")
        write(first / "native", "test-only executable\n")
        (first / "native").chmod(0o751)
        write(second / "other.stdout", "a second independent evidence root\n")
        for name in ("go-cache", "module-cache"):
            write(first / name / "cache-only-marker", "must not be archived\n")
        for name in ("normal", "flags", "search", "link", "persisted-go"):
            write(first / "environment" / (name + "-cache") / "cache-only-marker", "must not be archived\n")
        write(first / "nested/go-cache/business-fixture", "not a designated build cache\n")
        write(first / "environment/business-cache/fixture", "must be retained\n")
        sentinel = out / "external-sentinel"
        sentinel.write_text("EXTERNAL_TARGET_MUST_NEVER_BECOME_ARCHIVE_BYTES_73917", encoding="utf-8")
        try:
            (first / "external-link").symlink_to(sentinel)
        except (OSError, NotImplementedError) as error:
            applicability["symlinks"] = "unavailable on this host: " + str(error)
        if applicability["fifo"]:
            os.mkfifo(first / "unopened-pipe")
        if applicability["socket"]:
            endpoint = None
            previous = Path.cwd()
            try:
                endpoint = socket.socket(socket.AF_UNIX)
                # AF_UNIX has a short address limit; do not make this control
                # depend on the length of the caller's evidence output path.
                os.chdir(first)
                endpoint.bind("unopened-socket")
                sockets.append(endpoint)
            except OSError as error:
                if endpoint is not None:
                    endpoint.close()
                applicability["socket"] = "unavailable on this host: " + str(error)
            finally:
                os.chdir(previous)
        before = (snapshot(first), snapshot(second))
        packed = out / "packed"
        invoke("pack", ["pack", "--root", first, "--root", second, "--output", packed])
        invoke("verify", ["verify", "--manifest", packed / "manifest.json"])
        manifest = json.loads((packed / "manifest.json").read_text(encoding="utf-8"))
        records = {record["name"]: record for record in manifest["members"]}
        prefix = first.relative_to(artifacts).as_posix() + "/"
        with tarfile.open(packed / "evidence.tar.gz", "r:gz") as archive:
            payloads = {entry.name: archive.extractfile(entry).read() for entry in archive if entry.isfile()}
        checks = {
            "originalTreesUnchanged": before == (snapshot(first), snapshot(second)),
            "sevenCachesExcluded": len(manifest["excluded"]) == 7,
            "cacheBytesAbsent": not any("cache-only-marker" in name for name in records),
            "businessCacheFixtureRetained": prefix + "nested/go-cache/business-fixture" in records and prefix + "environment/business-cache/fixture" in records,
            "hiddenHistoryRetained": prefix + "synthetic/.git/config" in records and prefix + ".argocd-ls.yaml" in records,
            "failureRetained": payloads[prefix + "failed.stderr"] == (first / "failed.stderr").read_bytes(),
            "modeRetained": records[prefix + "native"]["mode"] == stat.S_IMODE((first / "native").stat().st_mode),
            "externalTargetNotRead": all(sentinel.read_bytes() not in value for value in payloads.values()),
            "bothRootsRetained": len(manifest["roots"]) == 2,
        }
        if applicability["symlinks"] is True:
            checks["linkMetadataRetained"] = records[prefix + "external-link"]["type"] == "symlink" and records[prefix + "external-link"]["link"] == str(sentinel)
        if applicability["fifo"]:
            checks["fifoMetadataOnly"] = records[prefix + "unopened-pipe"]["type"] == "fifo" and records[prefix + "unopened-pipe"]["size"] == 0
        if applicability["socket"] is True:
            checks["socketMetadataOnly"] = records[prefix + "unopened-socket"]["type"] == "socket" and records[prefix + "unopened-socket"]["size"] == 0
        rows.append({"id": "retention-and-replay", "checks": checks, "passed": all(checks.values())})
        (out / "inputs").rename(out / "detached-inputs")
        try:
            invoke("verify-without-original-inputs", ["verify", "--manifest", packed / "manifest.json"])
        finally:
            (out / "detached-inputs").rename(out / "inputs")

        invoke("nested-output", ["pack", "--root", first, "--output", first / "nested-output"], 2)
        invoke("outside-output", ["pack", "--root", first, "--output", ROOT / "invalid-evidence-output"], 2)
        invoke("outside-input", ["pack", "--root", ROOT / "tests", "--output", out / "invalid-input"], 2)
        invoke("artifacts-root", ["pack", "--root", artifacts, "--output", out / "invalid-root"], 2)
        invoke("overlap", ["pack", "--root", first, "--root", first / "synthetic", "--output", out / "overlap"], 2)
        invoke("missing-root", ["pack", "--root", out / "absent", "--output", out / "missing-root"], 2)
        invoke("existing-output", ["pack", "--root", first, "--output", packed], 2)
        if applicability["symlinks"] is True:
            (out / "linked-input").symlink_to(first, target_is_directory=True)
            invoke("linked-root", ["pack", "--root", out / "linked-input", "--output", out / "linked-root"], 2)

        def variant(name, mutation=None, edit_manifest=None):
            folder = out / "variants" / name
            folder.mkdir(parents=True)
            changed = copy.deepcopy(manifest)
            target = folder / "evidence.tar.gz"
            if mutation:
                with tarfile.open(packed / "evidence.tar.gz", "r:gz") as source, tarfile.open(target, "w:gz") as destination:
                    for entry in source:
                        payload = source.extractfile(entry).read() if entry.isfile() else None
                        for altered, data in mutation(copy.copy(entry), payload):
                            destination.addfile(altered, io.BytesIO(data) if data is not None else None)
                changed["archive"].update(size=target.stat().st_size, sha256=digest(target))
            else:
                shutil.copyfile(packed / "evidence.tar.gz", target)
            if edit_manifest:
                edit_manifest(changed)
            (folder / "manifest.json").write_text(json.dumps(changed), encoding="utf-8")
            invoke(name, ["verify", "--manifest", folder / "manifest.json"], 2)

        chosen = prefix + "failed.stderr"

        def altered_entry(change):
            def mutate(entry, payload):
                if entry.name == chosen:
                    change(entry)
                    if not entry.isfile():
                        payload = None
                return [(entry, payload)]
            return mutate

        variant("wrong-archive-sha", edit_manifest=lambda value: value["archive"].update(sha256="0" * 64))
        variant("missing-manifest-field", edit_manifest=lambda value: value.pop("excluded"))
        variant("wrong-schema-type", edit_manifest=lambda value: value.update(formatVersion=True))
        variant("invalid-cache-exclusion", edit_manifest=lambda value: value["excluded"].append(prefix + "failed.stderr"))
        variant("wrong-member-sha", edit_manifest=lambda value: next(record for record in value["members"] if record["name"] == chosen).update(sha256="0" * 64))
        variant("missing-manifest-entry", edit_manifest=lambda value: value["members"].pop())
        variant("duplicate-manifest-entry", edit_manifest=lambda value: value["members"].append(value["members"][0]))
        variant("tampered-file", mutation=lambda entry, payload: [(entry, b"!" * len(payload) if entry.name == chosen else payload)])
        variant("missing-archive-entry", mutation=lambda entry, payload: [] if entry.name == chosen else [(entry, payload)])
        variant("duplicate-archive-entry", mutation=lambda entry, payload: [(entry, payload)] * (2 if entry.name == chosen else 1))
        variant("wrong-mode", mutation=altered_entry(lambda entry: setattr(entry, "mode", entry.mode ^ 0o100)))

        def change_type(entry):
            entry.type, entry.size = tarfile.DIRTYPE, 0

        variant("wrong-type", mutation=altered_entry(change_type))
        if applicability["symlinks"] is True:
            def change_link(entry, payload):
                if entry.issym():
                    entry.linkname = str(sentinel) + "-changed"
                return [(entry, payload)]
            variant("wrong-link", mutation=change_link)
    except Exception as error:
        rows.append({"id": "runner", "passed": False, "error": repr(error)})
    finally:
        for endpoint in sockets:
            endpoint.close()
        report = {"scope": "real evidence CLI; synthetic fixtures only", "host": sys.platform,
                  "applicability": applicability, "toolSHA256": digest(TOOL),
                  "rows": rows, "passed": bool(rows) and all(row["passed"] for row in rows)}
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "cases": len(rows), "applicability": applicability}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
