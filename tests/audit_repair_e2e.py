#!/usr/bin/env python3
"""Independent CLI/stdin-stdout scenarios for the fixed CD stage."""
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
    p.write_text(text)
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
class LSP:
    def __init__(self, out, roots):
        CLIENTS.append(self)
        self.closed=False
        self.out = out
        self.start = time.monotonic()
        self.proc = subprocess.Popen([str(BINARY), 'serve', '--stdio'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.q, self.messages, self.sent, self.raw = queue.Queue(), [], [], []
        self.seq = 10
        def read():
            try:
                while True:
                    header = b''
                    while not header.endswith(b'\r\n\r\n'):
                        c = self.proc.stdout.read(1)
                        if not c:
                            return
                        header += c
                    length = int(next(line.split(b':', 1)[1] for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:')))
                    body = self.proc.stdout.read(length)
                    self.raw.append(header + body)
                    msg = json.loads(body)
                    self.messages.append({'elapsed': time.monotonic()-self.start, 'message': msg})
                    self.q.put(msg)
            except Exception as exc:
                self.q.put({'readerError': repr(exc)})
        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()
        self.send('initialize', {'processId': None, 'workspaceFolders':[{'uri':r.as_uri(),'name':r.name} for r in roots], 'capabilities':{'workspace':{'workspaceEdit':{'documentChanges':True}}}}, 1)
        self.wait(lambda m: m.get('id') == 1)
        self.send('initialized', {})

    def send(self, method, params=None, ident=None):
        msg = {'jsonrpc':'2.0', 'method':method}
        if params is not None:
            msg['params'] = params
        if ident is not None:
            msg['id'] = ident
        body = json.dumps(msg, ensure_ascii=False).encode()
        frame = f'Content-Length: {len(body)}\r\n\r\n'.encode()+body
        self.sent.append(frame)
        self.proc.stdin.write(frame)
        self.proc.stdin.flush()

    def wait(self, predicate, timeout=12):
        end = time.monotonic()+timeout
        while True:
            remaining = end-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('LSP response deadline')
            try:
                msg = self.q.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError('LSP response deadline')
            if predicate(msg):
                return msg

    def request(self, method, params, timeout=12):
        self.seq += 1
        ident = self.seq
        self.send(method, params, ident)
        return self.wait(lambda m: m.get('id') == ident, timeout)

    def open(self, path, text, version=1):
        self.send('textDocument/didOpen', {'textDocument':{'uri':path.as_uri(),'languageId':'yaml','version':version,'text':text}})

    def change(self, path, text, version):
        self.send('textDocument/didChange', {'textDocument':{'uri':path.as_uri(),'version':version},'contentChanges':[{'text':text}]})

    def diag(self, path, version):
        return self.wait(lambda m: m.get('method') == 'textDocument/publishDiagnostics' and m.get('params',{}).get('uri') == path.as_uri() and m['params'].get('version') == version)['params']['diagnostics']

    def close(self):
        if self.closed:return
        self.closed=True
        if self.proc.poll() is None:
            try:
                self.request('shutdown', {}, timeout=5)
                self.send('exit')
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
                self.proc.wait()
        self.thread.join(timeout=2)
        (self.out/'stdin').write_bytes(b''.join(self.sent))
        (self.out/'stdout').write_bytes(b''.join(self.raw))
        (self.out/'stderr').write_bytes(self.proc.stderr.read())
        (self.out/'messages.json').write_text(json.dumps(self.messages, indent=2)+'\n')

ROWS = []
def record(name, expected, observed, passed, root, lsp=None, command=None, exit_code=None):
    out = root.parent
    if lsp:
        lsp.close()
        command = [str(BINARY),'serve','--stdio']
        exit_code = lsp.proc.returncode
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
        expected=3 if damaged else 1
        passed=p.returncode==(2 if damaged else 1) and data['analysisComplete']==(not damaged) and len(unresolved)==1 and unresolved[0]['severity']==expected
        record(name,{'unresolvedSeverity':expected,'exit':2 if damaged else 1,'analysisComplete':not damaged},unresolved,passed,root,command=cmd,exit_code=p.returncode)

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
    msg=c.wait(lambda m:m.get('method')=='textDocument/publishDiagnostics')
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
    c.change(app,APP,2);after=c.diag(app,2)
    stale=c.request('codeAction/resolve',action)
    good=bool(edit) and edit[0]['textDocument']=={'uri':app.as_uri(),'version':1} and not any(d['code']=='SCHEMA_UNKNOWN_FIELD' for d in after) and stale.get('error',{}).get('code')==-32801
    record(name,{'editVersion':1,'staleError':-32801,'targetDiagnosticDisappears':True},{'before':before,'resolved':resolved,'after':after,'stale':stale},good,root,c)

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
                        record(client.out.name, {'scenarioMustComplete':True}, {'exception':repr(exc)}, False, client.out/'workspace')
    else:budget_case(args.count)
    (OUT/f'{args.mode}-report.json').write_text(json.dumps({'command':sys.argv,'binarySHA256':hash_file(BINARY),'testSHA256':hash_file(pathlib.Path(__file__)),'expectationsSHA256':hash_file(pathlib.Path(__file__)),'python':sys.version,'cases':ROWS},indent=2)+'\n')
    sys.exit(0 if ROWS and all(r['passed'] for r in ROWS) else 1)
