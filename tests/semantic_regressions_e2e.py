#!/usr/bin/env python3
"""Native CLI/LSP regression controls for AppSet patches and reference recovery.

Every case records its input, stdout/stderr (or LSP frames), and assertions.
The same runner can be applied to a baseline or a deliberately mutated binary;
a process crash, missing response, or missing positive control is a failure.
"""

import argparse
import copy
import json
import pathlib
import platform
import subprocess
import sys
import time

import audit_repair_e2e as wire


BASE_APPSET = {
    'apiVersion': 'argoproj.io/v1alpha1',
    'kind': 'ApplicationSet',
    'metadata': {'name': 'set', 'namespace': 'argocd'},
    'spec': {
        'goTemplate': True,
        'generators': [{'list': {'elements': [{'cluster': 'local'}]}}],
        'template': {
            'metadata': {'name': 'child'},
            'spec': {
                'project': 'team',
                'source': {'repoURL': 'https://example.invalid/repo'},
                'destination': {
                    'server': 'https://kubernetes.default.svc',
                    'namespace': 'prod',
                },
            },
        },
    },
}
TARGETS = ['3.0.0', '3.5.6']


def position(text, needle):
    prefix = text[:text.index(needle) + len(needle)]
    return {
        'line': prefix.count('\n'),
        'character': len(prefix.rsplit('\n', 1)[-1].encode('utf-16-le')) // 2,
    }


def fixture(out, text, target='3.0.0'):
    root = out / 'workspace'
    root.mkdir(parents=True)
    wire.write(root, '.argocd-ls.yaml', wire.CONFIG.replace('3.0.0', target))
    wire.write(root, 'project.yaml', wire.PROJECT)
    return root, wire.write(root, 'app.yaml', text)


def appset_text(patch=None, conflict=False):
    value = copy.deepcopy(BASE_APPSET)
    if patch is not None:
        value['spec']['templatePatch'] = patch
    if conflict:
        value['spec']['template']['spec']['destination']['name'] = 'in-cluster'
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def check(observed, name, condition, detail=None):
    checks = observed.setdefault('checks', {})
    checks[name] = checks.get(name, True) and bool(condition)
    assert condition, detail if detail is not None else name


def cli(out, root, observed):
    command = [str(wire.BINARY), 'check', '--root', str(root), '--format', 'json']
    completed = subprocess.run(command, capture_output=True, timeout=20)
    (out / 'stdout').write_bytes(completed.stdout)
    (out / 'stderr').write_bytes(completed.stderr)
    (out / 'command.json').write_text(json.dumps({
        'command': command, 'exit': completed.returncode,
    }, indent=2) + '\n', encoding='utf-8')
    observed['lastCLI'] = {'command': command, 'exit': completed.returncode}
    check(observed, 'cli_no_panic', b'panic:' not in completed.stderr, 'CLI panicked; see stderr artifact')
    try:
        report = json.loads(completed.stdout)
    except (ValueError, UnicodeError):
        report = None
    check(observed, 'cli_returns_json', isinstance(report, dict), 'CLI did not return a JSON report')
    check(observed, 'analysis_complete', report.get('analysisComplete') is True, report)
    check(observed, 'resource_count', report.get('resourcesAnalyzed') == 2, report)
    return completed.returncode, report


def patch_case(patch, expected_code=None, conflict=False):
    def run(out, observed):
        for target in TARGETS:
            directory = out / target
            root, _ = fixture(directory, appset_text(patch, conflict), target)
            exit_code, report = cli(directory, root, observed)
            observed[target] = {'exit': exit_code, 'diagnostics': report['diagnostics']}
            errors = [item for item in report['diagnostics'] if item['severity'] == 1]
            check(observed, 'expected_exit', exit_code == (1 if expected_code else 0), observed[target])
            if expected_code:
                check(observed, 'expected_diagnostic', [item['code'] for item in errors] == [expected_code], errors)
            else:
                check(observed, 'expected_diagnostic', not errors, errors)
            check(observed, 'protected_project', not any('CD_PROJECT_UNRESOLVED' in item['message'] for item in report['diagnostics']))
    return run


def patch_delete_destination(out, observed):
    for name, patch, expected in [
        ('deleted', 'spec:\n  destination:\n    name: null\n', False),
        ('present', None, True),
    ]:
        directory = out / name
        root, _ = fixture(directory, appset_text(patch, conflict=True))
        exit_code, report = cli(directory, root, observed)
        observed[name] = {'exit': exit_code, 'diagnostics': report['diagnostics']}
        conflicts = [item for item in report['diagnostics'] if 'CD_DESTINATION_CONFLICT' in item['message']]
        check(observed, 'destination_conflict_' + name, bool(conflicts) == expected, report)
        check(observed, 'expected_exit_' + name, exit_code == (1 if expected else 0), report)


def invalid_base_template(value):
    def run(out, observed):
        for target in TARGETS:
            app = copy.deepcopy(BASE_APPSET)
            app['spec']['generators'][0]['list']['template'] = app['spec']['template']
            app['spec']['template'] = value
            directory = out / target
            root, _ = fixture(directory, json.dumps(app, indent=2) + '\n', target)
            exit_code, report = cli(directory, root, observed)
            observed[target] = {'exit': exit_code, 'diagnostics': report['diagnostics']}
            check(observed, 'invalid_template_exit', exit_code == 1, report)
            check(observed, 'invalid_template_diagnostic', any(item['code'] == 'SCHEMA_TYPE' and item['severity'] == 1 for item in report['diagnostics']), report)
    return run


def request(client, path, method, text, needle, **extra):
    response = client.request('textDocument/' + method, {
        'textDocument': {'uri': path.as_uri()},
        'position': position(text, needle),
        **extra,
    })
    assert 'error' not in response, response
    return response['result']


def patch_lifecycle(out, observed):
    root, path = fixture(out, appset_text())
    peer = wire.write(root, 'peer.yaml', wire.APP)
    client = wire.LSP(out, [root])
    client.open(path, path.read_text())
    check(observed, 'initial_appset_clean', client.diag(path, 1) == [])
    client.open(peer, wire.APP)
    check(observed, 'initial_peer_clean', client.diag(peer, 1) == [])
    observed['versions'] = []
    for version, patch, expected in [
        (2, 'spec: null\n', 'CD_APPSET_STATIC_FIELD'),
        (3, 'spec: 42\n', 'CD_APPSET_TEMPLATE_SYNTAX'),
        (4, 'spec: []\n', 'CD_APPSET_TEMPLATE_SYNTAX'),
        (5, None, None),
    ]:
        text = appset_text(patch)
        (out / f'buffer-{version}.yaml').write_text(text, encoding='utf-8')
        client.change(path, text, version)
        diagnostics = client.diag(path, version)
        check(observed, 'lsp_alive', client.proc.poll() is None, 'LSP exited while handling a templatePatch edit')
        errors = [item for item in diagnostics if item['severity'] == 1]
        check(observed, 'expected_diagnostic', [item['code'] for item in errors] == ([expected] if expected else []), diagnostics)
        definition = request(client, peer, 'definition', wire.APP, 'project: team')
        check(observed, 'peer_definition', len(definition) == 1 and definition[0]['uri'] == (root / 'project.yaml').as_uri(), definition)
        observed['versions'].append({'version': version, 'diagnostics': diagnostics, 'peerDefinition': definition})


def reference_case(text, expected, needle='project: team', project_name='team', check_actions=True):
    def run(out, observed):
        root, path = fixture(out, text)
        if project_name != 'team':
            wire.write(root, 'project.yaml', wire.PROJECT.replace('name: team', 'name: "' + project_name + '"'))
        wire.write(root, 'other-project.yaml', wire.PROJECT.replace('name: team', 'name: other'))
        client = wire.LSP(out, [root])
        client.open(path, text)
        observed['diagnostics'] = client.diag(path, 1)
        observed['definition'] = request(client, path, 'definition', text, needle)
        observed['completion'] = request(client, path, 'completion', text, needle)
        if expected:
            check(observed, 'definition_target', len(observed['definition']) == 1 and observed['definition'][0]['uri'] == (root / 'project.yaml').as_uri())
            check(observed, 'completion_candidates', [item['label'] for item in observed['completion']] == [project_name])
            check(observed, 'partial_detail', 'reliable prefix only' in observed['completion'][0]['detail'])
        else:
            check(observed, 'definition_is_empty', observed['definition'] == [])
            check(observed, 'completion_is_empty', observed['completion'] == [])
        if check_actions:
            point = position(text, needle)
            actions = client.request('textDocument/codeAction', {
                'textDocument': {'uri': path.as_uri()},
                'range': {'start': point, 'end': point},
                'context': {'diagnostics': []},
            })
            observed['actions'] = actions
            check(observed, 'no_speculative_actions', actions.get('result') == [], actions)
    return run


def unfinished_key(out, observed):
    text = wire.APP.replace('  source: {repoURL: https://example.org/repo}\n', '') + '  sou\n  syncPolicy: {\n'
    root, path = fixture(out, text)
    client = wire.LSP(out, [root])
    client.open(path, text)
    client.diag(path, 1)
    observed['definition'] = request(client, path, 'definition', text, 'project: team')
    observed['referenceCompletion'] = request(client, path, 'completion', text, 'project: team')
    observed['keyCompletion'] = request(client, path, 'completion', text, '  sou')
    check(observed, 'definition_target', len(observed['definition']) == 1)
    check(observed, 'reference_completion', [item['label'] for item in observed['referenceCompletion']] == ['team'])
    check(observed, 'key_completion', {'source', 'sources'} <= {item['label'] for item in observed['keyCompletion']})
    symbols = client.request('textDocument/documentSymbol', {'textDocument': {'uri': path.as_uri()}})
    observed['symbols'] = symbols
    check(observed, 'no_recovered_symbols', symbols.get('result') == [], symbols)


def utf16_current_buffer(out, observed):
    # The recovered use belongs to the second document and includes a non-BMP
    # character. An unsaved comment insertion must move the offered edit range.
    first = 'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: ignored}\ndata: {note: "\U0001f680"}\n---\n'
    name = '\U0001f680team'
    text = first + wire.APP.replace('project: team', 'project: "' + name + '"') + '  syncPolicy: {\n'
    root, path = fixture(out, text)
    wire.write(root, 'project.yaml', wire.PROJECT.replace('name: team', 'name: "' + name + '"'))
    client = wire.LSP(out, [root])
    client.open(path, text)
    client.diag(path, 1)
    current = '# unsaved current-buffer line\n' + text
    client.change(path, current, 2)
    client.diag(path, 2)
    needle = 'project: "' + name
    observed['definition'] = request(client, path, 'definition', current, needle)
    observed['completion'] = request(client, path, 'completion', current, needle)
    check(observed, 'definition_target', len(observed['definition']) == 1)
    check(observed, 'completion_candidates', [item['label'] for item in observed['completion']] == [name])
    edit = observed['completion'][0]['textEdit']
    start = position(current, 'project: "')
    end = position(current, needle)
    check(observed, 'current_buffer_range', edit['range'] == {'start': start, 'end': end}, edit)
    check(observed, 'completion_text', edit['newText'] == name, edit)
    check(observed, 'utf16_range', end['character'] - start['character'] == 6, edit)


def source_ref_control(out, observed):
    text = wire.APP.replace('  source: {repoURL: https://example.org/repo}', '''  sources:
    - {repoURL: values, ref: values}
    - repoURL: chart
      helm:
        valueFiles: ["$values/config.yaml"]''') + '  syncPolicy: {\n'
    root, path = fixture(out, text)
    client = wire.LSP(out, [root])
    client.open(path, text)
    client.diag(path, 1)
    observed['completion'] = request(client, path, 'completion', text, 'valueFiles: ["$values')
    check(observed, 'complete_sequence_candidates', [item['label'] for item in observed['completion']] == ['values'])
    # A later declaration in this same source sequence must not be silently
    # omitted when deciding whether its reference population is complete.
    changed = text.replace('  destination:', '    - {repoURL: second, ref: values}\n  destination:')
    client.change(path, changed, 2)
    client.diag(path, 2)
    observed['laterDeclaration'] = request(client, path, 'completion', changed, 'valueFiles: ["$values')
    check(observed, 'incomplete_sequence_rejected', observed['laterDeclaration'] == [])


def appset_project_control(out, observed):
    text = '''apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata: {name: set}
spec:
  generators: []
  template:
    metadata: {name: app}
    spec:
      project: team
      source: {repoURL: x}
      destination: {server: https://kubernetes.default.svc, namespace: prod}
      syncPolicy: {
'''
    root, path = fixture(out, text)
    client = wire.LSP(out, [root])
    client.open(path, text)
    client.diag(path, 1)
    observed['completion'] = request(client, path, 'completion', text, 'project: team')
    check(observed, 'template_project_completion', [item['label'] for item in observed['completion']] == ['team'])
    changed = text.replace('      syncPolicy:', '      project: other\n      syncPolicy:')
    client.change(path, changed, 2)
    client.diag(path, 2)
    observed['ambiguousCompletion'] = request(client, path, 'completion', changed, 'project: team')
    check(observed, 'duplicate_template_project_rejected', observed['ambiguousCompletion'] == [])


def cases():
    patch = [
        ('patch-spec-null', patch_case('spec: null\n', 'CD_APPSET_STATIC_FIELD')),
        ('patch-spec-empty', patch_case('spec:\n', 'CD_APPSET_STATIC_FIELD')),
        ('patch-spec-null-tilde', patch_case('spec: ~\n', 'CD_APPSET_STATIC_FIELD')),
        ('patch-spec-number', patch_case('spec: 42\n', 'CD_APPSET_TEMPLATE_SYNTAX')),
        ('patch-spec-string', patch_case('spec: wrong\n', 'CD_APPSET_TEMPLATE_SYNTAX')),
        ('patch-spec-boolean', patch_case('spec: false\n', 'CD_APPSET_TEMPLATE_SYNTAX')),
        ('patch-spec-empty-array', patch_case('spec: []\n', 'CD_APPSET_TEMPLATE_SYNTAX')),
        ('patch-spec-array', patch_case('spec: [1]\n', 'CD_APPSET_TEMPLATE_SYNTAX')),
        ('patch-spec-object-array', patch_case('spec: [{project: other}]\n', 'CD_APPSET_TEMPLATE_SYNTAX')),
        ('patch-empty-object', patch_case('spec: {}\n')),
        ('patch-missing-spec', patch_case('metadata: {name: patched}\n')),
        ('patch-project-protected', patch_case('spec:\n  project: missing\n')),
        ('patch-destination-delete-control', patch_delete_destination),
        ('patch-base-template-null', invalid_base_template(None)),
        ('patch-base-template-scalar', invalid_base_template(42)),
        ('patch-base-template-array', invalid_base_template([])),
        ('patch-lsp-lifecycle', patch_lifecycle),
    ]
    damaged = '  syncPolicy: {\n'
    metadata_last = wire.APP.replace('metadata:\n  name: app\n', '')
    continued = wire.APP.replace('  project: team\n', '  project: team\n    extra\n')
    flow = 'apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: app}\nspec: {project: team, source: {repoURL: x}}\nbroken: {\n'
    recovery = [
        ('recovery-duplicate-complete-control', reference_case(wire.APP + '  project: other\n', False)),
        ('recovery-duplicate-project', reference_case(wire.APP + '  project: other\n' + damaged, False)),
        ('recovery-duplicate-project-before', reference_case(wire.APP.replace('  project: team', '  project: other\n  project: team') + damaged, False)),
        ('recovery-quoted-duplicate-project', reference_case(wire.APP + '  "project": other\n' + damaged, False)),
        ('recovery-escaped-duplicate-project', reference_case(wire.APP + '  "pro\\u006aect": other\n' + damaged, False)),
        ('recovery-duplicate-spec', reference_case(wire.APP + 'spec: {project: other}\nbroken: {\n', False)),
        ('recovery-duplicate-kind', reference_case(wire.APP + 'kind: Workflow\nbroken: {\n', False)),
        ('recovery-duplicate-api-version', reference_case(wire.APP + '"apiVersion": v1\nbroken: {\n', False)),
        ('recovery-duplicate-metadata-name', reference_case(metadata_last + 'metadata:\n  name: one\n  name: two\nbroken: {\n', False)),
        ('recovery-duplicate-metadata-namespace', reference_case(metadata_last + 'metadata: {name: app, namespace: one, "namespace": two}\nbroken: {\n', False)),
        ('recovery-metadata-after-use-control', reference_case(metadata_last + 'metadata: {name: app, namespace: argocd}\nbroken: {\n', True)),
        ('recovery-merge-after-use', reference_case(wire.APP + '  <<: {project: other}\n' + damaged, False)),
        ('recovery-quoted-merge-after-use', reference_case(wire.APP + '  "<<": {project: other}\n' + damaged, False)),
        ('recovery-alias-shared-after-use', reference_case(wire.APP.replace('project: team', 'project: &project team') + '  annotation: *project\n' + damaged, False, needle='&project team')),
        ('recovery-flow-ancestry-boundary', reference_case(flow, False)),
        ('recovery-multiline-complete-control', reference_case(continued, False, check_actions=False)),
        ('recovery-multiline-scalar', reference_case(continued + damaged, False)),
        ('recovery-same-indent-continuation', reference_case(wire.APP.replace('  project: team\n', '  project: team\n  extra\n') + damaged, False)),
        ('recovery-quoted-project-control', reference_case(wire.APP.replace('  project: team\n', '  project: "team"\n    # comment following a complete value\n') + damaged, True, needle='project: "team')),
        ('recovery-positive-control', reference_case(wire.APP + damaged, True)),
        ('recovery-unfinished-key-control', unfinished_key),
        ('recovery-utf16-current-buffer', utf16_current_buffer),
        ('recovery-source-ref-control', source_ref_control),
        ('recovery-appset-project-control', appset_project_control),
    ]
    return {'patch': patch, 'recovery': recovery}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=pathlib.Path)
    parser.add_argument('output', type=pathlib.Path)
    parser.add_argument('--case', choices=['all', 'patch', 'recovery'], default='all')
    args = parser.parse_args()
    wire.BINARY = args.binary.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    selected = cases()
    groups = selected.values() if args.case == 'all' else [selected[args.case]]
    for name, scenario in [case for group in groups for case in group]:
        out = output / name
        out.mkdir()
        observed, error, error_type = {}, None, None
        first_client = len(wire.CLIENTS)
        started = time.monotonic()
        try:
            scenario(out, observed)
        except Exception as exception:
            error = repr(exception)
            error_type = type(exception).__name__
        finally:
            for client in wire.CLIENTS[first_client:]:
                client.close()
                if client.proc.returncode != 0 and error is None:
                    error = f'LSP exited with {client.proc.returncode}'
                    error_type = 'ProcessExitError'
        row = {
            'id': name, 'passed': error is None, 'error': error, 'errorType': error_type,
            'checks': observed.pop('checks', {}),
            'observed': observed, 'seconds': time.monotonic() - started,
        }
        rows.append(row)
        (out / 'result.json').write_text(json.dumps(row, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'id': name, 'passed': row['passed'], 'error': error[:300] if error else None}), flush=True)
    report = {
        'command': sys.argv, 'binarySHA256': wire.hash_file(wire.BINARY),
        'runnerSHA256': wire.hash_file(pathlib.Path(__file__)),
        'python': sys.version, 'platform': platform.platform(), 'cases': rows,
        'passed': bool(rows) and all(row['passed'] for row in rows),
        'filesSHA256': {
            str(path.relative_to(output)): wire.hash_file(path)
            for path in output.rglob('*') if path.is_file()
        },
    }
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if rows and all(row['passed'] for row in rows) else 1


if __name__ == '__main__':
    sys.exit(main())
