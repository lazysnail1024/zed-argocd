#!/usr/bin/env python3
"""Prove the existing identity process oracle detects two compiling guard deletions."""
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

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"ready": 0, "wrong-product": 2, "not-ready": 2, "wrong-contract": 2,
            "missing-product": 2, "invalid-json": 2, "empty-catalog": 2}
GUARDS = [
    ("product", "wrong-product", '''    if id.product != "argo-cd" {
        return Err("Expected argo-cd server product".into());
    }
'''),
    ("readiness", "not-ready", '''    if !id.ready {
        return Err("Argo CD server analysis is not ready".into());
    }
'''),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/cd-1/ablation")
    out = parser.parse_args().output.absolute()
    out.mkdir(parents=True, exist_ok=False)
    report = {"command": sys.argv, "completed": False, "passed": False, "rows": []}
    try:
        channel = tomllib.loads((ROOT / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
        report["rustToolchain"] = channel
        report["sourceSHA256"] = {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ["src/identity.rs", "examples/identity_probe.rs", "Cargo.toml", "Cargo.lock",
                         "rust-toolchain.toml", "tests/adapter_e2e.py", "tests/ablate_identity.py"]
        }
        with tempfile.TemporaryDirectory(prefix="argocd-identity-ablation-") as temporary:
            for name, affected, guard in [("baseline", None, None), *GUARDS]:
                tree = Path(temporary) / name
                tree.mkdir()
                evidence = out / name
                evidence.mkdir()
                row = {"variant": name, "expectedFailure": affected, "passed": False}
                report["rows"].append(row)
                for entry in ["Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "native-server-version"]:
                    shutil.copy2(ROOT / entry, tree / entry)
                for entry in ["src", "examples"]:
                    shutil.copytree(ROOT / entry, tree / entry)
                source = tree / "src/identity.rs"
                text = source.read_text()
                if guard is not None and text.count(guard) != 1:
                    raise ValueError(f"{name}: expected exactly one current guard block")
                if guard is not None:
                    text = text.replace(guard, "", 1)
                    source.write_text(text)
                shutil.copy2(source, evidence / "identity.rs")
                command = ["cargo", "+" + channel, "build", "--offline", "--locked",
                           "--manifest-path", str(tree / "Cargo.toml"),
                           "--target-dir", str(Path(temporary) / "target"), "--example", "identity_probe"]
                build = subprocess.run(command, cwd=tree, capture_output=True, timeout=300)
                (evidence / "build.stdout").write_bytes(build.stdout)
                (evidence / "build.stderr").write_bytes(build.stderr)
                row.update(buildCommand=command, buildExit=build.returncode,
                           identitySHA256=hashlib.sha256(source.read_bytes()).hexdigest())
                if build.returncode != 0:
                    raise ValueError(f"{name}: compilation failed; not a detected regression")
                executable = "identity_probe.exe" if os.name == "nt" else "identity_probe"
                binary = evidence / executable
                shutil.copy2(Path(temporary) / "target/debug/examples" / executable, binary)
                command = [sys.executable, str(ROOT / "tests/adapter_e2e.py"), str(binary),
                           str(evidence / "oracle")]
                oracle = subprocess.run(command, capture_output=True, timeout=120)
                (evidence / "oracle.stdout").write_bytes(oracle.stdout)
                (evidence / "oracle.stderr").write_bytes(oracle.stderr)
                row.update(oracleCommand=command, oracleExit=oracle.returncode,
                           binarySHA256=hashlib.sha256(binary.read_bytes()).hexdigest())
                cases = json.loads((evidence / "oracle/results.json").read_bytes())
                expected_exits = dict(EXPECTED)
                if affected:
                    expected_exits[affected] = 0
                checks = {
                    "exactCases": [case["id"] for case in cases] == list(EXPECTED),
                    "expectedExits": {case["id"]: case["exit"] for case in cases} == expected_exits,
                    "oracleExpectationsUnchanged": all(case["expectedExit"] == EXPECTED[case["id"]] for case in cases),
                    "onlyExpectedFailure": [case["id"] for case in cases if not case["passed"]] == ([affected] if affected else []),
                    "oracleExit": oracle.returncode == (1 if affected else 0),
                }
                row.update(checks=checks, passed=all(checks.values()),
                           decision="retain guard" if affected else "unmodified control passes")
                if not row["passed"]:
                    raise ValueError(f"{name}: oracle did not show the expected behavior and intact controls")
        report["completed"] = True
        report["passed"] = [row["variant"] for row in report["rows"]] == ["baseline", "product", "readiness"]
    except Exception as error:
        report["error"] = repr(error)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "rows": report["rows"], "error": report.get("error")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
