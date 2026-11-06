#!/usr/bin/env python3
"""Real stdio regressions for opening a parent of six Git repositories.

The sibling failures are deterministic input/parser limits, not a guess about how
many files happen to consume five seconds on a particular runner. The complete
wire transcript proves that a background failure cannot erase an open buffer's
local diagnostics. --timeout requires a test-only build with its background
analysis deadline shortened; it fails unless an actual deadline is observed.
This is a native LSP test, not a Zed GUI installation test.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

import audit_repair_e2e as wire


VALID = wire.APP
INVALID = VALID.replace('project: team', 'project: 123')
FOREIGN = 'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: not-argocd}\ndata: {}\n'


def has_code(diagnostics, code):
    return any(item.get('code') == code for item in diagnostics)


def publications(client, file, since=0):
    return [entry['message']['params'] for entry in client.messages[since:]
            if entry['message'].get('method') == 'textDocument/publishDiagnostics'
            and entry['message']['params'].get('uri') == file.as_uri()]


def wait_message(client, predicate, since=0):
    for entry in client.messages[since:]:
        if predicate(entry['message']):
            return entry['message']
    return client.wait(predicate)


def wait_diagnostics(client, file, version, since=0):
    message = wait_message(client, lambda m:
                           m.get('method') == 'textDocument/publishDiagnostics'
                           and m['params'].get('uri') == file.as_uri()
                           and m['params'].get('version') == version, since)
    return message['params']['diagnostics']


def check(row, name, condition, observed=None):
    item = {'id': name, 'passed': bool(condition)}
    if observed is not None:
        item['observed'] = observed
    row['checks'].append(item)


def barrier(client):
    response = client.request('audit/processingBarrier', {})
    if response.get('error', {}).get('code') != -32601:
        raise AssertionError('unknown-method barrier must receive method-not-found')


def fixture(root, failure):
    repos = []
    for number in range(6):
        repo = root / ('repo-%d' % number)
        repo.mkdir(parents=True)
        subprocess.run(['git', 'init', '--quiet', str(repo)], check=True,
                       capture_output=True, timeout=10)
        repos.append(repo)
        for index in range(20):
            wire.write(repo, 'resources/%03d.yaml' % index, FOREIGN)
    app = wire.write(repos[0], 'app.yaml', VALID)
    # Git boundaries must not silently narrow the declared LSP workspace scope.
    project = wire.write(repos[1], 'project.yaml', wire.PROJECT)
    if failure == 'acquisition':
        # A regular file one byte above the existing 5 MiB per-file limit.
        problem = wire.write(repos[-1], 'oversized.yaml', '#' + 'x' * (5 * 1024 * 1024))
        reason = 'file exceeds input budget'
    elif failure == 'parser':
        # Small enough to acquire quickly; too many documents to parse safely.
        problem = wire.write(repos[-1], 'documents.yaml', '---\na: 1\n' * 2002)
        reason = 'stream exceeds 2000 documents'
    else:
        problem = wire.write(repos[-1], 'ordinary.yaml', FOREIGN)
        reason = 'context deadline exceeded'
    return app, project, problem, reason


def scenario(out, failure):
    out.mkdir(parents=True)
    root = out / 'parent'
    app, project, problem, reason = fixture(root, failure)
    row = {'id': 'six-repositories-' + failure, 'checks': [],
           'workspaceRoot': str(root), 'gitRepositories': 6,
           'failureFile': str(problem.relative_to(root)), 'failureReason': reason,
           'initialConfigPresent': (root / '.argocd-ls.yaml').exists()}
    client = None
    try:
        client = wire.LSP(out, [root])
        started = time.monotonic()
        client.open(app, INVALID, 1)
        first = wait_diagnostics(client, app, 1)
        row['firstDiagnosticSeconds'] = time.monotonic() - started
        check(row, 'zero-config-unsaved-buffer-type-error', has_code(first, 'SCHEMA_TYPE'), first)

        # Observe the actual background failure, then drain its entire serial
        # publication transaction with an ordinary JSON-RPC processing barrier.
        if failure == 'deadline':
            failed = wait_message(client, lambda m:
                                  m.get('method') == 'window/logMessage'
                                  and reason in m['params'].get('message', ''))
        else:
            failed = wait_message(client, lambda m:
                                  m.get('method') == 'textDocument/publishDiagnostics'
                                  and any(reason in d.get('message', '')
                                          for d in m['params'].get('diagnostics', [])))
        barrier(client)
        history = publications(client, app)
        check(row, 'background-failure-was-exercised', bool(failed), failed['params'])
        failure_index = next(index for index, entry in enumerate(client.messages)
                             if entry['message'] is failed)
        local_index = next((index for index, entry in enumerate(client.messages)
                            if entry['message'].get('method') == 'textDocument/publishDiagnostics'
                            and entry['message']['params'].get('uri') == app.as_uri()
                            and entry['message']['params'].get('version') == 1
                            and entry['message']['params'].get('x-argocd-analysis-scope') == 'document'
                            and has_code(entry['message']['params']['diagnostics'], 'SCHEMA_TYPE')), None)
        check(row, 'local-diagnostics-precede-background-failure', local_index is not None
              and local_index < failure_index,
              {'documentMessageIndex': local_index, 'backgroundFailureMessageIndex': failure_index})
        check(row, 'background-failure-cannot-clear-local-type-error', bool(history)
              and history[-1].get('version') == 1
              and has_code(history[-1]['diagnostics'], 'SCHEMA_TYPE'), history[-1] if history else None)

        hover = client.request('textDocument/hover', {
            'textDocument': {'uri': app.as_uri()}, 'position': {'line': 5, 'character': 5}})
        check(row, 'local-schema-hover-survives-background-failure',
              'string' in str((hover.get('result') or {}).get('contents', '')), hover)
        completion = client.request('textDocument/completion', {
            'textDocument': {'uri': app.as_uri()}, 'position': {'line': 5, 'character': 9}})
        check(row, 'local-field-completion-survives-background-failure',
              any(item.get('label') == 'project' for item in completion.get('result', [])), completion)

        since = len(client.messages)
        client.change(app, VALID, 2)
        fixed = wait_diagnostics(client, app, 2, since)
        check(row, 'editing-clears-type-error-without-saving', not has_code(fixed, 'SCHEMA_TYPE'), fixed)

        since = len(client.messages)
        client.change(app, INVALID, 3)
        client.change(app, VALID, 4)
        client.change(app, INVALID, 5)
        newest = wait_diagnostics(client, app, 5, since)
        barrier(client)
        history = publications(client, app, since)
        latest_index = next((index for index, item in enumerate(history)
                             if item.get('version') == 5), len(history))
        check(row, 'burst-settles-on-newest-invalid-version', has_code(newest, 'SCHEMA_TYPE'), newest)
        check(row, 'no-older-publication-after-newest-version', latest_index < len(history)
              and all(item.get('version') == 5 for item in history[latest_index:]), history)

        since = len(client.messages)
        client.change(app, FOREIGN, 6)
        foreign = wait_diagnostics(client, app, 6, since)
        check(row, 'foreign-gvk-clears-argo-schema-diagnostics', not has_code(foreign, 'SCHEMA_TYPE')
              and not any(d.get('code', '').startswith('CD_') for d in foreign), foreign)

        # Repair the unrelated repository without restarting the server. Add
        # only namespace/reference policy (no version pin) for navigation.
        problem.write_text(FOREIGN, encoding='utf-8')
        config = wire.write(root, '.argocd-ls.yaml',
                            'schemaVersion: 1\ncontrolPlaneNamespace: argocd\nreferencePolicy: closed\n')
        client.send('workspace/didChangeWatchedFiles', {'changes': [
            {'uri': problem.as_uri(), 'type': 2}, {'uri': config.as_uri(), 'type': 1}]})
        since = len(client.messages)
        client.change(app, VALID, 7)
        wait_diagnostics(client, app, 7, since)
        response = client.request('textDocument/definition', {
            'textDocument': {'uri': app.as_uri()}, 'position': {'line': 5, 'character': 13}})
        check(row, 'cross-repository-reference-analysis-recovers-without-restart',
              {item['uri'] for item in response.get('result', [])} == {project.as_uri()}, response)

        since = len(client.messages)
        client.change(app, INVALID, 8)
        reopened = wait_diagnostics(client, app, 8, since)
        check(row, 'repaired-workspace-still-checks-unsaved-buffer', has_code(reopened, 'SCHEMA_TYPE'), reopened)
        since = len(client.messages)
        client.send('textDocument/didClose', {'textDocument': {'uri': app.as_uri()}})
        closed = wait_message(client, lambda m:
                              m.get('method') == 'textDocument/publishDiagnostics'
                              and m['params'].get('uri') == app.as_uri()
                              and m['params'].get('version') is None, since)['params']
        check(row, 'closing-buffer-clears-unsaved-error', not has_code(closed['diagnostics'], 'SCHEMA_TYPE'), closed)
        barrier(client)
    except Exception as exc:
        row['error'] = repr(exc)
    finally:
        if client:
            client.close()
            versions = [item['version'] for item in publications(client, app)
                        if isinstance(item.get('version'), int)]
            check(row, 'all-observed-document-versions-are-monotonic', versions == sorted(versions), versions)
            check(row, 'clean-server-exit', client.proc.returncode == 0, client.proc.returncode)
        row['passed'] = bool(row['checks']) and not row.get('error') and all(
            item['passed'] for item in row['checks'])
        row['artifactSHA256'] = {name: wire.hash_file(out / name)
                                 for name in ('stdin', 'stdout', 'stderr', 'messages.json')
                                 if (out / name).exists()}
        (out / 'result.json').write_text(json.dumps(row, indent=2) + '\n', encoding='utf-8')
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=pathlib.Path)
    parser.add_argument('output', type=pathlib.Path)
    parser.add_argument('--timeout', action='store_true',
                        help='require a test-only binary with a shortened background deadline')
    args = parser.parse_args()
    wire.BINARY = args.binary.resolve()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.environ['ARGOCD_LS_TRACE'] = '1'
    rows = []
    failures = ('deadline',) if args.timeout else ('acquisition', 'parser')
    for failure in failures:
        row = scenario(out / failure, failure)
        rows.append(row)
        print(json.dumps({'id': row['id'], 'passed': row['passed'], 'error': row.get('error'),
                          'failedChecks': [item['id'] for item in row['checks'] if not item['passed']]}),
              flush=True)
    report = {'command': sys.argv, 'binarySHA256': wire.hash_file(wire.BINARY),
              'runnerSHA256': wire.hash_file(pathlib.Path(__file__)),
              'transportSHA256': wire.hash_file(pathlib.Path(wire.__file__)),
              'platform': sys.platform, 'python': sys.version,
              'guiAcceptance': False, 'timeoutBuildRequired': args.timeout, 'rows': rows,
              'passed': len(rows) == len(failures) and all(row['passed'] for row in rows)}
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
