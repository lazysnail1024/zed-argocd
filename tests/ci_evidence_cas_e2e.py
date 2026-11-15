#!/usr/bin/env python3
"""Independent typed transport fixtures; exercise the real pack/verify/export CLI."""

import argparse
import copy
from contextlib import contextmanager
import gzip
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
TYPES = {"file": b"0", "directory": b"5", "symlink": b"2", "fifo": b"6",
         "character": b"3", "block": b"4", "socket": b"s"}


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def physical(name, payload):
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.uid, info.gid, info.mtime, info.size = b"0", 0o644, 0, 0, 0, len(payload)
    return info, payload


def bundle(folder, inventory, payloads, physical_change=None, external_change=None):
    """Test-owned wire encoder: no production codec imports or inferred expectations."""
    folder.mkdir(parents=True)
    entries = [physical("inventory.json", canonical(inventory))]
    entries += [physical("objects/sha256/" + digest, payload) for digest, payload in payloads.items()]
    if physical_change:
        entries = physical_change(entries)
    with (folder / "evidence.tar.gz").open("xb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=1, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                for info, payload in entries:
                    archive.addfile(info, io.BytesIO(payload) if info.type == b"0" else None)
    value = copy.deepcopy(inventory)
    archive_bytes = (folder / "evidence.tar.gz").read_bytes()
    value["archive"] = {"name": "evidence.tar.gz", "size": len(archive_bytes), "sha256": sha(archive_bytes)}
    if external_change:
        external_change(value)
    (folder / "manifest.json").write_bytes(canonical(value) + b"\n")
    return folder / "manifest.json"


def logical_fixture():
    root = "portable-fixture"
    shared, failure = b"same bytes; different path, permissions and owner\n", b"original failure stderr\x00\xff\n"
    def member(name, kind, mode=0o640, **extra):
        return dict(name=root + name, type=kind, mode=mode, size=0, uid=123, gid=456,
                    mtime=1_600_000_123, **extra)
    records = [member("", "directory", 0o750),
               member("/a", "file", 0o751, sha256=sha(shared)),
               member("/b", "file", 0o600, sha256=sha(shared)),
               member("/failed.stderr", "file", sha256=sha(failure)),
               member("/empty", "file", sha256=sha(b"")),
               member("/" + "long-" * 25 + "雪", "file", sha256=sha(shared)),
               member("/external-link", "symlink", 0o777, link="../../unread-external-target"),
               member("/pipe", "fifo", 0o644), member("/socket", "socket", 0o600),
               member("/character", "character", 0o620, deviceMajor=1, deviceMinor=7),
               member("/block", "block", 0o640, deviceMajor=8, deviceMinor=17)]
    payloads = {sha(shared): shared, sha(failure): failure, sha(b""): b""}
    for record in records:
        if record["type"] == "file":
            record["size"] = len(payloads[record["sha256"]])
    records[2].update(uid=789, gid=321, mtime=-123_456_789)
    return {"formatVersion": 2, "roots": [root], "excluded": [], "members": records}, payloads


def tar_inventory(path):
    result = []
    with tarfile.open(path, "r:gz") as archive:
        for info in archive:
            value = dict(name=info.name, type=next((name for name, code in TYPES.items() if code == info.type), repr(info.type)),
                         mode=info.mode, size=info.size, uid=info.uid, gid=info.gid, mtime=info.mtime)
            if info.linkname:
                value["link"] = info.linkname
            if info.type in (b"3", b"4"):
                value.update(deviceMajor=info.devmajor, deviceMinor=info.devminor)
            if info.type == b"0":
                with archive.extractfile(info) as stream:
                    value["sha256"] = sha(stream.read())
            # The source packer has always emitted numeric ownership, not local names.
            if info.uname != "" or info.gname != "":
                raise AssertionError("Export invented owner/group names")
            result.append(value)
    return result


def source_inventory(root):
    values = []
    for path in [root, *sorted(root.rglob("*"))]:
        metadata = path.lstat()
        kind = ("symlink" if stat.S_ISLNK(metadata.st_mode) else "directory" if stat.S_ISDIR(metadata.st_mode)
                else "fifo" if stat.S_ISFIFO(metadata.st_mode) else "socket" if stat.S_ISSOCK(metadata.st_mode) else "file")
        value = dict(name=path.relative_to(ROOT / "artifacts").as_posix(), type=kind, mode=stat.S_IMODE(metadata.st_mode),
                     size=metadata.st_size if kind == "file" else 0, uid=metadata.st_uid, gid=metadata.st_gid, mtime=int(metadata.st_mtime))
        if kind == "symlink":
            value["link"] = os.readlink(path)
        if kind == "file":
            value["sha256"] = sha(path.read_bytes())
        values.append(value)
    return values


def driver(folder, mode):
    """Synchronize actual filesystem changes at a real read boundary in pack()."""
    sys.path.insert(0, str(ROOT / "tools"))
    import ci_evidence as codec
    source = folder / "source"
    source.mkdir(parents=True)
    payload = source / "payload"
    payload.write_bytes(b"original bytes")
    empty = source / "zz-empty"
    empty.mkdir()
    before = payload.stat()
    if mode == "owner-ctime" and not hasattr(os, "chown"):
        print(json.dumps({"rejected": False, "applicable": False, "reason": "os.chown unavailable"}))
        return 0
    original, attempted, changed, applicable = codec.open_regular, False, False, True

    @contextmanager
    def intercepted(path, metadata):
        nonlocal attempted, changed, applicable
        with original(path, metadata) as stream:
            yield stream
        if path == payload and not attempted:
            attempted = True
            if mode == "tree-add":
                (source / "new-member").write_bytes(b"not in the captured inventory")
            elif mode == "tree-delete":
                empty.rmdir()
            elif mode == "same-mtime-content":
                payload.write_bytes(b"different text")
                os.utime(payload, ns=(before.st_atime_ns, before.st_mtime_ns))
            elif mode == "owner-ctime":
                try:
                    os.chown(payload, -1, before.st_gid)
                except (PermissionError, NotImplementedError):
                    applicable = False
                    return
                if payload.stat().st_ctime_ns == before.st_ctime_ns:
                    raise AssertionError("Real ownership syscall did not change ctime")
            else:
                raise AssertionError(mode)
            changed = True

    codec.open_regular = intercepted
    try:
        codec.pack([source], folder / "packed", format_version=2)
    except (ValueError, OSError) as error:
        print(json.dumps({"rejected": changed, "applicable": True, "sourceStillPresent": payload.exists(), "reason": str(error)}))
        return 0 if changed else 1
    print(json.dumps({"rejected": False, "applicable": applicable, "mutationObserved": changed}))
    return 1 if applicable else 0


def fallback_driver(folder, both=False):
    sys.path.insert(0, str(ROOT / "tools"))
    import ci_evidence as codec
    source = folder / "source"
    source.mkdir(parents=True)
    (source / "failure.stderr").write_bytes(b"original failed test bytes\n")
    original = codec.pack
    calls = []

    def controlled(values, destination, format_version=1):
        calls.append(format_version)
        if format_version == 2 or both:
            Path(destination).mkdir(parents=True)
            (Path(destination) / "partial.raw").write_bytes(b"controlled codec failure material\n")
            raise ValueError("controlled transport failure; original test still failed")
        return original(values, destination, format_version=format_version)

    codec.pack = controlled
    try:
        result = codec.retain([source], folder / "retained")
    except (ValueError, OSError):
        result = None
    report = json.loads((folder / "retained/retention.json").read_text())
    checks = {"v2ThenV1": calls == [2, 1], "sourceRetained": (source / "failure.stderr").read_bytes() == b"original failed test bytes\n",
              "firstFailureRetained": (folder / "retained/cas-v2/partial.raw").read_bytes() == b"controlled codec failure material\n",
              "failureReasonVisible": "controlled transport failure" in report.get("fallbackReason", ""),
              "verifiedOutcome": report.get("verified") is (not both) and (result is not None) is (not both),
              "actualFormat": report.get("actualFormat") == (None if both else "v1")}
    if not both:
        checks["fallbackVerified"] = codec.verify(folder / "retained/v1/manifest.json")["verified"] is True
    print(json.dumps({"checks": checks, "passed": all(checks.values())}))
    return 0 if all(checks.values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = Path(os.path.abspath(args.output))
    if not out.is_relative_to(ROOT / "artifacts") or out == ROOT / "artifacts" or out.resolve() != out:
        parser.error("Output must be strictly inside checkout artifacts")
    out.mkdir(parents=True, exist_ok=False)
    rows, sockets = [], []
    applicability = {}

    def invoke(name, arguments, expected=0, reason=None, tool=TOOL, timeout=40):
        directory = out / "commands" / name
        directory.mkdir(parents=True)
        command = [sys.executable, str(tool), *map(str, arguments)]
        started = time.monotonic()
        process = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=timeout)
        (directory / "stdout").write_bytes(process.stdout)
        (directory / "stderr").write_bytes(process.stderr)
        checks = {"exit": process.returncode == expected}
        if reason:
            checks["intendedReason"] = reason.lower() in process.stderr.decode(errors="replace").lower()
        rows.append(dict(id=name, command=command, exit=process.returncode, elapsedSeconds=time.monotonic()-started,
                         checks=checks, passed=all(checks.values())))
        return process

    def check(name, **checks):
        rows.append({"id": name, "checks": checks, "passed": all(checks.values())})

    try:
        source = out / "source"
        (source / ".hidden/history").mkdir(parents=True)
        (source / "a").write_bytes(b"shared regular bytes\n")
        (source / "b").write_bytes((source / "a").read_bytes())
        (source / "a").chmod(0o751)
        (source / "b").chmod(0o600)
        (source / "failed.stderr").write_bytes(b"test failed; retain raw bytes\x00\xff\n")
        (source / ".hidden/history/config").write_bytes(b"original config\n")
        for path in source.rglob("*"):
            os.utime(path, (1_600_000_125, 1_600_000_125))
        sentinel = out / "outside"
        sentinel.write_bytes(b"never read through a symlink\n")
        try:
            (source / "link").symlink_to(sentinel)
            applicability["symlink"] = True
        except (OSError, NotImplementedError):
            applicability["symlink"] = False
        if hasattr(os, "mkfifo"):
            os.mkfifo(source / "fifo", 0o640)
            applicability["fifo"] = True
        if os.name != "nt":
            previous = Path.cwd()
            endpoint = None
            try:
                endpoint = socket.socket(socket.AF_UNIX)
                os.chdir(source)
                endpoint.bind("socket")
                sockets.append(endpoint)
                applicability["socket"] = True
            except OSError:
                if endpoint is not None:
                    endpoint.close()
                applicability["socket"] = False
            finally:
                os.chdir(previous)
        expected = source_inventory(source)
        (out / "expected-source-inventory.json").write_bytes(canonical(expected) + b"\n")
        v1, v2 = out / "v1", out / "v2"
        invoke("legacy-pack", ["pack", "--root", source, "--output", v1])
        invoke("legacy-verify", ["verify", "--manifest", v1 / "manifest.json"])
        invoke("cas-pack", ["pack", "--format", "cas-v2", "--root", source, "--output", v2])
        invoke("cas-verify", ["verify", "--manifest", v2 / "manifest.json"])
        if (v2 / "manifest.json").exists():
            manifest = json.loads((v2 / "manifest.json").read_bytes())
            with tarfile.open(v2 / "evidence.tar.gz", "r:gz") as archive:
                physical_entries = list(archive)
            check("physical-dedup", exactLogicalInventory=manifest["members"] == expected,
                  oneObjectPerDigest=len(physical_entries) == 1 + len({r["sha256"] for r in expected if r["type"] == "file"}),
                  allPhysicalRegular=all(entry.type == b"0" for entry in physical_entries),
                  originalUnchanged=source_inventory(source) == expected,
                  externalUnread=all(r.get("sha256") != sha(sentinel.read_bytes()) for r in manifest["members"]))
        else:
            check("physical-dedup", exactLogicalInventory=False, oneObjectPerDigest=False, allPhysicalRegular=False,
                  originalUnchanged=source_inventory(source) == expected, externalUnread=False)
        source.rename(out / "detached-source")
        invoke("detached-export", ["export", "--manifest", v2 / "manifest.json", "--output", out / "exported"])
        invoke("exported-v1-verify", ["verify", "--manifest", out / "exported/manifest.json"])
        check("exported-source-headers", exactHeadersAndContent=(out / "exported/evidence.tar.gz").exists()
              and tar_inventory(out / "exported/evidence.tar.gz") == expected,
              noLogicalTreeCreated=not (out / "exported" / source.relative_to(ROOT / "artifacts")).exists())
        invoke("legacy-export", ["export", "--manifest", v1 / "manifest.json", "--output", out / "exported-legacy"])
        check("legacy-bytes-preserved", identicalArchive=(out / "exported-legacy/evidence.tar.gz").exists()
              and (v1 / "evidence.tar.gz").read_bytes() == (out / "exported-legacy/evidence.tar.gz").read_bytes(),
              identicalManifest=(out / "exported-legacy/manifest.json").exists()
              and (v1 / "manifest.json").read_bytes() == (out / "exported-legacy/manifest.json").read_bytes())
        frozen = ROOT / "tests/fixtures/ci-evidence-v1"
        provenance = json.loads((frozen / "provenance.json").read_bytes())
        for name in ("case-distinct", "unix-names", "non-utf8"):
            held = out / ("old-v1-" + name)
            shutil.copytree(frozen / name, held)
            exported = out / ("old-v1-export-" + name)
            invoke("old-v1-verify-" + name, ["verify", "--manifest", held / "manifest.json"])
            invoke("old-v1-export-" + name, ["export", "--manifest", held / "manifest.json", "--output", exported])
            check("old-v1-bytes-" + name,
                  frozenHashes=all(sha((held / file).read_bytes()) == digest for file, digest in provenance["fixtures"][name].items()),
                  exactExport=all((exported / file).exists() and (exported / file).read_bytes() == (held / file).read_bytes()
                                  for file in ("manifest.json", "evidence.tar.gz")),
                  noExtraction=not (exported / "step7-legacy-independent-review").exists())
        inventory, payloads = logical_fixture()
        fixture = bundle(out / "literal", inventory, payloads)
        invoke("literal-types-verify", ["verify", "--manifest", fixture])
        invoke("literal-types-export", ["export", "--manifest", fixture, "--output", out / "literal-export"])
        check("literal-headers", allSevenTypesAndMetadata=(out / "literal-export/evidence.tar.gz").exists()
              and tar_inventory(out / "literal-export/evidence.tar.gz") == inventory["members"],
              noSpecialInstantiated=not (out / "literal-export/portable-fixture").exists())

        # The existing native workloads produce more than 250,000 input files.
        # Exercise that legitimate inventory through the wire format without
        # creating another quarter-million physical fixtures on every runner.
        # Long valid paths also put the manifest above the former 64 MiB cap.
        large_root = "large-native-fixture"
        large_parent = large_root + "/" + "e" * 192
        shared = b"complete original fixture bytes\n"
        shared_hash = sha(shared)
        large_records = [dict(name=name, type="directory", mode=0o750, size=0,
                              uid=123, gid=456, mtime=1_600_000_123)
                         for name in (large_root, large_parent)]
        large_records.extend(dict(name=large_parent + "/" + str(index), type="file", mode=0o640,
                                  size=len(shared), uid=123, gid=456, mtime=1_600_000_123,
                                  sha256=shared_hash) for index in range(250_001))
        large_fixture = bundle(out / "large-native-inventory", dict(formatVersion=2, roots=[large_root],
                               excluded=[], members=large_records), {shared_hash: shared})
        del large_records
        large_result = invoke("large-native-inventory", ["verify", "--manifest", large_fixture], timeout=120)
        observed = json.loads(large_result.stdout) if large_result.returncode == 0 else {}
        check("large-native-inventory-complete", allMembersVerified=observed.get("members") == 250_003,
              sharedObjectVerified=observed.get("objects") == 1 and observed.get("verified") is True,
              exceedsOldManifestLimit=large_fixture.stat().st_size > 64 * 1024 * 1024)

        def negative(name, reason, edit=None, alter=None, external=None, raw_manifest=None, raw_archive=None):
            changed = copy.deepcopy(inventory)
            if edit:
                edit(changed)
            folder = out / "variants" / name
            path = bundle(folder, changed, payloads, physical_change=alter, external_change=external)
            if raw_manifest:
                path.write_bytes(raw_manifest(path.read_bytes()))
            if raw_archive:
                raw_archive(folder / "evidence.tar.gz")
                value = json.loads(path.read_bytes())
                content = (folder / "evidence.tar.gz").read_bytes()
                value["archive"].update(size=len(content), sha256=sha(content))
                path.write_bytes(canonical(value))
            invoke(name, ["verify", "--manifest", path], 2, reason)

        negative("duplicate-json-key", "Duplicate manifest key", raw_manifest=lambda raw: raw.replace(b'"formatVersion":2', b'"formatVersion":2,"formatVersion":2'))
        negative("boolean-format", "Unsupported evidence", edit=lambda m: m.update(formatVersion=True))
        negative("unknown-field", "manifest", edit=lambda m: m.update(extra="undeclared"))
        negative("duplicate-logical", "Duplicate", edit=lambda m: m["members"].append(copy.deepcopy(m["members"][1])))
        negative("case-alias", "alias", edit=lambda m: m["members"][2].update(name="portable-fixture/A"))
        negative("file-directory-alias", "Duplicate", edit=lambda m: m["members"][0].update(name=m["members"][1]["name"]))
        negative("file-parent", "directory", edit=lambda m: m["members"][2].update(name="portable-fixture/a/child"))
        for name, unsafe in (("absolute", "/root"), ("traversal", "portable-fixture/../x"), ("dot-alias", "portable-fixture/./a"),
                             ("drive", "C:/x"), ("trailing-dot", "portable-fixture/a."), ("backslash", "portable-fixture\\a")):
            negative("unsafe-" + name, "Unsafe", edit=lambda m, value=unsafe: m["members"][1].update(name=value))
        negative("non-utf8-v2", "surrogates not allowed", edit=lambda m: m["members"][1].update(name="portable-fixture/raw-\udcff"))
        negative("unknown-type", "type", edit=lambda m: m["members"][1].update(type="hardlink"))
        negative("boolean-mode", "mode", edit=lambda m: m["members"][1].update(mode=True))
        negative("boolean-owner", "ownership", edit=lambda m: m["members"][1].update(uid=True))
        negative("fractional-mtime", "mtime", edit=lambda m: m["members"][1].update(mtime=1.5))
        negative("special-size", "size", edit=lambda m: m["members"][7].update(size=1))
        negative("negative-device", "device", edit=lambda m: m["members"][9].update(deviceMajor=-1))
        negative("object-size-bound", "limit", edit=lambda m: m["members"][1].update(size=8 * 1024**3))
        def excessive_logical_bytes(value):
            for index in range(4):
                record = copy.deepcopy(value["members"][1])
                record["name"] = "portable-fixture/extra-" + str(index)
                value["members"].append(record)
            for record in value["members"]:
                if record["type"] == "file":
                    record["size"] = 8 * 1024**3 - 1
        negative("logical-size-bound", "limit", edit=excessive_logical_bytes)
        negative("inventory-bound", "limit", edit=lambda m: m.update(members=[{}] * 1_000_001))
        oversized = bundle(out / "variants/manifest-byte-bound", inventory, payloads)
        with oversized.open("r+b") as stream:
            stream.truncate(256 * 1024 * 1024 + 1)
        invoke("manifest-byte-bound", ["verify", "--manifest", oversized], 2, "limit")
        negative("digest-size-conflict", "size", edit=lambda m: m["members"][2].update(size=1))
        negative("invalid-digest", "SHA-256", edit=lambda m: m["members"][1].update(sha256="a" * 63))
        negative("outer-metadata-not-bound", "inventory", external=lambda m: m["members"][1].update(uid=124))
        negative("wrong-archive-digest", "SHA-256", external=lambda m: m["archive"].update(sha256="0" * 64))
        negative("missing-object", "Missing", alter=lambda es: es[:-1])
        negative("duplicate-object", "Unexpected", alter=lambda es: es + [es[1]])
        negative("unexpected-object", "Unexpected", alter=lambda es: es + [physical("objects/sha256/" + "1" * 64, b"alien")])
        negative("changed-object-bytes", "SHA-256", alter=lambda es: [es[0], (es[1][0], b"!" * len(es[1][1])), *es[2:]])
        def change_header(entries, key, value, index=1):
            entries = copy.deepcopy(entries)
            setattr(entries[index][0], key, value)
            return entries
        negative("physical-special", "header", alter=lambda es: change_header(change_header(es, "type", b"6"), "size", 0))
        negative("physical-owner", "header", alter=lambda es: change_header(es, "uid", 123))
        negative("physical-pax", "header", alter=lambda es: change_header(es, "pax_headers", {"comment": "undeclared header"}))
        negative("inventory-header", "header", alter=lambda es: change_header(es, "mode", 0o600, 0))
        negative("truncated-gzip", "Compressed", raw_archive=lambda path: path.write_bytes(path.read_bytes()[:-6]))
        negative("expanded-tail-bound", "limit", raw_archive=lambda path: path.write_bytes(gzip.compress(gzip.decompress(path.read_bytes()) + b"\0" * 2_000_000, compresslevel=1, mtime=0)))
        negative("empty-second-gzip-member", "compressed trailing", raw_archive=lambda path: path.write_bytes(path.read_bytes() + gzip.compress(b"", mtime=0)))
        negative("compressed-zero-tail", "compressed trailing", raw_archive=lambda path: path.write_bytes(path.read_bytes() + b"\0" * 4096))
        invoke("export-existing", ["export", "--manifest", fixture, "--output", out / "literal"], 2, "already exist")
        invoke("export-outside", ["export", "--manifest", fixture, "--output", ROOT / "outside-evidence"], 2, "artifacts")
        for mode in ("tree-add", "tree-delete", "same-mtime-content", "owner-ctime"):
            result = invoke("source-" + mode, ["--mutation-driver", out / "mutations" / mode, mode], tool=Path(__file__))
            if result.returncode == 0:
                value = json.loads(result.stdout)
                rows[-1]["checks"]["mutationControl"] = (value.get("applicable") is False
                    or (value.get("applicable") is True and value.get("rejected") is True and value.get("sourceStillPresent") is True))
                rows[-1]["applicable"] = value.get("applicable")
                rows[-1]["passed"] = all(rows[-1]["checks"].values())
        (out / "detached-source").rename(source)
        invoke("retain-cas", ["retain", "--root", source, "--output", out / "retained"])
        if (out / "retained/retention.json").exists():
            value = json.loads((out / "retained/retention.json").read_bytes())
            check("retention-selection", preferredVerified=value.get("actualFormat") == "cas-v2" and value.get("verified") is True,
                  noFallback=value.get("fallbackReason") is None, sourceUnchanged=source_inventory(source) == expected)
        else:
            check("retention-selection", preferredVerified=False, noFallback=False, sourceUnchanged=source_inventory(source) == expected)
        for both, name in ((False, "verified-v1-fallback"), (True, "double-failure-retained")):
            result = invoke(name, ["--fallback-driver", out / name, "both" if both else "v2"], tool=Path(__file__))
            if result.returncode == 0:
                rows[-1]["checks"]["fallbackContract"] = json.loads(result.stdout).get("passed") is True
                rows[-1]["passed"] = all(rows[-1]["checks"].values())
    except Exception as error:
        rows.append({"id": "runner", "passed": False, "error": repr(error)})
    finally:
        for endpoint in sockets:
            endpoint.close()
        report = {"scope": "real typed transport CLI; independent synthetic headers, no product test execution claim",
                  "runnerSHA256": sha(Path(__file__).read_bytes()), "toolSHA256": sha(TOOL.read_bytes()), "host": sys.platform,
                  "applicability": applicability, "rows": rows, "passed": bool(rows) and all(row["passed"] for row in rows)}
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"passed": report["passed"], "cases": len(rows), "failed": [row["id"] for row in rows if not row["passed"]]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--mutation-driver":
        raise SystemExit(driver(Path(sys.argv[2]), sys.argv[3]))
    if len(sys.argv) > 1 and sys.argv[1] == "--fallback-driver":
        raise SystemExit(fallback_driver(Path(sys.argv[2]), sys.argv[3] == "both"))
    raise SystemExit(main())
