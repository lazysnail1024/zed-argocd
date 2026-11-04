#!/usr/bin/env python3
"""Independent candidate process oracle; imports only the stdio client helper."""
import hashlib, importlib.util, json, os, pathlib, shutil, subprocess, sys, time
HERE=pathlib.Path(__file__).resolve().parent
BINARY=pathlib.Path(os.environ.get('CD_AUDIT_BINARY',str(HERE.parent/'build/argocd-ls'))).resolve()
OUT=pathlib.Path(os.environ.get('CD_AUDIT_OUTPUT',str(HERE.parent/'artifacts/candidate-review'))).resolve()
os.environ['CD_AUDIT_BINARY']=str(BINARY)
spec=importlib.util.spec_from_file_location('client',HERE/'audit_repair_e2e.py');e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
CONFIG='schemaVersion: 1\ntargetVersion: "3.0.0"\ncontrolPlaneNamespace: argocd\nreferencePolicy: closed\n'
APP='apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: app}\nspec:\n  project: te\n  source: {repoURL: https://example.test/repo}\n  destination: {server: https://kubernetes.default.svc, namespace: default}\n'
SOURCES='apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: app}\nspec:\n  project: team\n  sources:\n    - {repoURL: values, ref: values}\n    - repoURL: chart\n      helm:\n        valueFiles: ["$va/path😀.yaml"] # keep\n  destination: {server: x, namespace: default}\n'
def project(name='team',ns='argocd'):
    return 'apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata:\n  name: '+json.dumps(name,ensure_ascii=False)+'\n  namespace: '+ns+'\nspec:\n  sourceRepos: ["*"]\n  destinations: [{server: "*", namespace: "*"}]\n'
def write(root,name,text):
    p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(text.encode());return p
def pos(text,needle,delta=None):
    index=text.index(needle)+(len(needle) if delta is None else delta);before=text[:index]
    return {'line':before.count('\n'),'character':len(before.rsplit('\n',1)[-1].encode('utf-16-le'))//2}
def apply(text,edit):
    lines=text.splitlines(keepends=True)
    def off(p):
        if p['line']==len(lines) and p['character']==0:return len(text)
        return len(''.join(lines[:p['line']]))+len(lines[p['line']].encode('utf-16-le')[:p['character']*2].decode('utf-16-le'))
    a,b=off(edit['range']['start']),off(edit['range']['end']);return text[:a]+edit['newText']+text[b:]
ROWS=[];CLIENTS=[];OBS={}
def begin(out,root,text,files=None,config=CONFIG):
    write(root,'.argocd-ls.yaml',config)
    for rel,value in ({'project.yaml':project()} if files is None else files).items():write(root,rel,value)
    file=write(root,'apps/app.yaml',text);c=e.LSP(out,[root]);CLIENTS.append(c);c.open(file,text,11);OBS['beforeDiagnostics']=c.diag(file,11)
    return c,file
def request(c,file,method,p):
    r=c.request('textDocument/'+method,{'textDocument':{'uri':file.as_uri()},'position':p})
    OBS.setdefault('responses',[]).append({'method':method,'position':p,'response':r});assert 'error' not in r,r
    return r.get('result')
def query(c,file,text,needle,delta=None):return request(c,file,'completion',pos(text,needle,delta))
def applied(c,file,text,item,expected,clean=True):
    edited=apply(text,item['textEdit']);OBS['actualApplied']=edited;OBS['expectedApplied']=expected
    write(file.parent.parent.parent,'applied.txt',edited)
    assert file.read_bytes()==text.encode(),'server wrote source file'
    edit=item['textEdit'];c.send('textDocument/didChange',{'textDocument':{'uri':file.as_uri(),'version':12},'contentChanges':[{'range':edit['range'],'text':edit['newText']}]})
    ds=c.diag(file,12);OBS['afterDiagnostics']=ds
    assert edited==expected,'client-applied completion changes unexpected bytes'
    if clean:assert not any(d['severity']==1 for d in ds),ds
    return edited
def run(name,expected,fn):
    global OBS
    out=OUT/name;root=out/'workspace'
    if root.exists():shutil.rmtree(root)
    root.mkdir(parents=True,exist_ok=True);OBS={};start=len(CLIENTS);error=None
    try:fn(out,root)
    except Exception as ex:error=repr(ex)
    finally:
        for c in CLIENTS[start:]:c.close()
    row={'id':name,'expected':expected,'observed':OBS,'passed':error is None,'exception':error,'exits':[c.proc.returncode for c in CLIENTS[start:]],'inputsSHA256':{str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()},'artifactSHA256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file() and p.name!='result.json'}}
    (out/'result.json').write_text(json.dumps(row,ensure_ascii=False,indent=2)+'\n');ROWS.append(row);print(json.dumps({'id':name,'passed':row['passed'],'exception':error},ensure_ascii=False),flush=True)

def quoted(out,root):
    text=APP.replace('metadata: {name: app}','metadata: {name: app, labels: {note: "😀"}}').replace('  project: te','  project: "texxx" # \u4e2d\u6587 😀').replace('\n','\r\n')
    c,f=begin(out,root,text,{'p1.yaml':project(),'p2.yaml':project('team-blue'),'foreign.yaml':project('team-other','elsewhere')})
    items=query(c,f,text,'"te');assert [i['label'] for i in items]==['team','team-blue']
    applied(c,f,text,items[0],text.replace('"texxx"','"team"'))
run('project-quoted-crlf-prefix-apply','Only team/team-blue; replace whole scalar contents, retain quotes/comments/CRLF/emoji and apply cleanly.',quoted)

def bool_name(out,root):
    text=APP.replace('project: te','project: o');c,f=begin(out,root,text,{'p.yaml':project('on')});items=query(c,f,text,'project: o');assert [i['label'] for i in items]==['on'];applied(c,f,text,items[0],text.replace('project: o','project: "on"'))
run('yaml-boolean-name-apply','Selected on is a quoted YAML string and resolves without new Error.',bool_name)

def empty_case(suffix,needle,expected_line):
    def fn(out,root):
        text=APP.replace('  project: te','  project:'+suffix);c,f=begin(out,root,text);items=query(c,f,text,needle);assert [i['label'] for i in items]==['team'];applied(c,f,text,items[0],text.replace('  project:'+suffix,expected_line))
    return fn
run('empty-project-no-separator','Cursor immediately after project: inserts a value with required YAML separation.',empty_case('','project:','  project: team'))
run('empty-project-comment','Cursor before comment inserts team and retains comment as a comment with separation.',empty_case(' # keep 😀','project: ','  project: team # keep 😀'))

def empty_names(out,root):
    for index,(suffix,name,expected) in enumerate([(' ', 'team', ' team'), ('', 'true', ' "true"'), (' # keep', 'true', ' "true" # keep')]):
        sub=out/str(index);sub.mkdir(exist_ok=True);work=sub/'workspace'
        if work.exists():shutil.rmtree(work)
        work.mkdir()
        text=APP.replace('project: te','project:'+suffix)
        c,f=begin(sub,work,text,{'p.yaml':project(name)})
        needle='project:'+suffix.split('#')[0]
        items=query(c,f,text,needle);assert [i['label'] for i in items]==[name]
        applied(c,f,text,items[0],text.replace('project:'+suffix,'project:'+expected))
run('empty-project-string-and-whitespace-controls','Empty insertions retain ordinary spacing and quoted Boolean-looking Project names, including adjacent comments.',empty_names)

def source_edit(out,root):
    text=SOURCES.replace('valueFiles: [','valueFiles: [').replace('"$va/path😀.yaml"','"$vaZZ/path😀.yaml"').replace('\n','\r\n')+'---\r\n'+SOURCES.replace('ref: values','ref: vault').replace('\n','\r\n')
    c,f=begin(out,root,text);items=query(c,f,text,'$va');assert [i['label'] for i in items]==['values'];applied(c,f,text,items[0],text.replace('$vaZZ/path😀.yaml','$values/path😀.yaml',1),clean=False)
    # The unchanged second document still has a missing ref; the first must now resolve.
    defs=request(c,f,'definition',pos(OBS['actualApplied'],'$values'))
    assert len(defs)==1 and defs[0]['uri']==f.as_uri() and defs[0]['range']['start']['line']==6,defs
run('source-ref-crlf-document-local-apply','Only values from current document; preserve $, path, quotes, comment, second document; definition resolves after actual edit.',source_edit)

def dup_flow(out,root):
    text=APP.replace('project: te','project: team').replace('  source: {repoURL: https://example.test/repo}','  sources: [{repoURL: a, ref: values}, {repoURL: b, ref: values}, {repoURL: c, helm: {valueFiles: ["$va/p"]}}]')
    c,f=begin(out,root,text);items=query(c,f,text,'$va');assert [i['label'] for i in items]==['values','values'];assert len({i['detail'] for i in items})==2,'same-line declaration origins collapsed'
run('duplicate-source-ref-same-line-origins','Two static declarations on one physical line have distinguishable source identities.',dup_flow)

def unknown_ns(out,root):
    c,f=begin(out,root,APP,{'b.yaml':project('team','two'),'a.yaml':project('team','one')},CONFIG.replace('controlPlaneNamespace: argocd\n',''))
    items=query(c,f,APP,'project: te');assert [i['label'] for i in items]==['team','team'] and len({i['detail'] for i in items})==2
    edited=applied(c,f,APP,items[0],APP.replace('project: te','project: team'))
    assert request(c,f,'definition',pos(edited,'project: team'))==[]
run('unknown-namespace-no-guessed-definition','Qualified duplicates remain visible, applied text remains legal, no topology guessed.',unknown_ns)

def exclusion(out,root):
    cfg=CONFIG+'exclude: ["excluded/**"]\nscopes:\n  - name: apps\n    include: ["apps/**"]\n  - name: other\n    include: ["other/**"]\n'
    files={'apps/p.yaml':project('team-local'),'other/p.yaml':project('team-other'),'excluded/p.yaml':project('team-excluded'),'apps/bad.yaml':project('team-broken')+'broken: [\n'}
    c,f=begin(out,root,APP,files,cfg);excluded=root/'excluded/p.yaml';c.open(excluded,files['excluded/p.yaml'],1);c.change(f,APP,12);c.diag(f,12)
    items=query(c,f,APP,'project: te');assert [i['label'] for i in items]==['team-local']
run('scope-open-exclusion-damaged-declaration','Only same-scope reliable Project; open excluded/other-scope/damaged declarations are absent.',exclusion)

def recovered(out,root):
    text=APP+'  broken: [\n';c,f=begin(out,root,text);items=query(c,f,text,'project: te');assert [i['label'] for i in items]==['team'] and 'reliable prefix only' in items[0]['detail']
    assert not any(d['code']=='CD_PROJECT_UNRESOLVED' for d in OBS['beforeDiagnostics'])
    assert request(c,f,'definition',pos(text,'project: te'))==[]
    assert request(c,f,'documentSymbol',pos(text,'project: te'))==[]
    p=pos(text,'project: te');fix=c.request('textDocument/codeAction',{'textDocument':{'uri':f.as_uri()},'range':{'start':p,'end':p},'context':{'diagnostics':[]}});OBS['fixes']=fix;assert fix.get('result')==[]
    applied(c,f,text,items[0],text.replace('project: te','project: team'),clean=False)
    c.change(f,APP.replace('project: te','project: team'),13);c.diag(f,13);assert len(request(c,f,'definition',pos(APP,'project: te')))==1
run('recovery-query-only-and-repair','Prefix candidates exist and apply exactly; no recovered declarations/symbols/definition/fix or missing Error; repairing restores navigation.',recovered)

def appset(out,root):
    text='apiVersion: argoproj.io/v1alpha1\nkind: ApplicationSet\nmetadata: {name: set}\nspec:\n  generators: []\n  goTemplate: true\n  template:\n    metadata: {name: "{{.name}}"}\n    spec:\n      project: te\n      sources:\n        - {repoURL: data, ref: values}\n        - {repoURL: other, ref: "{{.ref}}"}\n        - {repoURL: chart, helm: {valueFiles: ["$va/p"]}}\n      destination: {server: x, namespace: "{{.namespace}}"}\n'
    c,f=begin(out,root,text);items=query(c,f,text,'$va');assert [i['label'] for i in items]==['values'];applied(c,f,text,items[0],text.replace('$va/p','$values/p'),clean=False)
    assert [i['label'] for i in query(c,f,OBS['actualApplied'],'project: te')]==['team']
    assert query(c,f,OBS['actualApplied'],'{{.namespace')==[]
run('appset-static-leaf-apply-with-dynamic-siblings','Static template source ref applies; static Project candidates survive unrelated dynamic fields; dynamic cursor refuses literals.',appset)

def negative_forms(out,root):
    forms=[('foreign',APP.replace('kind: Application','kind: Workflow')+'  broken: [\n','project: te'),('current-line-damage',APP.replace('project: te','project: [te'),'project: [te'),('complete-duplicate',APP+'  project: other\n','project: te'),('tag',APP.replace('project: te','project: !custom te'),'!custom te'),('escaped',APP.replace('project: te','project: "t\\u0065"'),'t\\u0065'),('dynamic',APP.replace('project: te','project: "{{.project}}"'),'{{.project'),('source-suffix',SOURCES,'$va/path'),('multiline',APP.replace('project: te','project: "t\\\n    e"'),'    e')]
    c,f=begin(out,root,forms[0][1]);OBS['forms']=[]
    for n,(label,text,needle) in enumerate(forms,11):
        if n!=11:c.change(f,text,n);c.diag(f,n)
        items=query(c,f,text,needle);OBS['forms'].append({'id':label,'items':items});assert items==[],label
run('unsafe-query-positions-refused','Foreign/unsafe/dynamic/escaped/multiline/current-line-damage/path-suffix positions get no literal candidates.',negative_forms)

def typed_refs(out,root):
    cases=[('true','$tr',[]),('42','$4',[]),('"true"','$tr',['true'])]
    initial=SOURCES.replace('ref: values','ref: true').replace('$va/','$tr/')
    c,f=begin(out,root,initial);OBS['typedRefs']=[]
    failures=[]
    for version,(literal,needle,labels) in enumerate(cases,11):
        text=SOURCES.replace('ref: values','ref: '+literal).replace('$va/',needle+'/')
        if version!=11:c.change(f,text,version);c.diag(f,version)
        items=query(c,f,text,needle);actual=[i['label'] for i in items];OBS['typedRefs'].append({'declaration':literal,'expected':labels,'actual':actual})
        if actual!=labels:failures.append(literal)
    assert not failures,failures
run('source-ref-declaration-string-type','Boolean/numeric ref declarations are not strings; quoted true remains a usable static ref candidate.',typed_refs)

def neighboring(out,root):
    text=APP.replace('project: te','project: team')+'---\n'+APP.replace('metadata: {name: app}','metadata: {name: broken}')+'  broken: [\n'
    c,f=begin(out,root,text);defs=request(c,f,'definition',pos(text,'project: team'));assert len(defs)==1
    symbols=request(c,f,'documentSymbol',pos(text,'project: team'));assert [s['name'] for s in symbols]==['app']
run('complete-neighbor-of-damaged-document','Complete document navigation/symbol remains; damaged neighbor never becomes a symbol.',neighboring)

OUT.mkdir(parents=True,exist_ok=True)
(OUT/'audit_e2e.py.txt').write_bytes(pathlib.Path(__file__).read_bytes())
(OUT/'expectations.json').write_text(json.dumps({r['id']:r['expected'] for r in ROWS},indent=2)+'\n')
(OUT/'report.json').write_text(json.dumps({'command':sys.argv,'binary':str(BINARY),'binarySHA256':hashlib.sha256(BINARY.read_bytes()).hexdigest(),'testSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'clientSHA256':hashlib.sha256((HERE/'audit_repair_e2e.py').read_bytes()).hexdigest(),'expectationsSHA256':hashlib.sha256((pathlib.Path(__file__)).read_bytes()).hexdigest(),'python':sys.version,'cases':ROWS},ensure_ascii=False,indent=2)+'\n')
sys.exit(0 if ROWS and all(r['passed'] for r in ROWS) else 1)
