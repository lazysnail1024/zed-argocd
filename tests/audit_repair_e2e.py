#!/usr/bin/env python3
"""Independent CLI/stdin-stdout scope, edit and cancellation contracts."""
import argparse, hashlib, json, os, pathlib, queue, subprocess, sys, threading, time

HERE = pathlib.Path(__file__).resolve().parent
BINARY = pathlib.Path(os.environ.get('CD_AUDIT_BINARY', str(HERE.parent / 'build/argocd-ls'))).resolve()
OUT = pathlib.Path(os.environ.get('CD_AUDIT_OUTPUT', str(HERE.parent / 'artifacts/audit-repair'))).resolve()
CONFIG = 'schemaVersion: 1\ntargetVersion: "3.0.0"\ncontrolPlaneNamespace: argocd\nreferencePolicy: closed\n'
APP = 'apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: app\nspec:\n  project: team\n  source: {repoURL: https://example.org/repo}\n  destination: {server: https://kubernetes.default.svc, namespace: default}\n'
PROJECT = 'apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata:\n  name: team\n  namespace: argocd\nspec:\n  sourceRepos: ["*"]\n  destinations: [{server: "*", namespace: "*"}]\n'

def write(root, name, text):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')
    return p

def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def setup(name, config=CONFIG):
    out = OUT / name
    root = out / 'workspace'
    root.mkdir(parents=True, exist_ok=True)
    write(root, '.argocd-ls.yaml', config)
    return out, root

CLIENTS=[]
# Transport lives in support/session.py.
from support.session import Session
class LSP(Session):
    def __init__(self, out, roots):
        CLIENTS.append(self)
        super().__init__(out, roots, binary=BINARY)

ROWS = []
def record(name, expected, observed, passed, root, lsp=None, command=None, exit_code=None):
    out = root.parent
    if lsp:
        lsp.close()
        command = [str(BINARY),'serve','--stdio']
        exit_code = lsp.proc.returncode
        passed = passed and exit_code == 0
    row = {'id':name,'expected':expected,'observed':observed,'passed':passed,'command':command,'exit':exit_code,
           'inputSHA256':{str(p.relative_to(out)):hash_file(p) for p in root.rglob('*') if p.is_file()},
           'artifactSHA256':{p.name:hash_file(p) for p in out.iterdir() if p.is_file()}}
    (out/'result.json').write_text(json.dumps(row, indent=2)+'\n')
    ROWS.append(row)
    print(json.dumps({'id':name,'passed':passed,'observed':observed}), flush=True)

def cli_cases():
    for name, damaged in [('scope-isolation',False),('scope-incomplete',True)]:
        out, root = setup(name, CONFIG+'scopes:\n  - name: apps\n    include: ["apps/**"]\n  - name: projects\n    include: ["projects/**"]\n')
        write(root,'apps/app.yaml',APP)
        write(root,'projects/project.yaml',PROJECT)
        if damaged:
            write(root,'apps/damaged.yaml','apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata: [\n')
        cmd=[str(BINARY),'check','--root',str(root),'--format','json']
        p=subprocess.run(cmd,capture_output=True,timeout=15)
        (out/'stdout').write_bytes(p.stdout);(out/'stderr').write_bytes(p.stderr)
        data=json.loads(p.stdout)
        unresolved=[d for d in data['diagnostics'] if d['code']=='CD_PROJECT_UNRESOLVED']
        # Absence is only provable in a complete scope. A parse failure must
        # withhold the missing-project conclusion, not downgrade a guess to Info.
        expected=[] if damaged else [1]
        passed=p.returncode==(2 if damaged else 1) and data['analysisComplete']==(not damaged) and [d['severity'] for d in unresolved]==expected
        record(name,{'unresolvedSeverities':expected,'exit':2 if damaged else 1,'analysisComplete':not damaged},unresolved,passed,root,command=cmd,exit_code=p.returncode)

def excluded_case():
    name='opened-excluded-project'
    out,root=setup(name,CONFIG+'exclude: ["excluded/**"]\n')
    app=write(root,'app.yaml',APP);project=write(root,'excluded/project.yaml',PROJECT)
    c=LSP(out,[root])
    c.open(app,APP);before=c.diag(app,1)
    c.open(project,PROJECT);c.change(app,APP,2);after=c.diag(app,2)
    definition=c.request('textDocument/definition',{'textDocument':{'uri':app.as_uri()},'position':{'line':5,'character':13}})
    passed=any(d['code']=='CD_PROJECT_UNRESOLVED' for d in after) and definition.get('result')==[]
    record(name,{'afterOpeningExcludedProject':'Application stays unresolved; definition=[]'}, {'before':before,'after':after,'definition':definition},passed,root,c)

def helm_case():
    name='opened-helm-source';out,root=setup(name)
    write(root,'chart/Chart.yaml','apiVersion: v2\nname: chart\nversion: 0.1.0\n')
    bad=APP.replace('project: team','project: 42')
    app=write(root,'chart/templates/app.yaml',bad)
    c=LSP(out,[root]);c.open(app,bad);ds=c.diag(app,1)
    record(name,{'diagnostics':'pure YAML schema error'},{'diagnostics':ds},any(d['code']=='SCHEMA_TYPE' for d in ds),root,c)

def disk_case():
    name='buffer-overrides-budgeted-disk';out,root=setup(name)
    app=write(root,'app.yaml','#'+('a'*(5*1024*1024)))
    c=LSP(out,[root]);c.open(app,APP)
    msg=c.diag(app,1)
    response=c.request('textDocument/documentSymbol',{'textDocument':{'uri':app.as_uri()}})
    record(name,{'validOpenBuffer':'symbol app; no disk budget rejection'},{'firstDiagnostic':msg,'symbols':response},any(s.get('name')=='app' for s in response.get('result',[])),root,c)

def nested_case(reverse):
    name='nested-roots-child-first' if reverse else 'nested-roots-parent-first'
    out,root=setup(name)
    project=write(root,'project.yaml',PROJECT)
    child=root/'child';write(child,'.argocd-ls.yaml',CONFIG);app=write(child,'app.yaml',APP)
    c=LSP(out,[child,root] if reverse else [root,child]);c.open(app,APP);ds=c.diag(app,1)
    defs=c.request('textDocument/definition',{'textDocument':{'uri':app.as_uri()},'position':{'line':5,'character':13}})
    record(name,{'childOwnRoot':'unresolved Error and definition=[]'},{'diagnostics':ds,'definition':defs},any(d['code']=='CD_PROJECT_UNRESOLVED' and d['severity']==1 for d in ds) and defs.get('result')==[],root,c)

def completion_case():
    name='sequence-existing-key-completion';out,root=setup(name)
    text='apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: app\nspec:\n  project: team\n  sources:\n    - repoURL: https://example.org/repo\n      helm: {}\n      he: {}\n  destination: {server: https://kubernetes.default.svc, namespace: default}\n'
    app=write(root,'app.yaml',text);write(root,'project.yaml',PROJECT)
    c=LSP(out,[root]);c.open(app,text);before=c.diag(app,1)
    items=c.request('textDocument/completion',{'textDocument':{'uri':app.as_uri()},'position':{'line':9,'character':8}})
    selected=[i for i in items.get('result',[]) if i['label']=='helm']
    after=[]
    if selected:
        edited=text.replace('      he: {}','      helm: {}')
        write(root,'applied-completion.txt',edited)
        c.change(app,edited,2);after=c.diag(app,2)
    record(name,{'excludedCompletion':'helm already exists in the same sources item'},{'before':before,'completion':items,'afterApplyingHelm':after},not selected,root,c)

def fix_case():
    name='versioned-fix-and-stale-rejection';out,root=setup(name)
    text=APP.replace('project: team','proj: team')
    app=write(root,'app.yaml',text);write(root,'project.yaml',PROJECT)
    c=LSP(out,[root]);c.open(app,text);before=c.diag(app,1)
    response=c.request('textDocument/codeAction',{'textDocument':{'uri':app.as_uri()},'range':{'start':{'line':5,'character':2},'end':{'line':5,'character':6}},'context':{'diagnostics':[]}})
    action=next(a for a in response.get('result',[]) if a['data']['fixCode']=='FIX_FIELD_NAME')
    resolved=c.request('codeAction/resolve',action)
    edit=resolved.get('result',{}).get('edit',{}).get('documentChanges',[])
    assert len(edit)==1 and edit[0]['textDocument']=={'uri':app.as_uri(),'version':1}, 'fix must target the opened version'
    edits=edit[0].get('edits',[])
    assert edits, 'resolved fix must contain a text edit'
    # Replay the returned bytes. Sending the expected APP directly would let a
    # fix with an empty or incorrect replacement pass this consumer.
    lines=text.splitlines(keepends=True)
    def offset(position):
        line,character=position['line'],position['character']
        assert 0<=line<len(lines) and 0<=character<=len(lines[line].rstrip('\n')), 'edit range outside source'
        return sum(map(len,lines[:line]))+character
    changes=sorted([(offset(e['range']['start']),offset(e['range']['end']),e['newText']) for e in edits],reverse=True)
    applied=text;previous=len(text)
    for start,end,replacement in changes:
        assert start<=end<=previous, 'fix edits overlap or reverse a range'
        applied=applied[:start]+replacement+applied[end:]
        previous=start
    assert applied==APP, 'returned fix does not produce the expected Application'
    c.change(app,applied,2);after=c.diag(app,2)
    stale=c.request('codeAction/resolve',action)
    good=any(d['code']=='SCHEMA_UNKNOWN_FIELD' for d in before) and not any(d['code']=='SCHEMA_UNKNOWN_FIELD' or d.get('severity')==1 for d in after) and stale.get('error',{}).get('code')==-32801
    record(name,{'editVersion':1,'staleError':-32801,'returnedEditProducesExpectedApplication':True,'targetDiagnosticDisappears':True},{'before':before,'resolved':resolved,'appliedText':applied,'after':after,'stale':stale},good,root,c)

def budget_case(count):
    name=f'code-action-cancel-budget-{count}'
    out,root=setup(name)
    text='---\n'.join(APP.replace('name: app',f'name: app{i}').replace('project: team','proj: team') for i in range(count))
    app=write(root,'app.yaml',text);write(root,'project.yaml',PROJECT)
    c=LSP(out,[root]);c.open(app,text);ds=c.diag(app,1)
    start=time.monotonic()
    c.send('textDocument/codeAction',{'textDocument':{'uri':app.as_uri()},'range':{'start':{'line':5,'character':2},'end':{'line':5,'character':6}},'context':{'diagnostics':[]}},200)
    c.send('$/cancelRequest',{'id':200})
    # Observe cancellation independently: shutdown must not supply the cancellation reply.
    try:c.wait(lambda m:m.get('id')==200,timeout=0.2)
    except TimeoutError:pass
    c.send('shutdown',{},201)
    timedout=False
    try:
        shutdown=c.wait(lambda m:m.get('id')==201,timeout=8)
    except TimeoutError:
        timedout=True;shutdown=None
    elapsed=time.monotonic()-start
    if timedout:
        c.proc.kill();c.proc.wait()
    else:
        c.send('exit');c.proc.wait(timeout=5)
    action=[m for m in c.messages if m['message'].get('id')==200]
    action_elapsed=action[0]['elapsed']-(start-c.start) if action else None
    cancelled=bool(action) and action[0]['message'].get('error',{}).get('code')==-32800 and action_elapsed<=0.2
    shutdown_ok=not timedout and elapsed<=5
    record(name,{'cancelDispatchMaximumSeconds':0.2,'shutdownMaximumSeconds':5,'textBytes':len(text.encode())},{'shutdownLatencySeconds':elapsed,'timedoutAt8Seconds':timedout,'actionResponseCount':len(action),'actionLatencySeconds':action_elapsed,'actionError':action[0]['message'].get('error') if action else None,'assertions':{'shutdownWithin5Seconds':shutdown_ok,'cancellationObservedWithin200ms':cancelled},'diagnosticCount':len(ds),'shutdown':shutdown},shutdown_ok and cancelled,root,c)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['cli','lsp','budget']);parser.add_argument('--count',type=int,default=60);args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    if args.mode=='cli':cli_cases()
    elif args.mode=='lsp':
        for fn in [excluded_case,helm_case,disk_case,lambda:nested_case(False),lambda:nested_case(True),completion_case,fix_case]:
            try: fn()
            except Exception as exc:
                for client in CLIENTS:
                    if not client.closed:
                        client.close()
                        record(client.out.name, {'scenarioMustComplete':True}, {'exception':repr(exc),'exceptionType':type(exc).__name__}, False, client.out/'workspace')
    else:budget_case(args.count)
    expected={'cli':['scope-isolation','scope-incomplete'],'lsp':['opened-excluded-project','opened-helm-source','buffer-overrides-budgeted-disk','nested-roots-parent-first','nested-roots-child-first','sequence-existing-key-completion','versioned-fix-and-stale-rejection'],'budget':[f'code-action-cancel-budget-{args.count}']}[args.mode]
    passed=[row['id'] for row in ROWS]==expected and all(row['passed'] for row in ROWS)
    (OUT/f'{args.mode}-report.json').write_text(json.dumps({'command':sys.argv,'binarySHA256':hash_file(BINARY),'testSHA256':hash_file(pathlib.Path(__file__)),'expectationsSHA256':hash_file(pathlib.Path(__file__)),'python':sys.version,'expectedCases':expected,'cases':ROWS,'passed':passed},indent=2)+'\n')
    sys.exit(0 if passed else 1)
