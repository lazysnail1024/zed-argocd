#!/usr/bin/env python3
"""Real CLI/LSP regressions for effective paths, live Helm state and URI identity.

The same assertions run against a pre-fix binary for the negative control. Each
case retains its inputs and native stdio; no server state or parser is mocked.
"""

import argparse
import copy
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
import tomllib
from urllib.parse import urlsplit

import audit_repair_e2e as wire


REPO_A = "https://example.invalid/repo-a"
REPO_B = "https://example.invalid/repo-b"
PATH_CONFIG = wire.CONFIG + """exclude: ["repo-a/**", "repo-b/**"]
repositories:
  - {repoURL: https://example.invalid/repo-a, targetRevision: HEAD, path: repo-a}
  - {repoURL: https://example.invalid/repo-a, path: repo-a}
  - {repoURL: https://example.invalid/repo-b, targetRevision: feature, path: repo-b}
"""
BASE_SET = {
    "apiVersion": "argoproj.io/v1alpha1",
    "kind": "ApplicationSet",
    "metadata": {"name": "apps"},
    "spec": {
        "goTemplate": True,
        "generators": [{"list": {"elements": [{"name": "one"}]}}],
        "template": {
            "metadata": {"name": "child"},
            "spec": {
                "project": "team",
                "source": {"repoURL": REPO_A, "targetRevision": "HEAD", "path": "original"},
                "destination": {"server": "https://kubernetes.default.svc", "namespace": "default"},
            },
        },
    },
}


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def encode(value):
    # JSON is valid YAML and makes the fixture's effective values unambiguous.
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def missing_paths(diagnostics):
    return [d for d in diagnostics if d.get("code") == "CD_LOCAL_PATH_MISSING"
            or "CD_LOCAL_PATH_MISSING:" in d.get("message", "")]


def position(text, needle):
    before = text[:text.index(needle)]
    return {"line": before.count("\n"), "character": len(before.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2}


def apply_edits(text, edits):
    def offset(point):
        lines = text.splitlines(keepends=True)
        prefix = lines[point["line"]].encode("utf-16-le")[:point["character"] * 2].decode("utf-16-le")
        return sum(len(line) for line in lines[:point["line"]]) + len(prefix)
    spans = [(offset(edit["range"]["start"]), offset(edit["range"]["end"]), edit["newText"]) for edit in edits]
    for start, end, replacement in sorted(spans, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def diagnostics_since(client, path, version, start):
    def matches(message):
        params = message.get("params", {})
        return message.get("method") == "textDocument/publishDiagnostics" and params.get("uri") == path.as_uri() and params.get("version") == version
    for entry in client.messages[start:]:
        if matches(entry["message"]):
            return entry["message"]["params"]["diagnostics"]
    # Responses can overtake diagnostics. Retain only messages received after
    # the triggering edit/event, including when the version stays unchanged.
    message = client.wait(lambda m: matches(m) and any(e["message"] is m for e in client.messages[start:]))
    return message["params"]["diagnostics"]


def traces(folder):
    return [json.loads(line.split(" ", 1)[1]) for line in (folder / "stderr").read_text(encoding="utf-8").splitlines()
            if line.startswith("argocd-analysis ")]


class Suite:
    def __init__(self, binary, output):
        self.binary = binary
        self.output = output
        self.rows = []

    def record(self, ident, checks, observed=None, **extra):
        row = {"id": ident, "passed": all(checks.values()), "checks": checks, "observed": observed, **extra}
        self.rows.append(row)
        print(json.dumps({"id": ident, "passed": row["passed"]}), flush=True)

    def fixture(self, name, config=wire.CONFIG):
        folder = self.output / name
        root = folder / "workspace"
        root.mkdir(parents=True)
        write(root, ".argocd-ls.yaml", config)
        return folder, root

    def cli(self, folder, root, name):
        command = [str(self.binary), "check", "--root", str(root), "--format", "json"]
        result = subprocess.run(command, capture_output=True, timeout=25)
        (folder / (name + ".stdout")).write_bytes(result.stdout)
        (folder / (name + ".stderr")).write_bytes(result.stderr)
        report = json.loads(result.stdout)
        return result.returncode, report

    def local_paths(self):
        folder, root = self.fixture("effective-paths", PATH_CONFIG)
        write(root, "project.yaml", wire.PROJECT)
        for name in ["repo-a/original", "repo-a/patched", "repo-b/final"]:
            (root / name).mkdir(parents=True)
        app = root / "app.yaml"
        patched = copy.deepcopy(BASE_SET)
        patched["spec"]["templatePatch"] = "spec:\n  source:\n    path: patched\n"
        write(root, app.name, encode(patched))
        code, report = self.cli(folder, root, "patched-exists")
        self.record("path-patch-exists", {"complete": report["analysisComplete"], "exitZero": code == 0,
                    "noMissingPath": not missing_paths(report["diagnostics"]), "noSchemaError": not any(d["severity"] == 1 for d in report["diagnostics"])}, report)

        (root / "repo-a/patched").rmdir()
        code, report = self.cli(folder, root, "patched-absent")
        missing = missing_paths(report["diagnostics"])
        self.record("path-patch-absent", {"warningOnlyExitZero": code == 0, "missingWarning": len(missing) == 1 and missing[0]["severity"] == 2}, report)
        (root / "repo-a/patched").mkdir()
        code, report = self.cli(folder, root, "patched-restored")
        self.record("path-patch-restored", {"complete": report["analysisComplete"], "exitZero": code == 0, "noMissingPath": not missing_paths(report["diagnostics"])}, report)

        combo = copy.deepcopy(BASE_SET)
        combo["spec"]["templatePatch"] = "spec:\n  source:\n    repoURL: " + REPO_B + "\n    targetRevision: feature\n    path: final\n"
        direct = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", **copy.deepcopy(BASE_SET["spec"]["template"])}
        direct["spec"]["source"] = {"repoURL": REPO_B, "targetRevision": "feature", "path": "final"}
        observed = {}
        for name, value in [("set", combo), ("application", direct)]:
            write(root, app.name, encode(value))
            code, report = self.cli(folder, root, "combo-" + name)
            observed[name] = {"exit": code, "diagnostics": report["diagnostics"]}
        self.record("path-url-revision-combination", {"equivalentExistingSource": all(v["exit"] == 0 and not v["diagnostics"] for v in observed.values())}, observed)
        (root / "repo-b/final").rmdir()
        for name, value in [("set", combo), ("application", direct)]:
            write(root, app.name, encode(value))
            code, report = self.cli(folder, root, "combo-absent-" + name)
            observed[name] = {"exit": code, "diagnostics": report["diagnostics"]}
        self.record("path-url-revision-combination-absent", {"equivalentMissingSource": all(v["exit"] == 0 and len(missing_paths(v["diagnostics"])) == 1 for v in observed.values())}, observed)
        (root / "repo-b/final").mkdir()

        formerly_dynamic = copy.deepcopy(combo)
        formerly_dynamic["spec"]["template"]["spec"]["source"] = {"repoURL": "{{ .repoURL }}", "targetRevision": "{{ .revision }}", "path": "{{ .path }}"}
        write(root, app.name, encode(formerly_dynamic))
        for name, absent in [("exists", False), ("absent", True)]:
            if absent:
                (root / "repo-b/final").rmdir()
            code, report = self.cli(folder, root, "static-patch-over-dynamic-" + name)
            self.record("path-static-patch-over-dynamic-" + name, {"effectiveStaticStateChecked": bool(missing_paths(report["diagnostics"])) == absent,
                        "noSchemaError": not any(d["severity"] == 1 for d in report["diagnostics"])}, report)
        (root / "repo-b/final").mkdir()

        generator = copy.deepcopy(BASE_SET)
        generator["spec"]["generators"][0]["list"]["template"] = {"metadata": {}, "spec": {"project": "", "destination": {}, "source": {"repoURL": REPO_A, "path": "patched"}}}
        write(root, app.name, encode(generator))
        code, report = self.cli(folder, root, "generator-inherited-revision")
        self.record("path-generator-inherited-revision", {"complete": report["analysisComplete"], "noMissingPath": not missing_paths(report["diagnostics"]), "noSchemaError": not any(d["severity"] == 1 for d in report["diagnostics"])}, report)
        (root / "repo-a/patched").rmdir()
        code, report = self.cli(folder, root, "generator-path-absent")
        self.record("path-generator-inherited-revision-absent", {"missingWarning": len(missing_paths(report["diagnostics"])) == 1}, report)
        (root / "repo-a/patched").mkdir()

        for field in ["path", "repoURL", "targetRevision"]:
            dynamic = copy.deepcopy(BASE_SET)
            dynamic["spec"]["template"]["spec"]["source"][field] = "{{ ." + field + " }}"
            # A separate known missing source prevents an absent/unknown index
            # entry from passing just because the whole map happens to be empty.
            missing_app = copy.deepcopy(direct)
            missing_app["spec"]["source"] = {"repoURL": REPO_A, "path": "original"}
            (root / "repo-a/original").rmdir()
            write(root, "known-missing.yaml", encode(missing_app))
            write(root, app.name, encode(dynamic))
            code, report = self.cli(folder, root, "dynamic-" + field)
            own = [d for d in report["diagnostics"] if d["uri"] == app.as_uri()]
            self.record("path-dynamic-" + field, {"notCertainlyMissing": not missing_paths(own), "dynamicExplained": any(d["code"] == "CD_APPSET_DYNAMIC_FIELD" for d in own),
                        "knownMissingControl": len(missing_paths(report["diagnostics"])) == 1}, report)
            (root / "known-missing.yaml").unlink()
            (root / "repo-a/original").mkdir()

        write(root, app.name, encode(patched))
        client = wire.LSP(folder, [root])
        try:
            client.open(app, encode(patched))
            initial = client.diag(app, 1)
            self.record("path-lsp-initial", {"noMissingPath": not missing_paths(initial)}, initial)
            for name, missing in [("delete", True), ("restore", False)]:
                marker = len(client.messages)
                path = root / "repo-a/patched"
                path.rmdir() if missing else path.mkdir()
                client.send("workspace/didChangeWatchedFiles", {"changes": [{"uri": path.as_uri(), "type": 3 if missing else 1}]})
                diagnostics = diagnostics_since(client, app, 1, marker)
                self.record("path-filesystem-only-" + name, {"expectedPathState": bool(missing_paths(diagnostics)) == missing}, diagnostics)
        finally:
            client.close()

    def helm(self):
        manifest_path = pathlib.Path(__file__).resolve().parents[1] / "extension.toml"
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        server = manifest["language_servers"]["argocd-ls"]
        expected = {"YAML": "yaml", "Helm": "helm"}
        checks = {"languagesDeclared": all(name in server["languages"] for name in expected),
                  "protocolIDsMatch": all(server.get("language_ids", {}).get(name) == ident for name, ident in expected.items())}
        self.record("helm-language-registration", checks,
                    {"server": server, "extensionSHA256": wire.hash_file(manifest_path)})
        if not all(checks.values()):
            return
        # Exercise the IDs actually shipped by the adapter. Protocol coverage
        # does not establish that a real Zed host attached these servers.
        language_ids = [server["language_ids"][name] for name in expected]
        for language_id in language_ids:
            self.helm_language(language_id)
        self.helm_language_switch(language_ids)

    def helm_language(self, language_id):
        # Keep YAML case IDs stable for the existing mutation-test consumer.
        prefix = "helm-" if language_id == "yaml" else "helm-" + language_id + "-"
        folder, root = self.fixture(prefix + "transitions")
        write(root, "project.yaml", wire.PROJECT)
        write(root, "other.yaml", wire.APP.replace("name: app", "name: other"))
        write(root, "chart/Chart.yaml", "apiVersion: v2\nname: chart\nversion: 1.0.0\n")
        plain = wire.APP.replace("project: team", "project: 42")
        template = wire.APP.replace("project: team", "project: {{ .Values.project }}")
        quoted = wire.APP.replace("project: team", 'project: "{{ .Values.project }}"')
        app = write(root, "chart/templates/app.yaml", plain)
        disk_bytes = app.read_bytes()
        client = wire.LSP(folder, [root])
        transitions = []
        try:
            client.open(app, plain, language_id=language_id)
            diagnostics = client.diag(app, 1)
            self.record(prefix + "pure-yaml-control", {"schemaStillChecked": any(d["code"] == "SCHEMA_TYPE" and d["severity"] == 1 for d in diagnostics)}, diagnostics)
            for version, text, skipped in [(2, template, True), (3, plain, False), (4, quoted, True), (5, plain, False)]:
                marker = len(client.messages)
                client.change(app, text, version)
                symbols = client.request("textDocument/documentSymbol", {"textDocument": {"uri": app.as_uri()}})
                diagnostics = diagnostics_since(client, app, version, marker)
                checks = {"queryUsesCurrentClass": "error" not in symbols and (symbols.get("result") == [] if skipped else any(s["name"] == "app" for s in symbols.get("result", []))),
                          "sourceUnchanged": app.read_bytes() == disk_bytes}
                if skipped:
                    checks["onlyUnrenderedInfo"] = len(diagnostics) == 1 and diagnostics[0]["code"] == "HELM_UNRENDERED" and diagnostics[0]["severity"] == 3
                else:
                    checks["schemaRestored"] = any(d["code"] == "SCHEMA_TYPE" and d["severity"] == 1 for d in diagnostics) and not any(d["code"] == "HELM_UNRENDERED" for d in diagnostics)
                self.record(prefix + "unsaved-version-" + str(version), checks, {"diagnostics": diagnostics, "symbols": symbols})
                transitions.append(diagnostics)
        finally:
            client.close()
        hot = [row for row in traces(folder) if not row["fullScan"]]
        self.record(prefix + "transitions-no-acquisition", {"fourCompletedEdits": len(hot) == 4, "noUnrelatedReads": all(row["diskReads"] == 0 for row in hot),
                    "noFallback": all(row.get("acquisitionFallbacks") == 0 for row in hot)}, hot)

        direct_folder, direct_root = self.fixture(prefix + "direct-open")
        write(direct_root, "project.yaml", wire.PROJECT)
        write(direct_root, "chart/Chart.yaml", "apiVersion: v2\nname: chart\nversion: 1.0.0\n")
        direct = write(direct_root, "chart/templates/app.yaml", template)
        client = wire.LSP(direct_folder, [direct_root])
        try:
            client.open(direct, template, language_id=language_id)
            diagnostics = client.diag(direct, 1)
            compact = lambda ds: [(d["code"], d["severity"], d["message"]) for d in ds]
            self.record(prefix + "direct-and-edited-equivalent", {"sameDiagnosticContract": compact(diagnostics) == compact(transitions[0])}, diagnostics)
        finally:
            client.close()

    def helm_language_switch(self, language_ids):
        folder, root = self.fixture("helm-language-switch")
        write(root, "project.yaml", wire.PROJECT)
        write(root, "chart/Chart.yaml", "apiVersion: v2\nname: chart\nversion: 1.0.0\n")
        plain = wire.APP.replace("project: team", "project: 42")
        template = wire.APP.replace("project: team", 'project: "{{ .Values.project }}"')
        app = write(root, "chart/templates/app.yaml", plain)
        disk_bytes = app.read_bytes()
        client = wire.LSP(folder, [root])
        try:
            client.open(app, template, version=7, language_id=language_ids[0])
            diagnostics = client.diag(app, 7)
            self.record("helm-language-switch-initial", {"templateBuffer": len(diagnostics) == 1 and diagnostics[0]["code"] == "HELM_UNRENDERED"}, diagnostics)
            # didChange has no languageId. A host changes a document's language
            # by closing and reopening it; the new lifetime may reset version.
            for language_id in [language_ids[1], language_ids[0]]:
                client.send("textDocument/didClose", {"textDocument": {"uri": app.as_uri()}})
                client.wait(lambda message: message.get("method") == "textDocument/publishDiagnostics"
                            and message.get("params", {}).get("uri") == app.as_uri()
                            and message["params"].get("version") is None)
                marker = len(client.messages)
                client.open(app, plain, version=1, language_id=language_id)
                symbols = client.request("textDocument/documentSymbol", {"textDocument": {"uri": app.as_uri()}})
                diagnostics = diagnostics_since(client, app, 1, marker)
                self.record("helm-language-reopen-" + language_id,
                            {"resetVersionAccepted": any(d["code"] == "SCHEMA_TYPE" and d["severity"] == 1 for d in diagnostics),
                             "oldTemplateCleared": not any(d["code"] == "HELM_UNRENDERED" for d in diagnostics),
                             "currentSymbols": "error" not in symbols and any(s["name"] == "app" for s in symbols.get("result", [])),
                             "sourceUnchanged": app.read_bytes() == disk_bytes},
                            {"diagnostics": diagnostics, "symbols": symbols, "languageId": language_id})
                client.change(app, template, 2)
                diagnostics = client.diag(app, 2)
                self.record("helm-language-reopen-edit-" + language_id,
                            {"currentTemplate": len(diagnostics) == 1 and diagnostics[0]["code"] == "HELM_UNRENDERED" and diagnostics[0]["severity"] == 3,
                             "sourceUnchanged": app.read_bytes() == disk_bytes}, diagnostics)
        finally:
            client.close()

    def uri_one(self, alias):
        name = "uri-symlink" if alias else "uri-canonical"
        folder, root = self.fixture(name)
        actual = write(root, "source folder/app file.yaml", wire.APP)
        actual_project = write(root, "source folder/project file.yaml", wire.PROJECT)
        write(root, "other.yaml", wire.APP.replace("name: app", "name: other"))
        app, project = actual, actual_project
        if alias:
            app, project = root / "app alias.yaml", root / "project alias.yaml"
            try:
                app.symlink_to(actual)
                project.symlink_to(actual_project)
            except OSError as error:
                if os.name != "nt":
                    raise
                self.record(name, {"canonicalControlStillRequired": True}, {"reason": str(error)}, skipped="Windows runner does not permit creating symlinks")
                return
        client = wire.LSP(folder, [root])
        try:
            client.open(project, wire.PROJECT)
            client.open(app, wire.APP)
            initial = client.diag(app, 1)
            self.record(name + "-initial", {"diagnosticsUseClientURI": all(d["uri"] == app.as_uri() for d in initial), "validApp": not initial}, initial)
            text = wire.APP + "# comment-only edit\n"
            client.change(app, text, 2)
            diagnostics = client.diag(app, 2)
            definition = client.request("textDocument/definition", {"textDocument": {"uri": app.as_uri()}, "position": position(text, "team")})
            references = client.request("textDocument/references", {"textDocument": {"uri": project.as_uri()}, "position": position(wire.PROJECT, "team"), "context": {"includeDeclaration": True}})
            self.record(name + "-navigation", {"definitionClientURI": [v["uri"] for v in definition.get("result", [])] == [project.as_uri()],
                        "referencesClientURI": app.as_uri() in [v["uri"] for v in references.get("result", [])], "commentDoesNotChangeDiagnostics": not diagnostics}, {"definition": definition, "references": references})
            typo = text.replace("project: team", "proj: team")
            client.change(app, typo, 3)
            diagnostics = client.diag(app, 3)
            start = position(typo, "proj:")
            actions = client.request("textDocument/codeAction", {"textDocument": {"uri": app.as_uri()}, "range": {"start": start, "end": {**start, "character": start["character"] + 4}}, "context": {"diagnostics": diagnostics}})
            action = next(item for item in actions.get("result", []) if item.get("data", {}).get("fixCode") == "FIX_FIELD_NAME")
            resolved = client.request("codeAction/resolve", action)
            changes = resolved.get("result", {}).get("edit", {}).get("documentChanges", [])
            correct = len(changes) == 1 and changes[0]["textDocument"] == {"uri": app.as_uri(), "version": 3}
            applied = apply_edits(typo, changes[0]["edits"]) if correct else typo
            self.record(name + "-versioned-client-edit", {"clientURIAndVersion": correct, "exactBytes": applied == text}, resolved)
            client.change(app, applied, 4)
            diagnostics = client.diag(app, 4)
            self.record(name + "-edit-verified", {"diagnosticRemoved": not diagnostics}, diagnostics)
            if os.name == "nt" and not alias:
                self.record("uri-real-windows-drive", {"actualDriveURI": bool(re.match(r"^/[A-Za-z]:/", urlsplit(app.as_uri()).path))}, {"uri": app.as_uri(), "platform": platform.platform()})
        finally:
            client.close()
        hot = [row for row in traces(folder) if not row["fullScan"]]
        self.record(name + "-incremental-work", {"threeCompletedEdits": len(hot) == 3, "noDiskReads": all(row["diskReads"] == 0 for row in hot),
                    "oneParsedFile": all(row["parsedFiles"] == 1 for row in hot), "onlyAppAndProjectDomain": all(row["domainDocuments"] == 2 for row in hot),
                    "noHiddenFallback": all(row.get("acquisitionFallbacks") == 0 for row in hot)}, hot)

    def uri_retarget(self):
        folder, root = self.fixture("uri-retarget", wire.CONFIG + 'scopes: [{name: a, include: ["a/**"]}, {name: b, include: ["b/**"]}]\n')
        first = write(root, "a/app.yaml", wire.APP)
        second = write(root, "b/app.yaml", wire.APP.replace("name: app", "name: second"))
        first_project = write(root, "a/project.yaml", wire.PROJECT)
        second_project = write(root, "b/project.yaml", wire.PROJECT)
        alias = root / "client-alias.yaml"
        try:
            alias.symlink_to(first)
        except OSError as error:
            if os.name != "nt":
                raise
            self.record("uri-retarget", {"canonicalControlStillRequired": True}, {"reason": str(error)}, skipped="Windows runner does not permit creating symlinks")
            return
        client = wire.LSP(folder, [root])
        try:
            client.open(alias, wire.APP)
            client.diag(alias, 1)
            before = client.request("textDocument/definition", {"textDocument": {"uri": alias.as_uri()}, "position": position(wire.APP, "team")})
            alias.unlink()
            alias.symlink_to(second)
            text = second.read_text(encoding="utf-8")
            client.change(alias, text, 2)
            diagnostics = client.diag(alias, 2)
            after = client.request("textDocument/definition", {"textDocument": {"uri": alias.as_uri()}, "position": position(text, "team")})
            self.record("uri-retarget-scope-identity", {"initialScope": [v["uri"] for v in before.get("result", [])] == [first_project.as_uri()],
                        "newScope": [v["uri"] for v in after.get("result", [])] == [second_project.as_uri()], "noStaleDiagnostics": not diagnostics}, {"before": before, "after": after})
        finally:
            client.close()
        hot = [row for row in traces(folder) if not row["fullScan"]]
        self.record("uri-retarget-invalidates-membership", {"oneCompletedEdit": len(hot) == 1, "identityFallback": len(hot) == 1 and hot[0].get("acquisitionFallbacks") == 1,
                    "freshAcquisition": len(hot) == 1 and hot[0]["diskReads"] > 0}, hot)

    def uri(self):
        for callback in [lambda: self.uri_one(False), lambda: self.uri_one(True), self.uri_retarget]:
            try:
                callback()
            except Exception as error:
                self.record("uri-scenario", {"completed": False}, repr(error))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--case", choices=["all", "local-paths", "helm", "uri"], default="all")
    args = parser.parse_args()
    wire.BINARY = args.binary.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["ARGOCD_LS_TRACE"] = "1"
    suite = Suite(wire.BINARY, output)
    for name, callback in [("local-paths", suite.local_paths), ("helm", suite.helm), ("uri", suite.uri)]:
        if args.case not in ["all", name]:
            continue
        try:
            callback()
        except Exception as error:
            suite.record(name + "-scenario", {"completed": False}, repr(error))
    for client in wire.CLIENTS:
        client.close()
    report = {"command": sys.argv, "platform": platform.platform(), "python": sys.version,
              "binarySHA256": wire.hash_file(wire.BINARY), "runnerSHA256": wire.hash_file(pathlib.Path(__file__)),
              "rows": suite.rows, "passed": bool(suite.rows) and all(row["passed"] for row in suite.rows)}
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output / "artifact-hashes.json").write_text(json.dumps({str(path.relative_to(output)): wire.hash_file(path) for path in output.rglob("*") if path.is_file() and path.name != "artifact-hashes.json"}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(suite.rows), "failed": [row["id"] for row in suite.rows if not row["passed"]]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
