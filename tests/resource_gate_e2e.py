#!/usr/bin/env python3
"""Synthetic positive/negative controls for the resource evidence gate only."""
import copy
import json
from pathlib import Path

from resource_ci import SUITES, validate_limits, validate_report


def main():
    binary_sha, test_sha = "a" * 64, "b" * 64
    original = {"passed": True, "binarySHA256": binary_sha, "runnerSHA256": test_sha,
                "checks": {"noHarnessFailure": True, "measuredRSS": True, "exactBudgetRejection": True},
                "peakRSS": 100 * 1024 ** 2, "exitCode": 2}
    changes = {
        "valid-receipt": lambda r: None,
        "only-pass": lambda r: (r.clear(), r.update(passed=True)),
        "missing-check": lambda r: r["checks"].pop("exactBudgetRejection"),
        "false-check": lambda r: r["checks"].update(exactBudgetRejection=False),
        "truthy-check": lambda r: r["checks"].update(exactBudgetRejection="true"),
        "truthy-pass": lambda r: r.update(passed=1),
        "wrong-binary": lambda r: r.update(binarySHA256="c" * 64),
        "wrong-runner": lambda r: r.update(runnerSHA256="c" * 64),
        "reader-error": lambda r: r.update(readerError="broken pipe"),
        "no-rss": lambda r: r.update(peakRSS=0),
        "watchdog-rss": lambda r: r.update(peakRSS=384 * 1024 ** 2 + 1),
        "crash-exit": lambda r: r.update(exitCode=-9),
    }
    rows = []
    for name, mutate in changes.items():
        report = copy.deepcopy(original)
        mutate(report)
        try:
            validate_report(report, "retained-cli", "retained_input_probe.py", binary_sha, test_sha)
            accepted = True
        except (ValueError, KeyError, TypeError):
            accepted = False
        rows.append({"id": name, "accepted": accepted, "passed": accepted == (name == "valid-receipt")})
    limits = {"memory.max": str(512 * 1024 ** 2), "memory.swap.max": "0", "cpu.max": "400000 100000",
              "affinity": [0, 1, 2, 3], "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n"}
    changes = {
        "valid-limits": {}, "unlimited-memory": {"memory.max": "max"},
        "wrong-memory": {"memory.max": str(1024 * 1024 ** 2)}, "swap-enabled": {"memory.swap.max": "max"},
        "unlimited-cpu": {"cpu.max": "max 100000"}, "wrong-affinity": {"affinity": [0, 1]},
        "oom-kill": {"memory.events": "oom 1\noom_kill 1\n"},
    }
    for name, mutation in changes.items():
        try:
            validate_limits(limits | mutation, 512)
            accepted = True
        except (ValueError, KeyError, TypeError):
            accepted = False
        rows.append({"id": name, "accepted": accepted, "passed": accepted == (name == "valid-limits")})
    completion = {"passed": True, "binarySHA256": binary_sha, "runnerSHA256": test_sha,
                  "rows": [{"name": "sample-" + str(i), "passed": True, "seconds": [.01] * 200,
                            "expectedLabels": ["team"]} for i in range(5)]}
    for name in ("complete-samples", "missing-sample", "duplicate-sample", "omitted-requests", "latency-regression"):
        value = copy.deepcopy(completion)
        if name == "missing-sample": value["rows"].pop()
        if name == "duplicate-sample": value["rows"][1] = copy.deepcopy(value["rows"][0])
        if name == "omitted-requests": value["rows"][0]["seconds"].pop()
        if name == "latency-regression": value["rows"][0]["seconds"] = [.2] * 200
        try:
            validate_report(value, "completion", "bounded_completion_e2e.py", binary_sha, test_sha)
            accepted = True
        except (ValueError, KeyError, TypeError):
            accepted = False
        rows.append({"id": name, "accepted": accepted, "passed": accepted == (name == "complete-samples")})
    names = [case[0] for cases in SUITES.values() for case in cases]
    rows.append({"id": "orthogonal-inventory", "passed": len(names) == len(set(names)) == 10})
    report = {"scope": "synthetic gate controls; not measured resource acceptance", "rows": rows,
              "passed": len(rows) == 25 and all(row["passed"] is True for row in rows)}
    out = Path(__file__).resolve().parents[1] / "artifacts/resource-gate"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
