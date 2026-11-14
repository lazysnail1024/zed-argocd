#!/usr/bin/env python3
"""Execute existing resource contracts in verified Linux cgroup v2 scopes."""
import argparse
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import threading
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
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reject_constant(value):
    raise ValueError("Nonfinite JSON number: " + value)


def load_json(text):
    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            reject_constant(value)
        return result

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate JSON key: " + key)
            result[key] = value
        return result

    return json.loads(text, parse_constant=reject_constant, parse_float=finite_float,
                      object_pairs_hook=unique_object)


def number(value, label, positive=False):
    require(type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0),
            label + " must be a finite non-boolean " + ("positive" if positive else "nonnegative") + " number")
    return value


def measurements(value, count, label, positive=False):
    require(isinstance(value, list) and len(value) == count, label + " sample count differs")
    for sample in value:
        number(sample, label, positive)
    return value


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
        require(type(report.get("exitCode")) is int and report.get("exitCode") == (0 if name == "retained-buffers" else 2), "resource probe did not end normally")
        if name == "retained-buffers":
            require(type(report.get("sentBuffers")) is int and report.get("sentBuffers") == 100, "resource probe omitted buffers")
        return 1
    require(isinstance(rows, list) and rows and all(isinstance(row, dict) and row.get("passed") is True
            and "error" not in row for row in rows), "missing or failed resource cases")
    if script == "repository_rotation_e2e.py":
        assertions(report.get("checks"), {"allVisits", "normalShutdown", "boundedGrowth"})
        require(all(type(row.get("index")) is int and type(row.get("scope")) is int for row in rows) and [row.get("index") for row in rows] == list(range(120)), "scope visits are incomplete or duplicated")
        require(all(row.get("scope") == i % 100 and row.get("diagnostics") == []
                    and type(row.get("rss")) is int and row["rss"] > 0 for i, row in enumerate(rows)), "invalid scope observations")
        require(type(report.get("exitCode")) is int and report.get("exitCode") == 0 and rows[-1]["rss"] <= rows[19]["rss"] * 1.1,
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
        if not name.endswith("-cli"):
            number(rows[0].get("observed", {}).get("rss"), "retained boundary RSS", positive=True)
            measurements(report.get("metadata", {}).get("replacementRSS"), 5, "replacement RSS", positive=True)
        return len(rows)
    if script == "bounded_cli_e2e.py":
        expected = [*["tiny-" + str(i) for i in range(5)], *["full-" + str(i) for i in range(5)], "signal-cancellation"]
        require([row.get("name") for row in rows] == expected, "full CLI samples are incomplete or duplicated")
        for row in rows:
            expected_checks = {"exit3", "cancelReason", "notComplete", "notAllCounted", "processEnded"} if row["name"] == "signal-cancellation" else {
                "exit0", "exactFiles", "exactResources", "configuration", "exactDiagnostics", "complete", "deadline", "processEnded"}
            assertions(row.get("assertions"), expected_checks)
            for field in ("elapsedSeconds", "totalElapsedSeconds", "processElapsedSeconds", "readDecodeSeconds", "verificationSeconds"):
                number(row.get(field), field)
            phases = row.get("phaseSeconds")
            require(isinstance(phases, dict) and set(phases) == {"processStarted", "processObservedEnd", "decoded", "verified"}, "CLI phase timing inventory differs")
            for value in phases.values():
                number(value, "CLI phase timing")
            require(phases["processStarted"] <= phases["processObservedEnd"] <= phases["decoded"] <= phases["verified"], "CLI phases are out of order")
            for field, measured in (("processElapsedSeconds", phases["processObservedEnd"]-phases["processStarted"]),
                                    ("readDecodeSeconds", phases["decoded"]-phases["processObservedEnd"]),
                                    ("verificationSeconds", phases["verified"]-phases["processObservedEnd"]),
                                    ("elapsedSeconds", phases["verified"]), ("totalElapsedSeconds", phases["verified"])):
                require(math.isclose(row[field], measured, rel_tol=1e-9, abs_tol=1e-9), "CLI duration contradicts phases: " + field)
            if row["name"] != "signal-cancellation":
                require(row["processElapsedSeconds"] <= 5, "CLI process exceeded the original five-second deadline")
            number(row.get("peakRSSKiB"), "CLI peak RSS", positive=True)
            require(type(row.get("exit")) is int and row["exit"] == (3 if row["name"] == "signal-cancellation" else 0), "CLI exit is invalid")
        return len(rows)
    if script == "bounded_completion_e2e.py":
        require([row.get("name") for row in rows] == ["sample-" + str(i) for i in range(5)], "completion samples are incomplete")
        for row in rows:
            timings = measurements(row.get("seconds"), 200, "completion timing")
            if "p95Seconds" in row:
                number(row["p95Seconds"], "reported completion p95")
                require(row["p95Seconds"] == sorted(timings)[189], "reported completion p95 differs from samples")
            require(len(timings) == 200 and sorted(timings)[189] <= .1 and row.get("expectedLabels") == ["team"],
                    "completion sample omitted requests or exceeded its original p95 limit")
        return len(rows)
    require(script == "bounded_lsp_e2e.py", "unknown resource consumer")
    expected = ["five-diagnostic-samples", "query-latency", "sustained-newest-epoch", "close-clear",
                "ten-thousand-open-edit-close", "terminal-shutdown"]
    require(report.get("full") is True and [row.get("name") for row in rows] == expected, "full LSP lifecycle cases are incomplete")
    measurements(rows[0].get("seconds"), 5, "diagnostic timing")
    require(len(rows[0].get("seconds", [])) == 5 and max(rows[0]["seconds"]) <= .620, "diagnostic latency contract failed")
    queries = measurements(rows[1].get("seconds"), 1000, "query timing")
    require(len(queries) == 1000 and sorted(queries)[949] <= .1, "query latency contract failed")
    samples = measurements(rows[4].get("samplesRSSKiB"), 5, "lifecycle RSS", positive=True)
    number(rows[4].get("warmRSSKiB"), "warm RSS", positive=True)
    number(rows[5].get("seconds"), "shutdown timing")
    require(rows[4].get("twoActiveVersions") == ["3.0.0", "3.5.6"], "lifecycle target versions differ")
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
    spec = load_json(Path(path).read_text())
    before = limits_snapshot()
    save(Path(spec["limits"]) / "before.json", before)
    validate_limits(before, spec["memoryMiB"])
    child = subprocess.run(spec["command"], cwd=ROOT, env=os.environ | {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                           timeout=spec["timeoutSeconds"])
    after = limits_snapshot()
    save(Path(spec["limits"]) / "after.json", after)
    validate_limits(after, spec["memoryMiB"])
    return child.returncode


class UnitObserver:
    """Bounded, read-only observations from outside the task's memory cgroup."""

    def __init__(self, unit, logs, binary, consumer, worker_argv):
        self.unit, self.logs = unit, logs
        self.binary = str(Path(binary).resolve())
        self.expected = {"consumer": consumer, "worker": worker_argv}
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.observe, daemon=True)
        self.samples = deque(maxlen=200)
        self.maxima, self.identities = {}, {}
        self.errors = deque(maxlen=20)
        self.group = ""
        self.started = time.time_ns()
        self.count = 0

    def start(self):
        self.thread.start()

    def sample(self):
        if not self.group:
            membership = load_json((self.logs / "limits/before.json").read_text())["cgroup"]
            group = membership.removeprefix("0::")
            require(group.startswith("/") and ".." not in Path(group).parts
                    and Path(group).name == self.unit + ".service", "observer cgroup is not the owned unit")
            self.group = group
        folder = Path("/sys/fs/cgroup") / self.group.lstrip("/")
        row = {"unixNs": time.time_ns(), "cgroup": {}, "processes": [], "errors": []}
        for name in ("memory.current", "memory.peak", "memory.events", "memory.events.local", "memory.stat"):
            try:
                row["cgroup"][name] = (folder / name).read_text().strip()
            except OSError as error:
                row["errors"].append(name + ": " + repr(error))
        try:
            pids = (folder / "cgroup.procs").read_text().split()
        except OSError as error:
            pids = []
            row["errors"].append(repr(error))
        for pid in pids[:128]:
            proc = Path("/proc") / pid
            try:
                stat = (proc / "stat").read_text()
                start = stat.rsplit(")", 1)[1].split()[19]
                # Membership and start time must still match after reading metadata.
                exe = str((proc / "exe").resolve(strict=True))
                with (proc / "cmdline").open("rb") as stream:
                    raw = stream.read(65537)
                argv = [x.decode("utf-8", errors="replace") for x in raw.rstrip(b"\0").split(b"\0")]
                status = dict(line.split(":", 1) for line in (proc / "status").read_text().splitlines() if ":" in line)
                if ((proc / "cgroup").read_text().strip() != "0::" + self.group
                        or (proc / "stat").read_text().rsplit(")", 1)[1].split()[19] != start):
                    continue
                role = "other"
                if len(raw) <= 65536:
                    if exe == self.binary and argv and str(Path(argv[0]).resolve()) == self.binary:
                        role = "server"
                    else:
                        role = next((name for name, expected in self.expected.items() if argv == expected), "other")
                identity = {"pid": int(pid), "startTimeTicks": start, "exe": exe, "argv": argv,
                            "argvTruncated": len(raw) > 65536, "role": role}
                key = pid + ":" + start
                if len(self.identities) < 128 or key in self.identities:
                    self.identities[key] = identity
                row["processes"].append(identity | {"status": {k: status[k].strip() for k in
                    ("PPid", "VmRSS", "RssAnon", "RssFile", "VmSwap") if k in status}})
            except (OSError, ValueError, IndexError) as error:
                row["errors"].append(pid + ": " + repr(error))
        self.count += 1
        self.samples.append(row)
        metrics = {}
        for name in ("memory.current", "memory.peak"):
            if name in row["cgroup"]:
                metrics[name] = int(row["cgroup"][name])
        for line in row["cgroup"].get("memory.stat", "").splitlines():
            key, value = line.split()
            if key in ("anon", "file", "file_dirty", "file_writeback"):
                metrics[key] = int(value)
        for proc in row["processes"]:
            rss = int(proc["status"].get("VmRSS", "0 kB").split()[0]) * 1024
            key = "rss:" + proc["role"]
            metrics[key] = max(metrics.get(key, 0), rss)
        for key, value in metrics.items():
            if value > self.maxima.get(key, {}).get("value", -1):
                self.maxima[key] = {"value": value, "sample": row}

    def observe(self):
        while not self.stop_event.is_set():
            try:
                self.sample()
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.errors.append({"unixNs": time.time_ns(), "error": repr(error)})
            self.stop_event.wait(.05)

    def finish(self, journal=("sudo", "-n", "journalctl")):
        ended = time.time_ns()
        self.stop_event.set()
        self.thread.join(timeout=1)
        if self.thread.is_alive():
            receipt = {"unit": self.unit, "observerStopped": False,
                       "unavailableReason": "Observer did not stop within one second; mutable samples were not read."}
            save(self.logs / "unit-observations.json", receipt)
            return receipt
        receipt = {"unit": self.unit, "startUnixNs": self.started, "endUnixNs": ended,
                   "intervalSeconds": .05, "sampleCount": self.count, "cgroup": self.group,
                   "lastSamples": list(self.samples), "maxima": self.maxima,
                   "identities": list(self.identities.values()), "errors": list(self.errors),
                   "observerStopped": not self.thread.is_alive()}
        command = [*journal, "--dmesg", "--since=@" + str(self.started // 10**9),
                   "--until=@" + str(ended // 10**9 + 1), "--lines=2000", "--no-pager", "--output=json"]
        kernel = {"command": command, "matches": [], "classification": "Not performed; matching records require interpretation."}
        try:
            child = subprocess.run(command, capture_output=True, timeout=15)
            kernel.update(exit=child.returncode, stderr=child.stderr.decode("utf-8", errors="replace"))
            pids = {str(x["pid"]) for x in self.identities.values()}
            for line in child.stdout.decode("utf-8", errors="replace").splitlines():
                item = load_json(line)
                message = item.get("MESSAGE", "")
                group_match = self.group and re.search(re.escape(self.group) + r"(?=$|[\s,:;)])", message)
                pid_match = any(re.search(r"(?:Killed process |pid=|task_pid=)" + pid + r"(?![0-9])", message) for pid in pids)
                if group_match or pid_match:
                    kernel["matches"].append(item)
            kernel["availability"] = "matching records" if kernel["matches"] else "no matching records; victim unavailable"
            if child.returncode != 0:
                kernel["availability"] = "kernel journal query failed; victim unavailable"
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            kernel["error"] = repr(error)
            kernel["availability"] = "kernel journal unavailable"
        receipt["kernelJournal"] = kernel
        save(self.logs / "unit-observations.json", receipt)
        return receipt


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
            group = load_json(before.read_text())["cgroup"].removeprefix("0::")
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
    observer = UnitObserver(unit, logs, binary, argv, command[-4:])
    observer.start()
    started = time.monotonic()
    try:
        with (logs / "stdout").open("wb") as stdout, (logs / "stderr").open("wb") as stderr:
            row["started"] = True
            child = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=timeout + 30)
        row["exitCode"] = child.returncode
        require(child.returncode == 0, "resource command failed")
        for phase in ("before", "after"):
            validate_limits(load_json((logs / "limits" / (phase + ".json")).read_text()), memory)
        report = load_json((destination / "report.json").read_text())
        row["caseCount"] = validate_report(report, name, script, sha(binary), sha(ROOT / "tests" / script))
        row["passed"] = True
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        row["error"] = str(error)
        for name in ("stdout", "stderr"):
            if (logs / name).exists():
                print((logs / name).read_text(encoding="utf-8", errors="replace")[-4000:], flush=True)
    finally:
        try:
            observer.finish()
            row["unitObservations"] = str(logs / "unit-observations.json")
        except Exception as error:
            # Optional observations must never prevent termination receipts or cleanup.
            row["unitObservationError"] = repr(error)
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
    report = {"schemaVersion":2,"suite": args.suite, "rows": [], "passed": False,
              "workflowRun":{key:os.environ.get(key) for key in ("GITHUB_RUN_ID","GITHUB_RUN_ATTEMPT")},
              "producerReceipt":load_json((binary.parent/"receipt.json").read_text()) if (binary.parent/"receipt.json").is_file() else None}
    try:
        require(platform.system() == "Linux", "resource acceptance requires Linux")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        identity = load_json(subprocess.check_output([str(binary), "version", "--json"], timeout=20))
        require(identity.get("product") == "argo-cd" and identity.get("sourceCommit") == head
                and identity.get("dirty") is False and identity.get("buildMode")=="development" and identity.get("serverVersion")== (ROOT/"VERSION").read_text().strip()+"-dev", "resource binary does not identify this exact clean source")
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
