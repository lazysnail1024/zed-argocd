#!/usr/bin/env python3
"""Execute existing resource contracts in verified Linux cgroup v2 scopes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "retention": (
        ("retained-cli", "retained_input_probe.py", ("--mode", "cli"), 512, 90),
        ("retained-buffers", "retained_input_probe.py", ("--mode", "buffers"), 512, 180),
        ("projection-retention", "projection_retention_probe.py", (), 512, 90),
        ("repository-rotation", "repository_rotation_e2e.py", (), 512, 240),
    ),
    "boundaries": (
        ("retained-boundary-cli", "retained_budget_e2e.py", ("--mode", "cli"), 1024, 120),
        ("retained-boundary-lsp", "retained_budget_e2e.py", ("--mode", "lsp"), 1024, 180),
        ("retained-boundary-multiroot", "retained_budget_e2e.py", ("--mode", "lsp-multiroot"), 1024, 180),
    ),
    "lifecycle": (
        ("full-cli", "bounded_cli_e2e.py", ("--full",), 8192, 180),
        ("completion", "bounded_completion_e2e.py", (), 8192, 180),
        ("full-lsp", "bounded_lsp_e2e.py", ("--full",), 8192, 600),
    ),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assertions(value, expected=None):
    require(isinstance(value, dict) and bool(value) and all(v is True for v in value.values()),
            "missing, non-boolean or failed assertions")
    if expected is not None:
        require(set(value) == set(expected), "required assertions are missing or unexpected")


def validate_report(report, name, script, binary_sha, test_sha):
    require(isinstance(report, dict) and report.get("passed") is True, "resource consumer did not pass")
    require(not any(key in report for key in ("error", "readerError", "reportError")), "consumer reported a harness error")
    require(report.get("binarySHA256") == binary_sha, "consumer ran a different binary")
    require(report.get("runnerSHA256", report.get("testSHA256")) == test_sha, "consumer ran different test source")
    if "checks" in report:
        assertions(report["checks"])
    rows = report.get("rows", [])
    if script in ("retained_input_probe.py", "projection_retention_probe.py"):
        expected = {"noHarnessFailure", "measuredRSS", "exactBudgetRejection"}
        if name == "retained-buffers":
            expected |= {"allBuffersProcessed", "normalShutdown"}
        elif script == "projection_retention_probe.py":
            expected = {"noHarnessFailure", "measuredRSS", "budgetExit", "incomplete", "namedBudgetRefusal",
                        "onlyAdmittedResourceCounted", "noConfigurationFailure"}
        assertions(report.get("checks"), expected)
        require(type(report.get("peakRSS")) is int and 0 < report["peakRSS"] <= 384 * 1024 ** 2,
                "resource probe has no valid resident-memory measurement")
        require(report.get("exitCode") == (0 if name == "retained-buffers" else 2), "resource probe did not end normally")
        if name == "retained-buffers":
            require(report.get("sentBuffers") == 100, "resource probe omitted buffers")
        return 1
    require(isinstance(rows, list) and rows and all(isinstance(row, dict) and row.get("passed") is True
            and "error" not in row for row in rows), "missing or failed resource cases")
    if script == "repository_rotation_e2e.py":
        assertions(report.get("checks"), {"allVisits", "normalShutdown", "boundedGrowth"})
        require([row.get("index") for row in rows] == list(range(120)), "scope visits are incomplete or duplicated")
        require(all(row.get("scope") == i % 100 and row.get("diagnostics") == []
                    and type(row.get("rss")) is int and row["rss"] > 0 for i, row in enumerate(rows)), "invalid scope observations")
        require(report.get("exitCode") == 0 and rows[-1]["rss"] <= rows[19]["rss"] * 1.1,
                "scope rotation failed shutdown or the original ten-percent growth limit")
        return len(rows)
    if script == "retained_budget_e2e.py":
        expected = ["aggregate--1", "aggregate-0", "aggregate-1"] if name.endswith("-cli") else [
            "owner-exact-boundary", "owner-overflow-open", "refusal-hides-old-buffer-and-disk",
            "incremental-cannot-restore-refused", "full-replacement-recovers",
            *["same-size-replacement-" + str(i) for i in range(5, 10)], "close-reopen-clears-state", "shutdown"]
        require([row.get("id") for row in rows] == expected, "retained-input boundary cases are incomplete or duplicated")
        for row in rows:
            if row["id"].startswith("aggregate-"):
                checks = {"exit", "resources", "files", "complete"} | ({"reason"} if row["id"] == "aggregate-1" else {"configuration", "diagnostics"})
            elif row["id"].startswith("same-size-"):
                checks = {"current"}
            else:
                checks = {"owner-exact-boundary": {"hover", "noBudgetError"}, "owner-overflow-open": {"reason"},
                          "refusal-hides-old-buffer-and-disk": {"reason", "noOldHover"},
                          "incremental-cannot-restore-refused": {"reason"}, "full-replacement-recovers": {"onlyExpectedDiagnostic"},
                          "close-reopen-clears-state": {"restored"}, "shutdown": {"exit"}}[row["id"]]
            assertions(row.get("checks"), checks)
        return len(rows)
    if script == "bounded_cli_e2e.py":
        expected = [*["tiny-" + str(i) for i in range(5)], *["full-" + str(i) for i in range(5)], "signal-cancellation"]
        require([row.get("name") for row in rows] == expected, "full CLI samples are incomplete or duplicated")
        for row in rows:
            expected_checks = {"exit3", "cancelReason", "notComplete", "notAllCounted", "processEnded"} if row["name"] == "signal-cancellation" else {
                "exit0", "exactFiles", "exactResources", "configuration", "exactDiagnostics", "complete", "deadline", "processEnded"}
            assertions(row.get("assertions"), expected_checks)
        return len(rows)
    if script == "bounded_completion_e2e.py":
        require([row.get("name") for row in rows] == ["sample-" + str(i) for i in range(5)], "completion samples are incomplete")
        for row in rows:
            timings = row.get("seconds", [])
            require(len(timings) == 200 and sorted(timings)[189] <= .1 and row.get("expectedLabels") == ["team"],
                    "completion sample omitted requests or exceeded its original p95 limit")
        return len(rows)
    require(script == "bounded_lsp_e2e.py", "unknown resource consumer")
    expected = ["five-diagnostic-samples", "query-latency", "sustained-newest-epoch", "close-clear",
                "ten-thousand-open-edit-close", "terminal-shutdown"]
    require(report.get("full") is True and [row.get("name") for row in rows] == expected, "full LSP lifecycle cases are incomplete")
    require(len(rows[0].get("seconds", [])) == 5 and max(rows[0]["seconds"]) <= .620, "diagnostic latency contract failed")
    queries = rows[1].get("seconds", [])
    require(len(queries) == 1000 and sorted(queries)[949] <= .1, "query latency contract failed")
    samples = rows[4].get("samplesRSSKiB", [])
    require(len(samples) == 5 and samples[-1] <= rows[4]["warmRSSKiB"] * 1.1, "ten-thousand-cycle growth limit failed")
    require(rows[2].get("stale") == [] and rows[5].get("seconds", 6) <= 5, "stale publication or slow shutdown")
    return len(rows)


def limits_snapshot():
    membership = Path("/proc/self/cgroup").read_text().strip()
    require(membership.startswith("0::"), "resource worker requires cgroup v2")
    folder = Path("/sys/fs/cgroup") / membership[3:].lstrip("/")
    result = {name: (folder / name).read_text().strip()
              for name in ("memory.max", "memory.swap.max", "cpu.max", "memory.events")}
    result.update(cgroup=membership, affinity=sorted(os.sched_getaffinity(0)))
    return result


def validate_limits(limits, memory):
    require(limits.get("memory.max") == str(memory * 1024 ** 2), "requested memory limit was not enforced")
    require(limits.get("memory.swap.max") == "0", "swap must be disabled")
    quota, period = limits["cpu.max"].split()
    require(quota != "max" and 0 < int(quota) / int(period) <= 4 and len(limits["affinity"]) == 4,
            "resource contract requires exactly four allowed CPUs and at most four CPUs of quota")
    events = dict(line.split() for line in limits["memory.events"].splitlines())
    require(all(events.get(key) == "0" for key in ("oom", "oom_kill")), "resource scope experienced an OOM")


def worker(path):
    spec = json.loads(Path(path).read_text())
    before = limits_snapshot()
    save(Path(spec["limits"]) / "before.json", before)
    validate_limits(before, spec["memoryMiB"])
    child = subprocess.run(spec["command"], cwd=ROOT, env=os.environ | {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                           timeout=spec["timeoutSeconds"])
    after = limits_snapshot()
    save(Path(spec["limits"]) / "after.json", after)
    validate_limits(after, spec["memoryMiB"])
    return child.returncode


def capture_unit_evidence(unit, logs, control=("sudo", "-n", "systemctl"),
                          journal=("sudo", "-n", "journalctl")):
    """Capture outside the constrained worker before stopping its unique unit."""
    logs.mkdir(parents=True, exist_ok=True)
    receipt = {"unit": unit, "capturedAtUnixNs": time.time_ns(), "causeAvailable": False}

    def query(name, command):
        result = {"command": command}
        try:
            child = subprocess.run(command, capture_output=True, timeout=15)
            result["exit"] = child.returncode
            stdout, stderr = child.stdout, child.stderr
        except (OSError, subprocess.SubprocessError) as error:
            result["error"] = repr(error)
            stdout = getattr(error, "stdout", None) or b""
            stderr = getattr(error, "stderr", None) or b""
        (logs / (name + ".stdout")).write_bytes(stdout)
        (logs / (name + ".stderr")).write_bytes(stderr)
        return result, stdout.decode("utf-8", errors="replace")

    properties = ("Id,LoadState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,"
                  "ExecMainStartTimestamp,ExecMainExitTimestamp,OOMPolicy,MemoryPeak,"
                  "MemoryCurrent,CPUUsageNSec,ControlGroup,CollectMode")
    receipt["systemctl"], text = query("unit-show", [*control, "show", unit, "--property=" + properties])
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    receipt["properties"] = values
    receipt["causeAvailable"] = (receipt["systemctl"].get("exit") == 0
                                 and values.get("LoadState") == "loaded" and bool(values.get("Result")))
    if not receipt["causeAvailable"]:
        receipt["causeUnavailableReason"] = "Unit result was unavailable; a reclaimed unit is not evidence of success."
    group = values.get("ControlGroup", "")
    if not group:
        before = logs / "limits" / "before.json"
        try:
            group = json.loads(before.read_text())["cgroup"].removeprefix("0::")
        except (OSError, ValueError, KeyError, TypeError) as error:
            receipt["cgroupLookupError"] = repr(error)
    cgroup = {"path": group, "files": {}, "errors": {}}
    if group.startswith("/") and ".." not in Path(group).parts:
        folder = Path("/sys/fs/cgroup") / group.lstrip("/")
        for name in ("memory.events", "memory.events.local", "memory.current", "memory.peak", "memory.stat"):
            try:
                cgroup["files"][name] = (folder / name).read_text()
            except OSError as error:
                cgroup["errors"][name] = repr(error)
    else:
        cgroup["unavailableReason"] = "No valid task cgroup path was available."
    receipt["cgroup"] = cgroup
    receipt["journal"], _ = query("unit-journal", [*journal, "--unit=" + unit, "--no-pager", "--output=short-precise"])
    save(logs / "unit-evidence.json", receipt)
    return receipt


def execute(case, binary, output):
    name, script, extra, memory, timeout = case
    destination, logs = output / name, output / "logs" / name
    require(not destination.exists(), "refusing stale consumer evidence")
    cpus = sorted(os.sched_getaffinity(0))[:4]
    require(len(cpus) == 4, "resource runner requires at least four CPUs")
    argv = [sys.executable, str(ROOT / "tests" / script), str(binary), str(destination), *extra]
    spec = {"command": argv, "memoryMiB": memory, "timeoutSeconds": timeout, "limits": str(logs / "limits")}
    spec_path = output / "specs" / (name + ".json")
    save(spec_path, spec)
    unit = "argocd-resource-" + uuid.uuid4().hex
    command = ["sudo", "-n", "systemd-run", "--quiet", "--wait", "--pipe", "--unit=" + unit,
               "--uid=" + str(os.getuid()), "--gid=" + str(os.getgid()), "--working-directory=" + str(ROOT),
               "--property=MemoryMax=" + str(memory * 1024 ** 2), "--property=MemorySwapMax=0",
               "--property=CPUQuota=400%", "--property=AllowedCPUs=" + ",".join(map(str, cpus)),
               "--property=RuntimeMaxSec=" + str(timeout + 5), "--property=KillMode=control-group"]
    if os.environ.get("LD_LIBRARY_PATH"):
        command.append("--setenv=LD_LIBRARY_PATH=" + os.environ["LD_LIBRARY_PATH"])
    command += [sys.executable, str(Path(__file__).resolve()), "_worker", str(spec_path)]
    logs.mkdir(parents=True, exist_ok=False)
    row = {"id": name, "command": command, "passed": False, "started": False}
    started = time.monotonic()
    try:
        with (logs / "stdout").open("wb") as stdout, (logs / "stderr").open("wb") as stderr:
            row["started"] = True
            child = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=timeout + 30)
        row["exitCode"] = child.returncode
        require(child.returncode == 0, "resource command failed")
        for phase in ("before", "after"):
            validate_limits(json.loads((logs / "limits" / (phase + ".json")).read_text()), memory)
        report = json.loads((destination / "report.json").read_text())
        row["caseCount"] = validate_report(report, name, script, sha(binary), sha(ROOT / "tests" / script))
        row["passed"] = True
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        row["error"] = str(error)
        for name in ("stdout", "stderr"):
            if (logs / name).exists():
                print((logs / name).read_text(encoding="utf-8", errors="replace")[-4000:], flush=True)
    finally:
        # Failed units must survive until this unconstrained parent records the
        # termination cause. Worker-side finally blocks cannot survive SIGKILL.
        try:
            receipt = capture_unit_evidence(unit, logs)
            row["unitEvidence"] = str(logs / "unit-evidence.json")
            row["unitCauseAvailable"] = receipt["causeAvailable"]
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            row["unitEvidenceError"] = repr(error)
        # Stop/reset only this invocation's unique unit after preserving evidence.
        try:
            subprocess.run(["sudo", "-n", "systemctl", "stop", unit], capture_output=True, timeout=30, check=False)
            subprocess.run(["sudo", "-n", "systemctl", "reset-failed", unit], capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            row.update(passed=False, cleanupError=str(error))
        row["seconds"] = round(time.monotonic() - started, 3)
        save(logs / "command.json", row)
    print(json.dumps(row), flush=True)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=SUITES, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binary, output = args.binary.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"suite": args.suite, "rows": [], "passed": False}
    try:
        require(platform.system() == "Linux", "resource acceptance requires Linux")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        identity = json.loads(subprocess.check_output([str(binary), "version", "--json"], timeout=20))
        require(identity.get("product") == "argo-cd" and identity.get("sourceCommit") == head
                and identity.get("dirty") is False, "resource binary does not identify this exact clean source")
        report.update(sourceCommit=head, binarySHA256=sha(binary), identity=identity, runnerSHA256=sha(Path(__file__)))
        for case in SUITES[args.suite]:
            report["rows"].append(execute(case, binary, output))
            save(output / "report.json", report)
        report["passed"] = ([row["id"] for row in report["rows"]] == [case[0] for case in SUITES[args.suite]]
                            and all(row["passed"] is True for row in report["rows"]))
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
    save(output / "report.json", report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "_worker":
        sys.exit(worker(sys.argv[2]))
    sys.exit(main())
