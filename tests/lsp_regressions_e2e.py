#!/usr/bin/env python3
"""Real stdio regressions for persistent progress, read proofs, and file failures.

No production test hooks are used. Linux acquisition tests observe actual
IN_OPEN events; other platforms explicitly report that case as skipped.
Scope tests require a synchronous protocol barrier after the dependency edit to
arrive before the asynchronous query response, or fail for missing race coverage.
"""
import argparse
import ctypes
import json
import os
import pathlib
import select
import shutil
import struct
import sys
import tempfile
import time

import audit_repair_e2e as wire
import persistent_workspace_e2e as progress


def query(path, line=5, character=13, references=False):
    params = {'textDocument': {'uri': path.as_uri()},
              'position': {'line': line, 'character': character}}
    if references:
        params['context'] = {'includeDeclaration': False}
    return params


def result_uris(response):
    return {item['uri'] for item in response.get('result', [])}


def labels(response):
    result = response.get('result', [])
    if isinstance(result, dict):
        if type(result.get('isIncomplete')) is not bool or not isinstance(result.get('items'), list):
            raise ValueError('Invalid CompletionList response: ' + repr(response))
        result = result['items']
    if not isinstance(result, list):
        raise ValueError('Invalid completion response: ' + repr(response))
    return [item['label'] for item in result]


def has_code(diagnostics, code):
    return any(item.get('code') == code for item in diagnostics)


def publications(client, path, since=0):
    return [entry['message']['params'] for entry in client.messages[since:]
            if entry['message'].get('method') == 'textDocument/publishDiagnostics'
            and entry['message']['params']['uri'] == path.as_uri()]


def wait_diagnostics(client, path, version, since=0, predicate=lambda ds: True):
    def matches(params):
        return params.get('version') == version and params.get('x-argocd-analysis-scope', 'workspace') == 'workspace' and predicate(params['diagnostics'])
    for params in reversed(publications(client, path, since)):
        if matches(params):
            return params['diagnostics']
    response = client.wait(lambda m: m.get('method') == 'textDocument/publishDiagnostics'
                           and m['params']['uri'] == path.as_uri() and matches(m['params']))
    return response['params']['diagnostics']


def watched(client, changes):
    client.send('workspace/didChangeWatchedFiles',
                {'changes': [{'uri': path.as_uri(), 'type': kind} for path, kind in changes]})


def barrier_pair(client, request_id, barrier_id):
    first = client.wait(lambda m: m.get('id') in (request_id, barrier_id))
    second = client.wait(lambda m: m.get('id') in (request_id, barrier_id)
                          and m.get('id') != first['id'])
    responses = {first['id']: first, second['id']: second}
    return responses[request_id], responses[barrier_id], first['id'] == barrier_id


class Case:
    def __init__(self, out, name):
        self.out = out / name
        self.out.mkdir(parents=True)
        self.client = None
        self.traces = None
        self.roots = []
        self.temporary = None
        self.row = {'id': name, 'checks': []}

    def start(self, roots):
        self.roots = roots
        self.client = progress.Client(self.out, roots)
        self.traces = progress.Traces(self.client)
        return self.client

    def ready(self, mark=0):
        return progress.wait_ready(self.client, self.traces, mark, 0, 30, False)

    def check(self, name, condition, observed=None):
        row = {'id': name, 'passed': bool(condition)}
        if observed is not None:
            row['observed'] = observed
        self.row['checks'].append(row)

    def close(self):
        if self.client:
            self.client.close()

    def finish(self):
        self.close()
        if self.client:
            self.check('clean-server-exit', self.client.proc.returncode == 0,
                       self.client.proc.returncode)
        self.row['passed'] = bool(self.row['checks']) and not self.row.get('error') and all(
            check['passed'] for check in self.row['checks'])
        self.row['status'] = 'passed' if self.row['passed'] else 'failed'
        if self.temporary:
            # Preserve the exact final fixture after all observation ends. Some
            # developer workspaces are asynchronously mirrored by file readers;
            # observing an isolated temporary directory prevents their opens
            # from being mistaken for opens by the tested language server.
            exported = []
            self.row['workspaceArtifacts'] = []
            for root in self.roots:
                destination = self.out / root.name
                shutil.copytree(root, destination)
                exported.append(destination)
                self.row['workspaceArtifacts'].append({'originalRoot': str(root),
                                                      'artifactDirectory': root.name})
            self.roots = exported
            self.temporary.cleanup()
        self.row['inputSHA256'] = {
            str(path.relative_to(self.out)): wire.hash_file(path)
            for root in self.roots for path in sorted(root.rglob('*')) if path.is_file()}
        self.row['artifactSHA256'] = {
            name: wire.hash_file(self.out / name)
            for name in ('stdin', 'stdout', 'stderr', 'messages.json')
            if (self.out / name).exists()}
        (self.out / 'result.json').write_text(json.dumps(self.row, indent=2) + '\n')
        return self.row


class Opens:
    """Observe the tested binary's file opens, with no sampling sleep or hooks."""
    def __init__(self):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        self.libc = libc
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), 'inotify_init1')
        self.counts = {}
        self.consumed = {}
        self.events = []
        self.started = time.monotonic()

    def watch(self, path):
        descriptor = self.libc.inotify_add_watch(self.fd, os.fsencode(path), 0x20)
        if descriptor < 0:
            raise OSError(ctypes.get_errno(), 'inotify_add_watch')
        return descriptor

    def drain(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return
            cursor = 0
            while cursor < len(data):
                descriptor, mask, cookie, size = struct.unpack_from('iIII', data, cursor)
                cursor += 16 + size
                self.events.append({'watch': descriptor, 'mask': mask, 'cookie': cookie,
                                    'elapsedSeconds': time.monotonic() - self.started})
                if mask & 0x20:
                    self.counts[descriptor] = self.counts.get(descriptor, 0) + 1

    def next_open(self, descriptor, timeout=5):
        deadline = time.monotonic() + timeout
        while True:
            self.drain()
            consumed = self.consumed.get(descriptor, 0)
            if self.counts.get(descriptor, 0) > consumed:
                self.consumed[descriptor] = consumed + 1
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                return False

    def close(self):
        os.close(self.fd)


def rescan_case(case, change):
    # The command-line case name is retained for consumers of the old suite.
    # Its contract is now persistent progress: only configuration changes need
    # a fresh membership scan; ordinary watch events update their URI directly.
    case.temporary = tempfile.TemporaryDirectory(prefix='argocd-lsp-progress-')
    isolated = pathlib.Path(case.temporary.name).resolve()
    root = isolated / 'affected-root'
    healthy = isolated / 'independent-root'
    config = wire.write(root, '.argocd-ls.yaml', wire.CONFIG)
    project = wire.write(root, 'project.yaml', wire.PROJECT)
    sentinel = wire.write(root, '000-sentinel.yaml',
                          'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: sentinel}\n')
    text = wire.APP.replace('project: team', 'project: newteam') if change == 'create' else wire.APP
    app = wire.write(root, 'active.yaml', text)
    for index in range(2999):
        wire.write(root, 'other/%05d.yaml' % index,
                   wire.APP.replace('name: app', 'name: app%d' % index))
    wire.write(healthy, '.argocd-ls.yaml', wire.CONFIG)
    healthy_project = wire.write(healthy, 'project.yaml', wire.PROJECT)
    healthy_app = wire.write(healthy, 'app.yaml', wire.APP)
    client = case.start([root, healthy])
    client.open(app, text)
    client.open(healthy_app, wire.APP)
    initial_ready = case.ready()
    initial = wait_diagnostics(client, app, 1)
    wait_diagnostics(client, healthy_app, 1)
    case.check('initial-ready-control', initial_ready['stats']['complete'], initial_ready)
    case.check('initial-diagnostic-control', has_code(initial, 'CD_PROJECT_UNRESOLVED')
               == (change == 'create'), initial)
    initial_definition = client.request('textDocument/definition', query(app))
    case.check('initial-definition-control', result_uris(initial_definition)
               == (set() if change == 'create' else {project.as_uri()}), initial_definition)
    observed = Opens()
    try:
        scan_watch = observed.watch(sentinel)
        independent_watch = observed.watch(healthy_project)
        created = root / 'created-project.yaml'
        if change == 'rename':
            project.write_text(wire.PROJECT.replace('name: team', 'name: renamed'))
            changes = [(project, 2)]
        elif change == 'config':
            config.write_text(wire.CONFIG + 'exclude: ["project.yaml"]\n')
            changes = [(config, 2)]
        elif change == 'delete':
            project.unlink()
            changes = [(project, 3)]
        else:
            wire.write(root, created.name, wire.PROJECT.replace('name: team', 'name: newteam'))
            changes = [(created, 1)]
        mark = case.traces.mark()
        watched(client, changes)
        if change == 'config':
            opened = observed.next_open(scan_watch)
            case.check('configuration-change-starts-discovery', opened,
                       {'inotifyOpenCount': observed.counts.get(scan_watch, 0)})
        for version in (2, 3):
            client.change(app, text + '# edit while work is pending %d\n' % version, version)
        # A known-invalidated target must disappear immediately. A newly
        # created target may remain absent until its one queued read commits.
        immediate_definition = client.request('textDocument/definition', query(app))
        immediate_completion = client.request('textDocument/completion', query(app))
        expected = {created.as_uri()} if change == 'create' else set()
        case.check('known-change-never-returns-old-definition',
                   immediate_definition.get('error', {}).get('code') == -32801
                   or ('result' in immediate_definition and result_uris(immediate_definition) <= expected),
                   immediate_definition)
        completion_ok = immediate_completion.get('error', {}).get('code') == -32801
        if 'result' in immediate_completion:
            completion_ok = 'team' not in labels(immediate_completion)
        case.check('known-change-never-returns-old-completion', completion_ok, immediate_completion)
        ready = case.ready(mark)
        current = wait_diagnostics(client, app, 3)
        definition = client.request('textDocument/definition', query(app))
        case.check('latest-diagnostics-include-known-change',
                   has_code(current, 'CD_PROJECT_UNRESOLVED') != (change == 'create'), current)
        case.check('settled-definition-needs-no-new-event',
                   'result' in definition and result_uris(definition) == expected, definition)
        case.check('persistent-work-settles-with-newest-buffer',
                   ready['stats']['complete'] and ready['stats']['queueDepth'] == 0, ready)
        observed.drain()
        # The following explicit query is allowed to hydrate its selected
        # closed Project once. Measure unrelated background I/O before it.
        case.check('unaffected-root-not-opened-by-background-work',
                   observed.counts.get(independent_watch, 0) == 0,
                   {'opens': observed.counts.get(independent_watch, 0)})
        if change != 'config':
            case.check('single-uri-event-does-not-reread-sentinel',
                       observed.counts.get(scan_watch, 0) == 0,
                       {'opens': observed.counts.get(scan_watch, 0)})
            work = progress.work_totals(case.traces.since(mark))
            case.check('single-uri-event-does-not-restart-membership-scan',
                       work['discoveryWork'] == 0 and work['diskReads'] <= 1, work)
        independent = client.request('textDocument/definition', query(healthy_app))
        case.check('unaffected-root-stays-usable',
                   result_uris(independent) == {healthy_project.as_uri()}, independent)

        mark = case.traces.mark()
        config.write_text(wire.CONFIG)
        project.write_text(wire.PROJECT)
        restoring = [(config, 2), (project, 2)]
        if created.exists():
            created.unlink()
            restoring.append((created, 3))
        watched(client, restoring)
        client.change(app, wire.APP + '# restore control\n', 4)
        case.ready(mark)
        restored = wait_diagnostics(client, app, 4)
        restored_definition = client.request('textDocument/definition', query(app))
        case.check('restored-positive-control', not has_code(restored, 'CD_PROJECT_UNRESOLVED')
                   and result_uris(restored_definition) == {project.as_uri()}, restored_definition)
        mark = case.traces.mark()
        client.change(app, wire.APP + '# normal incremental control\n', 5)
        case.ready(mark)
        wait_diagnostics(client, app, 5)
        work = progress.work_totals(case.traces.since(mark))
        case.check('ordinary-edit-remains-incremental', work['diskReads'] == 0
                   and work['parsedFiles'] == 1 and work['discoveryWork'] == 0, work)
        observed.drain()
        case.row['inotifyEvents'] = observed.events
    finally:
        observed.close()
    case.close()
    trace_lines = (case.out / 'stderr').read_text().splitlines()
    case.row['traces'] = [json.loads(line.split(' ', 1)[1]) for line in trace_lines
                          if line.startswith('argocd-analysis ')]
    case.row['discardedTraces'] = [json.loads(line.split(' ', 1)[1]) for line in trace_lines
                                   if line.startswith('argocd-analysis-discarded ')]


def scope_config(mode):
    if mode == 'merged':
        body = '  - name: a\n    include: ["a/**", "b/**"]\n'
    elif mode == 'reassigned':
        body = '  - name: a\n    include: ["a/project.yaml", "b/**"]\n  - name: b\n    include: ["a/apps/**"]\n'
    else:
        name = 'renamed-a' if mode == 'switched' else 'a'
        body = '  - name: %s\n    include: ["a/**"]\n  - name: b\n    include: ["b/**"]\n' % name
    return wire.CONFIG + 'scopes:\n' + body + '  - name: c\n    include: ["c/**"]\n'


def scope_case(case, transition):
    old_mode, new_mode = {
        'merge': ('separate', 'merged'), 'split': ('merged', 'separate'),
        'reassign': ('separate', 'reassigned'), 'switch': ('separate', 'switched')}[transition]
    old_config, new_config = scope_config(old_mode), scope_config(new_mode)
    root = case.out / 'workspace'
    config = wire.write(root, '.argocd-ls.yaml', old_config)
    project = wire.write(root, 'a/project.yaml', wire.PROJECT)
    applications = {wire.write(root, 'a/apps/%03d.yaml' % index,
                              wire.APP.replace('name: app', 'name: app%d' % index)).as_uri()
                    for index in range(128)}
    other = wire.write(root, 'b/app.yaml', wire.APP)
    independent = wire.write(root, 'c/app.yaml', wire.APP)
    editable_text = wire.APP.replace('project: team', 'proj: team')
    editable = wire.write(root, 'a/apps/editable.yaml', editable_text)
    client = case.start([root])
    for path, text in ((config, old_config), (project, wire.PROJECT), (other, wire.APP),
                       (independent, wire.APP), (editable, editable_text)):
        client.open(path, text)
    case.ready()
    wait_diagnostics(client, editable, 1)
    params = query(project, 3, 12, references=True)
    expected_before = applications | ({other.as_uri()} if old_mode == 'merged' else set())
    stable = client.request('textDocument/references', params)
    case.check('exact-initial-reference-control', result_uris(stable) == expected_before, stable)
    actions = client.request('textDocument/codeAction', {
        'textDocument': {'uri': editable.as_uri()},
        'range': {'start': {'line': 5, 'character': 2}, 'end': {'line': 5, 'character': 6}},
        'context': {'diagnostics': []}})
    action = next(action for action in actions.get('result', [])
                  if action.get('data', {}).get('fixCode') == 'FIX_FIELD_NAME')
    resolved = client.request('codeAction/resolve', action)
    edits = resolved.get('result', {}).get('edit', {}).get('documentChanges', [])
    case.check('versioned-edit-positive-control', bool(edits)
               and edits[0]['textDocument'] == {'uri': editable.as_uri(), 'version': 1}, resolved)
    client.send('textDocument/references', params, 150)
    client.change(independent, wire.APP + '# independent scope\n', 2)
    client.send('audit/processingBarrier', {}, 151)
    response, barrier, before = barrier_pair(client, 150, 151)
    case.check('independent-scope-query-stays-valid', result_uris(response) == expected_before, response)
    stale_action = client.request('codeAction/resolve', action)
    independent_edits = stale_action.get('result', {}).get('edit', {}).get('documentChanges', [])
    case.check('independent-scope-does-not-invalidate-local-edit', bool(independent_edits)
               and independent_edits[0]['textDocument'] == {'uri': editable.as_uri(), 'version': 1},
               stale_action)
    wait_diagnostics(client, independent, 2)
    mark = case.traces.mark()
    client.change(editable, editable_text + '# changed current buffer\n', 2)
    changed_action = client.request('codeAction/resolve', action)
    case.check('changed-current-buffer-rejects-old-edit',
               changed_action.get('error', {}).get('code') == -32801, changed_action)
    case.ready(mark)
    actions = client.request('textDocument/codeAction', {
        'textDocument': {'uri': editable.as_uri()},
        'range': {'start': {'line': 5, 'character': 2}, 'end': {'line': 5, 'character': 6}},
        'context': {'diagnostics': []}})
    current_action = next(action for action in actions.get('result', [])
                          if action.get('data', {}).get('fixCode') == 'FIX_FIELD_NAME')
    current_resolved = client.request('codeAction/resolve', current_action)
    current_edits = current_resolved.get('result', {}).get('edit', {}).get('documentChanges', [])
    case.check('new-current-version-edit-positive-control', bool(current_edits)
               and current_edits[0]['textDocument'] == {'uri': editable.as_uri(), 'version': 2},
               current_resolved)
    mark = case.traces.mark()
    client.change(config, new_config, 2)
    client.send('textDocument/references', params, 200)
    client.change(other, wire.APP.replace('project: team', 'project: missing'), 2)
    client.send('audit/processingBarrier', {}, 201)
    response, barrier, before = barrier_pair(client, 200, 201)
    case.row['barrierBeforeResponse'] = before
    case.row['queryResponse'] = response
    dependent = transition in ('merge', 'reassign')
    # Independent-scope reads may correctly finish before the processing barrier.
    # For dependent edits, require the barrier to win so the in-flight proof is tested.
    # In every mode the next check still validates the exact fresh result or refusal.
    case.check('barrier-and-dependent-edit-order', (before or not dependent)
               and barrier.get('error', {}).get('code') == -32601, barrier)
    expected_after = set() if transition == 'reassign' else applications
    case.check('fresh-scope-proof-rejects-only-actual-dependencies',
               response.get('error', {}).get('code') == -32801 if dependent
               else response.get('error', {}).get('code') == -32801
               or ('result' in response and result_uris(response) == expected_after), response)
    stale_config_action = client.request('codeAction/resolve', current_action)
    case.check('changed-config-rejects-old-edit',
               stale_config_action.get('error', {}).get('code') == -32801, stale_config_action)
    case.ready(mark)
    wait_diagnostics(client, other, 2)
    settled = client.request('textDocument/references', params)
    case.check('settled-reference-control', 'result' in settled
               and result_uris(settled) == expected_after, settled)
    mark = case.traces.mark()
    client.change(other, wire.APP, 3)
    case.ready(mark)
    wait_diagnostics(client, other, 3)
    restored = client.request('textDocument/references', params)
    expected_restored = expected_after | ({other.as_uri()} if dependent else set())
    case.check('new-membership-positive-control', 'result' in restored
               and result_uris(restored) == expected_restored, restored)
    case.close()
    case.row['readProofTrace'] = [json.loads(line.split(' ', 1)[1])
                                 for line in (case.out / 'stderr').read_text().splitlines()
                                 if line.startswith('argocd-read-proof ')]


def healthy_queries(case, client, app, project, phase):
    definition = client.request('textDocument/definition', query(app))
    completion = client.request('textDocument/completion', query(app))
    references = client.request('textDocument/references', query(project, 3, 12, references=True))
    case.check(phase + '-definition', result_uris(definition) == {project.as_uri()}, definition)
    case.check(phase + '-completion', 'team' in labels(completion), completion)
    case.check(phase + '-references', result_uris(references) == {app.as_uri()}, references)
    history = publications(client, app)
    case.check(phase + '-schema-diagnostic-retained', bool(history)
               and has_code(history[-1]['diagnostics'], 'SCHEMA_TYPE'), history[-1] if history else None)


def parser_case(case, boundary, entry, failed_first):
    healthy = case.out / ('root-b' if failed_first else 'root-a')
    failed = case.out / ('root-a' if failed_first else 'root-b')
    for root in (healthy, failed):
        wire.write(root, '.argocd-ls.yaml', wire.CONFIG)
        wire.write(root, 'project.yaml', wire.PROJECT)
    text = wire.APP + '  revisionHistoryLimit: wrong\n'
    app = wire.write(healthy, 'app.yaml', text)
    project = healthy / 'project.yaml'
    failed_app = wire.write(failed, 'app.yaml', wire.APP)
    broken = wire.write(failed, 'zz-bad.yaml', 'a: 1\n')
    if boundary == 'depth':
        invalid = 'nested: ' + '[' * 257 + '0' + ']' * 257 + '\n'
        reason = 'depth exceeds 256'
    else:
        invalid = '---\na: 1\n' * 2002
        reason = 'stream exceeds 2000 documents'
    case.row['fixture'] = {'boundary': boundary, 'entry': entry, 'failedRootFirst': failed_first,
                           'bytes': len(invalid.encode())}
    client = case.start([failed, healthy] if failed_first else [healthy, failed])
    client.open(app, text)
    client.open(failed_app, wire.APP)
    case.ready()
    wait_diagnostics(client, app, 1)
    wait_diagnostics(client, failed_app, 1)
    healthy_queries(case, client, app, project, 'before-failure')
    control = client.request('textDocument/definition', query(failed_app))
    case.check('failed-root-initially-usable', result_uris(control) == {(failed / 'project.yaml').as_uri()}, control)
    since = len(client.messages)
    failure_mark = case.traces.mark()
    if entry == 'open':
        client.open(broken, invalid)
        version = 1
    else:
        broken.write_text(invalid)
        watched(client, [(broken, 2)])
        version = None
    diagnostics = wait_diagnostics(client, broken, version, since,
                                   lambda ds: has_code(ds, 'ANALYSIS_INCOMPLETE'))
    case.check('bounded-input-error-explained', any(reason in d['message'] for d in diagnostics), diagnostics)
    limited = progress.wait_until(client, lambda: next((row for row in case.traces.since(failure_mark)
        if row['delivered'] and row['stats'].get('state') == 'Limited'
        and row['stats'].get('queueDepth') == 0), None), 15, 'settled partial scope')
    case.check('failed-scope-is-not-reported-complete', limited['stats'].get('complete') is False, limited)
    healthy_queries(case, client, app, project, 'during-failure')
    known = client.request('textDocument/definition', query(failed_app))
    case.check('failed-file-does-not-hide-known-positive-candidate', result_uris(known)
               == {(failed / 'project.yaml').as_uri()}, known)
    refused = client.request('textDocument/references',
                             query(failed / 'project.yaml', 3, 12, references=True))
    case.check('failed-root-cannot-supply-stale-authority', refused.get('error', {}).get('code') == -32801,
               refused)
    since_missing = len(client.messages)
    client.change(failed_app, wire.APP.replace('project: team', 'project: absent'), 2)
    incomplete = wait_diagnostics(client, failed_app, 2, since_missing)
    case.check('partial-scope-cannot-assert-target-missing', not any(
        item.get('code') == 'CD_PROJECT_UNRESOLVED' and item.get('severity') == 1
        for item in incomplete), incomplete)
    actions = client.request('textDocument/codeAction', {
        'textDocument': {'uri': failed_app.as_uri()},
        'range': {'start': {'line': 5, 'character': 11}, 'end': {'line': 5, 'character': 17}},
        'context': {'diagnostics': incomplete}})
    case.check('partial-scope-cannot-offer-cross-file-edit',
               actions.get('error', {}).get('code') == -32801
               or ('result' in actions and all(
                   item.get('data', {}).get('fixCode') in ('FIX_FIELD_NAME', 'FIX_ENUM_VALUE')
                   for item in actions['result'])), actions)
    client.change(failed_app, wire.APP, 3)
    since = len(client.messages)
    mark = case.traces.mark()
    if entry == 'open':
        client.change(broken, 'a: 1\n', 2)
        version = 2
    else:
        broken.write_text('a: 1\n')
        watched(client, [(broken, 2)])
    case.ready(mark)
    recovered = wait_diagnostics(client, broken, version, since)
    case.check('failure-diagnostic-clears-after-repair', not has_code(recovered, 'ANALYSIS_INCOMPLETE'), recovered)
    healthy_queries(case, client, app, project, 'after-repair')
    restored = client.request('textDocument/definition', query(failed_app))
    case.check('failed-root-recovers-without-restart', result_uris(restored)
               == {(failed / 'project.yaml').as_uri()}, restored)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=pathlib.Path)
    parser.add_argument('output', type=pathlib.Path)
    parser.add_argument('--case', choices=('all', 'rescan-cancel', 'scope-proof', 'parser-root'), default='all')
    args = parser.parse_args()
    wire.BINARY = args.binary.resolve()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.environ['ARGOCD_LS_TRACE'] = '1'
    rows = []

    def run(name, function, *params):
        case = Case(out, name)
        try:
            function(case, *params)
        except Exception as exc:
            case.row['error'] = repr(exc)
        finally:
            row = case.finish()
            rows.append(row)
            print(json.dumps({'id': name, 'status': row['status'],
                              'failedChecks': [check['id'] for check in row['checks'] if not check['passed']],
                              'error': row.get('error')}), flush=True)

    if args.case in ('all', 'rescan-cancel'):
        if sys.platform.startswith('linux'):
            for change in ('rename', 'config', 'create', 'delete'):
                run('rescan-cancel-' + change, rescan_case, change)
        else:
            required = args.case == 'rescan-cancel'
            rows.append({'id': 'rescan-cancel', 'status': 'failed' if required else 'skipped',
                         'passed': False if required else None,
                         'reason': 'Linux IN_OPEN synchronization required; this platform was not exercised'})
    if args.case in ('all', 'scope-proof'):
        for transition in ('merge', 'split', 'reassign', 'switch'):
            run('scope-proof-' + transition, scope_case, transition)
    if args.case in ('all', 'parser-root'):
        for boundary in ('depth', 'documents'):
            for entry in ('open', 'disk'):
                for failed_first in (False, True):
                    name = 'parser-root-%s-%s-failed-%s' % (boundary, entry, 'first' if failed_first else 'last')
                    run(name, parser_case, boundary, entry, failed_first)
    passed = bool(rows) and any(row['status'] == 'passed' for row in rows) and all(
        row['status'] != 'failed' for row in rows)
    report = {'command': sys.argv, 'binarySHA256': wire.hash_file(wire.BINARY),
              'runnerSHA256': wire.hash_file(pathlib.Path(__file__)),
              'transportSHA256': wire.hash_file(pathlib.Path(wire.__file__)),
              'progressSHA256': wire.hash_file(pathlib.Path(progress.__file__)),
              'python': sys.version, 'platform': sys.platform, 'passed': passed, 'rows': rows}
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
