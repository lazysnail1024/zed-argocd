#!/usr/bin/env python3
"""Real stdio symbol consumers: sparse matches, cancellation, and live edits.

Many declarations deliberately share a single YAML file. Result limits must not
be mistaken for work limits, and background commits must not suppress current
file Schema completion. Trace mode observes progress only; it changes no budget.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

import audit_repair_e2e as wire
from persistent_workspace_e2e import Client, Traces, wait_ready, wait_until


parser = argparse.ArgumentParser()
parser.add_argument('binary', type=Path)
parser.add_argument('output', type=Path)
parser.add_argument('--count', type=int, default=1000, help='Application documents per file')
parser.add_argument('--files', type=int, default=2)
parser.add_argument('--max-query-seconds', type=float, default=2.0)
parser.add_argument('--max-local-seconds', type=float, default=2.0)
args = parser.parse_args()
if not 1 <= args.count <= 2500 or not 1 <= args.files <= 20:
    parser.error('count must be 1..2500 and files 1..20')
wire.BINARY, wire.OUT = args.binary.resolve(), args.output.resolve()
wire.OUT.mkdir(parents=True, exist_ok=True)
rows = []


def record(name, checks, details=None):
    row = {'id': name, 'checks': checks, 'passed': bool(checks) and all(checks.values()), 'details': details or {}}
    rows.append(row)
    print(name, 'PASS' if row['passed'] else 'FAIL', flush=True)


def position(text, needle):
    prefix = text[:text.index(needle) + len(needle)]
    return {'line': prefix.count('\n'), 'character': len(prefix.rsplit('\n', 1)[-1].encode('utf-16-le')) // 2}


def completion_items(response):
    result = response.get('result', [])
    return result.get('items', []) if isinstance(result, dict) else result


def responses(client, identifiers, mark, timeout):
    deadline = time.monotonic() + timeout
    while True:
        found = {row['message']['id']: row for row in client.messages[mark:] if row['message'].get('id') in identifiers}
        if len(found) == len(identifiers):
            return found
        if client.proc.poll() is not None or time.monotonic() >= deadline:
            raise TimeoutError('missing responses: ' + str(identifiers - found.keys()))
        time.sleep(.005)


def settle(client, query, expected):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        answer = client.request('workspace/symbol', {'query': query})
        if [item['name'] for item in answer.get('result', [])] == expected:
            return answer
        time.sleep(.02)
    raise TimeoutError('the real background index did not expose the last declaration')


def signatures(items):
    return [(item['location']['uri'], item['name']) for item in items]


client = None
try:
    out, root = wire.setup('symbols')
    texts = {}
    expected = []
    project = wire.write(root, '000-project.yaml', wire.PROJECT)
    texts[project.as_uri()] = wire.PROJECT
    expected.append((project.as_uri(), 'team'))
    focus_text = wire.APP.replace('name: app', 'name: focus-before').replace('  project: team\n', '  project: team\n  syncPol: {}\n')
    focus = wire.write(root, '001-focus.yaml', focus_text)
    texts[focus.as_uri()] = focus_text
    expected.append((focus.as_uri(), 'focus-before'))
    churn = wire.write(root, '002-churn.yaml', wire.APP.replace('name: app', 'name: churn'))
    texts[churn.as_uri()] = churn.read_text()
    expected.append((churn.as_uri(), 'churn'))
    for number in range(args.files):
        names = [f'symbol-{number:03d}-{item:05d}' for item in range(args.count)]
        text = 'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: ignored}\n---\n' + '\n---\n'.join(wire.APP.replace('name: app', 'name: ' + name) for name in names)
        path = wire.write(root, f'bulk-{number:03d}.yaml', text)
        texts[path.as_uri()] = text
        expected.extend((path.as_uri(), name) for name in names)
    last_name = names[-1]
    old_trace = os.environ.get('ARGOCD_LS_TRACE')
    os.environ['ARGOCD_LS_TRACE'] = '1'
    try:
        client = Client(out, [root])
    finally:
        if old_trace is None:
            os.environ.pop('ARGOCD_LS_TRACE', None)
        else:
            os.environ['ARGOCD_LS_TRACE'] = old_trace
    traces = Traces(client)
    client.open(focus, focus_text)

    def started_index():
        for row in traces.since(0):
            if row['stats'].get('knownFiles', 0) > 0:
                return row
        return None

    wait_until(client, started_index, 15, 'background index startup')

    # Keep the same scope changing while asking for a local Schema field. A
    # failed optional Project scan must not turn this into ContentModified.
    local = []
    progress_mark = traces.mark()
    progress_deadline = time.monotonic() + 15
    while time.monotonic() < progress_deadline and (len(local) < 16 or not any(row['stats'].get('diskReads', 0) > 0 for row in traces.since(progress_mark))):
        churn.write_text(texts[churn.as_uri()] + f'# change {len(local)}\n', encoding='utf-8')
        client.send('workspace/didChangeWatchedFiles', {'changes': [{'uri': churn.as_uri(), 'type': 2}]})
        started = time.monotonic()
        response = client.request('textDocument/completion', {'textDocument': {'uri': focus.as_uri()}, 'position': position(focus_text, 'syncPol')})
        local.append({'seconds': time.monotonic() - started, 'error': response.get('error'), 'labels': [item['label'] for item in completion_items(response)]})
        # Give the real 120 ms debounce time to dispatch accepted disk changes;
        # an unbroken event flood would only prove pre-index document fallback.
        # A large-file batch may outlive 16 requests: observe a completed batch,
        # not a fixed number of sleeps. Per-request latency bounds stay intact.
        time.sleep(.13)
    # Schema fields remain local when an unrelated Project buffer changes
    # after the request was accepted. Its name and the expected symbol set stay
    # unchanged; only the cross-file dependency generation changes.
    client.open(project, wire.PROJECT)
    client.request('textDocument/hover', {'textDocument': {'uri': focus.as_uri()}, 'position': position(focus_text, 'apiVersion')})
    for number in range(8):
        ident = 30000 + number
        started = time.monotonic()
        client.send('textDocument/completion', {'textDocument': {'uri': focus.as_uri()}, 'position': position(focus_text, 'syncPol')}, ident)
        client.change(project, wire.PROJECT + f'# unrelated revision {number}\n', number + 2)
        response = client.wait(lambda message: message.get('id') == ident)
        local.append({'phase': 'project-buffer-change', 'seconds': time.monotonic() - started, 'error': response.get('error'), 'labels': [item['label'] for item in completion_items(response)]})
    progress = traces.since(progress_mark)
    record('local-completion-during-index-commits', {'minimumQueriesObserved': len(local) >= 16, 'everyLocalFieldAvailable': all(row['error'] is None and 'syncPolicy' in row['labels'] for row in local), 'localLatencyBound': max(row['seconds'] for row in local) <= args.max_local_seconds, 'backgroundReadsObserved': any(row['stats'].get('diskReads', 0) > 0 for row in progress)}, {'requests': local, 'backgroundBatches': len(progress)})

    # Finding one last-named file is not proof that the unordered work queue has
    # processed every other file. Require the real terminal discovery signal.
    ready = wait_ready(client, traces, progress_mark, args.files + 4, 120, False)
    settle(client, last_name, [last_name])
    settle(client, 'team', ['team'])
    timings = []
    for query, names in [('no-declaration-can-match-this', []), (last_name, [last_name])]:
        for _ in range(4):
            started = time.monotonic()
            response = client.request('workspace/symbol', {'query': query})
            elapsed = time.monotonic() - started
            actual = [item['name'] for item in response.get('result', [])]
            timings.append({'query': query, 'seconds': elapsed, 'error': response.get('error'), 'names': actual})
    record('zero-and-sparse-matches', {'allQueriesComplete': all(row['error'] is None for row in timings), 'zeroMatchIsEmpty': all(row['names'] == [] for row in timings if row['query'].startswith('no-')), 'lastDeclarationFound': all(row['names'] == [last_name] for row in timings if row['query'] == last_name), 'latencyBound': max(row['seconds'] for row in timings) <= args.max_query_seconds}, {'samples': timings, 'symbolsPerFile': args.count, 'files': args.files})

    response = client.request('workspace/symbol', {'query': ''})
    items = response.get('result', [])
    actual = signatures(items)
    expected = sorted(expected, key=lambda item: item[0])
    positions = []
    text_lines = {uri: text.splitlines() for uri, text in texts.items()}
    for item in items:
        location = item['location']
        lines = text_lines[location['uri']]
        span = location['range']
        start, end = span['start'], span['end']
        positions.append(start['line'] == end['line'] and lines[start['line']].encode('utf-16-le')[start['character'] * 2:end['character'] * 2].decode('utf-16-le') == item['name'])
    record('single-file-many-symbols-and-global-cap', {'exactStableOrder': actual == expected[:20000], 'oneItemPerDeclaration': len(actual) == min(len(expected), 20000) and len(set(actual)) == len(actual), 'projectNamespaceAliasDeduplicated': sum(item['name'] == 'team' for item in items) == 1, 'allPositionsExact': bool(positions) and all(positions)}, {'expectedDeclarations': len(expected), 'returned': len(items), 'resultCap': 20000, 'ready': ready['stats']})

    concurrent = []
    for number in range(8):
        symbol_id, hover_id = 10000 + number * 2, 10001 + number * 2
        mark = len(client.messages)
        started = time.monotonic()
        client.send('workspace/symbol', {'query': 'no-declaration-can-match-this'}, symbol_id)
        client.send('$/cancelRequest', {'id': symbol_id})
        client.send('textDocument/hover', {'textDocument': {'uri': focus.as_uri()}, 'position': position(focus_text, 'apiVersion')}, hover_id)
        found = responses(client, {symbol_id, hover_id}, mark, args.max_local_seconds + 1)
        terminal = found[symbol_id]['message']
        hover = found[hover_id]['message']
        concurrent.append({'seconds': time.monotonic() - started, 'symbol': terminal.get('error', {'result': terminal.get('result')}), 'hoverUseful': bool(hover.get('result')), 'terminalCount': sum(row['message'].get('id') == symbol_id for row in client.messages[mark:])})
    record('cancel-symbol-scan-with-independent-hover', {'oneTerminalPerQuery': all(row['terminalCount'] == 1 for row in concurrent), 'cancellationObserved': any(row['symbol'].get('code') == -32800 for row in concurrent), 'onlyCancellationOrAlreadyCompleted': all(row['symbol'].get('code') == -32800 or row['symbol'].get('result') == [] for row in concurrent), 'hoverRemainsAvailable': all(row['hoverUseful'] for row in concurrent), 'latencyBound': max(row['seconds'] for row in concurrent) <= args.max_local_seconds}, {'requests': concurrent})

    mark = len(client.messages)
    changed = focus_text.replace('focus-before', 'focus-after')
    started = time.monotonic()
    client.send('workspace/symbol', {'query': ''}, 20000)
    client.change(focus, changed, 2)
    client.send('textDocument/hover', {'textDocument': {'uri': focus.as_uri()}, 'position': position(changed, 'apiVersion')}, 20001)
    found = responses(client, {20000, 20001}, mark, args.max_local_seconds + 1)
    edit_elapsed = time.monotonic() - started
    settle(client, 'focus-after', ['focus-after'])
    stale = client.request('workspace/symbol', {'query': 'focus-before'})
    new_response = client.request('workspace/symbol', {'query': 'focus-after'})
    new_items = new_response.get('result', [])
    record('didchange-during-symbol-query', {'independentHoverUseful': bool(found[20001]['message'].get('result')), 'inputLatencyBound': edit_elapsed <= args.max_local_seconds, 'oldNameWithdrawn': stale.get('result') == [], 'newNameReturnedOnce': len(new_items) == 1 and new_items[0]['name'] == 'focus-after'}, {'seconds': edit_elapsed, 'inflightSymbolError': found[20000]['message'].get('error'), 'fresh': new_response})
except Exception:
    record('runner-error', {'noException': False}, {'traceback': traceback.format_exc()})
finally:
    if client is not None:
        client.close()
report = {'passed': len(rows) == 5 and all(row['passed'] for row in rows), 'cases': rows, 'binarySHA256': hashlib.sha256(wire.BINARY.read_bytes()).hexdigest(), 'runnerSHA256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'boundary': 'Real stdio and YAML; trace-only observability, no scanner/parser/clock or product budget override.'}
(wire.OUT / 'results.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
sys.exit(0 if report['passed'] else 1)
