#!/usr/bin/env python3
"""A committed background diagnostic survives an unrelated protocol epoch.

Linux inotify observes the real file open, without a server delay or test hook.
The large comment makes the overlap repeatable while keeping the YAML valid.
"""
import argparse
import ctypes
import json
import os
import pathlib
import select
import sys
import time

import audit_repair_e2e as wire
from persistent_workspace_e2e import Client, Traces, wait_ready, wait_until


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("this consumer requires Linux inotify")
    wire.BINARY = args.binary.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    roots = [output / "a", output / "b"]
    for root in roots:
        wire.write(root, ".argocd-ls.yaml", wire.CONFIG)
        wire.write(root, "project.yaml", wire.PROJECT)
    changed = wire.write(roots[0], "app.yaml", wire.APP)
    current = wire.write(roots[1], "app.yaml", wire.APP)
    os.environ["ARGOCD_LS_TRACE"] = "1"
    client, descriptor, rows = None, -1, []
    report = {"binarySHA256": wire.hash_file(wire.BINARY),
              "runnerSHA256": wire.hash_file(pathlib.Path(__file__)), "rows": rows}
    try:
        client = Client(output, roots)
        traces = Traces(client)
        client.open(current, wire.APP)
        wait_ready(client, traces, 0, 4, 15, False)
        changed.write_text(wire.APP + "  revisionHistoryLimit: wrong-type\n#" + "x" * 4_500_000 + "\n")
        libc = ctypes.CDLL(None, use_errno=True)
        descriptor = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if descriptor < 0 or libc.inotify_add_watch(descriptor, os.fsencode(changed), 0x20) < 0:
            raise OSError(ctypes.get_errno(), "inotify file-open observation")
        mark, message_mark = traces.mark(), len(client.messages)
        client.send("workspace/didChangeWatchedFiles", {
            "changes": [{"uri": changed.as_uri(), "type": 2}]})
        if not select.select([descriptor], [], [], 10)[0]:
            raise TimeoutError("background file open")
        os.read(descriptor, 65536)
        client.change(current, wire.APP + "# unrelated current edit\n", 2)
        ready = wait_ready(client, traces, mark, 4, 8, False)

        def publication(path, version=None, code=None):
            for entry in client.messages[message_mark:]:
                message = entry["message"]
                params = message.get("params", {})
                if (message.get("method") == "textDocument/publishDiagnostics"
                        and params.get("uri") == path.as_uri()
                        and params.get("x-argocd-analysis-scope") == "workspace"
                        and (version is None or params.get("version") == version)
                        and (code is None or any(d.get("code") == code for d in params.get("diagnostics", [])))):
                    return params
            return None

        delivered = None
        try:
            delivered = wait_until(client, lambda: publication(changed, code="SCHEMA_TYPE"),
                                   2, "committed diagnostic before periodic reconciliation")
        except TimeoutError:
            pass
        work = [row for row in traces.since(mark) if row["stats"].get("scope") == "incremental"]
        checks = {
            "obsoleteBatchObserved": any(not row["delivered"] and row["stats"].get("parsedFiles", 0) > 0 for row in work),
            "committedDiagnosticDelivered": delivered is not None,
            "currentBufferVersionPreserved": publication(current, version=2) is not None,
            "readyWithoutRootRescan": ready["stats"]["complete"] and not any(row["stats"].get("fullScan") for row in work),
            "retryDoesNotRereadFile": sum(row["stats"].get("diskReads", 0) for row in work) == 1,
        }
        rows.append({"id": "cross-epoch-background-publication", "checks": checks,
                     "passed": all(checks.values()), "publication": delivered, "work": work})
    except Exception as error:
        rows.append({"id": "runner", "passed": False, "error": repr(error)})
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if client:
            client.close()
            rows.append({"id": "clean-exit", "passed": client.proc.returncode == 0})
        report["passed"] = len(rows) == 2 and all(row["passed"] for row in rows)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "rows": rows}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
