#!/usr/bin/env python3
"""The same real enum repair must work with default auto and an exact release."""
import argparse
import json
import pathlib
import sys
import time
import audit_repair_e2e as wire
from schema_queries_e2e import offset, position

TEXT = ('apiVersion: argoproj.io/v1alpha1\nkind: ApplicationSet\nmetadata:\n'
        '  name: set\nspec:\n  generators: []\n  syncPolicy:\n'
        '    applicationsSync: wrong # keep\n  template:\n    metadata:\n'
        '      name: child\n    spec:\n      project: default\n'
        '      destination: {server: x, namespace: default}\n')
EXPECTED = {'create-only', 'create-update', 'create-delete', 'sync'}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('binary', type=pathlib.Path)
    parser.add_argument('output', type=pathlib.Path)
    args = parser.parse_args()
    wire.BINARY = args.binary.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for target in ['auto', '3.5.6']:
        folder = output / target
        workspace = folder / 'workspace'
        workspace.mkdir(parents=True)
        if target != 'auto':
            wire.write(workspace, '.argocd-ls.yaml', 'schemaVersion: 1\ntargetVersion: "3.5.6"\n')
        path = workspace / 'set.yaml'  # Unsaved open buffers must work, too.
        row = {'id': target + '-enum-repair', 'checks': {}, 'passed': False}
        rows.append(row)
        client = None
        try:
            client = wire.LSP(folder, [workspace])
            client.open(path, TEXT, 7)
            before = client.diag(path, 7)
            row['beforeDiagnostics'] = before
            start = TEXT.index('wrong')
            span = {'start': position(TEXT, start), 'end': position(TEXT, start + len('wrong'))}
            completion = client.request('textDocument/completion', {'textDocument': {'uri': path.as_uri()}, 'position': span['end']})
            row['completion'] = completion
            params = {'textDocument': {'uri': path.as_uri()}, 'range': span, 'context': {'diagnostics': before}}
            listing = client.request('textDocument/codeAction', params)
            deadline = time.monotonic() + 8
            while listing.get('error', {}).get('code') == -32801 and time.monotonic() < deadline:
                time.sleep(.02)
                listing = client.request('textDocument/codeAction', params)
            row['listing'] = listing
            actions = [action for action in listing.get('result', []) if action.get('data', {}).get('fixCode') == 'FIX_ENUM_VALUE']
            proposed = {json.loads(action['data']['newText']) for action in actions}
            row['checks'].update({
                'constraintObserved': any(item['code'] == 'SCHEMA_CONSTRAINT' for item in before),
                'sharedCompletionCandidates': {item['label'] for item in completion.get('result', [])} == EXPECTED,
                'fourEnumActions': len(actions) == 4 and proposed == EXPECTED,
                'listDoesNotApplyEdit': all('edit' not in action for action in actions),
            })
            selected = next((item for item in actions if item['data']['newText'] == '"sync"'), None)
            if selected is not None:
                response = client.request('codeAction/resolve', selected)
                row['resolved'] = response
                changes = response.get('result', {}).get('edit', {}).get('documentChanges', [])
                row['checks']['oneVersionedChange'] = len(changes) == 1 and changes[0]['textDocument'] == {'uri': path.as_uri(), 'version': 7}
                if row['checks']['oneVersionedChange']:
                    after = TEXT
                    edits = changes[0]['edits']
                    for edit in sorted(edits, key=lambda item: offset(TEXT, item['range']['start']), reverse=True):
                        a, b = offset(TEXT, edit['range']['start']), offset(TEXT, edit['range']['end'])
                        after = after[:a] + edit['newText'] + after[b:]
                    row['checks']['onlyExactScalarChanged'] = after == TEXT.replace('wrong', '"sync"')
                    client.change(path, after, 8)
                    diagnostics = client.diag(path, 8)
                    row['afterDiagnostics'] = diagnostics
                    row['checks']['constraintRemoved'] = not any(item['code'] == 'SCHEMA_CONSTRAINT' for item in diagnostics)
                    stale = client.request('codeAction/resolve', selected)
                    row['checks']['oldVersionRejected'] = stale.get('error', {}).get('code') == -32801
            else:
                row['checks']['oneVersionedChange'] = False
            row['passed'] = all(row['checks'].values())
        except Exception as exc:
            row['error'] = repr(exc)
        finally:
            if client:
                client.close()
                row['checks']['cleanExit'] = client.proc.returncode == 0
                row['passed'] = row['passed'] and row['checks']['cleanExit']
    report = {'command': sys.argv, 'binarySHA256': wire.hash_file(wire.BINARY),
              'runnerSHA256': wire.hash_file(pathlib.Path(__file__)), 'rows': rows,
              'passed': len(rows) == 2 and all(row['passed'] for row in rows)}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'failed': [row['id'] for row in rows if not row['passed']]}))
    return 0 if report['passed'] else 1

if __name__ == '__main__':
    sys.exit(main())
