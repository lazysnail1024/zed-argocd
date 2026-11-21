#!/usr/bin/env python3
"""Pack replay evidence without build caches; verify it without extracting files."""

import argparse
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import tempfile
import traceback
import unicodedata
import zlib

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
ENVIRONMENT_CACHES = {name + "-cache" for name in ("normal", "flags", "search", "link", "persisted-go")}
TYPES = {"file": tarfile.REGTYPE, "directory": tarfile.DIRTYPE,
         "symlink": tarfile.SYMTYPE, "fifo": tarfile.FIFOTYPE,
         "character": tarfile.CHRTYPE, "block": tarfile.BLKTYPE,
         "socket": b"s"}
# The three existing large-workspace consumers alone retain over 250,000
# regular files. Keep finite transport limits without dropping their inputs.
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 1_000_000
MAX_READ_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024**3
MAX_OBJECT_BYTES = 8 * 1024**3 - 1  # Canonical physical USTAR size field.
MAX_LOGICAL_BYTES = 64 * 1024**3
MAX_ARCHIVE_BYTES = 64 * 1024**3
MAX_NAME_BYTES = 4096
MAX_LINK_BYTES = 16 * 1024
CHUNK = 1024 * 1024
ERRORS = (OSError, ValueError, KeyError, TypeError, tarfile.TarError, EOFError, UnicodeError, RecursionError, zlib.error)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def inventory_only(manifest):
    return {key: manifest[key] for key in ("formatVersion", "roots", "excluded", "members")}


class BoundedReader:
    def __init__(self, stream, limit):
        self.stream, self.limit, self.total = stream, limit, 0

    def read(self, size=-1):
        # The legacy TAR parser may ask for an attacker-declared PAX payload.
        # No single read may allocate an unbounded header or expanded stream.
        if size < 0 or size > MAX_READ_BYTES:
            raise ValueError("Expanded evidence read exceeds byte limit")
        value = self.stream.read(min(size, self.limit - self.total + 1))
        self.total += len(value)
        if self.total > self.limit:
            raise ValueError("Expanded evidence exceeds byte limit")
        return value


class SingleGzipReader:
    """Bound output allocation and reject every compressed byte after one member."""
    def __init__(self, stream):
        self.stream = stream
        self.decoder = zlib.decompressobj(wbits=31)
        self.pending = b""
        self.finished = False

    def read(self, size):
        if size < 0 or size > MAX_READ_BYTES:
            raise ValueError("Compressed read exceeds byte limit")
        result = bytearray()
        while len(result) < size and not self.finished:
            data = self.pending or self.stream.read(CHUNK)
            if not data:
                raise EOFError("Compressed stream ended before the end-of-stream marker")
            result.extend(self.decoder.decompress(data, size - len(result)))
            self.pending = self.decoder.unconsumed_tail
            if self.decoder.eof:
                if self.decoder.unused_data or self.stream.read(1):
                    raise ValueError("Unexpected compressed trailing bytes or additional gzip member")
                self.finished = True
        return bytes(result)


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


def safe_name(name, *, portable=False):
    if (not isinstance(name, str) or not name or len(name.encode("utf-8", errors="strict" if portable else "surrogateescape")) > MAX_NAME_BYTES
            or "\\" in name or "\0" in name):
        raise ValueError("Unsafe archive member name")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        raise ValueError("Unsafe archive member name")
    if portable:
        if name == "." or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError("Unsafe portable archive member name")
        for component in path.parts:
            stem = component.split(".", 1)[0].casefold()
            if (":" in component or component.rstrip(". ") != component
                    or stem in {"con", "prn", "aux", "nul"}
                    or re.fullmatch(r"(?:com|lpt)[1-9]", stem)):
                raise ValueError("Unsafe portable archive member name")
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


def signature(metadata, *, permissions=True, birthtime=False):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_mode if permissions else stat.S_IFMT(metadata.st_mode),
            metadata.st_uid, metadata.st_gid,
            metadata.st_birthtime_ns if birthtime else metadata.st_ctime_ns,
            getattr(metadata, "st_rdev", None))


def check_snapshot(path, expected, actual, phase, *, permissions=True, birthtime=False):
    before = signature(expected, permissions=permissions, birthtime=birthtime)
    after = signature(actual, permissions=permissions, birthtime=birthtime)
    fields = ("device", "inode", "size", "mtime_ns", "mode" if permissions else "file type", "uid", "gid",
              "birthtime_ns" if birthtime else "ctime_ns", "rdev")
    changed = [field for field, left, right in zip(fields, before, after) if left != right]
    if changed:
        raise ValueError("Evidence changed " + phase + ": " + str(path) + " (" + ", ".join(changed) + ")")


@contextmanager
def open_regular(path, metadata):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("Expected regular evidence file: " + str(path))
        # CPython's Windows path stat adds execute bits for .exe/.bat/.cmd/.com;
        # fd stat derives permissions from attributes without that suffix rule.
        # Its path stat also aliases ctime to creation time, while fd stat uses
        # metadata change time. Python 3.12+ exposes birthtime in both APIs.
        # Compare identity across APIs, then full mode within each API. Preserve
        # full ctime checks within each API and the path mode in the archive.
        check_snapshot(path, metadata, opened, "before it was read", permissions=os.name != "nt",
                       birthtime=os.name == "nt" and hasattr(metadata, "st_birthtime_ns"))
        check_snapshot(path, metadata, path.lstat(), "before it was read")
        yield stream
        check_snapshot(path, opened, os.fstat(stream.fileno()), "while it was read")
        check_snapshot(path, metadata, path.lstat(), "while it was read")


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
            record["sha256"] = reader.digest.hexdigest()
    else:
        archive.addfile(info)
    # Streaming tar still caches TarInfo objects on Python 3.12. The manifest
    # owns the inventory; do not retain a second copy for hundreds of thousands of files.
    archive.members.clear()
    return record


def selected_paths(values, destination):
    roots = [artifact_path(value) for value in values]
    output = artifact_path(destination)
    if not roots:
        raise ValueError("At least one evidence root is required")
    ordered = sorted(roots)
    if any(right.is_relative_to(left) for left, right in zip(ordered, ordered[1:])):
        raise ValueError("Evidence roots must not overlap or repeat")
    for root in roots:
        if not root.is_dir():
            raise ValueError("Every evidence root must be an existing directory")
        if output.is_relative_to(root):
            raise ValueError("Evidence output must be outside every input root")
    if output.exists() or output.is_symlink():
        raise ValueError("Evidence output must not already exist")
    return roots, output


def source_tree(roots):
    entries, omitted = [], []
    for root in roots:
        for path, metadata in walk(root, root, omitted):
            if len(entries) == MAX_MEMBERS:
                raise ValueError("Evidence inventory exceeds member limit")
            link = os.readlink(path) if stat.S_ISLNK(metadata.st_mode) else None
            entries.append((path, metadata, link))
    return entries, omitted


def check_tree(roots, before, omitted):
    after, current_omitted = source_tree(roots)
    if [item[0] for item in before] != [item[0] for item in after] or omitted != current_omitted:
        raise ValueError("Evidence changed while it was packed: directory member inventory")
    for (path, expected, link), (_, actual, current_link) in zip(before, after):
        check_snapshot(path, expected, actual, "while the tree was packed")
        if link != current_link:
            raise ValueError("Evidence changed while it was packed: link metadata")


def physical_info(name, size):
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.uid, info.gid, info.mtime, info.size = tarfile.REGTYPE, 0o644, 0, 0, 0, size
    return info


def typed_info(record):
    info = tarfile.TarInfo(record["name"])
    info.type, info.mode, info.size = TYPES[record["type"]], record["mode"], record["size"]
    info.uid, info.gid, info.mtime = record["uid"], record["gid"], record["mtime"]
    info.linkname = record.get("link", "")
    info.devmajor, info.devminor = record.get("deviceMajor", 0), record.get("deviceMinor", 0)
    return info


def pack(values, destination, format_version=1):
    if format_version not in (1, 2):
        raise ValueError("Unsupported evidence format")
    roots, output = selected_paths(values, destination)
    entries, omitted = source_tree(roots)
    manifest = {"formatVersion": format_version, "roots": [root.relative_to(ARTIFACTS).as_posix() for root in roots],
                "excluded": omitted, "members": []}
    objects = {}
    logical_bytes = 0
    for path, metadata, link in entries:
        entry_kind = kind(metadata)
        record = {"name": path.relative_to(ARTIFACTS).as_posix(), "type": entry_kind,
                  "mode": stat.S_IMODE(metadata.st_mode), "size": metadata.st_size if entry_kind == "file" else 0}
        logical_bytes += record["size"]
        if record["size"] > MAX_FILE_BYTES or logical_bytes > MAX_LOGICAL_BYTES:
            raise ValueError("Evidence source exceeds byte limit")
        if link is not None:
            record["link"] = link
        if entry_kind in ("character", "block"):
            record.update(deviceMajor=os.major(metadata.st_rdev), deviceMinor=os.minor(metadata.st_rdev))
        if format_version == 2:
            record.update(uid=metadata.st_uid, gid=metadata.st_gid, mtime=int(metadata.st_mtime))
        # Validate names/types/aliases before allocating output or opening any
        # regular content. V1 hashes are filled by the original one-pass writer.
        if entry_kind == "file":
            record["sha256"] = "0" * 64
        manifest["members"].append(record)
    validate_inventory(manifest, check_objects=False)
    if format_version == 2:
        for record, (path, metadata, _) in zip(manifest["members"], entries):
            if record["type"] != "file":
                continue
            with open_regular(path, metadata) as stream:
                reader = HashReader(stream)
                consume(reader)
                record["sha256"] = reader.digest.hexdigest()
            previous = objects.setdefault(record["sha256"], (path, metadata, record["size"]))
            if previous[2] != record["size"]:
                raise ValueError("Content digest has conflicting byte sizes")
    output.mkdir(parents=True)
    archive_path = output / "evidence.tar.gz"
    with archive_path.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=1, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT if format_version == 1 else tarfile.USTAR_FORMAT,
                          dereference=False) as archive:
            if format_version == 1:
                manifest["members"] = [add_member(archive, path, metadata) for path, metadata, _ in entries]
            else:
                import io
                encoded = canonical(inventory_only(manifest))
                archive.addfile(physical_info("inventory.json", len(encoded)), io.BytesIO(encoded))
                archive.members.clear()
                for digest, (path, metadata, size) in objects.items():
                    with open_regular(path, metadata) as stream:
                        reader = HashReader(stream)
                        archive.addfile(physical_info("objects/sha256/" + digest, size), reader)
                        if reader.digest.hexdigest() != digest:
                            raise ValueError("Evidence changed between hashing and object packing")
                    archive.members.clear()
    check_tree(roots, entries, omitted)
    manifest["archive"] = {"name": archive_path.name, "size": archive_path.stat().st_size,
                           "sha256": file_hash(archive_path)}
    encoded = (json.dumps(manifest, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("Evidence manifest exceeds byte limit")
    with (output / "manifest.json").open("xb") as stream:
        stream.write(encoded)
    return {"formatVersion": format_version, "members": len(manifest["members"]), "excludedCaches": len(manifest["excluded"]),
            "objects": len(objects) if format_version == 2 else None, "logicalBytes": logical_bytes,
            "archiveBytes": manifest["archive"]["size"], "manifestBytes": len(encoded),
            "transportBytes": manifest["archive"]["size"] + len(encoded), "archiveSHA256": manifest["archive"]["sha256"]}


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate manifest key: " + key)
        value[key] = item
    return value


def valid_record(record, roots, format_version=1):
    if not isinstance(record, dict):
        raise ValueError("Invalid member record")
    path = safe_name(record["name"], portable=format_version == 2)
    if owner_root(path, roots) is None:
        raise ValueError("Archive member is outside the declared evidence roots")
    entry_kind = record["type"]
    if not isinstance(entry_kind, str) or entry_kind not in TYPES:
        raise ValueError("Invalid member type")
    required = {"name", "type", "mode", "size"}
    if entry_kind == "file":
        required.add("sha256")
        if not isinstance(record.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", record["sha256"]):
            raise ValueError("Invalid file SHA-256")
    elif entry_kind == "symlink":
        required.add("link")
        if (not isinstance(record.get("link"), str) or "\0" in record["link"]
                or len(record["link"].encode("utf-8", errors="strict" if format_version == 2 else "surrogateescape")) > MAX_LINK_BYTES):
            raise ValueError("Invalid link metadata")
    elif entry_kind in ("character", "block"):
        required.update(("deviceMajor", "deviceMinor"))
        if any(type(record.get(key)) is not int or not 0 <= record[key] <= 2**32-1 for key in ("deviceMajor", "deviceMinor")):
            raise ValueError("Invalid device metadata")
    if format_version == 2:
        required.update(("uid", "gid", "mtime"))
        if any(type(record.get(key)) is not int or not 0 <= record[key] <= 2**32-1 for key in ("uid", "gid")):
            raise ValueError("Invalid member ownership")
        if type(record.get("mtime")) is not int or not -(2**63) <= record["mtime"] < 2**63:
            raise ValueError("Invalid member mtime")
    if entry_kind not in TYPES or set(record) != required:
        raise ValueError("Invalid member type or fields")
    if type(record["mode"]) is not int or not 0 <= record["mode"] <= 0o7777:
        raise ValueError("Invalid member mode")
    if type(record["size"]) is not int or record["size"] < 0 or (entry_kind != "file" and record["size"] != 0):
        raise ValueError("Invalid member size")
    if record["size"] > MAX_FILE_BYTES:
        raise ValueError("Member byte size exceeds limit")
    if format_version == 2 and record["size"] > MAX_OBJECT_BYTES:
        raise ValueError("Content object byte size exceeds USTAR limit")


def folded(name):
    return unicodedata.normalize("NFC", name).casefold()


def owner_root(path, roots):
    if path in roots:
        return path
    return next((parent for parent in path.parents if parent in roots), None)


def validate_inventory(manifest, *, check_objects=True):
    if (not isinstance(manifest, dict) or type(manifest.get("formatVersion")) is not int
            or manifest["formatVersion"] not in (1, 2)):
        raise ValueError("Unsupported evidence manifest")
    required = {"formatVersion", "roots", "excluded", "members"}
    if set(manifest) not in (required, required | {"archive"}) or any(
            not isinstance(manifest[key], list) for key in ("roots", "excluded", "members")):
        raise ValueError("Incomplete evidence manifest")
    if len(manifest["members"]) > MAX_MEMBERS:
        raise ValueError("Evidence inventory exceeds member limit")
    if len(manifest["roots"]) > MAX_MEMBERS:
        raise ValueError("Evidence root inventory exceeds member limit")
    if len(canonical(inventory_only(manifest))) > MAX_MANIFEST_BYTES:
        raise ValueError("Evidence manifest exceeds byte limit")
    portable = manifest["formatVersion"] == 2
    roots = [safe_name(name, portable=portable) for name in manifest["roots"]]
    ordered = sorted(PurePosixPath(folded(str(root))) if portable else root for root in roots)
    if not roots or any(right.is_relative_to(left) for left, right in zip(ordered, ordered[1:])):
        raise ValueError("Invalid declared evidence roots or path aliases")
    root_set = set(roots)
    omitted = [safe_name(name, portable=portable) for name in manifest["excluded"]]
    if len({folded(str(path)) if portable else str(path) for path in omitted}) != len(omitted):
        raise ValueError("Invalid excluded cache inventory")
    for path in omitted:
        root = owner_root(path, root_set)
        if root is None or not excluded(path.relative_to(root).parts):
            raise ValueError("Invalid excluded cache inventory")
    omitted_set = set(omitted)
    names, aliases, objects = {}, {}, {}
    total = 0
    for record in manifest["members"]:
        valid_record(record, root_set, manifest["formatVersion"])
        path = PurePosixPath(record["name"])
        if owner_root(path, omitted_set) is not None:
            raise ValueError("An excluded cache is present in the archive inventory")
        if record["name"] in names:
            raise ValueError("Duplicate manifest member")
        alias = folded(record["name"])
        if portable and alias in aliases:
            raise ValueError("Archive member path alias collision")
        names[record["name"]], aliases[alias] = record, record["name"]
        total += record["size"]
        if total > MAX_LOGICAL_BYTES:
            raise ValueError("Logical evidence bytes exceed limit")
        if record["type"] == "file" and check_objects:
            previous = objects.setdefault(record["sha256"], record["size"])
            if previous != record["size"]:
                raise ValueError("Content digest has conflicting byte sizes")
    for root in roots:
        if names.get(str(root), {}).get("type") != "directory":
            raise ValueError("Missing root directory record")
    if portable:
        for name in names:
            path = PurePosixPath(name)
            root = owner_root(path, root_set)
            for parent in path.parents:
                if not parent.is_relative_to(root):
                    break
                if names.get(str(parent), {}).get("type") != "directory":
                    raise ValueError("Missing or non-directory logical parent")
    return objects


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


def read_manifest(value):
    path = artifact_path(value)
    metadata = path.lstat()
    if metadata.st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Evidence manifest exceeds byte limit")
    with open_regular(path, metadata) as stream:
        encoded = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("Evidence manifest exceeds byte limit")
    def reject_constant(value):
        raise ValueError("Invalid JSON constant: " + value)
    manifest = json.loads(encoded, object_pairs_hook=unique_object, parse_constant=reject_constant)
    objects = validate_inventory(manifest)
    if "archive" not in manifest:
        raise ValueError("Incomplete evidence manifest")
    claim = manifest["archive"]
    if not isinstance(claim, dict) or set(claim) != {"name", "size", "sha256"} or claim.get("name") != "evidence.tar.gz":
        raise ValueError("Invalid archive identity")
    if type(claim.get("size")) is not int or not 0 < claim["size"] <= MAX_ARCHIVE_BYTES or not isinstance(claim.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", claim["sha256"]):
        raise ValueError("Invalid archive identity")
    archive_path = artifact_path(path.parent / claim["name"])
    archive_metadata = archive_path.lstat()
    if archive_metadata.st_size != claim["size"]:
        raise ValueError("Archive byte size mismatch")
    return manifest, encoded, archive_path, archive_metadata, objects


def padded(size):
    return (size + 511) // 512 * 512


def read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        value = stream.read(min(CHUNK, size - len(result)))
        if not value:
            raise ValueError("Missing archive bytes")
        result.extend(value)
    return bytes(result)


def check_cas(stream, manifest, objects, spool=None):
    inventory = canonical(inventory_only(manifest))
    sizes = [len(inventory), *objects.values()]
    payload_size = sum(512 + padded(size) for size in sizes)
    expanded_size = ((payload_size + 1024 + tarfile.RECORDSIZE - 1) // tarfile.RECORDSIZE) * tarfile.RECORDSIZE
    bounded = BoundedReader(stream, expanded_size)
    entries = [("inventory.json", len(inventory), None)]
    entries.extend(("objects/sha256/" + digest, size, digest) for digest, size in objects.items())
    offsets = {}
    for name, size, digest in entries:
        header = read_exact(bounded, 512)
        if header == b"\0" * 512:
            raise ValueError("Missing content object or inventory entry")
        # Content transport has no PAX/global/long-name headers. Compare the
        # physical USTAR bytes before a general TAR parser can consume them.
        if header != physical_info(name, size).tobuf(format=tarfile.USTAR_FORMAT):
            raise ValueError("Unexpected content object or noncanonical physical header")
        remaining, offset, content_hash = size, 0, hashlib.sha256()
        if digest is not None and spool is not None:
            offsets[digest] = (spool.tell(), size)
        while remaining:
            data = read_exact(bounded, min(CHUNK, remaining))
            if digest is None:
                if data != inventory[offset:offset+len(data)]:
                    raise ValueError("Inner inventory bytes differ from manifest")
            else:
                content_hash.update(data)
                if spool is not None:
                    spool.write(data)
            offset += len(data)
            remaining -= len(data)
        if digest is not None and content_hash.hexdigest() != digest:
            raise ValueError("Content object SHA-256 mismatch")
        if any(read_exact(bounded, padded(size) - size)):
            raise ValueError("Unexpected nonzero object padding")
    padding = read_exact(bounded, expanded_size - payload_size)
    if any(padding):
        raise ValueError("Unexpected or duplicate content object after inventory")
    # Reach the gzip footer/CRC and reject additional expanded bytes, including
    # concatenated compressed members. This does not instantiate any TAR entry.
    if bounded.read(1):
        raise ValueError("Unexpected expanded archive bytes")
    return offsets


def check_archive(manifest, archive_path, archive_metadata, objects, spool=None):
    claim = manifest["archive"]
    offsets, count = {}, 0
    with open_regular(archive_path, archive_metadata) as raw:
        reader = HashReader(raw)
        if manifest["formatVersion"] == 2:
            offsets = check_cas(SingleGzipReader(reader), manifest, objects, spool)
            count = len(manifest["members"])
        else:
            with gzip.GzipFile(fileobj=reader, mode="rb") as compressed:
                # V1 is the historical general typed TAR format. Its bounded
                # allowance includes original PAX name/link/numeric headers.
                limit = sum(padded(record["size"]) + 512 + 4 * MAX_NAME_BYTES
                            + MAX_LINK_BYTES for record in manifest["members"]) + tarfile.RECORDSIZE
                bounded = BoundedReader(compressed, limit)
                with tarfile.open(fileobj=bounded, mode="r|") as archive:
                    for info in archive:
                        if count == len(manifest["members"]):
                            raise ValueError("Unexpected or duplicate archive member")
                        check_member(archive, info, manifest["members"][count])
                        count += 1
                        archive.members.clear()
                consume(bounded)
        consume(reader)
        if reader.digest.hexdigest() != claim.get("sha256"):
            raise ValueError("Archive SHA-256 mismatch")
    if count != len(manifest["members"]):
        raise ValueError("Missing archive member")
    return offsets


def verify(value):
    manifest, _, archive_path, metadata, objects = read_manifest(value)
    check_archive(manifest, archive_path, metadata, objects)
    return {"formatVersion": manifest["formatVersion"], "members": len(manifest["members"]),
            "objects": len(objects) if manifest["formatVersion"] == 2 else None,
            "archiveSHA256": manifest["archive"]["sha256"], "verified": True}


def export(value, destination):
    output = artifact_path(destination)
    if output.exists() or output.is_symlink():
        raise ValueError("Evidence output must not already exist")
    manifest, encoded, archive_path, metadata, objects = read_manifest(value)
    # Only regular object bytes enter this anonymous temporary file. No logical
    # path, link, FIFO, socket or device is ever extracted onto the filesystem.
    with tempfile.TemporaryFile() as spool:
        offsets = check_archive(manifest, archive_path, metadata, objects, spool)
        output.mkdir(parents=True)
        target = output / "evidence.tar.gz"
        if manifest["formatVersion"] == 1:
            # Preserve every original V1 TAR header and compressed byte, including
            # any historical PAX headers, rather than normalizing a replay.
            with open_regular(archive_path, metadata) as source, target.open("xb") as destination_stream:
                reader = HashReader(source)
                while data := reader.read(CHUNK):
                    destination_stream.write(data)
                if reader.digest.hexdigest() != manifest["archive"]["sha256"]:
                    raise ValueError("Archive SHA-256 changed during export")
        else:
            with target.open("xb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=1, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT, dereference=False) as archive:
                    for record in manifest["members"]:
                        info = typed_info(record)
                        if record["type"] == "file":
                            spool.seek(offsets[record["sha256"]][0])
                            archive.addfile(info, spool)
                        else:
                            archive.addfile(info)
                        archive.members.clear()
            legacy = {**inventory_only(manifest), "formatVersion": 1,
                      "members": [{key: item for key, item in record.items() if key not in {"uid", "gid", "mtime"}}
                                  for record in manifest["members"]],
                      "archive": {"name": target.name, "size": target.stat().st_size, "sha256": file_hash(target)}}
            encoded = (json.dumps(legacy, separators=(",", ":")) + "\n").encode("utf-8")
        with (output / "manifest.json").open("xb") as stream:
            stream.write(encoded)
    checked = verify(output / "manifest.json")
    return {**checked, "sourceFormatVersion": manifest["formatVersion"], "exported": True,
            "archiveBytes": target.stat().st_size, "manifestBytes": len(encoded),
            "transportBytes": target.stat().st_size + len(encoded)}


def retain(values, destination):
    roots, output = selected_paths(values, destination)
    output.mkdir(parents=True)
    report = {"schemaVersion": 1, "actualFormat": None, "fallbackReason": None, "verified": False,
              "roots": [str(root) for root in roots], "attempts": []}
    for label, version in (("cas-v2", 2), ("v1", 1)):
        try:
            packed = pack(roots, output / label, format_version=version)
            checked = verify(output / label / "manifest.json")
            report["attempts"].append({"format": label, "packed": packed, "verification": checked, "verified": True})
            report.update(actualFormat=label, verified=True, manifest=label + "/manifest.json")
            break
        except ERRORS as error:
            # A transport failure never changes a test report/receipt and never
            # deletes source trees or partial archive bytes. Fallback must itself
            # pass the full historical verifier before retention can succeed.
            failure = str(error)
            (output / (label + ".error.txt")).write_text(traceback.format_exc(), encoding="utf-8")
            report["attempts"].append({"format": label, "verified": False, "error": failure})
            if version == 2:
                report["fallbackReason"] = failure
    (output / "retention.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not report["verified"]:
        raise ValueError("Both evidence formats failed; retention is incomplete. Raw sources and partial outputs were retained: " + str(output))
    if report["fallbackReason"] is not None:
        print("CAS transport failed; verified v1 fallback retained: " + report["fallbackReason"], file=sys.stderr)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    writer = commands.add_parser("pack")
    writer.add_argument("--root", type=Path, action="append", required=True)
    writer.add_argument("--output", type=Path, required=True)
    writer.add_argument("--format", choices=("v1", "cas-v2"), default="v1")
    reader = commands.add_parser("verify")
    reader.add_argument("--manifest", type=Path, required=True)
    exporter = commands.add_parser("export")
    exporter.add_argument("--manifest", type=Path, required=True)
    exporter.add_argument("--output", type=Path, required=True)
    retention = commands.add_parser("retain")
    retention.add_argument("--root", type=Path, action="append", required=True)
    retention.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "pack":
            result = pack(args.root, args.output, format_version=2 if args.format == "cas-v2" else 1)
        elif args.command == "export":
            result = export(args.manifest, args.output)
        elif args.command == "retain":
            result = retain(args.root, args.output)
        else:
            result = verify(args.manifest)
        print(json.dumps(result, sort_keys=True))
    except ERRORS as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
