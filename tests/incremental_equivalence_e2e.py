#!/usr/bin/env python3
"""Compare real incremental sessions with fresh final-state sessions."""
import argparse
import hashlib
import json
import os
import random
from pathlib import Path
import subprocess
import sys
import time

import audit_repair_e2e as wire
from persistent_workspace_e2e import Client as TraceClient, wait_until


def cancellation_client(folder, roots):
    # Only this history needs live worker lifecycle evidence. Other histories
    # keep the ordinary client and the caller's original tracing setting.
    previous = os.environ.get('ARGOCD_LS_TRACE')
    os.environ['ARGOCD_LS_TRACE'] = '1'
    try:
        return TraceClient(folder, roots)
    finally:
        if previous is None:
            os.environ.pop('ARGOCD_LS_TRACE', None)
        else:
            os.environ['ARGOCD_LS_TRACE'] = previous


def cancelled_worker_release(client, ident):
    def released():
        # Ignore the last possibly partial line, not malformed complete records.
        for line in b''.join(client.stderr_chunks).split(b'\n')[:-1]:
            if not line.startswith(b'argocd-work '):
                continue
            row = json.loads(line[len(b'argocd-work '):])
            if (row.get('origin') == 'request' and row.get('requestId') == ident
                    and row.get('method') == 'textDocument/codeAction'
                    and row.get('event') == 'worker-released'):
                return row
    return wait_until(client, released, 5, 'cancelled edit worker releases its slot')


def normalized(value, roots):
    if isinstance(value,dict):return {normalized(key,roots):normalized(item,roots) for key,item in value.items()}
    if isinstance(value,list):return [normalized(item,roots) for item in value]
    if isinstance(value,str):
        for index,root in enumerate(roots):value=value.replace(root.as_uri(),'root'+str(index))
    return value


def text_position(text, index):
    prefix = text[:index]
    return {'line': prefix.count('\n'), 'character': len(prefix.rsplit('\n', 1)[-1].encode('utf-16-le')) // 2}


def edit_offset(text, point):
    """Client-side UTF-16 application, rejecting invalid or split-surrogate ranges."""
    line, column = point.get('line'), point.get('character')
    if type(line) is not int or type(column) is not int or line < 0 or column < 0:
        raise ValueError('Edit position is not a nonnegative integer pair')
    lines = text.split('\n')
    if line >= len(lines):
        raise ValueError('Edit line is outside the document')
    content = lines[line].removesuffix('\r')
    units = content.encode('utf-16-le')
    if column * 2 > len(units):
        raise ValueError('Edit column is outside the line')
    prefix = units[:column * 2].decode('utf-16-le')
    return sum(len(part) + 1 for part in lines[:line]) + len(prefix)


def apply_text_edits(text, edits):
    if not isinstance(edits, list) or not edits:
        raise ValueError('An actual nonempty edit is required')
    replacements = []
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get('newText'), str):
            raise ValueError('Malformed text edit')
        span = edit.get('range', {})
        start, end = edit_offset(text, span.get('start', {})), edit_offset(text, span.get('end', {}))
        if start > end:
            raise ValueError('Reversed text edit')
        replacements.append((start, end, edit['newText']))
    replacements.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(replacements, replacements[1:]):
        if previous[1] > current[0] or previous[0] == current[0]:
            raise ValueError('Overlapping or ambiguous same-position edits')
    result = text
    for start, end, replacement in reversed(replacements):
        result = result[:start] + replacement + result[end:]
    if result == text:
        raise ValueError('The advertised edit made no change')
    return result


def apply_document_changes(edit, texts, versions, expected_uris):
    if not isinstance(edit, dict) or 'changes' in edit:
        raise ValueError('Expected a versioned WorkspaceEdit')
    changes = edit.get('documentChanges')
    if not isinstance(changes, list) or not changes:
        raise ValueError('WorkspaceEdit has no document changes')
    seen, result = set(), dict(texts)
    for change in changes:
        document = change.get('textDocument', {})
        uri = document.get('uri')
        if uri not in expected_uris or uri in seen or 'kind' in change:
            raise ValueError('Edit named an unexpected/repeated document or resource operation')
        if type(document.get('version')) is not int or document != {'uri': uri, 'version': versions[uri]}:
            raise ValueError('Edit does not name the currently accepted version')
        seen.add(uri)
        result[uri] = apply_text_edits(texts[uri], change.get('edits'))
    if seen != expected_uris:
        raise ValueError('Edit omitted an expected affected document')
    return result


def settled_request(client, method, params):
    deadline = time.monotonic() + 3
    while True:
        response = client.request(method, params)
        if response.get('error', {}).get('code') != -32801 or time.monotonic() >= deadline:
            if 'error' in response:
                raise ValueError('Current operation failed: ' + repr(response))
            return response['result']
        time.sleep(.02)


def applied_edit_session(folder, operation, incremental):
    """Expected complete texts are fixed before asking the real server for edits."""
    root = folder / 'workspace'
    root.mkdir(parents=True)
    project = ('apiVersion: argoproj.io/v1alpha1\nkind: AppProject\n'
               'metadata: {annotations: {note: "😀"}, name: team, namespace: argocd}\n'
               'spec:\n  sourceRepos: ["*"]\n  destinations: [{server: "*", namespace: "*"}]\n').replace('\n', '\r\n')
    complete_app = ('apiVersion: argoproj.io/v1alpha1\nkind: Application\n'
                    'metadata: {name: current}\n'
                    'spec: {project: team, source: {repoURL: "https://example.org/😀"}, destination: {server: "https://kubernetes.default.svc", namespace: default}}\n').replace('\n', '\r\n')
    app = complete_app if operation == 'rename' else complete_app.replace('destination:', 'dest:')
    sources = {'left/project.yaml': project, 'left/app.yaml': app,
               'left/another.yaml': wire.APP.replace('name: app', 'name: another').replace('\n', '\r\n'),
               'right/project.yaml': project, 'right/app.yaml': complete_app}
    config = wire.CONFIG.replace('3.0.0', '3.5.6') + 'scopes:\n  - name: left\n    include: ["left/**"]\n  - name: right\n    include: ["right/**"]\n'
    (root / '.argocd-ls.yaml').write_bytes(config.encode())
    paths = {name: root / name for name in sources}
    for name, text in sources.items():
        paths[name].parent.mkdir(parents=True, exist_ok=True)
        paths[name].write_bytes(text.encode())
    expected = dict(sources)
    if operation == 'rename':
        expected['left/project.yaml'] = project.replace('name: team', 'name: "platform"')
        for name in ('left/app.yaml', 'left/another.yaml'):
            expected[name] = sources[name].replace('project: team', 'project: "platform"')
    else:
        expected['left/app.yaml'] = complete_app
    # This file is written before any initialize/query/edit response exists.
    (folder / 'independent-oracle.json').write_text(json.dumps({'before': sources, 'expectedAfter': expected}, ensure_ascii=False, indent=2) + '\n')
    versions = {path.as_uri(): 7 + 2 * index for index, path in enumerate(paths.values())}
    texts = {paths[name].as_uri(): text for name, text in sources.items()}
    expected_texts = {paths[name].as_uri(): text for name, text in expected.items()}
    app_path, app_uri = paths['left/app.yaml'], paths['left/app.yaml'].as_uri()
    selected = {'textDocument': {'uri': app_uri}}
    observed, checks = {}, {}
    client = wire.LSP(folder, [root])
    try:
        if incremental:
            old = complete_app.replace('project: team', 'project: temporarily-missing')
            client.open(app_path, old, 1)
            client.diag(app_path, 1)
        for name, path in paths.items():
            if incremental and path == app_path:
                client.change(path, sources[name], versions[path.as_uri()])
            else:
                client.open(path, sources[name], versions[path.as_uri()])
            client.diag(path, versions[path.as_uri()])
        point = text_position(app, app.index('project: team') + len('project: '))
        binding = selected | {'position': point}
        before_definition = settled_request(client, 'textDocument/definition', binding)
        if len(before_definition) != 1 or before_definition[0]['uri'] != paths['left/project.yaml'].as_uri():
            raise ValueError('Initial reference did not bind to the left scope Project')
        # Warm the same real query before collecting the edit in both sessions.
        if settled_request(client, 'textDocument/definition', binding) != before_definition:
            raise ValueError('Unchanged definition query was not repeatable')
        observed['beforeDefinition'] = before_definition
        if operation == 'rename':
            prepared = settled_request(client, 'textDocument/prepareRename', binding)
            if (prepared or {}).get('placeholder') != 'team':
                raise ValueError('Project rename preparation did not select team')
            edit = settled_request(client, 'textDocument/rename', binding | {'newName': 'platform'})
            expected_uris = {paths[name].as_uri() for name in ('left/project.yaml', 'left/app.yaml', 'left/another.yaml')}
            applied = apply_document_changes(edit, texts, versions, expected_uris)
            if sum(len(change['edits']) for change in edit['documentChanges']) != 3:
                raise ValueError('Project rename did not edit exactly one declaration and two uses')
            observed['workspaceEdit'] = edit
        else:
            position = text_position(app, app.index('dest:') + len('dest'))
            items = settled_request(client, 'textDocument/completion', selected | {'position': position})
            candidates = [item for item in items if item.get('label') == 'destination']
            if len(candidates) != 1 or candidates[0].get('insertTextFormat', 1) != 1 or candidates[0].get('additionalTextEdits'):
                raise ValueError('Expected exactly one plain destination field completion')
            edit = candidates[0].get('textEdit')
            if not isinstance(edit, dict) or 'range' not in edit:
                raise ValueError('Completion omitted its explicit text range')
            if app[edit_offset(app, edit['range']['start']):edit_offset(app, edit['range']['end'])] != 'dest':
                raise ValueError('Completion did not select precisely the unfinished key')
            applied = dict(texts)
            applied[app_uri] = apply_text_edits(app, [edit])
            expected_uris = {app_uri}
            observed['completionEdit'] = edit
        if applied != expected_texts:
            raise ValueError('Applied output differs from the predetermined complete texts')
        checks['nonempty_current_edits'] = True
        checks['exact_expected_texts'] = True
        checks['utf16_crlf_preserved'] = all('😀' in applied[paths[name].as_uri()] for name in ('left/project.yaml', 'left/app.yaml')) and all(applied[uri].count('\r\n') == texts[uri].count('\r\n') for uri in expected_uris)
        checks['other_scope_unchanged'] = all(applied[paths[name].as_uri()] == sources[name] for name in ('right/project.yaml', 'right/app.yaml'))
        # Publish the selected caller last, after all affected declaration bytes.
        for uri in sorted(expected_uris - {app_uri}) + [app_uri]:
            versions[uri] += 1
            path = next(path for path in paths.values() if path.as_uri() == uri)
            client.change(path, applied[uri], versions[uri])
        diagnostics = client.diag(app_path, versions[app_uri])
        if diagnostics:
            raise ValueError('Applied valid document has unexpected diagnostics: ' + repr(diagnostics))
        checks['applied_diagnostics_clear'] = True
        current = applied[app_uri]
        token = '"platform"' if operation == 'rename' else 'team'
        at = current.index('project: ' + token) + len('project: ') + (1 if operation == 'rename' else 0)
        query = selected | {'position': text_position(current, at)}
        definition = settled_request(client, 'textDocument/definition', query)
        references = settled_request(client, 'textDocument/references', query | {'context': {'includeDeclaration': True}})
        left_uris = {paths[name].as_uri() for name in ('left/project.yaml', 'left/app.yaml', 'left/another.yaml')}
        if len(definition) != 1 or definition[0]['uri'] != paths['left/project.yaml'].as_uri() or {item['uri'] for item in references} != left_uris or len(references) != 3:
            raise ValueError('Edited binding lost a use or crossed the selected scope')
        other = paths['right/app.yaml']
        other_query = {'textDocument': {'uri': other.as_uri()}, 'position': text_position(complete_app, complete_app.index('project: team') + len('project: '))}
        other_definition = settled_request(client, 'textDocument/definition', other_query)
        if len(other_definition) != 1 or other_definition[0]['uri'] != paths['right/project.yaml'].as_uri():
            raise ValueError('Same-spelled Project in the right scope lost its binding')
        checks['bindings_remain_scope_local'] = True
        if operation == 'field':
            # Independently apply the corresponding resolved quick fix as well.
            versions[app_uri] += 1
            client.change(app_path, app, versions[app_uri])
            bad_diagnostics = client.diag(app_path, versions[app_uri])
            actions = settled_request(client, 'textDocument/codeAction', selected | {'range': {'start': position, 'end': position}, 'context': {'diagnostics': bad_diagnostics}})
            candidates = [action for action in actions if action.get('kind', '').startswith('quickfix') and 'destination' in action.get('title', '')]
            if len(candidates) != 1:
                raise ValueError('Expected one independent destination field repair')
            resolved = settled_request(client, 'codeAction/resolve', candidates[0])
            fixed = apply_document_changes(resolved.get('edit'), texts, versions, {app_uri})
            if fixed != expected_texts:
                raise ValueError('Resolved quick fix differs from the predetermined complete texts')
            versions[app_uri] += 1
            client.change(app_path, fixed[app_uri], versions[app_uri])
            if client.diag(app_path, versions[app_uri]):
                raise ValueError('Resolved repair did not clear the intended diagnostic')
            if settled_request(client, 'textDocument/definition', query) != definition or settled_request(client, 'textDocument/references', query | {'context': {'includeDeclaration': True}}) != references or settled_request(client, 'textDocument/definition', other_query) != other_definition:
                raise ValueError('Applied quick fix did not preserve the independently checked scope-local bindings')
            checks['quickfix_bindings_valid'] = True
            observed['quickfixEdit'] = resolved['edit']
            checks['quickfix_expected_texts'] = True
        checks['disk_bytes_unchanged'] = all(path.read_bytes() == sources[name].encode() for name, path in paths.items()) and (root / '.argocd-ls.yaml').read_bytes() == config.encode()
        if not all(checks.values()):
            raise ValueError('An independent applied edit check failed: ' + repr(checks))
        observed.update(checks=checks, appliedTexts=applied, definitionAfter=definition, referencesAfter=references, otherScopeDefinition=other_definition)
        (folder / 'applied-observation.json').write_text(json.dumps(observed, ensure_ascii=False, indent=2) + '\n')
        return normalized(observed, [root])
    finally:
        client.close()
        if client.proc.returncode != 0:
            raise ValueError('Applied edit session did not shut down cleanly')


def observe(client, app, revision, roots):
    diagnostics=client.diag(app,revision)
    position={'line':5,'character':12}
    result={'version':revision,'diagnostics':diagnostics}
    for method,extra in [('definition',{}),('references',{'context':{'includeDeclaration':True}}),('completion',{})]:
        response=client.request('textDocument/'+method,{'textDocument':{'uri':app.as_uri()},'position':position,**extra})
        if 'error' in response:raise ValueError('Incomplete query: '+repr(response))
        result[method]=response['result']
    actions=client.request('textDocument/codeAction',{'textDocument':{'uri':app.as_uri()},'range':{'start':{'line':5,'character':2},'end':{'line':5,'character':6}},'context':{'diagnostics':diagnostics}})
    if 'error' in actions:raise ValueError('Code-action state is incomplete')
    result['actions']=[]
    for action in actions.get('result',[]):
        resolved=client.request('codeAction/resolve',action)
        if 'error' in resolved:raise ValueError('Current action could not resolve')
        result['actions'].append({'title':action['title'],'kind':action.get('kind'),'edit':resolved['result'].get('edit')})
    return normalized(result,roots)


def session(binary,folder,case,incremental,seed):
    folder.mkdir(parents=True);roots=[folder/'root-a',folder/'root-b']
    for root in roots:
        root.mkdir();wire.write(root,'.argocd-ls.yaml',wire.CONFIG);wire.write(root,'project.yaml',wire.PROJECT);wire.write(root,'app.yaml',wire.APP)
    app=roots[0]/'app.yaml';project=roots[0]/'project.yaml';final=wire.APP;revision=10
    if case=='overlay':final=wire.APP.replace('project: team','project: missing')
    if case=='edit-candidates':final=wire.APP.replace('project: team','proj: team')
    if case=='delete':project.unlink()
    traced_cancel = incremental and case == 'cancel-recovery'
    cancellation = None
    client = cancellation_client(folder, roots) if traced_cancel else wire.LSP(folder, roots)
    try:
        if incremental:
            client.open(app,wire.APP,1);client.diag(app,1)
            if case in ('edit-undo','overlay'):
                client.change(app,wire.APP.replace('project: team','project: missing'),2);client.diag(app,2)
            if case=='edit-candidates':
                client.change(app,final,2);diagnostics=client.diag(app,2)
                previous=client.request('textDocument/codeAction',{'textDocument':{'uri':app.as_uri()},'range':{'start':{'line':5,'character':2},'end':{'line':5,'character':6}},'context':{'diagnostics':diagnostics}})['result']
                if not previous:raise ValueError('Independent edit candidate control is empty')
            if case=='cancel-recovery':
                large='---\n'.join(wire.APP.replace('name: app','name: app'+str(i)).replace('project: team','proj: team') for i in range(60))
                client.change(app,large,2);client.diag(app,2)
                client.send('textDocument/codeAction',{'textDocument':{'uri':app.as_uri()},'range':{'start':{'line':5,'character':2},'end':{'line':5,'character':6}},'context':{'diagnostics':[]}},200)
                cancellation = {'checks': {'cancel_reply_within_200ms': False,
                                           'immediate_read_is_current': False,
                                           'worker_released_within_5s': False,
                                           'single_cancel_reply': False}}
                cancel_started = time.monotonic()
                client.send('$/cancelRequest',{'id':200})
                cancelled=client.wait(lambda message:message.get('id')==200,.2)
                reply_seconds = time.monotonic() - cancel_started
                cancellation.update(reply=cancelled, replySeconds=reply_seconds)
                cancellation['checks']['cancel_reply_within_200ms'] = cancelled.get('error',{}).get('code') == -32800 and reply_seconds <= .2
                if not cancellation['checks']['cancel_reply_within_200ms']:
                    raise ValueError('No in-flight cancellation within 200ms')
                # A read-lane query remains immediate. References and edits use
                # the one-worker edit lane, which cancellation replies do not free.
                hover = client.request('textDocument/hover', {'textDocument': {'uri': app.as_uri()},
                                       'position': text_position(large, large.index('repoURL'))})
                cancellation['immediateHover'] = hover
                cancellation['checks']['immediate_read_is_current'] = 'error' not in hover and 'Type: string' in json.dumps(hover.get('result')) and 'Target: 3.0.0' in json.dumps(hover.get('result'))
                if not cancellation['checks']['immediate_read_is_current']:
                    raise ValueError('Immediate read after cancellation did not describe the current input')
                released = cancelled_worker_release(client, 200)
                cancellation['workerRelease'] = released
                release_ns = released.get('timing', {}).get('cancelToReleaseNs')
                cancellation['checks']['worker_released_within_5s'] = type(release_ns) is int and 0 <= release_ns <= 5_000_000_000
                if not cancellation['checks']['worker_released_within_5s']:
                    raise ValueError('Cancelled edit worker did not release within the existing 5s budget')
            if case=='delete':
                # Restore then remove inside the live session, not only before startup.
                project.write_text(wire.PROJECT)
                client.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':project.as_uri(),'type':1}]})
                client.change(app,wire.APP,2);client.diag(app,2)
                project.unlink();client.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':project.as_uri(),'type':3}]})
            if case=='target-roundtrip':
                config=roots[0]/'.argocd-ls.yaml'
                for index,target in enumerate(['3.5.6','3.0.0'],2):
                    config.write_text(wire.CONFIG.replace('3.0.0',target))
                    client.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':config.as_uri(),'type':2}]})
                    version_text=wire.APP.replace('  source: {repoURL: https://example.org/repo}', '  sources: [{repoURL: a, ref: values, helm: {valueFiles: ["$values/v.yaml"]}}]')
                    client.change(app,version_text,index);version_diagnostics=client.diag(app,index)
                    if [d['code'] for d in version_diagnostics] != ([] if target=='3.5.6' else ['CD_VERSION_FEATURE']):raise ValueError('Target A/B/A independent semantic oracle failed')
            if case=='repeated-events':
                for _ in range(3):client.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':app.as_uri(),'type':2}]})
            if case=='seeded-undo':
                randomizer=random.Random(seed)
                for index in range(2,6):
                    client.change(app,wire.APP+'# seed '+str(randomizer.randrange(100000))+'\n',index);client.diag(app,index)
            client.change(app,final,revision)
        else:client.open(app,final,revision)
        observed=observe(client,app,revision,roots)
        other=roots[1]/'app.yaml';client.open(other,wire.APP,revision);observed['otherRoot']=observe(client,other,revision,roots)
        if 'root1/project.yaml' not in json.dumps(observed['otherRoot']['definition']):raise ValueError('Second root navigation escaped ownership')
        if case=='edit-candidates':
            if not observed['actions']:raise ValueError('Final edit candidates were not restored')
            if incremental and client.request('codeAction/resolve',previous[0]).get('error',{}).get('code')!=-32801:raise ValueError('Old edit token remained valid')
        # Run the CLI against the effective overlay, independently of LSP response shape.
        app.write_text(final)
        command=[str(binary),'check','--root',str(roots[0]),'--format','json']
        result=subprocess.run(command,capture_output=True,timeout=20)
        (folder/'cli.stdout').write_bytes(result.stdout);(folder/'cli.stderr').write_bytes(result.stderr)
        report=json.loads(result.stdout)
        if report.get('analysisComplete') is not True or result.returncode not in (0,1):raise ValueError('Final state is incomplete')
        observed['analysisComplete']=True
        expected_codes=['CD_PROJECT_UNRESOLVED'] if case in ('overlay','delete') else ['SCHEMA_REQUIRED','SCHEMA_UNKNOWN_FIELD'] if case=='edit-candidates' else []
        if sorted(d['code'] for d in observed['diagnostics'])!=sorted(expected_codes):raise ValueError('Independent diagnostics oracle failed')
        if case not in ('overlay','delete','edit-candidates') and 'root0/project.yaml' not in json.dumps(observed['definition']):raise ValueError('Navigation escaped root ownership')
        return observed
    finally:
        client.close()
        if cancellation is not None:
            cancellation['checks']['single_cancel_reply'] = sum(row['message'].get('id') == 200 for row in client.messages) == 1
            (folder / 'cancellation-observation.json').write_text(json.dumps(cancellation, indent=2) + '\n')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('binary',type=Path);parser.add_argument('output',type=Path);parser.add_argument('--seed',type=int,default=20261114);args=parser.parse_args()
    wire.BINARY=args.binary.resolve();args.output=args.output.resolve();args.output.mkdir(parents=True,exist_ok=False);rows=[]
    for case in ('edit-undo','overlay','delete','target-roundtrip','repeated-events','root-isolation','seeded-undo','cancel-recovery','edit-candidates'):
        row={'id':case,'passed':False}
        try:
            a=session(wire.BINARY,args.output/(case+'-incremental'),case,True,args.seed)
            b=session(wire.BINARY,args.output/(case+'-fresh'),case,False,args.seed)
            row.update(incremental=a,fresh=b,passed=a==b)
            if case == 'cancel-recovery':
                cancellation = json.loads((args.output / (case+'-incremental') / 'cancellation-observation.json').read_text())
                row['assertions'] = cancellation['checks'] | {'semantics_match_fresh': a == b}
                row['passed'] = all(row['assertions'].values())
        except Exception as error:row['error']=repr(error)
        rows.append(row)
        (args.output/'report.json').write_text(json.dumps({'rows':rows,'passed':False},indent=2)+'\n')
    for case, operation in [('applied-project-rename', 'rename'), ('applied-field-edits', 'field')]:
        row = {'id': case, 'passed': False}
        try:
            a = applied_edit_session(args.output / (case + '-incremental'), operation, True)
            b = applied_edit_session(args.output / (case + '-fresh'), operation, False)
            assertions = {name: a['checks'].get(name) is True and b['checks'].get(name) is True for name in a['checks']}
            assertions['incremental_matches_fresh'] = a == b
            row.update(incremental=a, fresh=b, assertions=assertions, passed=bool(assertions) and all(assertions.values()))
        except Exception as error:
            row['error'] = repr(error)
        rows.append(row)
        (args.output/'report.json').write_text(json.dumps({'rows':rows,'passed':False},indent=2)+'\n')
    report={'schemaVersion':1,'binarySHA256':hashlib.sha256(wire.BINARY.read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'seed':args.seed,'command':sys.argv,'cacheEvidence':'Semantic repeatability only; work-count cache proof remains in incremental_performance_e2e.py.',
            'cancellationEvidence':'cancel-recovery requires -32800 within 200ms, an immediate current read and the cancelled edit worker release within 5s before restoring final state and comparing a fresh session; capacity errors are not retried or accepted as results.',
            'appliedEditEvidence':'Project rename and field completion/repair are applied with strict test-owned UTF-16/version/range checks to predetermined full CRLF texts; same-spelled other-scope bindings and disk bytes remain unchanged.',
            'rows':rows,'passed':all(row['passed'] for row in rows)}
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'passed':report['passed'],'failures':[(r['id'],r.get('error')) for r in rows if not r['passed']]}));return 0 if report['passed'] else 1
if __name__=='__main__':sys.exit(main())
