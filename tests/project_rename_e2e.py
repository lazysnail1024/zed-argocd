#!/usr/bin/env python3
"""Real multi-file Project rename, current buffers, and binding refusal boundaries."""
import argparse
import json
import pathlib
import sys
import time
import audit_repair_e2e as wire
from schema_queries_e2e import position, offset

CONFIG = 'schemaVersion: 1\ntargetVersion: "3.5.6"\ncontrolPlaneNamespace: argocd\nreferencePolicy: closed\n'
PROJECT = wire.PROJECT
APP = wire.APP
SET = ('apiVersion: argoproj.io/v1alpha1\nkind: ApplicationSet\nmetadata: {name: set}\n'
       'spec:\n  generators: []\n  template:\n    metadata: {name: child}\n'
       '    spec:\n      project: team\n      source: {repoURL: https://example.org/repo}\n'
       '      destination: {server: https://kubernetes.default.svc, namespace: default}\n')
GENERATOR = SET.replace('  generators: []\n',
    '  generators:\n    - list:\n        elements: [{name: child}]\n'
    '        template:\n          spec:\n            project: team\n')
# Only the outer template is overridden; the first generator project is the binding.
GENERATOR = GENERATOR.replace('\n    spec:\n      project: team\n', '\n    spec:\n      project: ignored-base\n')
CASES = ['three-file-dirty', 'from-application', 'appset-generator', 'duplicate', 'alias',
         'dynamic', 'new-name-declared', 'new-name-unresolved-reference', 'namespace-boundary',
         'scope-boundary', 'closed-reference', 'invalid-name', 'open-policy', 'default-project', 'new-default-name']
NEGATIVE = {'duplicate', 'alias', 'dynamic', 'new-name-declared', 'new-name-unresolved-reference',
            'closed-reference', 'invalid-name', 'open-policy', 'default-project', 'new-default-name'}


def request(client, method, params):
    response = client.request(method, params)
    deadline = time.monotonic() + 3
    while response.get('error', {}).get('code') == -32801 and time.monotonic() < deadline:
        time.sleep(.02)
        response = client.request(method, params)
    return response


def scenario(name, folder):
    workspace = folder / 'workspace'
    workspace.mkdir(parents=True)
    prefix = 'left/' if name == 'scope-boundary' else ''
    files = {prefix + 'project.yaml': PROJECT, prefix + 'app.yaml': APP, prefix + 'set.yaml': SET}
    buffers = dict(files)
    config = CONFIG
    selected = prefix + 'project.yaml'
    needle = 'name: team'
    token = 'team'
    new_name = 'platform'
    if name == 'three-file-dirty':
        files[prefix + 'app.yaml'] = APP.replace('project: team', 'project: disk-only')
        buffers[prefix + 'app.yaml'] += '# unsaved editor content\n'
    if name == 'from-application':
        selected, needle = prefix + 'app.yaml', 'project: team'
    if name == 'appset-generator':
        buffers[prefix + 'set.yaml'] = files[prefix + 'set.yaml'] = GENERATOR
        selected, needle = prefix + 'set.yaml', 'project: team'
    if name == 'duplicate':
        # Both declarations are editable so this case specifically exercises
        # declaration uniqueness, independently of the closed-buffer guard.
        buffers['duplicate.yaml'] = files['duplicate.yaml'] = PROJECT
    if name == 'alias':
        buffers[prefix + 'app.yaml'] = files[prefix + 'app.yaml'] = APP.replace('  name: app\n', '  name: app\n  annotations: {project: &binding team}\n').replace('project: team\n', 'project: *binding\n')
    if name == 'dynamic':
        buffers[prefix + 'set.yaml'] = files[prefix + 'set.yaml'] = SET.replace('project: team', 'project: "{{project}}"')
    if name == 'new-name-declared':
        files['existing.yaml'] = PROJECT.replace('name: team', 'name: platform')
    if name == 'new-name-unresolved-reference':
        files['orphan.yaml'] = APP.replace('name: app', 'name: orphan').replace('project: team', 'project: platform')
    if name == 'namespace-boundary':
        files['other-namespace.yaml'] = PROJECT.replace('namespace: argocd', 'namespace: unrelated')
    if name == 'scope-boundary':
        config += 'scopes:\n  - name: left\n    include: ["left/*.yaml"]\n  - name: right\n    include: ["right/*.yaml"]\n'
        files['right/project.yaml'] = PROJECT
        files['right/app.yaml'] = APP
    if name == 'invalid-name': new_name = 'bad name'
    if name == 'open-policy': config = config.replace('referencePolicy: closed', 'referencePolicy: open')
    if name == 'default-project':
        for path in buffers:
            buffers[path] = files[path] = files[path].replace('name: team', 'name: default').replace('project: team', 'project: default')
        needle, token = 'name: default', 'default'
    if name == 'new-default-name': new_name = 'default'
    for path, text in files.items(): wire.write(workspace, path, text)
    wire.write(workspace, '.argocd-ls.yaml', config)
    row = {'id': name, 'checks': {}, 'passed': False}
    client = None
    try:
        client = wire.LSP(folder, [workspace])
        versions = {}
        for i, (path, text) in enumerate(buffers.items()):
            if name == 'closed-reference' and path.endswith('set.yaml'): continue
            versions[path] = 7 + i * 2
            client.open(workspace / path, text, versions[path])
            client.diag(workspace / path, versions[path])
        selected_text = buffers[selected]
        start = selected_text.index(needle) + len(needle) - len(token)
        params = {'textDocument': {'uri': (workspace / selected).as_uri()}, 'position': position(selected_text, start)}
        prepared = request(client, 'textDocument/prepareRename', params)
        renamed = request(client, 'textDocument/rename', params | {'newName': new_name})
        row.update(prepared=prepared, renamed=renamed)
        changes = (renamed.get('result') or {}).get('documentChanges', [])
        if name in NEGATIVE:
            row['checks']['refusedWithoutEdits'] = 'error' in renamed and not changes
        else:
            row['checks']['preparedExactBinding'] = prepared.get('result', {}).get('placeholder') == token
            expected_uris = {(workspace / path).as_uri() for path in buffers}
            actual_uris = [change.get('textDocument', {}).get('uri') for change in changes]
            row['checks']['allThreeFilesExactlyOnce'] = len(changes) == 3 and set(actual_uris) == expected_uris
            row['checks']['eachEditHasCurrentVersion'] = all(change.get('textDocument') == {'uri': (workspace / path).as_uri(), 'version': version} for change in changes for path, version in versions.items() if change.get('textDocument', {}).get('uri') == (workspace / path).as_uri())
            applied = {}
            for change in changes:
                path = next(path for path in buffers if (workspace / path).as_uri() == change['textDocument']['uri'])
                text = buffers[path]
                result = text
                for edit in sorted(change['edits'], key=lambda item: offset(text, item['range']['start']), reverse=True):
                    a, b = offset(text, edit['range']['start']), offset(text, edit['range']['end'])
                    result = result[:a] + edit['newText'] + result[b:]
                expected = text.replace('name: team', 'name: "platform"') if path.endswith('project.yaml') else text.replace('project: team', 'project: "platform"')
                applied[path] = result
                row['checks']['exactBytes:' + path] = result == expected
            if len(applied) == 3:
                for path, text in applied.items():
                    versions[path] += 1
                    client.change(workspace / path, text, versions[path])
                last = list(applied)[-1]
                diagnostics = client.diag(workspace / last, versions[last])
                row['checks']['appliedScopeHasNoBrokenProjectReference'] = not any(d['code'] in ['CD_PROJECT_UNRESOLVED', 'CD_APPSET_STATIC_FIELD'] and d.get('severity') == 1 for d in diagnostics)
                text = applied[prefix + 'app.yaml']
                at = text.index('project: "platform"') + len('project: "')
                definition = request(client, 'textDocument/definition', {'textDocument': {'uri': (workspace / (prefix + 'app.yaml')).as_uri()}, 'position': position(text, at)})
                row['definitionAfter'] = definition
                row['checks']['newBindingNavigatesToRenamedProject'] = len(definition.get('result', [])) == 1 and definition['result'][0]['uri'] == (workspace / (prefix + 'project.yaml')).as_uri()
        row['checks']['diskInputsUnchanged'] = all((workspace / path).read_text() == text for path, text in files.items())
        row['passed'] = bool(row['checks']) and all(row['checks'].values())
    except Exception as exc:
        row['error'] = repr(exc)
    finally:
        if client:
            client.close()
            row['checks']['cleanExit'] = client.proc.returncode == 0
            row['passed'] = row['passed'] and row['checks']['cleanExit']
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('binary', type=pathlib.Path)
    parser.add_argument('output', type=pathlib.Path)
    parser.add_argument('--case', choices=CASES)
    args = parser.parse_args()
    wire.BINARY = args.binary.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for case in [args.case] if args.case else CASES:
        row = scenario(case, output / case)
        rows.append(row)
        print(case, 'PASS' if row['passed'] else 'FAIL', flush=True)
        (output / 'report.json').write_text(json.dumps({'rows': rows, 'passed': all(item['passed'] for item in rows), 'binarySHA256': wire.hash_file(wire.BINARY), 'command': sys.argv}, indent=2) + '\n')
    return 0 if all(row['passed'] for row in rows) else 1

if __name__ == '__main__': sys.exit(main())
