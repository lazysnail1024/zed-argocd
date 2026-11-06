#!/usr/bin/env python3
"""Black-box file-install acceptance using the immutable, public Zed adapter Release.

This does not start Zed or claim GUI/LSP activation acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = tomllib.loads((ROOT / "extension.toml").read_text())
ID = EXTENSION["id"]
VERSION = EXTENSION["version"]
REPO = EXTENSION["repository"].removeprefix("https://github.com/")
ARCHIVE = f"{REPO.split('/')[-1]}-{VERSION}.zip"
IS_WINDOWS = sys.platform == "win32"
SCRIPT = ROOT / ("install.ps1" if IS_WINDOWS else "install.sh")


def download(version, output):
    output.mkdir(parents=True)
    name = f"{REPO.split('/')[-1]}-{version}.zip"
    base = f"https://github.com/{REPO}/releases/download/zed-v{version}"
    for asset in ("SHA256SUMS", name):
        request = urllib.request.Request(f"{base}/{asset}", headers={"User-Agent": "zed-install-e2e"})
        with urllib.request.urlopen(request, timeout=120) as response:
            (output / asset).write_bytes(response.read())
    digest = hashlib.sha256((output / name).read_bytes()).hexdigest()
    lines = (output / "SHA256SUMS").read_text().splitlines()
    assert lines.count(f"{digest}  {name}") == 1, f"Published SHA256SUMS mismatch: {name}"
    return name


def snapshot(folder):
    if not folder.exists():
        return None
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob("*") if p.is_file()}


def check_identity(folder, version):
    manifest = json.loads((folder / "MANIFEST.json").read_text())
    assert manifest["extensionId"] == ID and manifest["version"] == version
    assert (folder / "extension.wasm").stat().st_size > 1000
    assert (folder / "extension.toml").is_file()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    cases = []
    with tempfile.TemporaryDirectory(prefix="zed-alpha-file-install-") as temporary:
        root = Path(temporary)
        home = root / "User Home With Spaces"
        home.mkdir()
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["XDG_DATA_HOME"] = str(root / "XDG Data With Spaces")
        env["LOCALAPPDATA"] = str(root / "Local App Data With Spaces")
        installed = ((Path(env["LOCALAPPDATA"]) / "Zed/extensions/installed") if IS_WINDOWS
                     else ((home / "Library/Application Support/Zed/extensions/installed")
                           if sys.platform == "darwin" else
                           (Path(env["XDG_DATA_HOME"]) / "zed/extensions/installed")))
        target = installed / ID
        state_dir = installed.parent / ".alpha-installer"
        previous = state_dir / f"{ID}.previous"

        def run(label, *options, expected_success=True, script=SCRIPT, process_env=env,
                windows_host="powershell.exe"):
            if IS_WINDOWS:
                command = [windows_host, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(script), *options]
            else:
                command = ["sh", str(script), *options]
            result = subprocess.run(command, env=process_env, capture_output=True, text=True,
                                    timeout=240)
            successful = result.returncode == 0
            cases.append({"case": label, "exitCode": result.returncode,
                          "expectedSuccess": expected_success,
                          "output": (result.stdout + result.stderr)[-800:]})
            if args.report:
                args.report.mkdir(parents=True, exist_ok=True)
                (args.report / "report.json").write_text(json.dumps(
                    {"repository": REPO, "platform": sys.platform, "cases": cases},
                    indent=2) + "\n")
            assert successful == expected_success, f"{label}: {result.stdout}\n{result.stderr}"
            assert not list(state_dir.glob(f".{ID}.install.*")), f"{label}: staging was not cleaned"
            assert sorted(p.name for p in installed.iterdir()) == [ID], (
                f"{label}: backup or staging leaked into Zed's installed directory")
            return result

        def opts(assets):
            return (["-AssetsDir", str(assets)] if IS_WINDOWS else
                    ["--assets-dir", str(assets)])

        def rollback():
            return ("-Rollback",) if IS_WINDOWS else ("--rollback",)

        # Exercise the actual online download path (shell curl / Windows Invoke-WebRequest).
        run("online-real-release")
        check_identity(target, VERSION)
        original = snapshot(target)
        assert original and previous.exists() is False
        original_timestamp = (target / "extension.wasm").stat().st_mtime_ns
        run("no-backup-rollback-refused", *rollback(), expected_success=False)
        assert snapshot(target) == original

        # Local fixtures contain the unchanged downloaded public release assets;
        # only negative cases modify copies under this isolated test directory.
        assets = root / "actual release assets"
        download(VERSION, assets)
        result = run("idempotent-reinstall", *opts(assets))
        assert "Already installed" in result.stdout, (
            f"Idempotence was not detected: {result.stdout!r} {result.stderr!r}; "
            f"same tree: {snapshot(target) == original}; backup: {previous.exists()}")
        assert snapshot(target) == original and not previous.exists()
        assert (target / "extension.wasm").stat().st_mtime_ns == original_timestamp

        # Argo CD has a published alpha.1 to exercise a *real* release upgrade.
        # Workflows has no earlier Zed Release, so simulate an old installation
        # by adding a legacy file to a copy of the actual alpha.1 tree.
        if ID == "argocd-lsp":
            older_version = "0.0.1-alpha.1"
            older_assets = root / "older real release assets"
            download(older_version, older_assets)
            options = (["-Version", older_version] if IS_WINDOWS else
                       ["--version", older_version])
            run("install-real-older-release", *options, *opts(older_assets))
            check_identity(target, older_version)
        else:
            (target / "old-adapter-marker.txt").write_text("previous install\n")
        old = snapshot(target)
        run("upgrade-and-retain-backup", *opts(assets))
        assert snapshot(target) == original and snapshot(previous) == old
        run("explicit-rollback", *rollback())
        assert snapshot(target) == old and snapshot(previous) == original
        run("rollback-is-reversible", *rollback())
        assert snapshot(target) == original and snapshot(previous) == old

        def unchanged():
            assert snapshot(target) == original and snapshot(previous) == old

        bad_hash = root / "bad checksum"
        shutil.copytree(assets, bad_hash)
        sums = bad_hash / "SHA256SUMS"
        sums.write_text(sums.read_text().replace(
            hashlib.sha256((assets / ARCHIVE).read_bytes()).hexdigest(), "0" * 64))
        run("reject-mismatched-checksum", *opts(bad_hash), expected_success=False)
        unchanged()

        bad_zip = root / "corrupt downloaded archive"
        shutil.copytree(assets, bad_zip)
        with (bad_zip / ARCHIVE).open("ab") as stream:
            stream.write(b"corrupt!")
        run("reject-corrupt-download", *opts(bad_zip), expected_success=False)
        unchanged()

        bad_payload = root / "invalid zip with matching checksum"
        shutil.copytree(assets, bad_payload)
        (bad_payload / ARCHIVE).write_bytes(b"invalid zip")
        checksum = hashlib.sha256((bad_payload / ARCHIVE).read_bytes()).hexdigest()
        (bad_payload / "SHA256SUMS").write_text(f"{checksum}  {ARCHIVE}\n")
        run("reject-invalid-zip-even-when-hash-matches", *opts(bad_payload),
            expected_success=False)
        unchanged()

        bad_identity = root / "wrong identity with matching checksum"
        shutil.copytree(assets, bad_identity)
        archive = bad_identity / ARCHIVE
        replacement = root / "wrong-identity.zip"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(replacement, "w") as output:
            for member in source.infolist():
                data = source.read(member)
                if member.filename == f"{ID}/MANIFEST.json":
                    data = data.replace(f'"extensionId": "{ID}"'.encode(),
                                        b'"extensionId": "wrong-lsp"')
                output.writestr(member, data)
        shutil.move(replacement, archive)
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        (bad_identity / "SHA256SUMS").write_text(f"{checksum}  {ARCHIVE}\n")
        run("reject-wrong-adapter-identity", *opts(bad_identity), expected_success=False)
        unchanged()

        # Fault-inject the activation move *after* the active and previous trees
        # have been rotated. Verify full recovery, including the older backup.
        injected = root / SCRIPT.name
        contents = SCRIPT.read_text()
        if IS_WINDOWS:
            anchor = ("    Move-Item -LiteralPath $staged -Destination $target\n"
                      "    $activated = $true")
            mutation = '    throw "injected activation failure"\n    $activated = $true'
        else:
            anchor = 'mv "$staged" "$target"\nactivated=true'
            mutation = 'false # injected activation failure\nactivated=true'
        assert contents.count(anchor) == 1, "Activation fault injection lost its target"
        injected.write_text(contents.replace(anchor, mutation))
        # Make the current tree different so the activation path is reached.
        (target / "need-replacement.txt").write_text("mutated\n")
        before_failure = snapshot(target)
        run("recover-after-activation-failure", *opts(assets), expected_success=False,
            script=injected)
        assert snapshot(target) == before_failure and snapshot(previous) == old
        run("repair-after-failure", *opts(assets))
        assert snapshot(target) == original and snapshot(previous) == before_failure

        # A hard interruption may leave only the last backup. The next invocation
        # restores it before proceeding with the requested installation.
        shutil.move(target, root / "active-before-interruption")
        run("recover-missing-active-directory", *opts(assets))
        assert snapshot(target) == original and snapshot(previous) == before_failure
        if IS_WINDOWS:
            run("powershell-7-compatibility", *opts(assets), windows_host="pwsh")

        # Check Linux's HOME fallback separately from XDG_DATA_HOME.
        if sys.platform == "linux":
            fallback = env.copy()
            fallback.pop("XDG_DATA_HOME")
            run("linux-home-default", *opts(assets), process_env=fallback)
            check_identity(home / ".local/share/zed/extensions/installed" / ID, VERSION)

    report = {"repository": REPO, "version": VERSION, "platform": sys.platform,
              "scope": "filesystem-only; Zed GUI not launched", "cases": cases}
    if args.report:
        args.report.mkdir(parents=True, exist_ok=True)
        (args.report / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS {REPO}: {len(cases)} isolated filesystem acceptance cases on {sys.platform}")


if __name__ == "__main__":
    main()
