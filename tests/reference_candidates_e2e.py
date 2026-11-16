#!/usr/bin/env python3
"""Independent current-source candidate review and explicit-navigation consumer.

Expected identities and ranges come from authored fixtures. The consumer imports
only the framed stdio transport and never calls the product's candidate helpers.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import time
import traceback

from support.session import Session

STATUS = 'argocd/analysisStatus'
KIND = 'source.openReferenceCandidate'
COMMAND = 'argocd.openReferenceCandidate'
CONFIG = 'schemaVersion: 1\ntargetVersion: "3.0.0"\nreferencePolicy: closed\n'
APP = '''apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  project: team
  source: {repoURL: https://example.test/repo, path: manifests}
  destination: {server: https://kubernetes.default.svc, namespace: default}
'''
APPSET = '''apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata: {name: applications}
spec:
  generators:
    - list:
        elements: [{name: app}]
        template:
          metadata: {name: generated}
          spec:
            project: team
            source: {repoURL: https://example.test/repo, path: manifests}
            destination: {server: https://kubernetes.default.svc, namespace: default}
  template:
    metadata: {name: base}
    spec:
      project: base
      source: {repoURL: https://example.test/base, path: manifests}
      destination: {server: https://kubernetes.default.svc, namespace: default}
'''

CASE_CHECKS = {
    'unbound-exact-candidates-and-explicit-open': ['reportIdentity', 'exactCandidates', 'completeKnownScope', 'ordinaryDefinitionPreserved', 'referencesRemainUnbound', 'renameRemainsUnbound', 'explicitExplanation', 'oneActionPerCandidate', 'resolveHasNoSideEffects', 'showDocumentExactSelections', 'hostAcknowledgementsAccepted'],
    'readonly-host-explicit-open': ['readOnlyCapability', 'candidateActionAvailable', 'resolveHasNoSideEffects', 'showDocumentExactSelection', 'hostAcknowledgementAccepted'],
    'host-without-show-document': ['candidateActionAvailable', 'resolveHasNoSideEffects', 'locationAndExplanationReturned', 'notClaimedOpened', 'explicitFallbackMessage', 'noShowDocument'],
    'appset-physical-project-candidates': ['reportIdentity', 'exactCandidates', 'ordinaryDefinitionPreserved', 'candidateActionsAvailable'],
    'known-namespace-keeps-original-binding': ['noCandidateReport', 'noCandidateAction', 'namespaceFilteredDefinition', 'referencesRemainAvailable'],
    'dynamic-project-does-not-produce-candidates': ['noCandidateReport', 'noCandidateAction', 'noDefinition'],
    'incomplete-scope-retains-known-candidates': ['reportIdentity', 'knownCandidatesRetained', 'scopeIncomplete', 'notClaimedComplete', 'notTruncatedWithoutLimit'],
    'display-limit-is-explicit': ['atMostTwentyKnownItems', 'displayLimitReached', 'scopeComplete', 'truncated', 'notClaimedComplete'],
    'query-limit-is-explicit': ['atMostTwentyKnownItems', 'singleExactItemBelowDisplayLimit', 'scopeComplete', 'truncated', 'notClaimedComplete'],
    'source-change-refuses-old-action': ['candidateActionAvailable', 'oldResolveIsStale', 'oldExecuteIsStale', 'noLateOpen'],
    'target-change-refuses-old-action': ['candidateActionAvailable', 'oldResolveIsStale', 'oldExecuteIsStale', 'noLateOpen'],
    'config-change-refuses-old-action': ['candidateActionAvailable', 'oldResolveIsStale', 'oldExecuteIsStale', 'noLateOpen'],
    'scope-root-exclusion-boundaries': ['onlyOwnedScopeCandidates', 'onlyOwnedScopeActions', 'ordinaryDefinitionPreserved', 'excludedOpenDoesNotLeak'],
    'same-file-duplicate-declarations': ['reportIdentity', 'exactDuplicateCandidates', 'distinctActionTitles', 'ordinaryDefinitionPreserved', 'exactDuplicateSelections'],
    'quoted-name-crlf-utf16-ranges': ['reportSelectionTokenRange', 'candidateTokenRange', 'ordinaryDefinitionScalarRange', 'exactShownTokenRange'],
    'alias-project-refuses-candidates': ['noCandidateReport', 'noCandidateAction', 'noDefinition'],
    'merge-project-refuses-candidates': ['noCandidateReport', 'noCandidateAction', 'noDefinition'],
    'derived-target-declarations-excluded': ['onlyExactCandidate', 'onlyExactAction', 'ordinaryDefinitionPreserved'],
    'quickfix-filter-excludes-candidates': ['candidateActionControl', 'quickfixRequestSucceeds', 'candidateActionFiltered', 'noOpenSideEffect'],
    'fresh-actions-after-source-target-restoration': ['oldActionControl', 'currentSourceIdentity', 'freshCandidateAction', 'oldActionStillStale', 'freshResolveSucceeds', 'freshExecuteOpensExactTarget'],
    'target-prefix-shift': ['oldActionControl', 'oldResolveIsStale', 'oldExecuteIsStale', 'noOldOpen', 'shiftedCandidateRange', 'newActionAvailable', 'freshResolveHasNoSideEffects', 'freshExecuteOpensShiftedTarget'],
    'disk-config-without-watch-refuses-old-action': ['candidateActionAvailable', 'externalConfigChangeApplied', 'oldResolveIsStale', 'oldExecuteIsStale', 'noLateOpen'],
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project(namespace, name='team'):
    # A non-BMP scalar before the declaration on the same physical line proves
    # that returned selections use UTF-16 columns, not byte or code-point counts.
    return ('apiVersion: argoproj.io/v1alpha1\nkind: AppProject\n'
            'metadata: {annotations: {description: "😀"}, name: ' + name + ', namespace: ' + namespace + '}\n'
            'spec:\n  sourceRepos: ["*"]\n  destinations: [{server: "*", namespace: "*"}]\n')


def position(text, offset):
    prefix = text[:offset]
    return {'line': prefix.count('\n'), 'character': len(prefix.rsplit('\n', 1)[-1].encode('utf-16-le')) // 2}


def location(path, text, offset, length=4):
    return {'uri': path.as_uri(), 'range': {'start': position(text, offset), 'end': position(text, offset + length)}}


def same_locations(actual, expected):
    return isinstance(actual, list) and sorted(json.dumps(x, sort_keys=True) for x in actual) == sorted(json.dumps(x, sort_keys=True) for x in expected)


def stale(response):
    error = response.get('error', {})
    return error.get('code') == -32801 and error.get('data', {}).get('reasonCode') == 'STALE_INPUT'


class Client(Session):
    def __init__(self, out, roots, binary, edit=True, show=True):
        self.edit, self.show = edit, show
        self.host_requests = []
        super().__init__(out, roots, binary=binary, server_request=self.host_request)

    def host_request(self, message):
        self.host_requests.append(message)
        if message['method'] != 'window/showDocument':
            raise ValueError('unexpected server request: ' + message['method'])
        return {'success': True}

    def send(self, method, params=None, ident=None):
        if method == 'initialize':
            params['capabilities']['workspace']['workspaceEdit']['documentChanges'] = self.edit
            params['capabilities']['textDocument'] = {'codeAction': {'codeActionLiteralSupport': {'codeActionKind': {'valueSet': ['source', KIND]}}}}
            params['capabilities']['window'] = {'showDocument': {'support': self.show}}
        return super().send(method, params, ident)


def settled(client, method, params):
    deadline = time.monotonic() + 6
    while True:
        response = client.request(method, params)
        if response.get('error', {}).get('code') != -32801 or time.monotonic() >= deadline:
            return response
        time.sleep(.02)


def candidate_actions(client, query):
    response = settled(client, 'textDocument/codeAction', {
        'textDocument': query['textDocument'],
        'range': {'start': query['position'], 'end': query['position']},
        'context': {'diagnostics': [], 'only': [KIND]},
    })
    actions = response.get('result')
    if not isinstance(actions, list):
        return [], response
    return [a for a in actions if a.get('kind') == KIND and a.get('command', {}).get('command') == COMMAND], response


def barrier(client, query):
    # A later ordinary response also verifies that host acknowledgements did not
    # damage framing or close the server connection.
    return settled(client, 'textDocument/documentSymbol', {'textDocument': query['textDocument']})


def report_identity(report, expected_selection):
    return (isinstance(report, dict) and 'binding unproven' in str(report.get('label', '')).lower()
            and report.get('selection') == expected_selection and report.get('reasonCode') == 'TARGET_UNKNOWN'
            and bool(report.get('message')))


def exact_candidates(items, expected):
    if not isinstance(items, list) or len(items) != len(expected):
        return False
    actual = []
    for item in items:
        if item.get('kind') != 'AppProject' or item.get('name') != 'team' or item.get('reasonCode') != 'TARGET_UNKNOWN' or not item.get('message'):
            return False
        actual.append({'namespace': item.get('namespace'), 'location': item.get('location')})
    return sorted(json.dumps(x, sort_keys=True) for x in actual) == sorted(json.dumps(x, sort_keys=True) for x in expected)


def run_case(name, binary, out):
    folder = out / name
    root = folder / 'workspace'
    root.mkdir(parents=True)
    checks = {key: False for key in CASE_CHECKS[name]}
    checks.update(cleanExit=False, diskUnchanged=False)
    observed = {}
    config = CONFIG + ('controlPlaneNamespace: one\n' if name.startswith('known-namespace') else '')
    text = APPSET if name.startswith('appset-') else APP
    source_raw = 'team'
    if name.startswith('dynamic-'):
        text = text.replace('project: team', 'project: "{{project}}"')
        source_raw = '"{{project}}"'
    elif name.startswith('alias-project-'):
        text = text.replace('metadata:\n  name: app', 'metadata: {name: app, annotations: {selected: &chosen team}}').replace('project: team', 'project: *chosen')
        source_raw = '*chosen'
    elif name.startswith('merge-project-'):
        text = text.replace('spec:\n  project: team', 'spec:\n  <<: {project: team}')
    elif name.startswith('quoted-'):
        text = text.replace('project: team', "project: 'team'").replace('\n', '\r\n')
        source_raw = "'team'"
    path = root / ('set.yaml' if name.startswith('appset-') else 'app.yaml')
    files = {'.argocd-ls.yaml': config, path.name: text}
    expected = []
    count = 21 if name.startswith('display-limit') else 1001 if name.startswith('query-limit') else 1 if name.startswith(('readonly-', 'host-without-', 'source-change-', 'target-change-', 'config-change-', 'quoted-', 'derived-target-', 'quickfix-', 'fresh-actions-', 'target-prefix-', 'disk-config-')) else 2
    if count > 2:
        target = root / 'projects.yaml'
        content = ''
        for i in range(count):
            namespace = f'ns-{i:04}'
            # The index's substring search admits 1001 symbols, while exact
            # name filtering keeps one. This must expose index truncation even
            # though the twenty-item presentation limit never becomes active.
            declared = f'team-old-{i:04}' if name.startswith('query-limit-') and i > 0 else 'team'
            fragment = project(namespace, declared)
            start = len(content) + fragment.index('name: ' + declared) + len('name: ')
            content += fragment + '---\n'
            if declared == 'team':
                expected.append({'namespace': namespace, 'offset': start})
        files[target.name] = content
        expected = [{'namespace': item['namespace'], 'location': location(target, content, item['offset'])} for item in expected]
    else:
        for i, namespace in enumerate(['one', 'two'][:count]):
            target = root / f'project-{i}.yaml'
            content = project(namespace)
            files[target.name] = content
            expected.append({'namespace': namespace, 'location': location(target, content, content.index('name: team') + len('name: '))})
    extra_roots, opened_before_source = [], []
    expected_definitions = None
    if name.startswith('scope-root-'):
        config += 'exclude: ["excluded/**"]\nscopes:\n  - name: apps\n    include: ["apps/**"]\n  - name: other\n    include: ["other/**"]\n'
        path = root / 'apps/app.yaml'
        files = {'.argocd-ls.yaml': config, 'apps/app.yaml': text, 'apps/project.yaml': project('one'),
                 'other/project.yaml': project('other'), 'excluded/project.yaml': project('excluded'),
                 'child/.argocd-ls.yaml': CONFIG, 'child/project.yaml': project('child')}
        target = root / 'apps/project.yaml'
        expected = [{'namespace': 'one', 'location': location(target, files['apps/project.yaml'], files['apps/project.yaml'].index('name: team') + len('name: '))}]
        extra_roots = [root / 'child']
        opened_before_source = ['other/project.yaml', 'excluded/project.yaml', 'child/project.yaml']
    elif name.startswith('same-file-'):
        content = project('one') + '---\n' + project('one')
        target = root / 'duplicates.yaml'
        files = {'.argocd-ls.yaml': config, path.name: text, target.name: content}
        first = content.index('name: team') + len('name: ')
        second = content.index('name: team', first + 4) + len('name: ')
        expected = [{'namespace': 'one', 'location': location(target, content, offset)} for offset in [first, second]]
    elif name.startswith('quoted-'):
        target = root / 'project-0.yaml'
        content = project('one').replace('name: team', 'name: "team"').replace('\n', '\r\n')
        files[target.name] = content
        offset = content.index('name: "team"') + len('name: ')
        expected = [{'namespace': 'one', 'location': location(target, content, offset + 1)}]
        expected_definitions = [location(target, content, offset, 6)]
    elif name.startswith('derived-target-'):
        alias = project('two').replace('metadata: {annotations: {description: "😀"}, name: team, namespace: two}', 'metadata: {annotations: {selected: &chosen team}, name: *chosen, namespace: two}')
        merged = project('three').replace('metadata: {annotations: {description: "😀"}, name: team, namespace: three}', 'metadata: {<<: {name: team, namespace: three}}')
        files['alias.yaml'], files['merge.yaml'] = alias, merged
    if name.startswith('incomplete-'):
        files['broken.yaml'] = 'apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata: [\n'
    for file, content in files.items():
        (root / file).parent.mkdir(parents=True, exist_ok=True)
        (root / file).write_bytes(content.encode('utf-8'))
    initial = {file: digest(root / file) for file in files}
    expected_disk = initial.copy()
    start = text.index('project: ' + source_raw) + len('project: ')
    selection = location(path, text, start + (1 if name.startswith('quoted-') else 0), 4 if name.startswith('quoted-') else len(source_raw))
    query = {'textDocument': {'uri': path.as_uri()}, 'position': position(text, start + 1)}
    (folder / 'expected.json').write_text(json.dumps({'selection': selection, 'candidates': expected, 'ordinaryDefinitions': expected_definitions, 'checks': list(checks)}, indent=2) + '\n')
    client = None
    try:
        client = Client(folder, [root, *extra_roots], binary, edit=name not in ['readonly-host-explicit-open', 'host-without-show-document'], show=name != 'host-without-show-document')
        for opened in opened_before_source:
            client.open(root / opened, files[opened], 1)
            client.diag(root / opened, 1)
        client.open(path, text, 1)
        observed['diagnostics'] = client.diag(path, 1)
        deadline = time.monotonic() + 6
        while True:
            observed['status'] = settled(client, STATUS, query)
            status = observed['status'].get('result') or {}
            if status.get('completeness', {}).get('workspace') or name.startswith('incomplete-') or time.monotonic() >= deadline:
                break
            time.sleep(.02)
        report = status.get('referenceCandidates') or {}
        items = report.get('items', [])
        actions, observed['actionList'] = candidate_actions(client, query)
        expected_locations = [item['location'] for item in expected]
        if name == 'unbound-exact-candidates-and-explicit-open':
            checks['reportIdentity'] = report_identity(report, selection)
            checks['exactCandidates'] = exact_candidates(items, expected)
            checks['completeKnownScope'] = report.get('scopeComplete') is True and report.get('complete') is True and report.get('truncated') is False
            observed['definitions'] = settled(client, 'textDocument/definition', query)
            checks['ordinaryDefinitionPreserved'] = same_locations(observed['definitions'].get('result'), expected_locations)
            observed['references'] = settled(client, 'textDocument/references', query | {'context': {'includeDeclaration': True}})
            checks['referencesRemainUnbound'] = observed['references'].get('result') == []
            observed['rename'] = settled(client, 'textDocument/rename', query | {'newName': 'renamed'})
            checks['renameRemainsUnbound'] = observed['rename'].get('error', {}).get('data', {}).get('reasonCode') == 'TARGET_UNKNOWN'
            explanation = settled(client, 'textDocument/codeAction', {'textDocument': query['textDocument'], 'range': {'start': query['position'], 'end': query['position']}, 'context': {'diagnostics': [], 'only': ['source.argocd.explainAnalysis']}})
            explain_actions = explanation.get('result') or []
            if len(explain_actions) == 1:
                observed['explanation'] = client.request('workspace/executeCommand', explain_actions[0]['command'])
                barrier(client, query)
                returned = (observed['explanation'].get('result') or {}).get('referenceCandidates') or {}
                checks['explicitExplanation'] = report_identity(returned, selection) and exact_candidates(returned.get('items'), expected) and sum(row['message'].get('method') == 'window/showMessage' for row in client.messages) == 1
            checks['oneActionPerCandidate'] = len(actions) == len(expected) and len({a.get('title') for a in actions}) == len(expected) and all('edit' not in a for a in actions)
            before = len(client.host_requests)
            observed['resolved'] = [client.request('codeAction/resolve', action) for action in actions]
            barrier(client, query)
            checks['resolveHasNoSideEffects'] = bool(actions) and all('error' not in response and 'edit' not in (response.get('result') or {}) for response in observed['resolved']) and len(client.host_requests) == before == 0
            observed['executed'] = [client.request('workspace/executeCommand', action['command']) for action in actions]
            observed['afterAcknowledgements'] = barrier(client, query)
            shown = [{'uri': request['params'].get('uri'), 'range': request['params'].get('selection')} for request in client.host_requests]
            checks['showDocumentExactSelections'] = all('error' not in response for response in observed['executed']) and same_locations(shown, expected_locations)
            checks['hostAcknowledgementsAccepted'] = len(client.host_requests) == len(expected) and 'error' not in observed['afterAcknowledgements']
        elif name in ['readonly-host-explicit-open', 'host-without-show-document']:
            checks['candidateActionAvailable'] = len(actions) == 1 and 'edit' not in actions[0]
            if name.startswith('readonly-'):
                initialize = next(row['message']['result'] for row in client.messages if row['message'].get('id') == 1)
                caps = initialize.get('capabilities', {})
                checks['readOnlyCapability'] = 'renameProvider' not in caps and COMMAND in caps.get('executeCommandProvider', {}).get('commands', [])
            if actions:
                observed['resolved'] = client.request('codeAction/resolve', actions[0])
                barrier(client, query)
                checks['resolveHasNoSideEffects'] = 'error' not in observed['resolved'] and 'edit' not in (observed['resolved'].get('result') or {}) and not client.host_requests
                observed['executed'] = client.request('workspace/executeCommand', actions[0]['command'])
                observed['afterAcknowledgement'] = barrier(client, query)
                if name.startswith('readonly-'):
                    shown = [{'uri': request['params'].get('uri'), 'range': request['params'].get('selection')} for request in client.host_requests]
                    checks['showDocumentExactSelection'] = 'error' not in observed['executed'] and same_locations(shown, expected_locations)
                    checks['hostAcknowledgementAccepted'] = 'error' not in observed['afterAcknowledgement'] and len(client.host_requests) == 1
                else:
                    result = observed['executed'].get('result') or {}
                    checks['locationAndExplanationReturned'] = result.get('location') == expected_locations[0] and bool(result.get('message'))
                    checks['notClaimedOpened'] = result.get('opened', False) is False
                    notices = [row['message'].get('params', {}).get('message') for row in client.messages if row['message'].get('method') == 'window/showMessage']
                    checks['explicitFallbackMessage'] = len(notices) == 1 and bool(notices[0])
                    checks['noShowDocument'] = not client.host_requests
        elif name.startswith('appset-'):
            checks['reportIdentity'] = report_identity(report, selection)
            checks['exactCandidates'] = exact_candidates(items, expected)
            observed['definitions'] = settled(client, 'textDocument/definition', query)
            checks['ordinaryDefinitionPreserved'] = same_locations(observed['definitions'].get('result'), expected_locations)
            checks['candidateActionsAvailable'] = len(actions) == len(expected) and all('edit' not in a for a in actions)
        elif name.startswith(('known-namespace-', 'dynamic-', 'alias-project-', 'merge-project-')):
            checks['noCandidateReport'] = not report or not items
            checks['noCandidateAction'] = actions == [] and isinstance(observed['actionList'].get('result'), list)
            observed['definitions'] = settled(client, 'textDocument/definition', query)
            if name.startswith('known-namespace-'):
                checks['namespaceFilteredDefinition'] = same_locations(observed['definitions'].get('result'), expected_locations[:1])
                refs = next((op for op in status.get('operations', []) if op.get('operation') == 'references'), {})
                checks['referencesRemainAvailable'] = refs.get('allowed') is True and refs.get('resultCount') == 1
            else:
                checks['noDefinition'] = observed['definitions'].get('result') == []
        elif name.startswith('incomplete-'):
            checks['reportIdentity'] = report_identity(report, selection)
            checks['knownCandidatesRetained'] = exact_candidates(items, expected)
            checks['scopeIncomplete'] = report.get('scopeComplete') is False
            checks['notClaimedComplete'] = report.get('complete') is False
            checks['notTruncatedWithoutLimit'] = report.get('truncated') is False
        elif name.startswith(('display-limit-', 'query-limit-')):
            known = {json.dumps(item['location'], sort_keys=True) for item in expected}
            checks['atMostTwentyKnownItems'] = isinstance(items, list) and 0 < len(items) <= 20 and all(item.get('kind') == 'AppProject' and item.get('name') == 'team' and json.dumps(item.get('location'), sort_keys=True) in known for item in items)
            if name.startswith('display-'):
                checks['displayLimitReached'] = len(items) == 20
            else:
                checks['singleExactItemBelowDisplayLimit'] = len(items) == 1 and exact_candidates(items, expected)
            checks['scopeComplete'] = report.get('scopeComplete') is True
            checks['truncated'] = report.get('truncated') is True
            checks['notClaimedComplete'] = report.get('complete') is False
        elif name.startswith('scope-root-'):
            checks['onlyOwnedScopeCandidates'] = report_identity(report, selection) and exact_candidates(items, expected)
            checks['onlyOwnedScopeActions'] = len(actions) == 1 and 'edit' not in actions[0]
            observed['definitions'] = settled(client, 'textDocument/definition', query)
            checks['ordinaryDefinitionPreserved'] = same_locations(observed['definitions'].get('result'), expected_locations)
            excluded = [(root / item).as_uri() for item in opened_before_source]
            checks['excludedOpenDoesNotLeak'] = len(items) == 1 and all(item.get('location', {}).get('uri') not in excluded for item in items)
        elif name.startswith(('same-file-', 'quoted-')):
            observed['definitions'] = settled(client, 'textDocument/definition', query)
            if name.startswith('same-file-'):
                checks['reportIdentity'] = report_identity(report, selection)
                checks['exactDuplicateCandidates'] = exact_candidates(items, expected)
                checks['distinctActionTitles'] = len(actions) == 2 and len({action.get('title') for action in actions}) == 2
                checks['ordinaryDefinitionPreserved'] = same_locations(observed['definitions'].get('result'), expected_locations)
            else:
                checks['reportSelectionTokenRange'] = report_identity(report, selection)
                checks['candidateTokenRange'] = exact_candidates(items, expected)
                checks['ordinaryDefinitionScalarRange'] = same_locations(observed['definitions'].get('result'), expected_definitions)
            observed['executed'] = [client.request('workspace/executeCommand', action['command']) for action in actions]
            observed['afterAcknowledgement'] = barrier(client, query)
            shown = [{'uri': request['params'].get('uri'), 'range': request['params'].get('selection')} for request in client.host_requests]
            check = 'exactDuplicateSelections' if name.startswith('same-file-') else 'exactShownTokenRange'
            checks[check] = bool(actions) and all('error' not in response for response in observed['executed']) and same_locations(shown, expected_locations) and 'error' not in observed['afterAcknowledgement']
        elif name.startswith('derived-target-'):
            checks['onlyExactCandidate'] = report_identity(report, selection) and exact_candidates(items, expected)
            checks['onlyExactAction'] = len(actions) == 1 and 'edit' not in actions[0]
            observed['definitions'] = settled(client, 'textDocument/definition', query)
            checks['ordinaryDefinitionPreserved'] = same_locations(observed['definitions'].get('result'), expected_locations)
        elif name.startswith('quickfix-'):
            checks['candidateActionControl'] = len(actions) == 1 and exact_candidates(items, expected)
            observed['quickfixOnly'] = settled(client, 'textDocument/codeAction', {'textDocument': query['textDocument'], 'range': {'start': query['position'], 'end': query['position']}, 'context': {'diagnostics': [], 'only': ['quickfix']}})
            fixes = observed['quickfixOnly'].get('result')
            checks['quickfixRequestSucceeds'] = isinstance(fixes, list)
            checks['candidateActionFiltered'] = isinstance(fixes, list) and not any(action.get('kind') == KIND or action.get('command', {}).get('command') == COMMAND for action in fixes)
            barrier(client, query)
            checks['noOpenSideEffect'] = not client.host_requests
        elif name.startswith('fresh-actions-'):
            checks['oldActionControl'] = len(actions) == 1
            if actions:
                old = actions[0]
                target = root / 'project-0.yaml'
                client.change(path, text + '# new source\n', 2)
                client.diag(path, 2)
                client.open(target, files[target.name] + '# new target\n', 1)
                client.diag(target, 1)
                client.change(target, files[target.name], 2)
                client.diag(target, 2)
                client.change(path, text, 3)
                client.diag(path, 3)
                observed['restoredStatus'] = settled(client, STATUS, query)
                restored = observed['restoredStatus'].get('result') or {}
                checks['currentSourceIdentity'] = restored.get('inputIdentity', {}).get('version') == 3 and restored.get('inputIdentity', {}).get('sha256') == hashlib.sha256(text.encode()).hexdigest()
                current, observed['freshActionList'] = candidate_actions(client, query)
                checks['freshCandidateAction'] = len(current) == 1 and exact_candidates((restored.get('referenceCandidates') or {}).get('items'), expected)
                observed['oldExecuteAfterRestore'] = client.request('workspace/executeCommand', old['command'])
                checks['oldActionStillStale'] = stale(observed['oldExecuteAfterRestore'])
                if current:
                    observed['freshResolve'] = client.request('codeAction/resolve', current[0])
                    checks['freshResolveSucceeds'] = 'error' not in observed['freshResolve'] and 'edit' not in (observed['freshResolve'].get('result') or {})
                    observed['freshExecute'] = client.request('workspace/executeCommand', current[0]['command'])
                    observed['afterAcknowledgement'] = barrier(client, query)
                    shown = [{'uri': request['params'].get('uri'), 'range': request['params'].get('selection')} for request in client.host_requests]
                    checks['freshExecuteOpensExactTarget'] = 'error' not in observed['freshExecute'] and same_locations(shown, expected_locations) and 'error' not in observed['afterAcknowledgement']
        elif name == 'target-prefix-shift':
            checks['oldActionControl'] = len(actions) == 1
            if actions:
                target = root / 'project-0.yaml'
                shifted = '# inserted before the declaration in an unsaved buffer\n' + files[target.name]
                shifted_location = location(target, shifted, shifted.index('name: team') + len('name: '))
                shifted_expected = [{'namespace': 'one', 'location': shifted_location}]
                observed['expectedShiftedLocation'] = shifted_location
                client.open(target, shifted, 1)
                client.diag(target, 1)
                observed['oldResolve'] = client.request('codeAction/resolve', actions[0])
                observed['oldExecute'] = client.request('workspace/executeCommand', actions[0]['command'])
                barrier(client, query)
                checks['oldResolveIsStale'] = stale(observed['oldResolve'])
                checks['oldExecuteIsStale'] = stale(observed['oldExecute'])
                checks['noOldOpen'] = not client.host_requests
                observed['shiftedStatus'] = settled(client, STATUS, query)
                shifted_report = (observed['shiftedStatus'].get('result') or {}).get('referenceCandidates') or {}
                checks['shiftedCandidateRange'] = exact_candidates(shifted_report.get('items'), shifted_expected)
                current, observed['freshActionList'] = candidate_actions(client, query)
                checks['newActionAvailable'] = len(current) == 1
                if current:
                    observed['freshResolve'] = client.request('codeAction/resolve', current[0])
                    barrier(client, query)
                    checks['freshResolveHasNoSideEffects'] = 'error' not in observed['freshResolve'] and 'edit' not in (observed['freshResolve'].get('result') or {}) and not client.host_requests
                    observed['freshExecute'] = client.request('workspace/executeCommand', current[0]['command'])
                    observed['afterAcknowledgement'] = barrier(client, query)
                    shown = [{'uri': request['params'].get('uri'), 'range': request['params'].get('selection')} for request in client.host_requests]
                    checks['freshExecuteOpensShiftedTarget'] = 'error' not in observed['freshExecute'] and same_locations(shown, [shifted_location]) and 'error' not in observed['afterAcknowledgement']
        elif name.startswith('disk-config-'):
            checks['candidateActionAvailable'] = len(actions) == 1
            if actions:
                after = config + 'controlPlaneNamespace: one\n'
                config_path = root / '.argocd-ls.yaml'
                config_path.write_bytes(after.encode())
                expected_disk['.argocd-ls.yaml'] = digest(config_path)
                observed['externalConfigChange'] = {'before': config, 'after': after, 'watchNotificationSent': False}
                checks['externalConfigChangeApplied'] = initial['.argocd-ls.yaml'] != expected_disk['.argocd-ls.yaml']
                # Neither a watcher nor an intervening analysis query discovers
                # this change for the command: resolve/execute must reacquire it.
                observed['oldResolve'] = client.request('codeAction/resolve', actions[0])
                observed['oldExecute'] = client.request('workspace/executeCommand', actions[0]['command'])
                barrier(client, query)
                checks['oldResolveIsStale'] = stale(observed['oldResolve'])
                checks['oldExecuteIsStale'] = stale(observed['oldExecute'])
                checks['noLateOpen'] = not client.host_requests
        else:
            checks['candidateActionAvailable'] = len(actions) == 1
            if actions:
                action = actions[0]
                if name.startswith('source-'):
                    client.change(path, text + '# changed source\n', 2)
                    client.diag(path, 2)
                elif name.startswith('target-'):
                    target = root / 'project-0.yaml'
                    client.open(target, files[target.name] + '# changed target\n', 1)
                    client.diag(target, 1)
                else:
                    config_path = root / '.argocd-ls.yaml'
                    client.open(config_path, config + 'controlPlaneNamespace: one\n', 1)
                    settled(client, STATUS, query)
                observed['oldResolve'] = client.request('codeAction/resolve', action)
                observed['oldExecute'] = client.request('workspace/executeCommand', action['command'])
                barrier(client, query)
                checks['oldResolveIsStale'] = stale(observed['oldResolve'])
                checks['oldExecuteIsStale'] = stale(observed['oldExecute'])
                checks['noLateOpen'] = not client.host_requests
        observed['hostRequests'] = client.host_requests
    except Exception:
        observed['exception'] = traceback.format_exc()
    finally:
        if client is not None:
            try:
                client.close()
                checks['cleanExit'] = client.proc.returncode == 0 and client.lifecycle.get('shutdownResponse') is True and client.lifecycle.get('exitSent') is True
            except Exception:
                observed['closeException'] = traceback.format_exc()
        checks['diskUnchanged'] = all((root / file).is_file() and digest(root / file) == value for file, value in expected_disk.items())
    row = {'id': name, 'assertions': checks, 'observed': observed, 'inputSHA256': initial, 'passed': all(checks.values())}
    (folder / 'result.json').write_text(json.dumps(row, indent=2, ensure_ascii=False) + '\n')
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    binary, output = args.binary.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    report = {'command': sys.argv, 'binaryCommand': [str(binary), 'serve', '--stdio'], 'binarySHA256': digest(binary), 'runnerSHA256': digest(Path(__file__)), 'transportSHA256': digest(Path(__file__).parent / 'support/session.py'), 'environment': {'python': sys.version, 'platform': platform.platform()}, 'rows': rows, 'passed': False}
    for name in CASE_CHECKS:
        rows.append(run_case(name, binary, output))
        report['passed'] = len(rows) == len(CASE_CHECKS) and all(row['passed'] for row in rows)
        (output / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        failed = [key for key, value in rows[-1]['assertions'].items() if not value]
        print(json.dumps({'id': name, 'passed': rows[-1]['passed'], 'failed': failed}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
