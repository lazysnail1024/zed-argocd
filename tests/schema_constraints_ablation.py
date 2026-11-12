#!/usr/bin/env python3
"""Prove the raw-schema oracle detects separately lost production constraints."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TEST = "TestEveryReleaseSchemaConstraints"
VARIANTS = ("baseline", "type", "required", "enum")


def run(command, folder, name):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=180)
    (folder / (name + ".stdout")).write_bytes(result.stdout)
    (folder / (name + ".stderr")).write_bytes(result.stderr)
    (folder / (name + ".json")).write_text(json.dumps({
        "command": command, "exitCode": result.returncode,
    }, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "rows": [], "scope": "real production compiler and independent raw-schema oracle"}
    try:
        go = os.environ.get("GO") or shutil.which("go")
        if not go:
            raise RuntimeError("Go is required")
        source = ROOT / "internal/assets/schema.go"
        original = source.read_text(encoding="utf-8")
        anchor = "\tif err := compiler.AddResource(uri, value); err != nil {"
        if original.count(anchor) != 1:
            raise ValueError("production compiler mutation anchor changed")
        report["inputs"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            "internal/assets/schema.go", "internal/assets/catalog_test.go", "internal/assets/catalog.json",
            "tests/schema_constraints_ablation.py", "go.mod", "go.sum",
        )}
        for variant in VARIANTS:
            folder = output / variant
            folder.mkdir()
            binary = folder / ("assets.test.exe" if os.name == "nt" else "assets.test")
            build = [go, "test", "-mod=vendor", "-c", "-o", str(binary)]
            if variant != "baseline":
                # Visit schema nodes only: a property *named* "type" is data,
                # and must remain present in the type-keyword counterfactual.
                helper = '''
func auditDropConstraint(node map[string]any) {
    delete(node, %s)
    for _, key := range []string{"properties", "$defs", "definitions"} {
        if group, ok := node[key].(map[string]any); ok {
            for _, value := range group {
                if child, ok := value.(map[string]any); ok { auditDropConstraint(child) }
            }
        }
    }
    for _, key := range []string{"items", "additionalProperties"} {
        if child, ok := node[key].(map[string]any); ok { auditDropConstraint(child) }
    }
    for _, key := range []string{"anyOf", "allOf", "oneOf"} {
        if group, ok := node[key].([]any); ok {
            for _, value := range group {
                if child, ok := value.(map[string]any); ok { auditDropConstraint(child) }
            }
        }
    }
}
''' % json.dumps(variant)
                mutant = folder / "schema.go"
                mutant.write_text(original.replace(anchor, "\tauditDropConstraint(value)\n" + anchor) + helper,
                                  encoding="utf-8")
                overlay = folder / "overlay.json"
                overlay.write_text(json.dumps({"Replace": {str(source): str(mutant)}}), encoding="utf-8")
                build.append("-overlay=" + str(overlay))
            built = run(build + ["./internal/assets"], folder, "build")
            if built.returncode != 0:
                raise RuntimeError(variant + ": compilation failure is not a detected mutation")
            result = run([str(binary), "-test.run=^" + TEST + "$", "-test.v"], folder, "oracle")
            stdout = result.stdout.decode("utf-8", errors="replace")
            expected = "PASS" if variant == "baseline" else "FAIL"
            checks = {
                "compiled": built.returncode == 0,
                "exactTestRan": stdout.count("=== RUN   " + TEST + "\n") == 1,
                "normalCompletion": result.returncode == (0 if variant == "baseline" else 1)
                    and stdout.endswith(expected + "\n") and ("--- " + expected + ": " + TEST + " (") in stdout,
                "namedConstraint": variant == "baseline" or ("declared " + variant + " constraint discarded at ") in stdout,
                "noPanic": b"panic:" not in result.stderr and b"fatal error:" not in result.stderr,
            }
            row = {"id": variant, "checks": checks, "passed": all(checks.values()),
                   "binarySHA256": hashlib.sha256(binary.read_bytes()).hexdigest()}
            report["rows"].append(row)
            if not row["passed"]:
                raise RuntimeError(variant + ": oracle did not reach its expected semantic result")
        report["passed"] = [row["id"] for row in report["rows"]] == list(VARIANTS)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
