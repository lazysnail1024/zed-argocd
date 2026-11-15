#!/usr/bin/env python3
"""Independent current-buffer status and guard-derived refusal contracts."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

import audit_repair_e2e as wire
from schema_queries_e2e import position

CONFIG = 'schemaVersion: 1\ntargetVersion: "3.5.6"\ncontrolPlaneNamespace: argocd\nreferencePolicy: closed\n'
COMMAND = 'argocd.explainAnalysis'
METHOD = 'argocd/analysisStatus'
KIND = 'source.argocd.explainAnalysis'


class Client(wire.LSP):
    def send(self, method, params=None, ident=None):
        if method == 'initialize':
            params['capabilities']['textDocument'] = {'codeAction': {'codeActionLiteralSupport': {'codeActionKind': {'valueSet': ['quickfix', 'source']}}}}
        return super().send(method, params, ident)


def request(client, method, params):
    deadline = time.monotonic() + 6
    while True:
        response = client.request(method, params)
        if response.get('error', {}).get('code') != -32801 or time.monotonic() >= deadline:
            return response
        time.sleep(.02)


def query(file, text, token='team'):
    return {'textDocument': {'uri': file.as_uri()}, 'position': position(text, text.index(token))}


def availability(status, operation):
    return next((item for item in status.get('operations', []) if item.get('operation') == operation), {})


def run(binary, output):
    wire.BINARY = binary
    rows = []

    def record(name, checks, observed):
        rows.append({'id': name, 'assertions': checks, 'observed': observed, 'passed': bool(checks) and all(checks.values())})
        (output / 'report.json').write_text(json.dumps({'rows': rows, 'passed': all(row['passed'] for row in rows), 'command': sys.argv, 'binaryCommand': [str(binary), 'serve', '--stdio'], 'environment': {'python': sys.version, 'platform': platform.platform()}, 'binarySHA256': wire.hash_file(binary), 'runnerSHA256': wire.hash_file(Path(__file__))}, indent=2) + '\n')
        print(name, 'PASS' if rows[-1]['passed'] else 'FAIL', flush=True)

    folder = output / 'cold-index'
    root = folder / 'workspace'
    root.mkdir(parents=True)
    wire.write(root, '.argocd-ls.yaml', CONFIG)
    app = wire.write(root, 'app.yaml', wire.APP)
    for index in range(1500): wire.write(root, f'ordinary-{index:04}.yaml', 'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: ordinary}\n')
    client = Client(folder, [root])
    try:
        client.open(app, wire.APP)
        status = request(client, METHOD, query(app, wire.APP)).get('result') or {}
        hover = request(client, 'textDocument/hover', {'textDocument': {'uri': app.as_uri()}, 'position': {'line': 5, 'character': 4}})
        record('cold-index-current-document', {'currentDocumentIndependent': status.get('completeness', {}).get('document') is True, 'indexNotDeclaredComplete': status.get('completeness', {}).get('workspace') is False, 'hoverUsable': hover.get('result') is not None, 'noUnsolicitedMessage': not any(item['message'].get('method') == 'window/showMessage' for item in client.messages)}, {'status': status, 'hover': hover})
    finally:
        client.close()

    scenarios = [('closed-file', CONFIG, ''), ('incomplete', CONFIG, 'broken'), ('ambiguous', CONFIG, 'duplicate'), ('dynamic', CONFIG, 'dynamic'), ('namespace', CONFIG.replace('controlPlaneNamespace: argocd\n', ''), ''), ('open-policy', CONFIG.replace('referencePolicy: closed', 'referencePolicy: open'), ''), ('isolated', CONFIG + 'exclude: ["excluded/**"]\n', '')]
    for name, config, extra in scenarios:
        folder = output / name
        root = folder / 'workspace'
        root.mkdir(parents=True)
        wire.write(root, '.argocd-ls.yaml', config)
        project = wire.write(root, 'project.yaml', wire.PROJECT)
        text = wire.APP + '# current unsaved buffer\n'
        app = wire.write(root, 'excluded/app.yaml' if name == 'isolated' else 'app.yaml', wire.APP)
        if extra == 'broken': wire.write(root, 'broken.yaml', 'apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata: [\n')
        if extra == 'duplicate': wire.write(root, 'duplicate.yaml', wire.PROJECT)
        if extra == 'dynamic': wire.write(root, 'dynamic.yaml', wire.APP.replace('name: app', 'name: dynamic').replace('project: team', 'project: "{{project}}"'))
        client = Client(folder, [root])
        try:
            client.open(app, text, 7)
            client.diag(app, 7)
            params = query(app, text)
            status_response = request(client, METHOD, params)
            status = status_response.get('result') or {}
            # No protocol success, empty body, or scalar result can stand in for the contract.
            common = {'bufferIdentity': status.get('inputIdentity', {}).get('source') == 'buffer' and status.get('inputIdentity', {}).get('version') == 7 and status.get('inputIdentity', {}).get('sha256') == hashlib.sha256(text.encode()).hexdigest(), 'rootAndScope': status.get('root') == str(root) and bool(status.get('scope')), 'documentComplete': status.get('completeness', {}).get('document') is True, 'readOnly': app.read_text() == wire.APP and (root / '.argocd-ls.yaml').read_text() == config}
            expected = {'closed-file': 'AFFECTED_FILE_NOT_OPEN', 'incomplete': 'INDEX_INCOMPLETE', 'ambiguous': 'AMBIGUOUS_BINDING', 'dynamic': 'DYNAMIC_REFERENCE', 'namespace': 'TARGET_UNKNOWN', 'open-policy': 'REFERENCE_SCOPE_OPEN', 'isolated': 'ISOLATED_DOCUMENT'}[name]
            operation = availability(status, 'rename')
            renamed = request(client, 'textDocument/rename', params | {'newName': 'platform'})
            refusal = renamed.get('error', {}).get('data', {})
            common.update(guardReason=operation.get('allowed') is False and operation.get('reasonCode') == expected, structuredError=refusal.get('reasonCode') == expected and bool(refusal.get('nextStep')), noUnsafeEdit='result' not in renamed)
            if name in ['ambiguous', 'dynamic', 'namespace']:
                refs = availability(status, 'references')
                common.update(noFalseZero=refs.get('allowed') is False and 'resultCount' not in refs and refs.get('reasonCode') == expected)
            if name == 'isolated':
                common.update(exclusionExplained=any(r.get('reasonCode') == 'ISOLATED_DOCUMENT' for r in status.get('reasons', [])), referencesRemainIsolated=availability(status, 'references').get('reasonCode') == 'ISOLATED_DOCUMENT')
            if name == 'incomplete':
                references = availability(status, 'references')
                common.update(independentCompleteness=status.get('completeness', {}).get('workspace') is False, unknownIsNotZero=references.get('allowed') is False and 'resultCount' not in references and references.get('reasonCode') == 'INDEX_INCOMPLETE')
            record(name, common, {'status': status_response, 'rename': renamed})
            if name == 'closed-file':
                locations = operation.get('relatedLocations', [])
                client.open(project, wire.PROJECT, 3)
                client.diag(project, 3)
                recovered = request(client, METHOD, params).get('result') or {}
                record('open-file-recovery', {'affectedFileIdentified': any(item.get('uri') == project.as_uri() for item in locations), 'renameRecovered': availability(recovered, 'rename').get('allowed') is True, 'noMutation': app.read_text() == wire.APP and project.read_text() == wire.PROJECT}, recovered)
                # Zero is stated only for the complete query at a real definition.
                only_project = wire.PROJECT.replace('name: team', 'name: unused')
                unused = wire.write(root, 'unused.yaml', only_project)
                client.open(unused, only_project, 1)
                client.diag(unused, 1)
                zero = request(client, METHOD, query(unused, only_project, 'unused')).get('result') or {}
                record('proven-zero-references', {'completeScope': zero.get('completeness', {}).get('workspace') is True, 'knownZero': availability(zero, 'references').get('allowed') is True and availability(zero, 'references').get('resultCount') == 0}, zero)

                action_request = {'textDocument': {'uri': app.as_uri()}, 'range': {'start': params['position'], 'end': params['position']}, 'context': {'diagnostics': [], 'only': [KIND]}}
                listed = request(client, 'textDocument/codeAction', action_request)
                actions = [a for a in listed.get('result', []) if a.get('command', {}).get('command') == COMMAND]
                before = sum(item['message'].get('method') == 'window/showMessage' for item in client.messages)
                explained = request(client, 'workspace/executeCommand', actions[0]['command']) if actions else {}
                if actions: client.wait(lambda row: row.get('method') == 'window/showMessage')
                after = sum(item['message'].get('method') == 'window/showMessage' for item in client.messages)
                client.change(app, text + '# next revision\n', 8)
                client.diag(app, 8)
                stale = client.request('workspace/executeCommand', actions[0]['command']) if actions else {}
                fresh = request(client, METHOD, params).get('result') or {}
                record('explicit-explanation-and-stale-action', {'oneDiscoverableAction': len(actions) == 1 and actions[0].get('kind') == KIND, 'explicitMessageOnly': before == 0 and after == 1, 'currentBufferExplained': explained.get('result', {}).get('inputIdentity', {}).get('version') == 7, 'staleActionRefused': stale.get('error', {}).get('data', {}).get('reasonCode') == 'STALE_INPUT', 'freshStatusNotOverwritten': fresh.get('inputIdentity', {}).get('version') == 8, 'noUnsolicitedFlood': sum(item['message'].get('method') == 'window/showMessage' for item in client.messages) == 1}, {'actions': listed, 'execute': explained, 'stale': stale, 'fresh': fresh})

                before_reopen = request(client, 'textDocument/codeAction', action_request).get('result', [])
                client.send('textDocument/didClose', {'textDocument': {'uri': app.as_uri()}})
                closed_status = client.request(METHOD, params)
                client.open(app, text + '# reopened with reused client version\n', 8)
                client.diag(app, 8)
                reopened = client.request('workspace/executeCommand', before_reopen[0]['command']) if before_reopen else {}
                record('reopened-input-identity', {'closedBufferNotMislabelled': closed_status.get('error', {}).get('data', {}).get('reasonCode') == 'INPUT_UNAVAILABLE', 'reusedClientVersionIsStale': reopened.get('error', {}).get('data', {}).get('reasonCode') == 'STALE_INPUT', 'noExtraNotification': sum(item['message'].get('method') == 'window/showMessage' for item in client.messages) == 1}, {'closed': closed_status, 'reopened': reopened})

                command = [str(binary), 'explain-config', '--root', str(root)]
                completed = subprocess.run(command, capture_output=True, timeout=10)
                (folder / 'cli.stdout').write_bytes(completed.stdout)
                (folder / 'cli.stderr').write_bytes(completed.stderr)
                disk = json.loads(completed.stdout)
                record('disk-configuration-provenance', {'exitZero': completed.returncode == 0, 'explicitDiskInput': disk.get('inputSource') == 'disk', 'notAnalysisProof': disk.get('coverage') == 'configuration-only', 'configIdentity': bool(disk.get('targets')) and bool(disk['targets'][0].get('configHash'))}, {'command': command, 'exit': completed.returncode, 'result': disk})
        except Exception as error:
            record(name + '-runner', {'completed': False}, repr(error))
        finally:
            client.close()
            if client.proc.returncode != 0: record(name + '-shutdown', {'cleanExit': False}, client.proc.returncode)
    folder = output / 'source-ref'
    root = folder / 'workspace'
    root.mkdir(parents=True)
    wire.write(root, '.argocd-ls.yaml', CONFIG)
    source = wire.APP.replace('  source: {repoURL: https://example.org/repo}\n', '  sources:\n    - {repoURL: "https://example.org/a", ref: values}\n    - {repoURL: "https://example.org/b", ref: values, helm: {valueFiles: ["$values/config.yaml"]}}\n')
    app = wire.write(root, 'app.yaml', source)
    client = Client(folder, [root])
    try:
        client.open(app, source)
        client.diag(app, 1)
        params = query(app, source, '$values')
        params['position']['character'] += 1
        status = request(client, METHOD, params).get('result') or {}
        denied = request(client, 'textDocument/rename', params | {'newName': 'settings'})
        record('source-ref-ambiguity', {'sourceGuardNotProjectGuard': availability(status, 'rename').get('reasonCode') == 'AMBIGUOUS_BINDING', 'sameErrorReason': denied.get('error', {}).get('data', {}).get('reasonCode') == 'AMBIGUOUS_BINDING', 'noEdit': 'result' not in denied}, {'status': status, 'rename': denied})
        repaired = source.replace('org/b", ref: values,', 'org/b", ref: chart,')
        broken = wire.write(root, 'broken.yaml', 'apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata: [\n')
        client.send('workspace/didChangeWatchedFiles', {'changes': [{'uri': broken.as_uri(), 'type': 1}]})
        client.change(app, repaired, 2)
        client.diag(app, 2)
        params = query(app, repaired, '$values')
        params['position']['character'] += 1
        status = request(client, METHOD, params).get('result') or {}
        record('local-rename-with-incomplete-scope', {'documentComplete': status.get('completeness', {}).get('document') is True, 'scopeIncomplete': status.get('completeness', {}).get('workspace') is False, 'localRenameStillAvailable': availability(status, 'rename').get('allowed') is True, 'referenceAbsenceUnproved': availability(status, 'references').get('reasonCode') == 'INDEX_INCOMPLETE'}, status)
    finally:
        client.close()

    folder = output / 'nested-roots'
    parent = folder / 'workspace'
    child = parent / 'child'
    child.mkdir(parents=True)
    wire.write(parent, '.argocd-ls.yaml', CONFIG)
    wire.write(parent, 'project.yaml', wire.PROJECT)
    wire.write(child, '.argocd-ls.yaml', CONFIG)
    app = wire.write(child, 'app.yaml', wire.APP)
    client = Client(folder, [parent, child])
    try:
        client.open(app, wire.APP)
        client.diag(app, 1)
        status = request(client, METHOD, query(app, wire.APP)).get('result') or {}
        record('nested-root-ownership', {'deepestRoot': status.get('root') == str(child), 'onlySuppliedRoots': status.get('roots') == [str(parent), str(child)], 'noBorrowedParentBinding': availability(status, 'rename').get('reasonCode') == 'BINDING_NOT_FOUND', 'childScopeKnown': status.get('completeness', {}).get('workspace') is True}, status)
    finally:
        client.close()
    # Projection removes dynamic values from the static graph. Their unknown
    # markers and default Project's implicit uses must not become proven zero.
    projected = '''apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata: {name: generated}
spec:
  goTemplate: true
  generators: [{list: {elements: [{name: one}]}}]
  template:
    metadata: {name: app}
    spec:
      project: team
      sources:
      - {repoURL: 'https://example.org/a', ref: values}
      - repoURL: https://example.org/chart
        helm:
          valueFiles: ['{{ .valueFile }}']
      destination: {server: x, namespace: default}
'''
    for name, selected_text, needle, expected in [('implicit-default-references', wire.PROJECT.replace('name: team', 'name: default'), 'default', 'BUILTIN_BINDING'), ('projected-dynamic-source-references', projected, 'values', 'DYNAMIC_REFERENCE')]:
        folder = output / name
        root = folder / 'workspace'
        root.mkdir(parents=True)
        wire.write(root, '.argocd-ls.yaml', CONFIG)
        if name == 'implicit-default-references':
            wire.write(root, 'app.yaml', wire.APP.replace('  project: team\n', ''))
        else:
            wire.write(root, 'project.yaml', wire.PROJECT)
        selected = wire.write(root, 'selected.yaml', selected_text)
        client = Client(folder, [root])
        try:
            client.open(selected, selected_text)
            client.diag(selected, 1)
            status = request(client, METHOD, query(selected, selected_text, needle)).get('result') or {}
            refs = availability(status, 'references')
            record(name, {'scopeAnalysisComplete': status.get('completeness', {}).get('workspace') is True, 'implicitOrDynamicUncertaintyExplained': refs.get('allowed') is False and refs.get('reasonCode') == expected, 'notAProvenZero': 'resultCount' not in refs}, status)
        finally:
            client.close()
    return all(row['passed'] for row in rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('binary', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.resolve().mkdir(parents=True, exist_ok=False)
    sys.exit(0 if run(args.binary.resolve(), args.output.resolve()) else 1)
