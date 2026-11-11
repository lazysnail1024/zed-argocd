#!/usr/bin/env python3
"""Compile isolated fault variants and require the real regression consumer to fail.

Each selected consumer first runs against the unmodified supplied binary. A
missing mutation anchor, build failure, timeout, malformed report or unrelated
runner exception is a failed mutation check, never a successful detection.
"""

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]


def mutation(ident, path, before, after, consumer, case, failure, failed_checks,
             required_checks=(), positive_cases=(), assertion_error=False, additional_failures=()):
    return {"id": ident, "file": path, "before": before, "after": after,
            "consumer": consumer, "case": case, "expectedFailure": failure,
            "failureChecks": list(failed_checks), "requiredChecks": list(required_checks),
            "positiveCases": list(positive_cases), "assertionErrorExpected": assertion_error,
            "additionalFailures": dict(additional_failures)}


MUTATIONS = [
    mutation("R-01-spec-null", "internal/analysis/appset.go",
             "if effectiveSpec == nil {", "if false {",
             "semantic_regressions_e2e.py", "patch", "patch-spec-null", ("cli_no_panic",),
             positive_cases=("patch-empty-object",), assertion_error=True),
    # Losing a new URI's related-priority admission loses both its notification
    # and queued work. Existing files and later configuration rediscovery remain
    # usable, so the failure must be the missing newly created target.
    mutation("R-02-drop-new-file-events", "internal/lsp/incremental.go",
             "func (s *workspaceIndex) enqueue(uri source.URI, read bool, priority shared.Priority) {\n\tfile := s.files[uri]",
             "func (s *workspaceIndex) enqueue(uri source.URI, read bool, priority shared.Priority) {\n\tfile := s.files[uri]\n\tif file == nil && priority == shared.Related {\n\t\treturn\n\t}",
             "lsp_regressions_e2e.py", "rescan-cancel", "rescan-cancel-create",
             ("settled-definition-needs-no-new-event", "latest-diagnostics-include-known-change"),
             ("initial-ready-control", "initial-diagnostic-control", "initial-definition-control",
              "restored-positive-control", "unaffected-root-stays-usable", "clean-server-exit")),
    # The last completed batch now contains metadata, not query scope facts.
    # Building a read proof from that batch falls back to a global epoch and
    # incorrectly rejects an independent-scope edit. Actual dependencies must
    # still be rejected in the same consumer's separate race control.
    mutation("R-03-proof-from-old-scope", "internal/lsp/server.go",
             "proof = newReadProof(m, fresh, copied, queryRoots, selected, queryDisk)",
             "proof = newReadProof(m, input, copied, queryRoots, selected, queryDisk)",
             "lsp_regressions_e2e.py", "scope-proof", "scope-proof-merge",
             ("independent-scope-query-stays-valid",),
             ("exact-initial-reference-control", "versioned-edit-positive-control",
              "barrier-and-dependent-edit-order", "fresh-scope-proof-rejects-only-actual-dependencies",
              "settled-reference-control", "changed-current-buffer-rejects-old-edit",
              "changed-config-rejects-old-edit", "clean-server-exit")),
    # Parse failures are file facts. Expanding their completeness veto to every
    # root denies exhaustive references in a fully known, healthy root.
    mutation("R-04-global-parser-completeness", "internal/lsp/incremental.go",
             "return root.issues[scope] == 0 && root.issues[\"\"] == 0",
             "for _, candidate := range s.roots {\n\t\tfor _, issues := range candidate.issues {\n\t\t\tif issues > 0 {\n\t\t\t\treturn false\n\t\t\t}\n\t\t}\n\t}\n\treturn root.issues[scope] == 0 && root.issues[\"\"] == 0",
             "lsp_regressions_e2e.py", "parser-root", "parser-root-depth-open-failed-first",
             ("during-failure-references",),
             ("before-failure-references", "during-failure-definition", "during-failure-schema-diagnostic-retained",
              "bounded-input-error-explained", "failed-root-cannot-supply-stale-authority",
              "after-repair-references", "after-repair-schema-diagnostic-retained", "clean-server-exit")),
    mutation("R-05-ignore-effective-paths", "internal/analysis/local_paths.go",
             "documents, _ = appSetVariants(doc)", "documents = nil",
             "workspace_regressions_e2e.py", "local-paths", "path-patch-absent", ("missingWarning",),
             ("warningOnlyExitZero",), ("path-patch-exists", "path-patch-restored")),
    mutation("R-06-skip-buffer-classification", "internal/lsp/incremental.go",
             "if workspace.IsHelmTemplate(rootPath, path, snapshot) {",
             "if false && workspace.IsHelmTemplate(rootPath, path, snapshot) {",
             "workspace_regressions_e2e.py", "helm", "helm-unsaved-version-2", ("onlyUnrenderedInfo",),
             ("queryUsesCurrentClass",), ("helm-pure-yaml-control",)),
    # Canonical identity and the spelling used in client results are different
    # responsibilities. Using the spelling as an acquisition key duplicates an
    # aliased resource while the canonical-path control still works.
    mutation("R-07-raw-buffer-identity", "internal/lsp/incremental.go",
             "canonical := source.URI(workspace.FileURI(path))\n\t\tb.uri = uri\n\t\topen[canonical] = b",
             "canonical := source.URI(uri)\n\t\tb.uri = uri\n\t\topen[canonical] = b",
             "workspace_regressions_e2e.py", "uri", "uri-symlink-navigation",
             ("definitionClientURI", "commentDoesNotChangeDiagnostics"), ("referencesClientURI",),
             positive_cases=("uri-canonical-navigation", "uri-canonical-incremental-work",
                             "uri-symlink-versioned-client-edit"),
             additional_failures=(("uri-symlink-incremental-work", ("noDiskReads",)),)),
    mutation("R-08-trust-truncated-prefix", "internal/analysis/reference_recovery.go",
             "func (r referenceRecovery) permits(ctx context.Context, parts []string) bool {",
             "func (r referenceRecovery) permits(ctx context.Context, parts []string) bool {\n\treturn true",
             "semantic_regressions_e2e.py", "recovery", "recovery-duplicate-project", ("definition_is_empty",),
             positive_cases=("recovery-positive-control", "recovery-unfinished-key-control"), assertion_error=True),
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FatalCommandError(RuntimeError):
    """The worker must stop so its one outer process boundary can close."""
    def __init__(self, execution):
        self.execution = execution
        super().__init__("Mutation command did not complete: " + str(execution["exit"]))


def command(args, folder, name, env, cwd=ROOT, timeout=180):
    started = time.monotonic()
    fatal = None
    try:
        # The standalone supervisor or native_ci owns this worker's process
        # group. Never create a nested session that could escape its cleanup.
        process = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate(timeout=timeout)
        code = process.returncode
    except subprocess.TimeoutExpired as error:
        stdout, stderr, code = error.stdout or b"", error.stderr or b"", "timeout"
        fatal = error
    except (OSError, subprocess.SubprocessError) as error:
        stdout, stderr, code = b"", (repr(error) + "\n").encode(), "launch-failed"
        fatal = error
    (folder / (name + ".stdout")).write_bytes(stdout)
    (folder / (name + ".stderr")).write_bytes(stderr)
    execution = {"command": args, "cwd": str(cwd), "exit": code,
                 "cleanupError": None, "elapsedSeconds": time.monotonic() - started}
    if fatal is not None:
        # Do not continue or try to drain pipes still held by grandchildren.
        # Saving the failure and exiting releases the whole tree at the outer
        # boundary; keeping the worker alive would leave descendants running.
        raise FatalCommandError(execution) from fatal
    return execution


def run_consumer(spec, binary, folder, env):
    args = [sys.executable, str(ROOT / "tests" / spec["consumer"]), str(binary),
            str(folder / "consumer"), "--case", spec["case"]]
    execution = command(args, folder, "consumer", env)
    path = folder / "consumer/report.json"
    execution["report"] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    return execution


def behavior_failure(report, spec):
    if not isinstance(report, dict) or report.get("passed") is not False:
        return False
    rows = {row["id"]: row for row in report.get("rows", report.get("cases", []))}
    row = rows.get(spec["expectedFailure"], {})
    if row.get("passed") is not False:
        return False
    if row.get("error") and not (spec["assertionErrorExpected"] and row.get("errorType") == "AssertionError"):
        return False
    checks = row.get("checks", {})
    if isinstance(checks, list):
        checks = {check["id"]: check.get("passed") for check in checks}
    for ident, names in spec.get("additionalFailures", {}).items():
        additional = rows.get(ident, {})
        if additional.get("passed") is not False or additional.get("error"):
            return False
        values = additional.get("checks", {})
        if isinstance(values, list):
            values = {check["id"]: check.get("passed") for check in values}
        if not all(values.get(name) is False for name in names):
            return False
    # Require the specific faulty behavior and its independent preconditions.
    # Cleanup, missing race coverage or a transport timeout cannot kill a mutant.
    return (all(checks.get(name) is False for name in spec["failureChecks"])
            and all(checks.get(name) is True for name in spec["requiredChecks"])
            and all(rows.get(name, {}).get("passed") is True for name in spec["positiveCases"]))


def supervise(args):
    # Standalone runs use the same proven task-tree boundary as native CI.
    # CI passes --worker because native_ci.run already owns that boundary;
    # workers and their commands never create a nested session that can escape it.
    import native_ci

    output = args.output.resolve()
    if output.exists():
        raise ValueError("Output must not already exist")
    argv = [sys.executable, str(pathlib.Path(__file__).resolve()), str(args.binary.resolve()),
            str(output), "--worker"]
    if args.mutation:
        argv += ["--mutation", args.mutation]
    started = time.monotonic()
    process = close = None
    stdout = stderr = b""
    status, cleanup_error = "launch-failed", None
    try:
        process, close = native_ci.start_command(argv, dict(os.environ, PYTHONUTF8="1"))
        try:
            stdout, stderr = process.communicate(timeout=480)
            status = process.returncode
        except subprocess.TimeoutExpired as error:
            stdout, stderr, status = error.stdout or b"", error.stderr or b"", "timeout"
    finally:
        if close:
            try:
                close()
            except OSError as error:
                cleanup_error = repr(error)
        if process:
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                cleanup_error = "Mutation task-tree output did not close after cleanup"
            process.stdout.close()
            process.stderr.close()
            if os.name == "nt":
                process._handle.Close()
        output.mkdir(parents=True, exist_ok=True)
        (output / "boundary.json").write_text(json.dumps({"command": argv, "exit": status,
            "cleanupError": cleanup_error, "elapsedSeconds": time.monotonic() - started,
            "passed": status == 0 and cleanup_error is None}, indent=2) + "\n", encoding="utf-8")
        (output / "worker.stdout").write_bytes(stdout)
        (output / "worker.stderr").write_bytes(stderr)
    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(stderr)
    return 0 if status == 0 and cleanup_error is None else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--mutation", choices=[m["id"] for m in MUTATIONS])
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.worker:
        return supervise(args)
    binary, output = args.binary.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    selected = [m for m in MUTATIONS if args.mutation is None or m["id"] == args.mutation]
    env = dict(os.environ, GOWORK="off", GOTOOLCHAIN="local", CGO_ENABLED="1", GOFLAGS="", GOPROXY="off")
    go = env.get("GO") or (str(pathlib.Path(env["GOROOT"]) / "bin/go") if env.get("GOROOT") else shutil.which("go"))
    rows, controls = [], {}
    report = {"binarySHA256": digest(binary), "runnerSHA256": digest(pathlib.Path(__file__)),
              "python": sys.version, "platform": sys.platform, "command": sys.argv,
              "expectedMutations": [m["id"] for m in selected], "controls": controls,
              "rows": rows, "completed": False, "passed": False}

    def save():
        pending = output / "report.pending.json"
        pending.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        pending.replace(output / "report.json")

    save()
    for spec in selected:
        folder = output / spec["id"]
        folder.mkdir()
        row = {"id": spec["id"], "mutation": spec, "passed": False, "detected": False}
        rows.append(row)
        try:
            if not go:
                raise RuntimeError("Pinned Go toolchain is required")
            key = spec["consumer"] + ":" + spec["case"]
            if key not in controls:
                control_dir = output / ("control-" + spec["case"])
                control_dir.mkdir()
                controls[key] = run_consumer(spec, binary, control_dir, env)
            control = controls[key]
            row["positiveControl"] = (control["exit"] == 0 and control.get("cleanupError") is None
                                      and isinstance(control.get("report"), dict) and control["report"].get("passed") is True)
            if not row["positiveControl"]:
                raise RuntimeError("The unmodified binary did not pass this consumer")
            with tempfile.TemporaryDirectory(prefix="argocd-audit-mutation-") as temporary:
                source = pathlib.Path(temporary)
                for name in ("go.mod", "go.sum", "cmd", "internal", "vendor"):
                    original, destination = ROOT / name, source / name
                    shutil.copytree(original, destination) if original.is_dir() else shutil.copy2(original, destination)
                target = source / spec["file"]
                before = target.read_text(encoding="utf-8")
                if before.count(spec["before"]) != 1:
                    raise RuntimeError("Mutation anchor must match exactly once")
                row["originalSourceSHA256"] = digest(target)
                target.write_text(before.replace(spec["before"], spec["after"], 1), encoding="utf-8")
                row["mutatedSourceSHA256"] = digest(target)
                (folder / "mutated-source.txt").write_bytes(target.read_bytes())
                mutated = folder / ("argocd-ls.exe" if os.name == "nt" else "argocd-ls")
                row["build"] = command([go, "build", "-mod=vendor", "-buildvcs=false", "-trimpath",
                                         "-o", str(mutated), "./cmd/argocd-ls"], folder, "build", env, source)
                if row["build"]["exit"] != 0 or row["build"].get("cleanupError") is not None:
                    raise RuntimeError("Fault variant did not compile")
                row["binarySHA256"] = digest(mutated)
                row["oracle"] = run_consumer(spec, mutated, folder, env)
                row["detected"] = (row["oracle"]["exit"] == 1 and row["oracle"].get("cleanupError") is None
                                   and behavior_failure(row["oracle"]["report"], spec))
                row["passed"] = row["detected"]
        except FatalCommandError as error:
            # A command that cannot finish its I/O has lost its own lifecycle
            # proof. Abort immediately so the owning CI/supervisor boundary
            # kills every descendant before another mutation can start.
            row["error"] = repr(error)
            row["fatalCommand"] = error.execution
            save()
            return 1
        except Exception as error:
            row["error"] = repr(error)
        save()
        print(json.dumps({"id": row["id"], "passed": row["passed"], "error": row.get("error")}), flush=True)
    report["completed"] = True
    report["passed"] = len(rows) == len(selected) and bool(rows) and all(row["passed"] for row in rows)
    save()
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
