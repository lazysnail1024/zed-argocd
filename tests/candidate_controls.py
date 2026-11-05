"""Exercise candidate rejection with the release E2E's existing native artifacts.

Only this host's native server and supplied WASM are real builds. Foreign host
records remain synthetic parser controls, never five-platform runtime evidence.
"""

import hashlib
import json
from pathlib import Path
import shutil
import sys


def exercise(invoke, record, fixture, inputs, output, common, wasm_inputs):
    prefix = "tools" if (fixture / "tools/candidate.py").is_file() else "scripts"
    tool = [sys.executable, fixture / prefix / "candidate.py"]
    version, commit = common[1], common[3]
    invoke("candidate-version-without-tag", tool + ["version", "--source-commit", commit])
    invoke("candidate-requested-version-mismatch", tool + ["version", "--source-commit", commit,
           "--requested-version", "999.0.0"], "must match")
    wasm = []
    for item in wasm_inputs:
        variant, separator, path = item.partition("=")
        if not separator:
            raise ValueError("Expected variant=path for existing compiled WASM")
        wasm += ["--wasm", variant + "=" + str(Path(path).absolute())]
    adapters = output / "candidate-adapter-controls"
    platforms = ["linux/amd64", "linux/arm64", "darwin/amd64", "darwin/arm64", "windows/amd64"]
    for platform in platforms:
        suffix = platform.replace("/", "-")
        invoke("candidate-stage-" + suffix, tool + ["adapter", "--native-assets", inputs / suffix,
               "--platform", platform, *common, *wasm, "--output", adapters / suffix])
    collect = tool + ["collect", "--native-inputs", inputs, "--adapter-inputs", adapters, *common]
    combined = output / "candidate-combined"
    collected = invoke("candidate-aggregate-controls", collect + ["--output", combined])
    report = json.loads((combined / "candidate.json").read_bytes())
    checksums = dict((name, value) for value, name in
                     (line.split("  ") for line in (combined / "SHA256SUMS").read_text().splitlines()))
    observed = {path.relative_to(combined).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in combined.rglob("*") if path.is_file() and path != combined / "SHA256SUMS"}
    record("candidate-complete-inventory", {
        "stdoutMatchesReceipt": json.loads(collected.stdout) == report,
        "allFilesHashed": checksums == observed,
        "allNativeHosts": set(report["platforms"]) == set(platforms),
        "explicitlyUnpublished": report["publication"] == "not-published" and report["installationChannel"] == "none",
        "editorNotClaimed": report["editorAcceptance"] == "not-executed"})
    chosen = adapters / "linux-amd64"
    manifest = chosen / "adapter.json"
    original = manifest.read_bytes()
    metadata = json.loads(original)
    name = sorted(metadata["wasm"])[0]
    artifact = chosen / name
    original_wasm = artifact.read_bytes()
    rejected = []

    def refuse(name, reason):
        destination = output / ("candidate-refused-" + name)
        invoke("candidate-" + name + "-refused", collect + ["--output", destination], reason)
        rejected.append(destination)

    artifact.write_bytes(original_wasm + b"corrupt")
    refuse("tampered-wasm", "SHA-256 or size mismatch")
    artifact.write_bytes(original_wasm)
    changed = json.loads(original)
    changed["sourceCommit"] = "0" * 40
    manifest.write_text(json.dumps(changed))
    refuse("mixed-source", "source identity or metadata mismatch")
    manifest.write_bytes(original)
    extra = chosen / "server.bin"
    extra.write_bytes(b"native payload is forbidden here")
    refuse("native-extra-file", "native payloads are forbidden")
    extra.unlink()
    native_record = json.loads(next((inputs / "linux-amd64").glob("release-*.json")).read_bytes())
    native = (inputs / "linux-amd64" / native_record["binaryAsset"]).read_bytes()
    embedded = original_wasm + b"\x00" + native[:4096]
    artifact.write_bytes(embedded)
    changed = json.loads(original)
    changed["wasm"][name] = {"sha256": hashlib.sha256(embedded).hexdigest(), "bytes": len(embedded)}
    manifest.write_text(json.dumps(changed))
    refuse("embedded-native-with-valid-hash", "Native payload found")
    artifact.write_bytes(original_wasm)
    manifest.write_bytes(original)
    fake = original_wasm[:8] + b"this is not a WASM component body"
    artifact.write_bytes(fake)
    changed = json.loads(original)
    changed["wasm"][name] = {"sha256": hashlib.sha256(fake).hexdigest(), "bytes": len(fake)}
    manifest.write_text(json.dumps(changed))
    refuse("fake-wasm-header", "WASM component framing")
    artifact.write_bytes(original_wasm)
    manifest.write_bytes(original)
    held = output / "held-adapter"
    chosen.rename(held)
    refuse("missing-adapter-host", "all five native hosts")
    held.rename(chosen)
    duplicate = adapters / "duplicate"
    shutil.copytree(chosen, duplicate)
    refuse("duplicate-adapter-host", "duplicate candidate adapter host")
    shutil.rmtree(duplicate)
    record("candidate-failure-is-atomic", {"noPartialCandidateOutputs": all(not path.exists() for path in rejected)})

