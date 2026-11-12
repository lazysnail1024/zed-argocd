#!/usr/bin/env python3
"""Pack replay evidence without build caches; verify it without extracting files."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
ENVIRONMENT_CACHES = {name + "-cache" for name in ("normal", "flags", "search", "link", "persisted-go")}
TYPES = {"file": tarfile.REGTYPE, "directory": tarfile.DIRTYPE,
         "symlink": tarfile.SYMTYPE, "fifo": tarfile.FIFOTYPE,
         "character": tarfile.CHRTYPE, "block": tarfile.BLKTYPE,
         "socket": b"s"}


class HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        value = self.stream.read(size)
        self.digest.update(value)
        return value


def consume(stream):
    while stream.read(1024 * 1024):
        pass


def file_hash(path):
    with path.open("rb") as stream:
        reader = HashReader(stream)
        consume(reader)
        return reader.digest.hexdigest()


def artifact_path(value):
    path = Path(os.path.abspath(value))
    if path == ARTIFACTS or not path.is_relative_to(ARTIFACTS):
        raise ValueError("Paths must be strictly inside this checkout's artifacts directory")
    if path.resolve() != path or ARTIFACTS.is_symlink():
        raise ValueError("Selected paths must not traverse symlinks")
    return path


def safe_name(name):
    if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
        raise ValueError("Unsafe archive member name")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        raise ValueError("Unsafe archive member name")
    return path


def excluded(parts):
    return (len(parts) == 1 and parts[0] in ("go-cache", "module-cache")) or (
        len(parts) == 2 and parts[0] == "environment" and parts[1] in ENVIRONMENT_CACHES)


def walk(root, path, omitted):
    if excluded(path.relative_to(root).parts):
        omitted.append(path.relative_to(ARTIFACTS).as_posix())
        return
    metadata = path.lstat()
    yield path, metadata
    if stat.S_ISDIR(metadata.st_mode):
        for child in sorted(path.iterdir()):
            yield from walk(root, child, omitted)


def kind(metadata):
    for name, predicate in (("file", stat.S_ISREG), ("directory", stat.S_ISDIR),
                            ("symlink", stat.S_ISLNK), ("fifo", stat.S_ISFIFO),
                            ("character", stat.S_ISCHR), ("block", stat.S_ISBLK),
                            ("socket", stat.S_ISSOCK)):
        if predicate(metadata.st_mode):
            return name
    raise ValueError("Unsupported filesystem entry type")


def signature(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_mode)


def open_regular(path, metadata):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    stream = os.fdopen(descriptor, "rb")
    actual = os.fstat(stream.fileno())
    if not stat.S_ISREG(actual.st_mode) or signature(actual) != signature(metadata):
        stream.close()
        raise ValueError("Evidence changed before it was read: " + str(path))
    return stream


def add_member(archive, path, metadata):
    name = path.relative_to(ARTIFACTS).as_posix()
    safe_name(name)
    entry_kind = kind(metadata)
    info = tarfile.TarInfo(name)
    info.type = TYPES[entry_kind]
    info.mode = stat.S_IMODE(metadata.st_mode)
    info.mtime = int(metadata.st_mtime)
    info.uid, info.gid = metadata.st_uid, metadata.st_gid
    info.size = metadata.st_size if entry_kind == "file" else 0
    record = {"name": name, "type": entry_kind, "mode": info.mode, "size": info.size}
    if entry_kind == "symlink":
        info.linkname = os.readlink(path)
        record["link"] = info.linkname
    if entry_kind in ("character", "block"):
        info.devmajor, info.devminor = os.major(metadata.st_rdev), os.minor(metadata.st_rdev)
        record.update(deviceMajor=info.devmajor, deviceMinor=info.devminor)
    if entry_kind == "file":
        with open_regular(path, metadata) as stream:
            reader = HashReader(stream)
            archive.addfile(info, reader)
            if signature(os.fstat(stream.fileno())) != signature(metadata):
                raise ValueError("Evidence changed while it was read: " + name)
            record["sha256"] = reader.digest.hexdigest()
    else:
        archive.addfile(info)
    # Streaming tar still caches TarInfo objects on Python 3.12. The manifest
    # owns the inventory; do not retain a second copy for hundreds of thousands of files.
    archive.members.clear()
    return record


def pack(values, destination):
    roots = [artifact_path(value) for value in values]
    output = artifact_path(destination)
    for root in roots:
        if not root.is_dir():
            raise ValueError("Every evidence root must be an existing directory")
        if sum(root.is_relative_to(other) or other.is_relative_to(root) for other in roots) != 1:
            raise ValueError("Evidence roots must not overlap or repeat")
        if output.is_relative_to(root):
            raise ValueError("Evidence output must be outside every input root")
    if output.exists() or output.is_symlink():
        raise ValueError("Evidence output must not already exist")
    output.mkdir(parents=True)
    archive_path = output / "evidence.tar.gz"
    manifest = {"formatVersion": 1, "roots": [root.relative_to(ARTIFACTS).as_posix() for root in roots],
                "excluded": [], "members": []}
    with archive_path.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=1, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT, dereference=False) as archive:
            for root in roots:
                for path, metadata in walk(root, root, manifest["excluded"]):
                    manifest["members"].append(add_member(archive, path, metadata))
    manifest["archive"] = {"name": archive_path.name, "size": archive_path.stat().st_size,
                           "sha256": file_hash(archive_path)}
    (output / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="utf-8")
    return {"members": len(manifest["members"]), "excludedCaches": len(manifest["excluded"]),
            "archiveBytes": manifest["archive"]["size"], "archiveSHA256": manifest["archive"]["sha256"]}


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate manifest key: " + key)
        value[key] = item
    return value


def valid_record(record, roots):
    path = safe_name(record["name"])
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError("Archive member is outside the declared evidence roots")
    entry_kind = record["type"]
    required = {"name", "type", "mode", "size"}
    if entry_kind == "file":
        required.add("sha256")
        if not isinstance(record.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", record["sha256"]):
            raise ValueError("Invalid file SHA-256")
    elif entry_kind == "symlink":
        required.add("link")
        if not isinstance(record.get("link"), str) or "\0" in record["link"]:
            raise ValueError("Invalid link metadata")
    elif entry_kind in ("character", "block"):
        required.update(("deviceMajor", "deviceMinor"))
        if any(type(record.get(key)) is not int or record[key] < 0 for key in ("deviceMajor", "deviceMinor")):
            raise ValueError("Invalid device metadata")
    if entry_kind not in TYPES or set(record) != required:
        raise ValueError("Invalid member type or fields")
    if type(record["mode"]) is not int or not 0 <= record["mode"] <= 0o7777:
        raise ValueError("Invalid member mode")
    if type(record["size"]) is not int or record["size"] < 0 or (entry_kind != "file" and record["size"] != 0):
        raise ValueError("Invalid member size")


def check_member(archive, info, expected):
    if (info.name, info.type, info.mode, info.size) != (
            expected["name"], TYPES[expected["type"]], expected["mode"], expected["size"]):
        raise ValueError("Archive member inventory, type, mode or size mismatch")
    if info.linkname != expected.get("link", ""):
        raise ValueError("Archive link metadata mismatch")
    if (info.devmajor, info.devminor) != (expected.get("deviceMajor", 0), expected.get("deviceMinor", 0)):
        raise ValueError("Archive device metadata mismatch")
    if expected["type"] == "file":
        stream = archive.extractfile(info)
        if stream is None:
            raise ValueError("Missing regular member bytes")
        with stream:
            reader = HashReader(stream)
            consume(reader)
        if reader.digest.hexdigest() != expected["sha256"]:
            raise ValueError("Archive member SHA-256 mismatch")


def verify(value):
    path = artifact_path(value)
    metadata = path.lstat()
    with open_regular(path, metadata) as stream:
        manifest = json.load(stream, object_pairs_hook=unique_object)
    if not isinstance(manifest, dict) or type(manifest.get("formatVersion")) is not int or manifest["formatVersion"] != 1:
        raise ValueError("Unsupported evidence manifest")
    if set(manifest) != {"formatVersion", "roots", "excluded", "members", "archive"} or any(
            not isinstance(manifest[key], list) for key in ("roots", "excluded", "members")):
        raise ValueError("Incomplete evidence manifest")
    roots = [safe_name(name) for name in manifest["roots"]]
    if not roots or any(sum(root.is_relative_to(other) or other.is_relative_to(root) for other in roots) != 1 for root in roots):
        raise ValueError("Invalid declared evidence roots")
    omitted = [safe_name(name) for name in manifest["excluded"]]
    if len(set(omitted)) != len(omitted) or any(not any(
            path.is_relative_to(root) and excluded(path.relative_to(root).parts) for root in roots) for path in omitted):
        raise ValueError("Invalid excluded cache inventory")
    names = set()
    for record in manifest["members"]:
        valid_record(record, roots)
        if any(PurePosixPath(record["name"]).is_relative_to(path) for path in omitted):
            raise ValueError("An excluded cache is present in the archive inventory")
        if record["name"] in names:
            raise ValueError("Duplicate manifest member")
        names.add(record["name"])
    if not all(any(record["name"] == str(root) and record["type"] == "directory" for record in manifest["members"]) for root in roots):
        raise ValueError("Missing root directory record")
    claim = manifest["archive"]
    if not isinstance(claim, dict) or set(claim) != {"name", "size", "sha256"} or claim.get("name") != "evidence.tar.gz":
        raise ValueError("Invalid archive identity")
    if type(claim.get("size")) is not int or claim["size"] <= 0 or not isinstance(claim.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", claim["sha256"]):
        raise ValueError("Invalid archive identity")
    archive_path = artifact_path(path.parent / claim["name"])
    archive_metadata = archive_path.lstat()
    if archive_metadata.st_size != claim["size"]:
        raise ValueError("Archive byte size mismatch")
    count = 0
    with open_regular(archive_path, archive_metadata) as raw:
        reader = HashReader(raw)
        with tarfile.open(fileobj=reader, mode="r|gz") as archive:
            for info in archive:
                if count == len(manifest["members"]):
                    raise ValueError("Unexpected or duplicate archive member")
                check_member(archive, info, manifest["members"][count])
                count += 1
                archive.members.clear()
        consume(reader)
        if reader.digest.hexdigest() != claim.get("sha256"):
            raise ValueError("Archive SHA-256 mismatch")
    if count != len(manifest["members"]):
        raise ValueError("Missing archive member")
    return {"members": count, "archiveSHA256": claim["sha256"], "verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    writer = commands.add_parser("pack")
    writer.add_argument("--root", type=Path, action="append", required=True)
    writer.add_argument("--output", type=Path, required=True)
    reader = commands.add_parser("verify")
    reader.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = pack(args.root, args.output) if args.command == "pack" else verify(args.manifest)
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, EOFError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
