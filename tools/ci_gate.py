#!/usr/bin/env python3
"""Require every same-run native lane before the existing release collector runs."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from native_ci import COMPILER_CASES, PLATFORM_COMPILERS, SUITES, inventory_digest, required_steps


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    require(not path.is_symlink() and stat.S_ISREG(path.stat().st_mode), "Expected a regular report: " + str(path))
    require(path.stat().st_size <= 4 * 1024 * 1024, "Report exceeds its size limit")
    return json.loads(path.read_text(encoding="utf-8"))


def source_context(args):
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT)
    require(re.fullmatch("[0-9a-f]{40}", args.source_commit) and head == args.source_commit and not dirty,
            "Gate requires the exact clean source commit")
    require((ROOT / "VERSION").read_text().strip() == args.version, "Gate version differs from VERSION")
    require(args.jobs_result == "success", "Every required native lane must succeed")
    require(re.fullmatch("[1-9][0-9]*", args.run_id) and re.fullmatch("[1-9][0-9]*", args.run_attempt), "Expected an exact workflow run and attempt")
    names = subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=ROOT).decode().split("\0")
    sources = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names if name}
    return {"sourceCommit": head,
            "sourceTree": subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip(),
            "sourceInputsSHA256": inventory_digest(sources),
            "runnerSHA256": hashlib.sha256((ROOT / "tests/native_ci.py").read_bytes()).hexdigest(),
            "goVersion": "go" + next(line.split()[1] for line in (ROOT / "go.mod").read_text().splitlines() if line.startswith("go ")),
            "version": args.version,
            "workflowRun": {"GITHUB_RUN_ID": args.run_id, "GITHUB_RUN_ATTEMPT": args.run_attempt}}


def validate_build(build, platform, suite, context):
    require(build.get("platform") == platform, "Build platform differs from its lane")
    require(build.get("sourceInputsSHA256") == context["sourceInputsSHA256"], "Build source inventory differs from the checked-out source")
    host, architecture = platform.split("/")
    require(build.get("goEnvironment") == {"GOVERSION": context["goVersion"], "GOHOSTOS": host, "GOHOSTARCH": architecture,
                                          "GOOS": host, "GOARCH": architecture}, "Build did not use the pinned native Go host and target")
    require(PLATFORM_COMPILERS[platform] in build.get("compiler", ""), "Build compiler differs from its pinned platform")
    tools = build.get("toolSHA256", {})
    require(set(tools) == {"go", "cc"} and all(re.fullmatch("[0-9a-f]{64}", value) for value in tools.values()), "Missing toolchain digests")
    require(re.fullmatch("[0-9a-f]{64}", build.get("binarySHA256", "")), "Missing native binary digest")
    identity = build.get("identity", {})
    require(identity.get("product") == "argo-cd" and identity.get("dirty") is False, "Build identity must be clean Argo CD source")
    if suite == "compiler-environment":
        require(re.fullmatch("[0-9a-f]{40}", identity.get("sourceCommit", "")) and identity["sourceCommit"] != context["sourceCommit"],
                "Compiler controls require their separate synthetic source history")
        require(identity.get("buildMode") == "release" and identity.get("serverVersion") == "0.1.0", "Wrong synthetic compiler build identity")
    else:
        require(identity.get("sourceCommit") == context["sourceCommit"], "Build identity differs from the exact source commit")
        mode, version = ("release", context["version"]) if suite == "consumers" else ("development", context["version"] + "-dev")
        require(identity.get("buildMode") == mode and identity.get("serverVersion") == version, "Wrong native lane build identity")


def validate_compiler_cases(report):
    cases = report.get("securityCases", [])
    require([case.get("id") for case in cases] == list(COMPILER_CASES), "Compiler controls are missing, duplicated or reordered")
    checks = {"buildExit0", "initialBuildCacheEmpty", "nativeExit0", "identity", "binary", "sourceInputs", "noMarker", "packageExit0", "packagedBinary"}
    cache_paths = []
    for case, build in zip(cases, report["buildEvidence"]):
        require(case.get("passed") is True and set(case.get("checks", {})) == checks and all(value is True for value in case["checks"].values()),
                "Compiler control has a missing or failed assertion")
        observed = case.get("observed", {})
        cache = observed.get("cachePath", "")
        require(observed.get("initialBuildCacheEmpty") is True and cache.replace("\\", "/").endswith("/" + case["id"] + "-cache"),
                "Compiler control must use a fresh case-specific cache")
        require(observed.get("markerBeforeNative") is False and observed.get("markerAfterNative") is False, "Compiler input marker executed")
        require(observed.get("nativeSHA256") == build["binarySHA256"], "Compiler case and native build digest differ")
        cache_paths.append(cache)
    require(len(set(cache_paths)) == len(COMPILER_CASES), "Compiler controls reused a cache")
    first = report["buildEvidence"][0]
    require(all(build == first for build in report["buildEvidence"]), "Compiler controls did not build identical declared inputs")


def validate_report(report, platform, suite, context):
    require(report.get("schemaVersion") == 1 and report.get("suite") == suite and report.get("expectedNativePlatform") == platform, "Wrong lane identity")
    require(report.get("releaseVersion") == context["version"] and report.get("workflowRun") == context["workflowRun"], "Wrong release version or workflow run")
    require(report.get("runnerSHA256") == context["runnerSHA256"], "Lane runner differs from the checked-out source")
    require(report.get("expectedProvenance") == {"sourceCommit": context["sourceCommit"], "sourceTree": context["sourceTree"], "dirty": False, "buildMode": "release"}, "Wrong lane source provenance")
    require(report.get("initialBuildCachesEmpty") is True, "Lane did not start with empty build caches")
    require(report.get("host") == {"linux": "linux", "darwin": "darwin", "windows": "win32"}[platform.split("/")[0]], "Wrong lane execution host")
    require(report.get("completed") is True and report.get("passed") is True and report.get("activeCommand") is None, "Lane is incomplete or failed")
    expected = required_steps(platform, suite)
    rows = report.get("rows", [])
    require(report.get("expectedSteps") == expected and [row.get("id") for row in rows] == expected, "Required real-process steps are missing or duplicated")
    require(all(row.get("passed") is True and type(row.get("exit")) is int and row["exit"] == 0 and row.get("cleanupError") is None for row in rows),
            "A real-process step failed or did not clean up")
    builds = report.get("buildEvidence", [])
    require(len(builds) == (5 if suite == "compiler-environment" else 1), "Missing native build evidence")
    for build in builds:
        validate_build(build, platform, suite, context)
    if suite == "compiler-environment":
        validate_compiler_cases(report)
    else:
        require(report.get("securityCases") == [], "Unexpected compiler controls in another lane")


def verify(args):
    context = source_context(args)
    expected_names = {"argocd-acceptance-" + platform.replace("/", "-") + "-" + suite for platform in PLATFORM_COMPILERS for suite in SUITES}
    paths = list(args.inputs.glob("*/report.json"))
    require({path.parent.name for path in paths} == expected_names and len(paths) == len(expected_names), "Expected exactly fifteen native acceptance reports")
    reports = {}
    for platform in PLATFORM_COMPILERS:
        reports[platform] = {}
        for suite in SUITES:
            name = "argocd-acceptance-" + platform.replace("/", "-") + "-" + suite
            report = read_json(args.inputs / name / "report.json")
            validate_report(report, platform, suite, context)
            reports[platform][suite] = report
        consumer = reports[platform]["consumers"]["buildEvidence"][0]
        for suite in SUITES:
            for build in reports[platform][suite]["buildEvidence"]:
                require(build["toolSHA256"] == consumer["toolSHA256"] and build["compiler"] == consumer["compiler"], "Native lanes used different platform toolchains")
    records = [read_json(path) for path in args.assets.glob("*/release-*.json")]
    require(len(records) == len(PLATFORM_COMPILERS) and {record.get("platform") for record in records} == set(PLATFORM_COMPILERS), "Expected exactly five native release records")
    for record in records:
        require(record.get("sourceCommit") == context["sourceCommit"] and record.get("version") == context["version"], "Release record source differs from tested source")
        binary = reports[record["platform"]]["consumers"]["buildEvidence"][0]["binarySHA256"]
        require(record.get("package", {}).get("binarySHA256") == binary, "Release bytes differ from the accepted native binary")
    return {"schemaVersion": 1, "passed": True, "sourceCommit": context["sourceCommit"], "sourceTree": context["sourceTree"],
            "workflowRun": context["workflowRun"], "version": context["version"], "acceptedLanes": sorted(expected_names), "nativePlatforms": sorted(PLATFORM_COMPILERS)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--jobs-result", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({"passed": False, "error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
