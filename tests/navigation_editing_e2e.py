#!/usr/bin/env python3
"""Real stdio: private source rename, workspace symbols and bounded requests."""
import argparse
import hashlib
import json
import pathlib
import sys
import time
import traceback

import audit_repair_e2e as client

parser = argparse.ArgumentParser()
parser.add_argument('binary', type=pathlib.Path)
parser.add_argument('output', type=pathlib.Path)
args = parser.parse_args()
client.BINARY = args.binary.resolve()
client.OUT = args.output.resolve()
client.OUT.mkdir(parents=True, exist_ok=False)
rows = []

SOURCE = '''# 当前 😀
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: source-app
spec:
  project: team
  sources:
    - repoURL: https://example.org/values
      ref: values
    - repoURL: https://example.org/chart
      path: chart
      ref: charts
      helm:
        valueFiles: ["$values/values.yaml", '$values/路径😀.yaml']
        fileParameters:
          - name: config
            path: "$values/config.json"
  destination: {server: x, namespace: default}
'''


def position(text, needle, delta=0):
    before = text[:text.index(needle)+delta]
    return {'line': before.count('\n'), 'character': len(before.rsplit('\n', 1)[-1].encode('utf-16-le'))//2}


def offset(text, pos):
    lines = text.splitlines(keepends=True)
    line = lines[pos['line']]
    prefix = line.encode('utf-16-le')[:pos['character']*2].decode('utf-16-le')
    return sum(len(part) for part in lines[:pos['line']]) + len(prefix)


def apply(text, edits):
    changes = [(offset(text, e['range']['start']), offset(text, e['range']['end']), e['newText']) for e in edits]
    for start, end, replacement in sorted(changes, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def request(c, method, file, text, needle, new_name=None):
    delta = 5 if needle.startswith('ref: ') else (1 if needle.startswith('$') else 0)
    params = {'textDocument': {'uri': file.as_uri()}, 'position': position(text, needle, delta)}
    if new_name is not None:
        params['newName'] = new_name
    return c.request(method, params)


def navigation():
    out, root = client.setup('navigation')
    client.write(root, 'project.yaml', client.PROJECT)
    neighbor = SOURCE.replace('source-app', 'neighbor-app').replace('values', 'neighbor')
    text = (SOURCE+'---\n'+neighbor).replace('\n', '\r\n')
    file = client.write(root, 'app.yaml', '')
    file.write_bytes(text.encode())
    c = client.LSP(out, [root])
    try:
        capabilities = next(x['message']['result']['capabilities'] for x in c.messages if x['message'].get('id') == 1)
        assert capabilities['renameProvider']['prepareProvider'] and capabilities['workspaceSymbolProvider']
        c.open(file, text)
        c.diag(file, 1)
        prepared = request(c, 'textDocument/prepareRename', file, text, 'ref: values')['result']
        assert prepared['placeholder'] == 'values'
        assert text[offset(text, prepared['range']['start']):offset(text, prepared['range']['end'])] == 'values'
        no_name = request(c, 'textDocument/prepareRename', file, text, 'config.json')
        assert no_name.get('result') is None
        conflict = request(c, 'textDocument/rename', file, text, 'ref: values', 'charts')
        invalid = request(c, 'textDocument/rename', file, text, 'ref: values', 'bad name')
        assert conflict.get('error') and invalid.get('error')
        result = request(c, 'textDocument/rename', file, text, 'ref: values', 'settings')
        change = result['result']['documentChanges'][0]
        assert change['textDocument'] == {'uri': file.as_uri(), 'version': 1}
        assert len(change['edits']) == 4
        updated = apply(text, change['edits'])
        first, second = updated.split('---\r\n')
        assert '$values/' not in first and first.count('$settings/') == 3
        assert second == neighbor.replace('\n', '\r\n')
        c.change(file, updated, 2)
        diagnostics = c.diag(file, 2)
        assert not any(d['severity'] == 1 for d in diagnostics), diagnostics
        refs = c.request('textDocument/references', {'textDocument': {'uri': file.as_uri()}, 'position': position(updated, 'settings"'), 'context': {'includeDeclaration': False}})
        assert len(refs['result']) == 3
        symbols = c.request('workspace/symbol', {'query': ''})['result']
        names = sorted(item['name'] for item in symbols)
        assert names == ['neighbor-app', 'source-app', 'team'], symbols
        assert sum(item['name'] == 'team' for item in symbols) == 1
        assert c.request('workspace/symbol', {'query': 'neighbor'})['result'][0]['location']['uri'] == file.as_uri()
        rows.append({'id': 'versioned-local-rename-and-symbols', 'passed': True, 'edits': 4, 'references': 3, 'symbols': names})

        for version, bad in enumerate([
            SOURCE.replace('ref: charts', 'ref: values'),
            SOURCE.replace('ref: values', 'ref: &shared values').replace('name: source-app', 'name: source-app\n  annotations: {copy: *shared}'),
        ], 3):
            # Anchors are document-local; define before alias in this fixture.
            if '*shared' in bad:
                bad = SOURCE.replace('name: source-app', 'name: source-app\n  annotations: {copy: &shared values}').replace('ref: values', 'ref: *shared')
            c.change(file, bad, version)
            c.diag(file, version)
            denied = request(c, 'textDocument/rename', file, bad, '$values/', 'settings')
            assert denied.get('error') and 'result' not in denied
        rows.append({'id': 'ambiguous-and-derived-rename-refused', 'passed': True})
    finally:
        c.close()


def bounded_requests():
    out, root = client.setup('bounded-requests')
    client.write(root, 'project.yaml', client.PROJECT)
    # Input analysis is deliberately nontrivial, while each request uses the
    # production entry point and default concurrency limits.
    text = client.APP + '  info:\n' + ''.join(f'    - {{name: item-{n}, value: value-{n}}}\n' for n in range(1800))
    file = client.write(root, 'app.yaml', text)
    c = client.LSP(out, [root])
    try:
        c.open(file, text)
        c.diag(file, 1)
        ids = list(range(10000, 10128))
        started = time.monotonic()
        for ident in ids:
            c.send('textDocument/hover', {'textDocument': {'uri': file.as_uri()}, 'position': position(text, 'project: team')}, ident)
        received = {}
        while len(received) < len(ids):
            answer = c.wait(lambda m: m.get('id') in ids, timeout=20)
            received[answer['id']] = answer
        busy = sum(r.get('error', {}).get('code') == -32803 for r in received.values())
        useful = sum('result' in r for r in received.values())
        assert busy > 0 and useful > 0 and busy+useful == len(ids), received
        healthy = c.request('textDocument/hover', {'textDocument': {'uri': file.as_uri()}, 'position': position(text, 'project: team')})
        assert healthy.get('result') is not None
        rows.append({'id': 'bounded-inflight-and-recovery', 'passed': True, 'busy': busy, 'useful': useful, 'requests': len(ids), 'seconds': time.monotonic()-started})
    finally:
        c.close()


try:
    navigation()
    bounded_requests()
except Exception as exc:
    rows.append({'id': 'failure', 'passed': False, 'error': repr(exc), 'traceback': traceback.format_exc()})
report = {'binarySHA256': hashlib.sha256(client.BINARY.read_bytes()).hexdigest(), 'testSHA256': hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(), 'cases': rows, 'passed': len(rows) == 3 and all(row['passed'] for row in rows)}
(client.OUT/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
print(json.dumps(report, ensure_ascii=False))
sys.exit(0 if report['passed'] else 1)
