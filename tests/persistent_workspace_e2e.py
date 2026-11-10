#!/usr/bin/env python3
"""Exercise the persistent index through real stdio and filesystem events.

Ready is a terminal discovery/analysis observation, not the first publication.
The released baseline has no Ready field: its explicitly identified legacy
adapter requires a terminal full scan covering all inputs, followed by the same
cross-file definition assertion. No server clock, scanner or parser is mocked.

--padding sets the minimum byte size of each initial resource YAML. For example,
--count 50000 --padding 4096 builds roughly 200 MiB of inputs without keeping them
all open. The event burst separately creates more files than the 512-slot queue.
--reference-mix uses approximately 90% ConfigMap, 9% Application and 1% Workflow;
--ordinary-files adds non-YAML directory entries without changing the assertions.
"""

import argparse
import json
import os
import pathlib
import platform
import sys
import time

import audit_repair_e2e as wire


FOREIGN = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: ordinary}\ndata: {value: before}\n"
APP = wire.APP.replace("namespace: default}", "namespace: default, name: conflict}")


class Client(wire.LSP):
    """Reuse the established wire recorder, with live stderr trace draining."""

    def __init__(self, output, roots):
        original = wire.subprocess.Popen

        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            # The shared helper's read(65536) otherwise waits for a full buffered
            # read; tiny workspaces might never expose their Ready trace live.
            # stdout remains buffered so its framed read(length) stays exact.
            process.stderr = process.stderr.detach()
            return process

        wire.subprocess.Popen = launch
        try:
            super().__init__(output, roots)
        finally:
            wire.subprocess.Popen = original


class Traces:
    def __init__(self, client):
        self.client = client
        self.consumed = 0
        self.partial = b""
        self.rows = []
        self.legacy_files = 0

    def refresh(self):
        chunks = self.client.stderr_chunks[self.consumed:]
        self.consumed += len(chunks)
        if not chunks:
            return
        lines = (self.partial + b"".join(chunks)).split(b"\n")
        self.partial = lines.pop()
        for line in lines:
            for prefix, delivered in ((b"argocd-analysis ", True),
                                      (b"argocd-analysis-discarded ", False)):
                if line.startswith(prefix):
                    try:
                        value = json.loads(line[len(prefix):])
                    except (ValueError, UnicodeError):
                        break
                    self.rows.append({"elapsed": time.monotonic() - self.client.start,
                                      "delivered": delivered, "stats": value})
                    break

    def mark(self):
        self.refresh()
        return len(self.rows)

    def since(self, mark):
        self.refresh()
        return self.rows[mark:]


def wait_until(client, predicate, timeout, description):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value is not None:
            return value
        if client.proc.poll() is not None:
            raise AssertionError("server exited while waiting for " + description)
        if time.monotonic() >= deadline:
            raise TimeoutError(description)
        time.sleep(0.02)


def diagnostic(client, file, since, predicate, timeout=15):
    def find():
        for entry in client.messages[since:]:
            message = entry["message"]
            params = message.get("params", {})
            if (message.get("method") == "textDocument/publishDiagnostics"
                    and params.get("uri") == file.as_uri() and predicate(params)):
                return {"elapsed": entry["elapsed"], "params": params}
        return None

    return wait_until(client, find, timeout, "diagnostics for " + file.name)


def has_code(params, code):
    return any(item.get("code") == code for item in params.get("diagnostics", []))


def workspace_publication(params):
    return params.get("x-argocd-analysis-scope", "workspace") == "workspace"


def wait_ready(client, traces, mark, files, timeout, allow_legacy):
    def find():
        rows = traces.since(mark)
        for offset, row in enumerate(rows):
            if not row["delivered"]:
                continue
            stats = row["stats"]
            if (stats.get("state") == "Ready" and stats.get("complete") is True
                    and stats.get("queueDepth") == 0 and stats.get("knownFiles", 0) >= files):
                return {"mode": "explicit-ready", "traceIndex": mark + offset, **row}
            if (allow_legacy and not stats.get("state")
                    and (stats.get("phase") == "domain"
                         or traces.legacy_files and stats.get("phase") == "schema")):
                if (stats.get("fullScan") and stats.get("diskReads", 0) >= files - 1
                        and (traces.legacy_files or stats.get("parsedFiles", 0) >= files)):
                    traces.legacy_files = files
                    return {"mode": "legacy-terminal-full-scan", "traceIndex": mark + offset, **row}
                if not stats.get("fullScan") and traces.legacy_files >= files:
                    return {"mode": "legacy-terminal-buffer-refresh", "traceIndex": mark + offset, **row}
        return None

    return wait_until(client, find, timeout, "terminal Ready covering %d files" % files)


def work_totals(rows):
    return {name: sum(row["stats"].get(name, 0) for row in rows)
            for name in ("diskReads", "parsedFiles", "discoveryWork", "domainDocuments")}


def padded(text, minimum):
    missing = minimum - len(text.encode("utf-8"))
    if missing <= 0:
        return text
    return text + "#" + "x" * max(0, missing - 2) + "\n"


def point(text, needle, after=False):
    offset = text.index(needle) + (len(needle) if after else 0)
    prefix = text[:offset]
    return {"line": prefix.count("\n"),
            "character": len(prefix.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2}


def rss_kib(client, field="VmRSS"):
    status = pathlib.Path("/proc") / str(client.proc.pid) / "status"
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1])
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--padding", type=int, default=0)
    parser.add_argument("--reference-mix", action="store_true")
    parser.add_argument("--ordinary-files", type=int, default=0)
    parser.add_argument("--max-rss-mib", type=int, default=0,
                        help="require an observed kernel VmHWM below this limit (Linux)")
    parser.add_argument("--burst", type=int, default=640)
    parser.add_argument("--ready-timeout", type=float, default=90)
    parser.add_argument("--reconcile-timeout", type=float, default=65)
    parser.add_argument("--allow-legacy-ready", action="store_true",
                        help="use the explicit one-shot completion adapter for the baseline binary")
    args = parser.parse_args()
    if args.count < 3 or min(args.padding, args.ordinary_files, args.max_rss_mib) < 0 or args.burst <= 512:
        parser.error("--count must be >= 3, --burst > 512, and byte/file/RSS limits nonnegative")
    wire.BINARY = args.binary.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = output / "workspace"
    root.mkdir()
    wire.write(root, ".argocd-ls.yaml", wire.CONFIG)
    app_text = padded(APP, args.padding)
    app = wire.write(root, "current/app.yaml", app_text)
    ordinary = None
    total_bytes = len(app_text.encode())
    foreign_text = padded(FOREIGN, args.padding)
    distribution = {"foreignConfigMaps": 0, "applications": 1, "appProjects": 1, "workflows": 0}
    for number in range(args.count - 2):
        filename = "resources/%03d/%06d.yaml" % (number // 1000, number)
        kind = number % 100
        resource_text = foreign_text
        if args.reference_mix and 90 <= kind < 99:
            resource_text = padded(wire.APP.replace("name: app", "name: parent-app-%06d" % number), args.padding)
            distribution["applications"] += 1
        elif args.reference_mix and kind == 99:
            resource_text = padded("apiVersion: argoproj.io/v1alpha1\nkind: Workflow\nmetadata: {name: parent-workflow-%06d}\nspec: {entrypoint: main, templates: [{name: main, container: {image: alpine}}]}\n" % number, args.padding)
            distribution["workflows"] += 1
        else:
            distribution["foreignConfigMaps"] += 1
        created = wire.write(root, filename, resource_text)
        ordinary = ordinary or created
        total_bytes += len(resource_text.encode())
    for number in range(args.ordinary_files):
        wire.write(root, "ordinary/%03d/%06d.txt" % (number // 1000, number), "ordinary text\n")
    project_text = padded(wire.PROJECT, args.padding)
    project = wire.write(root, "related/project.yaml", project_text)
    total_bytes += len(project_text.encode())
    os.environ["ARGOCD_LS_TRACE"] = "1"
    rows = []
    report = {"command": sys.argv, "binary": str(wire.BINARY),
              "binarySHA256": wire.hash_file(wire.BINARY),
              "runnerSHA256": wire.hash_file(pathlib.Path(__file__)),
              "platform": platform.platform(), "python": sys.version,
              "fixture": {"resourceFiles": args.count, "minimumFileBytes": args.padding,
                          "resourceBytes": total_bytes, "burstFiles": args.burst,
                          "ordinaryNonYAMLFiles": args.ordinary_files,
                          "distribution": distribution}, "rows": rows}
    client = None
    traces = None
    phase = "startup"

    def record(ident, checks, observed):
        row = {"id": ident, "passed": all(checks.values()), "checks": checks,
               "observed": observed}
        rows.append(row)
        print(json.dumps({"id": ident, "passed": row["passed"], "observed": observed}), flush=True)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    try:
        client = Client(output, [root])
        traces = Traces(client)
        opened = time.monotonic()
        client.open(app, app_text)
        local = diagnostic(client, app, 0,
                           lambda p: p.get("version") == 1 and has_code(p, "CD_DESTINATION_CONFLICT"),
                           timeout=min(args.ready_timeout, 15))
        ready = wait_ready(client, traces, 0, args.count, args.ready_timeout, args.allow_legacy_ready)
        definition = client.request("textDocument/definition", {
            "textDocument": {"uri": app.as_uri()}, "position": point(app_text, "team")})
        record("startup-ready-and-current-file", {
            "terminalCompleteEvidence": ready["mode"] in ("explicit-ready", "legacy-terminal-full-scan"),
            "currentFileRespondsWithinTwoSeconds": local["elapsed"] - (opened - client.start) <= 2,
            "crossFileDefinition": {location["uri"] for location in definition.get("result", [])} == {project.as_uri()},
        }, {"ready": ready, "firstLocalMilliseconds": (local["elapsed"] - (opened - client.start)) * 1000,
            "rssKiB": rss_kib(client), "definition": definition, "work": work_totals(traces.rows)})

        phase = "single-uri-save"
        mark = traces.mark()
        client.send("textDocument/didSave", {"textDocument": {"uri": app.as_uri()}})
        saved = wait_ready(client, traces, mark, args.count, args.ready_timeout, args.allow_legacy_ready)
        save_work = work_totals(traces.since(mark))
        record("single-uri-save-does-not-read-unrelated-files", {
            "noDiskReadsForAuthoritativeOpenBuffer": save_work["diskReads"] == 0,
            "noReparseForIdenticalAcceptedInput": save_work["parsedFiles"] == 0,
        }, {"work": save_work, "ready": saved})

        phase = "single-uri-watch"
        mark = traces.mark()
        ordinary.write_text(foreign_text.replace("before", "after!"), encoding="utf-8")
        client.send("workspace/didChangeWatchedFiles", {"changes": [{"uri": ordinary.as_uri(), "type": 2}]})
        watched = wait_ready(client, traces, mark, args.count, args.ready_timeout, args.allow_legacy_ready)
        watched_work = work_totals(traces.since(mark))
        record("single-uri-watch-reads-and-parses-one-file", {
            "exactlyOneDiskRead": watched_work["diskReads"] == 1,
            "exactlyOneParse": watched_work["parsedFiles"] == 1,
        }, {"work": watched_work, "ready": watched})

        phase = "missed-watch"
        mark = traces.mark()
        message_mark = len(client.messages)
        before = project.stat()
        renamed = project_text.replace("name: team", "name: gone")
        project.write_text(renamed, encoding="utf-8")
        os.utime(project, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = project.stat()
        started = time.monotonic()
        # Deliberately send neither a watcher notification nor a foreground
        # reference request: this must be the natural reconciliation path.
        missing = diagnostic(client, app, message_mark,
                             lambda p: workspace_publication(p) and p.get("version") == 1
                             and any(d.get("code") == "CD_PROJECT_UNRESOLVED" and d.get("severity") == 1
                                     for d in p.get("diagnostics", [])), args.reconcile_timeout)
        reconciled = wait_ready(client, traces, mark, args.count, args.ready_timeout, args.allow_legacy_ready)
        elapsed = time.monotonic() - started
        record("same-size-same-mtime-missed-watch-reconciles", {
            "sizeUnchanged": before.st_size == after.st_size,
            "mtimeUnchanged": before.st_mtime_ns == after.st_mtime_ns,
            "closedWorldMissingDiagnostic": has_code(missing["params"], "CD_PROJECT_UNRESOLVED"),
            "withinReconciliationDeadline": elapsed <= args.reconcile_timeout,
        }, {"seconds": elapsed, "ready": reconciled, "diagnostic": missing,
            "work": work_totals(traces.since(mark))})

        phase = "restore-missing-target"
        mark, message_mark = traces.mark(), len(client.messages)
        project.write_text(project_text, encoding="utf-8")
        client.send("workspace/didChangeWatchedFiles", {"changes": [{"uri": project.as_uri(), "type": 2}]})
        restored = wait_ready(client, traces, mark, args.count, args.ready_timeout, args.allow_legacy_ready)
        restored_diagnostics = diagnostic(client, app, message_mark,
                                         lambda p: workspace_publication(p) and p.get("version") == 1
                                         and not has_code(p, "CD_PROJECT_UNRESOLVED"))
        record("missing-target-reappearance-wakes-caller", {
            "unresolvedCleared": not has_code(restored_diagnostics["params"], "CD_PROJECT_UNRESOLVED")
        }, {"ready": restored, "work": work_totals(traces.since(mark))})

        phase = "queue-capacity-burst"
        changes, expected = [], set()
        for number in range(args.burst):
            name = "event-%04d" % number
            target = wire.write(root, "events/%04d.yaml" % number,
                                wire.PROJECT.replace("name: team", "name: " + name))
            changes.append({"uri": target.as_uri(), "type": 1})
            expected.add(name)
        mark = traces.mark()
        client.send("workspace/didChangeWatchedFiles", {"changes": changes})
        burst_ready = wait_ready(client, traces, mark, args.count + args.burst,
                                 args.ready_timeout, args.allow_legacy_ready)
        burst_rows = traces.since(mark)
        edited = app_text.replace("project: team", "project: event-")
        mark = traces.mark()
        client.change(app, edited, 2)
        current = diagnostic(client, app, 0, lambda p: p.get("version") == 2
                             and has_code(p, "CD_DESTINATION_CONFLICT"))
        edited_ready = wait_ready(client, traces, mark, args.count + args.burst,
                                  args.ready_timeout, args.allow_legacy_ready)
        completion = client.request("textDocument/completion", {
            "textDocument": {"uri": app.as_uri()}, "position": point(edited, "event-", after=True)})
        observed = {item.get("label") for item in completion.get("result", [])}
        queue_depths = [row["stats"]["queueDepth"] for row in burst_rows if "queueDepth" in row["stats"]]
        record("over-512-file-events-are-not-lost", {
            "burstExceedsQueueCapacity": args.burst > 512,
            "allCandidatesRetained": observed == expected,
            "queueBoundRespected": not queue_depths or max(queue_depths) <= 512,
            "currentBufferStillUpdates": current["params"].get("version") == 2,
        }, {"expectedCandidates": len(expected), "observedCandidates": len(observed),
            "missing": sorted(expected - observed), "unexpected": sorted(observed - expected),
            "responseError": completion.get("error"), "maxQueueDepth": max(queue_depths, default=None),
            "ready": burst_ready, "editedReady": edited_ready, "work": work_totals(burst_rows),
            "rssKiB": rss_kib(client)})
    except Exception as error:
        rows.append({"id": phase, "passed": False, "error": repr(error)})
        print(json.dumps(rows[-1]), flush=True)
    finally:
        if client is not None:
            # VmHWM is the kernel's process-lifetime RSS high-water mark up to
            # this observation, distinct from the current VmRSS sample. Other
            # platforms retain null values without an extra dependency/thread.
            report["memoryBeforeClose"] = {"rssKiB": rss_kib(client),
                                           "peakRssKiB": rss_kib(client, "VmHWM")}
            if args.max_rss_mib:
                peak = report["memoryBeforeClose"]["peakRssKiB"]
                rows.append({"id": "kernel-peak-rss-bound",
                             "passed": peak is not None and peak <= args.max_rss_mib * 1024,
                             "observedKiB": peak, "limitMiB": args.max_rss_mib})
            client.close()
            if traces is not None:
                traces.refresh()
                (output / "traces.json").write_text(json.dumps(traces.rows, indent=2) + "\n")
            rows.append({"id": "clean-server-exit", "passed": client.proc.returncode == 0,
                         "returncode": client.proc.returncode})
        report["passed"] = len(rows) == 7 + bool(args.max_rss_mib) and all(row.get("passed") for row in rows)
        report["artifactSHA256"] = {name: wire.hash_file(output / name)
                                    for name in ("stdin", "stdout", "stderr", "messages.json", "traces.json")
                                    if (output / name).exists()}
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
