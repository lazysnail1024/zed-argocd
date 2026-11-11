#!/usr/bin/env python3
"""Independent stdio guards for private rename and foreground reservations."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

import audit_repair_e2e as wire


parser = argparse.ArgumentParser()
parser.add_argument('binary', type=Path)
parser.add_argument('output', type=Path)
parser.add_argument('--cases', default='rename,capacity,symbols')
args = parser.parse_args()
wire.BINARY, wire.OUT = args.binary.resolve(), args.output.resolve()
wire.OUT.mkdir(parents=True, exist_ok=True)
rows = []
SOURCE = '''apiVersion: argoproj.io/v1alpha1
kind: Application
metadata: {name: current}
spec:
  project: team
  sources:
    - repoURL: https://example.org/values
      ref: values
    - repoURL: https://example.org/chart
      path: chart
      ref: chart
      helm:
        valueFiles: ["$values/路径😀.yaml", '$values/other.yaml']
        fileParameters:
          - name: config
            path: "$values/config.json"
  destination: {server: x, namespace: default}
'''


def record(name, assertions, details=None):
    row = {'id': name, 'assertions': assertions, 'passed': bool(assertions) and all(assertions.values()), 'details': details or {}}
    rows.append(row)
    (wire.OUT / 'results.json').write_text(json.dumps({'cases': rows, 'passed': all(r['passed'] for r in rows)}, indent=2) + '\n')
    print(name, 'PASS' if row['passed'] else 'FAIL', flush=True)


def position(text, needle, advance=0):
    prefix = text[:text.index(needle) + advance]
    return {'line': prefix.count('\n'), 'character': len(prefix.rsplit('\n', 1)[-1].encode('utf-16-le')) // 2}


def offset(text, point):
    lines = text.splitlines(keepends=True)
    if point['line'] == len(lines) and point['character'] == 0:
        return len(text)
    prefix = lines[point['line']].encode('utf-16-le')[:point['character'] * 2].decode('utf-16-le')
    return sum(len(line) for line in lines[:point['line']]) + len(prefix)


def apply(text, changes):
    replacements = [(offset(text, e['range']['start']), offset(text, e['range']['end']), e['newText']) for e in changes]
    for start, end, value in sorted(replacements, reverse=True):
        text = text[:start] + value + text[end:]
    return text


def rename(client, file, text, name='settings', needle='ref: values', advance=5):
    return client.request('textDocument/rename', {'textDocument': {'uri': file.as_uri()}, 'position': position(text, needle, advance), 'newName': name})


def rename_guards():
    out, root = wire.setup('rename')
    wire.write(root, 'project.yaml', wire.PROJECT)
    file = wire.write(root, 'stream.yaml', SOURCE)
    client = wire.LSP(out, [root])
    try:
        # The selected token follows a non-BMP scalar on the same flow line;
        # document edits must use UTF-16 while the plan itself uses byte spans.
        text = ('# 当前 😀\n' + SOURCE + '---\n' + SOURCE.replace('name: current', 'name: sibling')).replace('\n', '\r\n')
        file.write_bytes(text.encode())
        client.open(file, text)
        client.diag(file, 1)
        response = rename(client, file, text, needle='$values/other.yaml', advance=1)
        changes = response.get('result', {}).get('documentChanges', [])
        edits = changes[0].get('edits', []) if len(changes) == 1 else []
        result = apply(text, edits) if edits else ''
        first, _, sibling = result.partition('---\r\n')
        expected_sibling = SOURCE.replace('name: current', 'name: sibling').replace('\n', '\r\n')
        record('all-source-uses-renamed', {'versionedSingleDocument': len(changes) == 1 and changes[0].get('textDocument') == {'uri': file.as_uri(), 'version': 1}, 'allFourOccurrences': len(edits) == 4 and first.count('$settings/') == 3 and '$values/' not in first, 'siblingDocumentUnchanged': sibling == expected_sibling, 'unicodeAndCRLFPreserved': '路径😀.yaml' in first and result.count('\r\n') == text.count('\r\n')}, {'response': response})

        invalid_neighbor = SOURCE.replace('name: current', 'name: sibling').replace('destination: {server: x, namespace: default}', 'destination: {server: x, name: duplicate-target, namespace: default}')
        multi = SOURCE + '---\n' + invalid_neighbor
        client.change(file, multi, 2)
        client.diag(file, 2)
        unchanged_errors = rename(client, file, multi)
        changes = unchanged_errors.get('result', {}).get('documentChanges', [])
        edited = apply(multi, changes[0].get('edits', [])) if len(changes) == 1 else ''
        record('unchanged-sibling-error-is-not-new', {'privateRenameStillAvailable': len(changes) == 1 and len(changes[0].get('edits', [])) == 4, 'existingInvalidSiblingPreserved': edited.partition('---\n')[2] == invalid_neighbor}, {'response': unchanged_errors})

        duplicate = SOURCE.replace('ref: chart', 'ref: values')
        client.change(file, duplicate, 3)
        client.diag(file, 3)
        prepared = client.request('textDocument/prepareRename', {'textDocument': {'uri': file.as_uri()}, 'position': position(duplicate, 'ref: values', 5)})
        denied = rename(client, file, duplicate)
        record('duplicate-source-definition-refused', {'noAmbiguousPreparation': prepared.get('result') is None, 'noAmbiguousEdit': 'error' in denied and 'result' not in denied}, {'prepare': prepared, 'rename': denied})

        capture = SOURCE.replace("'$values/other.yaml'", "'$captured/other.yaml'")
        client.change(file, capture, 4)
        client.diag(file, 4)
        denied = rename(client, file, capture, name='captured')
        record('unresolved-reference-capture-refused', {'noNewAccidentalBinding': 'error' in denied and 'result' not in denied}, {'response': denied})

        alias = SOURCE.replace('metadata: {name: current}', 'metadata: {name: current, annotations: {value: &shared values}}').replace('ref: values', 'ref: *shared')
        escaped = SOURCE.replace('"$values/路径😀.yaml"', '"$val\\u0075es/路径😀.yaml"')
        denied_responses = []
        for version, content in enumerate([alias, escaped], 5):
            client.change(file, content, version)
            client.diag(file, version)
            denied_responses.append(rename(client, file, content, needle='$values/other.yaml', advance=1))
        record('derived-source-occurrences-refused', {'sharedDefinitionRefused': 'error' in denied_responses[0], 'encodedSiblingUseRefused': 'error' in denied_responses[1]}, {'responses': denied_responses})
    finally:
        client.close()


def capacity_guards():
    out, root = wire.setup('capacity')
    wire.write(root, 'project.yaml', wire.PROJECT)
    text = wire.APP + '  info:\n' + ''.join('    - {name: item-%d, value: value-%d}\n' % (n, n) for n in range(1800))
    file = wire.write(root, 'app.yaml', text)
    client = wire.LSP(out, [root])
    try:
        client.open(file, text)
        client.diag(file, 1)
        params = {'textDocument': {'uri': file.as_uri()}, 'position': position(text, 'project: team')}
        identifiers = set(range(1000, 1128))
        for ident in sorted(identifiers):
            client.send('textDocument/hover', params, ident)
        seen = {}
        while len(seen) != len(identifiers):
            response = client.wait(lambda row: row.get('id') in identifiers, timeout=20)
            seen[response['id']] = response
        busy = sum(row.get('error', {}).get('code') == -32803 for row in seen.values())
        useful = sum('result' in row for row in seen.values())
        record('bounded-reader-admission', {'finiteCapacityRejectsBurst': busy > 0, 'someUsefulReplies': useful > 0, 'everyRequestTerminates': len(seen) == 128}, {'busy': busy, 'useful': useful, 'responses': len(seen)})

        cancelled = set(range(2000, 2008))
        for ident in sorted(cancelled):
            client.send('textDocument/hover', params, ident)
        for ident in sorted(cancelled):
            client.send('$/cancelRequest', {'id': ident})
        completed = {}
        while len(completed) != len(cancelled):
            response = client.wait(lambda row: row.get('id') in cancelled, timeout=12)
            completed[response['id']] = response
        healthy = {}
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            healthy = client.request('textDocument/hover', params)
            if healthy.get('result') is not None:
                break
            time.sleep(.02)
        counts = {ident: sum(row['message'].get('id') == ident for row in client.messages) for ident in cancelled}
        record('cancelled-worker-reservations-released', {'oneTerminalPerRequest': all(value == 1 for value in counts.values()), 'capacityRecovered': healthy.get('result') is not None}, {'terminalCounts': counts, 'healthyResponse': healthy})
    finally:
        client.close()


def symbol_guards():
    out, workspace = wire.setup('symbols')
    # Different root lengths make root admission order disagree with URI order.
    # A non-CRD document also makes YAML ordinal differ from retained CRD ordinal.
    roots = [workspace / 'a', workspace / 'zzzz']
    texts = {}
    expected = []
    for root, suffix in zip(roots, ['a', 'z']):
        wire.write(root, '.argocd-ls.yaml', wire.CONFIG)
        project = wire.PROJECT.replace('name: team', 'name: team-' + suffix)
        apps = [wire.APP.replace('name: app', 'name: app-' + suffix + '-' + str(n)).replace('project: team', 'project: team-' + suffix) for n in [2, 1]]
        text = '# 符号 😀\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: ignored}\n---\n' + '\n---\n'.join([apps[0], project, apps[1]])
        file = wire.write(root, 'stream.yaml', text)
        texts[file.as_uri()] = text
        expected.extend([(file.as_uri(), 'app-' + suffix + '-2'), (file.as_uri(), 'team-' + suffix), (file.as_uri(), 'app-' + suffix + '-1')])
    client = wire.LSP(out, list(reversed(roots)))
    try:
        for uri, text in texts.items():
            file = next(root / 'stream.yaml' for root in roots if (root / 'stream.yaml').as_uri() == uri)
            client.open(file, text)
            client.diag(file, 1)
        response = client.request('workspace/symbol', {'query': ''})
        symbols = response.get('result', [])
        order = [(item['location']['uri'], item['name']) for item in symbols]
        exact = all(texts[item['location']['uri']][offset(texts[item['location']['uri']], item['location']['range']['start']):offset(texts[item['location']['uri']], item['location']['range']['end'])] == item['name'] for item in symbols)
        record('global-symbol-order-and-origin', {'globalURIAndSourceOrder': order == expected, 'oneItemPerDeclaration': len(symbols) == 6 and len(set(order)) == 6, 'exactMultiDocumentPositions': bool(symbols) and exact}, {'response': response})

        file = roots[0] / 'stream.yaml'
        current = texts[file.as_uri()].replace('name: app-a-1', 'name: renamed-a-1')
        client.change(file, current, 2)
        client.diag(file, 2)
        fresh = client.request('workspace/symbol', {'query': 'renamed-a'})
        stale = client.request('workspace/symbol', {'query': 'app-a-1'})
        items = fresh.get('result', [])
        location = items[0]['location'] if len(items) == 1 else None
        record('symbol-buffer-revision-is-current', {'updatedNameReturned': len(items) == 1 and items[0]['name'] == 'renamed-a-1', 'withdrawnNameAbsent': stale.get('result') == [], 'updatedPositionExact': location is not None and current[offset(current, location['range']['start']):offset(current, location['range']['end'])] == 'renamed-a-1'}, {'fresh': fresh, 'stale': stale})
    finally:
        client.close()


try:
    if 'rename' in args.cases.split(','):
        rename_guards()
    if 'capacity' in args.cases.split(','):
        capacity_guards()
    if 'symbols' in args.cases.split(','):
        symbol_guards()
except Exception:
    record('runner-error', {'noException': False}, {'traceback': traceback.format_exc()})
report = {'cases': rows, 'passed': bool(rows) and all(row['passed'] for row in rows), 'binarySHA256': hashlib.sha256(wire.BINARY.read_bytes()).hexdigest(), 'testSHA256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'boundary': 'Native stdio with original scheduling and real YAML; no timing injection or environment override.'}
(wire.OUT / 'results.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
sys.exit(0 if report['passed'] else 1)
