#!/usr/bin/env python3
"""Real stdio workloads and independent calibration for opt-in work accounting.

The source fixtures and public results are specified before any work records are
read. Counts measure performed work, not an alternative correctness oracle.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

import audit_repair_e2e as wire
from persistent_workspace_e2e import Client, Traces, point, wait_ready, wait_until
from incremental_equivalence_e2e import apply_document_changes


COUNTERS = ('readAttempts', 'readFiles', 'readBytes', 'readFailures', 'parseCalls',
            'parseBytes', 'projectionCalls', 'configDecodes', 'schemaLoads',
            'schemaCompiles', 'schemaQueries', 'domainPasses', 'domainResources',
            'factsRestored')
FOREIGN = 'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: ordinary}\ndata: {value: untouched}\n'


def work_records(client):
    result = []
    for line in b''.join(client.stderr_chunks).splitlines():
        if line.startswith(b'argocd-work '):
            try:
                result.append(json.loads(line[len(b'argocd-work '):]))
            except ValueError:
                pass  # The live final line can still be partial.
    return result


def finished(client):
    return [row for row in work_records(client) if row['event'] in ('worker-released', 'completed')]


def totals(records):
    return {key: sum(row.get('work', {}).get(key, 0) for row in records) for key in COUNTERS}


def query(client, method, path, text, needle, after=False):
    response = client.request('textDocument/' + method, {
        'textDocument': {'uri': path.as_uri()}, 'position': point(text, needle, after)})
    if 'error' in response:
        raise AssertionError(response)
    return response['result']


def setup(output, name, config=wire.CONFIG):
    folder = output / name
    root = folder / 'workspace'
    root.mkdir(parents=True)
    wire.write(root, '.argocd-ls.yaml', config)
    app = wire.write(root, 'current.yaml', wire.APP)
    project = wire.write(root, 'project.yaml', wire.PROJECT)
    return folder, root, app, project


def mixed(output):
    folder, root, app, project = setup(output, 'mixed-first-open')
    for index in range(1000):
        wire.write(root, 'foreign/%04d.yaml' % index, FOREIGN)
    client = Client(folder, [root])
    trace = Traces(client)
    try:
        client.open(app, wire.APP)
        first = client.diag(app, 1, scope='document')
        ready = wait_ready(client, trace, 0, 1002, 25, False)
        definition = query(client, 'definition', app, wire.APP, 'team')
        warm_ids = []
        warm_answers = []
        for _ in range(6):
            warm_answers.append(query(client, 'hover', app, wire.APP, 'repoURL'))
            warm_ids.append(client.seq)
        # A closed selected resource forces physical acquisition in a foreground
        # request. Its exact bytes are known independently of the recorder.
        closed = client.request('textDocument/documentSymbol', {'textDocument': {'uri': project.as_uri()}})
        closed_id = client.seq
        checks = {'currentDocumentChecked': first == [],
                  'readyCoversMixedWorkspace': ready['stats']['knownFiles'] == 1003,
                  'definitionIsExactOwner': len(definition) == 1 and definition[0]['uri'] == project.as_uri(),
                  'warmQueryResultsExact': all(answer == warm_answers[0] and 'Type: string' in json.dumps(answer) and 'Target: 3.0.0' in json.dumps(answer) for answer in warm_answers),
                  'closedSymbolNonempty': any(item['name'] == 'team' for item in closed.get('result', []))}
    finally:
        client.close()
    records = finished(client)
    count = totals(records)
    closed_work = [r for r in records if r.get('requestId') == closed_id]
    checks.update(actualAcquisitionBytes=count['readBytes'] >= 1000 * len(FOREIGN.encode()),
                  coldSchemaWork=count['schemaLoads'] > 0 and count['schemaCompiles'] > 0,
                  originsSeparated={'request', 'document-background', 'workspace-background'} <= {r['origin'] for r in records},
                  closedReadBytes=bool(closed_work) and closed_work[-1]['work']['readBytes'] == len(wire.PROJECT.encode()))
    return checks, {'totals': count, 'closedRequest': closed_work, 'readiness': ready,
                    'warmRequestIds': warm_ids, 'warmWork': totals([r for r in records if r.get('requestId') in warm_ids])}, records


def during_indexing(output):
    folder, root, app, _ = setup(output, 'queries-during-indexing')
    for index in range(1200):
        wire.write(root, 'foreign/%04d.yaml' % index, FOREIGN)
    partial = wire.APP + '  syncPol\n'
    client = Client(folder, [root])
    trace = Traces(client)
    try:
        client.open(app, wire.APP)
        before_ready = not any(r['stats'].get('complete') for r in trace.since(0))
        hover = query(client, 'hover', app, wire.APP, 'repoURL')
        client.change(app, partial, 2)
        items = query(client, 'completion', app, partial, 'syncPol', True)
        ident = client.seq
        selected = [item for item in items if item['label'] == 'syncPolicy']
        checks = {'queryIssuedBeforeReady': before_ready,
                  'hoverHasTypeAndTarget': 'Type: string' in json.dumps(hover) and 'Target: 3.0.0' in json.dumps(hover),
                  'recoveredFieldHasExpectedEdit': len(selected) == 1 and selected[0]['textEdit']['newText'] == 'syncPolicy: '}
    finally:
        client.close()
    records = finished(client)
    selected = [r for r in records if r.get('requestId') == ident]
    checks['recoveryParseCounted'] = bool(selected) and selected[-1]['work'].get('recoveryParseCalls', 0) > 0
    checks['schemaQueryWorkCounted'] = bool(selected) and selected[-1]['work']['schemaQueries'] > 0
    return checks, {'request': selected, 'hover': hover, 'completion': items}, records


def continuous(output):
    folder, root, app, _ = setup(output, 'continuous-current-edits')
    client = Client(folder, [root])
    trace = Traces(client)
    samples = []
    try:
        client.open(app, wire.APP)
        client.diag(app, 1)
        wait_ready(client, trace, 0, 2, 15, False)
        for version in range(2, 10):
            text = wire.APP.replace('project: team', 'project: 42') if version % 2 == 0 else wire.APP
            text += '# exact accepted version %d\n' % version
            start = time.monotonic()
            client.change(app, text, version)
            diagnostics = client.diag(app, version)
            samples.append({'version': version, 'milliseconds': (time.monotonic() - start) * 1000,
                            'codes': [d['code'] for d in diagnostics]})
        checks = {'currentVersionSemantics': all(('SCHEMA_TYPE' in row['codes']) == (row['version'] % 2 == 0) for row in samples),
                  'eightRealEdits': len(samples) == 8}
    finally:
        client.close()
    records = finished(client)
    count = totals(records)
    checks['changedInputActualParse'] = count['parseCalls'] >= 8 and count['parseBytes'] >= sum(len(wire.APP.encode()) for _ in samples)
    checks['domainWorkVisible'] = count['domainPasses'] > 0 and count['domainResources'] >= count['domainPasses']
    return checks, {'samples': samples, 'totals': count}, records


def rotation(output):
    folder, root, app, project = setup(output, 'scope-target-rotation')
    other = root / 'nested'
    other.mkdir()
    wire.write(other, '.argocd-ls.yaml', wire.CONFIG.replace('3.0.0', '3.6.1'))
    other_app = wire.write(other, 'current.yaml', wire.APP)
    other_project = wire.write(other, 'project.yaml', wire.PROJECT)
    client = Client(folder, [root, other])
    trace = Traces(client)
    outcomes = []
    try:
        for path in (app, other_app):
            client.open(path, wire.APP)
            client.diag(path, 1)
        wait_ready(client, trace, 0, 4, 15, False)
        for path, owner, target in ((app, project, '3.0.0'), (other_app, other_project, '3.6.1'), (app, project, '3.0.0')):
            definition = query(client, 'definition', path, wire.APP, 'team')
            hover = query(client, 'hover', path, wire.APP, 'repoURL')
            outcomes.append(len(definition) == 1 and definition[0]['uri'] == owner.as_uri() and 'Target: ' + target in json.dumps(hover))
        mark = trace.mark()
        client.send('workspace/didChangeConfiguration', {'settings': {'targetVersion': '3.4.13'}})
        wait_ready(client, trace, mark, 4, 15, False)
        changed = query(client, 'hover', other_app, wire.APP, 'repoURL')
        mark = trace.mark()
        client.send('workspace/didChangeConfiguration', {'settings': {'targetVersion': ''}})
        wait_ready(client, trace, mark, 4, 15, False)
        restored = query(client, 'hover', other_app, wire.APP, 'repoURL')
        # A real dependency replacement must invalidate same-spelled navigation.
        project.write_text(wire.PROJECT.replace('name: team', 'name: renamed'))
        mark = trace.mark()
        client.send('workspace/didChangeWatchedFiles', {'changes': [{'uri': project.as_uri(), 'type': 2}]})
        wait_ready(client, trace, mark, 4, 15, False)
        absent = query(client, 'definition', app, wire.APP, 'team')
        still_other = query(client, 'definition', other_app, wire.APP, 'team')
        checks = {'threeScopeSelectionsExact': all(outcomes),
                  'targetRoundtripInvalidates': 'Target: 3.4.13' in json.dumps(changed) and 'Target: 3.6.1' in json.dumps(restored),
                  'dependencyInvalidatesLocally': absent == [] and len(still_other) == 1 and still_other[0]['uri'] == other_project.as_uri()}
    finally:
        client.close()
    records = finished(client)
    checks['invalidationWorkVisible'] = totals(records)['configDecodes'] >= 4 and totals(records)['readBytes'] > 0
    return checks, {'selections': outcomes, 'totals': totals(records)}, records


def cancellation(output):
    folder, root, app, _ = setup(output, 'cancel-and-recover')
    # Excluded from discovery, but an explicit selected file remains queryable.
    (root / '.argocd-ls.yaml').write_text(wire.CONFIG + 'exclude: ["heavy.yaml"]\n')
    heavy_text = '---\n'.join(wire.APP.replace('name: app', 'name: heavy%d' % i) for i in range(1800))
    heavy = wire.write(root, 'heavy.yaml', heavy_text)
    client = Client(folder, [root])
    trace = Traces(client)
    descriptor = None
    try:
        client.open(app, wire.APP)
        client.diag(app, 1)
        wait_ready(client, trace, 0, 2, 15, False)
        # didOpen uses the authoritative large buffer and starts document work;
        # cancellation is sent after the request's actual worker-start trace.
        client.open(heavy, heavy_text)
        client.send('textDocument/codeAction', {'textDocument': {'uri': heavy.as_uri()}, 'range': {'start': {'line': 5, 'character': 2}, 'end': {'line': 5, 'character': 15}}, 'context': {'diagnostics': []}}, 700)
        try:
            started = wait_until(client, lambda: next((r for r in work_records(client) if r.get('requestId') == 700 and r['event'] == 'started'), None), 1, 'actual request worker start')
        except TimeoutError:
            started = None  # Frozen pre-instrumentation baseline records this gap.
        begin = time.monotonic()
        client.send('$/cancelRequest', {'id': 700})
        response = client.wait(lambda m: m.get('id') == 700, 2)
        reply_ms = (time.monotonic() - begin) * 1000
        current = query(client, 'hover', app, wire.APP, 'repoURL')
        checks = {'cancelReplyWithinExistingBudget': response.get('error', {}).get('code') == -32800 and reply_ms <= 200,
                  'nextQueryHasCurrentResult': 'Type: string' in json.dumps(current) and 'Target: 3.0.0' in json.dumps(current)}
        if started:
            wait_until(client, lambda: next((r for r in finished(client) if r.get('requestId') == 700), None), 5, 'cancelled worker releases its slot')
        next_edit_lane = client.request('argocd/compatibility', {'textDocument': {'uri': app.as_uri()}})
        checks['sameLaneReusedAfterRelease'] = 'error' not in next_edit_lane and len(next_edit_lane['result'].get('resources', [])) == 1 and next_edit_lane['result']['resources'][0]['name'] == 'app'
    finally:
        if descriptor is not None:
            os.close(descriptor)
        client.close()
    records = work_records(client)
    lifecycle = [r for r in records if r.get('requestId') == 700]
    released = next((r for r in lifecycle if r['event'] == 'worker-released'), None)
    replied = next((r for r in lifecycle if r['event'] == 'reply'), None)
    checks['workerReleaseSeparateFromReply'] = bool(released and replied and released['timing'].get('cancelToReleaseNs') is not None and replied['timing'].get('cancelToReplyNs') is not None)
    checks['cancelledWorkWasReal'] = bool(released) and released['work']['parseCalls'] > 0 and released['work']['parseBytes'] >= len(heavy_text.encode())
    return checks, {'replyMilliseconds': reply_ms, 'response': response, 'lifecycle': lifecycle}, finished(client)


def trace_off(output):
    answers = []
    records = []
    rename_ids = []
    source_text = wire.APP.replace('  source: {repoURL: https://example.org/repo}',
        '  sources:\n    - repoURL: https://example.org/values\n      ref: values\n'
        '    - repoURL: https://example.org/chart\n      helm: {valueFiles: ["$values/config.yaml"]}')
    expected = source_text.replace('ref: values', 'ref: "assets"').replace('$values/config.yaml', '$assets/config.yaml')
    for enabled in (False, True):
        if enabled:
            os.environ['ARGOCD_LS_TRACE'] = '1'
        else:
            os.environ.pop('ARGOCD_LS_TRACE', None)
        folder, root, app, _ = setup(output, 'trace-' + str(enabled).lower())
        client = Client(folder, [root])
        try:
            client.open(app, wire.APP)
            diagnostics = client.diag(app, 1)
            answers.append({'diagnostics': diagnostics, 'hover': query(client, 'hover', app, wire.APP, 'repoURL')})
            client.change(app, source_text, 2)
            client.diag(app, 2)
            response = client.request('textDocument/rename', {'textDocument': {'uri': app.as_uri()},
                'position': point(source_text, 'ref: values', True), 'newName': 'assets'})
            rename_ids.append(client.seq)
            applied = apply_document_changes(response.get('result'), {app.as_uri(): source_text}, {app.as_uri(): 2}, {app.as_uri()})
            answers[-1]['renamedText'] = applied[app.as_uri()]
        finally:
            client.close()
        records.append(finished(client))
    return {'traceOffHasNoDetailedRecords': not records[0],
            'publicResultUnchanged': answers[0] == answers[1],
            'sourceRefRenameApplied': all(answer['renamedText'] == expected for answer in answers),
            'sourceRefPlanDomainWork': any(r.get('requestId') == rename_ids[1] and r['work']['domainPasses'] == 2 and r['work']['domainResources'] == 2 for r in records[1]),
            'traceOnHasActualWork': totals(records[1])['parseCalls'] > 0}, {'public': answers}, records[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    wire.BINARY = args.binary.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ['ARGOCD_LS_TRACE'] = '1'
    rows = []
    for name, run in [('mixed-first-open', mixed), ('queries-during-indexing', during_indexing),
                      ('continuous-current-edits', continuous), ('scope-target-rotation', rotation),
                      ('cancel-and-recover', cancellation), ('trace-off-control', trace_off)]:
        row = {'id': name, 'passed': False}
        try:
            checks, observed, records = run(output)
            checks['boundedCounterContract'] = bool(records) and all(
                all(type(r.get('work', {}).get(k)) is int and r['work'][k] >= 0 for k in COUNTERS)
                and r.get('origin') in ('request', 'document-background', 'workspace-background')
                for r in records)
            row.update(checks=checks, observed=observed, passed=all(checks.values()))
            (output / (name + '-work.json')).write_text(json.dumps(records, indent=2) + '\n')
        except Exception as error:
            row['error'] = repr(error)
        rows.append(row)
        report = {'schemaVersion': 1, 'command': sys.argv, 'binarySHA256': wire.hash_file(wire.BINARY),
                  'runnerSHA256': wire.hash_file(Path(__file__)), 'rows': rows,
                  'passed': len(rows) == 6 and all(row['passed'] for row in rows)}
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'id': name, 'passed': row['passed'], 'checks': row.get('checks'), 'error': row.get('error')}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
