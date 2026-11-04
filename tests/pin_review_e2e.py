#!/usr/bin/env python3
"""Independent exact-byte pin E2E: real stdio LSP plus CLI config replay."""
import hashlib,importlib.util,json,os,pathlib,shutil,subprocess,sys
HERE=pathlib.Path(__file__).resolve().parent
BINARY=pathlib.Path(os.environ.get('CD_AUDIT_BINARY',str(HERE.parent/'build/argocd-ls'))).resolve()
OUT=pathlib.Path(os.environ.get('CD_AUDIT_OUTPUT',str(HERE.parent/'artifacts/pin-review'))).resolve()
os.environ.update(CD_AUDIT_BINARY=str(BINARY),CD_AUDIT_OUTPUT=str(OUT))
spec=importlib.util.spec_from_file_location('client',HERE/'audit_repair_e2e.py');e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
RUNNER=pathlib.Path(__file__).read_bytes()
ROWS=[]
CASES=[]

def case(name,text,selection,expected=None,override='',files=None,forbid=False,late_damage=False,persisted='root'):
    CASES.append(dict(name=name,text=text,selection=selection,expected=expected,override=override,files=files,forbid=forbid,late_damage=late_damage,persisted=persisted))

t='# \u524d\u8a00 😀\r\nschemaVersion: 1\r\ntargetVersion: "3.5.6" # \u4fdd\u7559 😀\r\ncontrolPlaneNamespace: argocd\r\n'
case('quoted-crlf-unicode',t,'"3.5.6"',t.replace('"3.5.6"','"3.0.0"'),'3.0.0')
t='schemaVersion: 1\ntargetVersion: "3.0.0"\nscopes:\n  - name: development\n    targetVersion: "3.5.6"\n    include: ["dev/**"]\n  - include:\n      - "apps/**"\n      - "extra/**"\n    name: "\u751f\u4ea7😀" # keep exactly\n'
case('second-scope-nested-includes',t,'"\u751f\u4ea7😀"',t+'    targetVersion: "3.0.0"\n',persisted='\u751f\u4ea7😀')
t='schemaVersion: 1\ntargetVersion: "3.0.0"\nscopes:\n- name: prod # keep\n  include:\n  - "apps/**"\n'
case('indentless-scope-sequence',t,'name: prod',t.replace('- name: prod # keep\n','- name: prod # keep\n  targetVersion: "3.0.0"\n'),persisted='prod')
t='schemaVersion: 1\ntargetVersion: "3.0.0"\nscopes: [{name: "\u751f\u4ea7😀", include: ["apps/**"], targetVersion: \'3.5.6\'}]\n'
case('flow-existing-scalar-unicode-column',t,"'3.5.6'",t.replace("'3.5.6'","'3.0.0'"),'3.0.0',persisted='\u751f\u4ea7😀')
t='  schemaVersion: 1 # keep indentation\n  controlPlaneNamespace: argocd\n'
case('indented-root-insertion',t,'schemaVersion',t.replace('  schemaVersion: 1 # keep indentation\n','  schemaVersion: 1 # keep indentation\n  targetVersion: "3.0.0"\n'),'3.0.0')
t='schemaVersion: 1\ntargetVersion: "3.0.0"\nscopes:\n  - name: root\n    include: ["apps/**"]\n'
case('named-root-scope-insertion',t,'name: root',t.replace('  - name: root\n','  - name: root\n    targetVersion: "3.0.0"\n'),persisted='root-named')
t='schemaVersion: 1\ntargetVersion: "3.5.6"\nscopes:\n  - name: root\n    targetVersion: "3.5.6"\n    include: ["apps/**"]\n'
case('named-root-scope-scalar-replacement',t,'    targetVersion:',t.replace('    targetVersion: "3.5.6"','    targetVersion: "3.0.0"'),'3.0.0',persisted='root-named')
t='schemaVersion: 1\ntargetVersion: "3.\\\n  5.6"\ncontrolPlaneNamespace: argocd\n'
case('physically-multiline-quoted-scalar',t,'targetVersion',override='3.0.0',forbid=True)
t='{schemaVersion: 1, controlPlaneNamespace: argocd}\n'
case('flow-missing-pin-refused',t,'schemaVersion',override='3.0.0',forbid=True)
t='schemaVersion: 1\ntargetVersion: &version "3.5.6"\n'
case('anchored-pin-refused',t,'targetVersion',override='3.0.0',forbid=True)
t='schemaVersion: 1\ntargetVersion: "3.5.6"\n'
broken='apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata: [broken\n'
case('incomplete-scope-with-no-projected-documents',t,'"3.5.6"',override='3.0.0',files={'broken.yaml':broken},forbid=True)
case('incomplete-scope-with-projected-resource',t,'"3.5.6"',override='3.0.0',files={'apps/app.yaml':e.APP,'broken.yaml':broken},forbid=True)
case('dependency-damaged-between-action-and-resolve',t,'"3.5.6"',override='3.0.0',late_damage=True)

def write(p,text):
    p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(text.encode());return p

def selection(text,needle):
    start=text.index(needle);line=text.count('\n',0,start);line_start=text.rfind('\n',0,start)+1
    col=len(text[line_start:start].encode('utf-16-le'))//2
    # Include the entire selected physical line, so key, value and comment selection are covered.
    end=text.find('\n',start)
    if end<0:end=len(text)
    if text[line_start:end].endswith('\r'):end-=1
    return {'start':{'line':line,'character':col},'end':{'line':line,'character':len(text[line_start:end].encode('utf-16-le'))//2}}

def apply(text,change):
    lines=text.splitlines(keepends=True)
    def off(p):
        if p['line']==len(lines) and p['character']==0:return len(text)
        prefix=lines[p['line']].encode('utf-16-le')[:p['character']*2].decode('utf-16-le')
        return len(''.join(lines[:p['line']]))+len(prefix)
    spans=[(off(x['range']['start']),off(x['range']['end']),x['newText']) for x in change['edits']]
    for a,b,new in sorted(spans,reverse=True):text=text[:a]+new+text[b:]
    return text

def cli(out,root,override='',label='before'):
    cmd=[str(BINARY),'explain-config','--root',str(root)]
    if override:cmd+=['--target-version',override]
    p=subprocess.run(cmd,capture_output=True,timeout=15)
    (out/f'{label}.stdout').write_bytes(p.stdout);(out/f'{label}.stderr').write_bytes(p.stderr)
    return {'command':cmd,'exit':p.returncode,'report':json.loads(p.stdout) if p.returncode==0 else None}

for item in CASES:
    name=item['name'];out=OUT/name;root=out/'workspace'
    if root.exists():shutil.rmtree(root)
    root.mkdir(parents=True,exist_ok=True)
    config=write(root/'.argocd-ls.yaml',item['text']);uri=config.as_uri();initial=config.read_bytes()
    for rel,text in (item['files'] if item['files'] is not None else {'apps/app.yaml':e.APP}).items():write(root/rel,text)
    expected={'action':'forbidden' if item['forbid'] else 'required','resolvedEdit': 'forbidden after dependency damage' if item['late_damage'] else 'version=31; exact expected bytes','exactText':item['expected']}
    before=cli(out,root,item['override']);observed={'beforeCLI':before};passed=False;c=None
    try:
        assert before['exit']==0,'input configuration must be valid before the pin'
        c=e.LSP(out,[root])
        if item['override']:c.send('workspace/didChangeConfiguration',{'settings':{'targetVersion':item['override']}})
        c.open(config,item['text'],31);ds=c.diag(config,31);observed['beforeDiagnostics']=ds
        params={'textDocument':{'uri':uri},'range':selection(item['text'],item['selection']),'context':{'diagnostics':[]}}
        response=c.request('textDocument/codeAction',params);observed['actionResponse']=response
        pins=[a for a in response.get('result',[]) if a.get('data',{}).get('fixCode')=='FIX_PIN_VERSION']
        if item['forbid']:
            passed=not pins and 'error' not in response
        else:
            assert len(pins)==1,'expected exactly one pin action in the requested mapping'
            if item['late_damage']:write(root/'broken.yaml',broken)
            resolved=c.request('codeAction/resolve',pins[0]);observed['resolveResponse']=resolved
            if item['late_damage']:
                passed=resolved.get('error',{}).get('code')==-32801
            else:
                changes=resolved.get('result',{}).get('edit',{}).get('documentChanges',[])
                assert len(changes)==1 and changes[0]['textDocument']=={'uri':uri,'version':31}
                edited=apply(item['text'],changes[0]);observed['appliedText']=edited;write(out/'applied.yaml.txt',edited)
                assert edited==item['expected'],'actual client-applied bytes differ from independent expected text'
                assert config.read_bytes()==initial,'server must not write the configuration to disk'
                c.send('textDocument/didChange',{'textDocument':{'uri':uri,'version':32},'contentChanges':[{'range':edit['range'],'text':edit['newText']} for edit in changes[0]['edits']]})
                post=c.diag(config,32);observed['afterDiagnostics']=post
                assert not any(d['severity']==1 for d in post),'edited config must remain valid'
                later=c.request('textDocument/codeAction',params);observed['afterActions']=later
                assert later.get('result')==[],'same pin action must disappear after applying its edit'
                replay=out/'replay'
                if replay.exists():shutil.rmtree(replay)
                shutil.copytree(root,replay);write(replay/'.argocd-ls.yaml',edited)
                result=cli(out,replay,label='after');observed['afterCLI']=result
                assert result['exit']==0
                cfg=result['report']['config'];target=item['persisted']
                value=cfg['targetVersion'] if target=='root' else next(x['targetVersion'] for x in cfg['scopes'] if x['name']==('root' if target=='root-named' else target))
                assert value=='3.0.0','the selected persisted target must be the explicit current target'
                passed=True
    except Exception as exc:observed['exception']=repr(exc)
    finally:
        if c:c.close()
    row={'id':name,'expected':expected,'observed':observed,'passed':passed,'lspExit':c.proc.returncode if c else None,'inputSHA256':{str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()},'artifactsSHA256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file() and p.name!='result.json'}}
    (out/'result.json').write_text(json.dumps(row,indent=2,ensure_ascii=False)+'\n');ROWS.append(row)
    print(json.dumps({'id':name,'passed':passed,'actionCount':len(observed.get('actionResponse',{}).get('result',[])),'exception':observed.get('exception'),'resolveError':observed.get('resolveResponse',{}).get('error')},ensure_ascii=False),flush=True)
OUT.mkdir(parents=True,exist_ok=True);(OUT/'pin_audit_e2e.py.txt').write_bytes(RUNNER)
(OUT/'report.json').write_text(json.dumps({'command':sys.argv,'binarySHA256':hashlib.sha256(BINARY.read_bytes()).hexdigest(),'testSHA256':hashlib.sha256(RUNNER).hexdigest(),'clientSHA256':e.hash_file(HERE/'audit_repair_e2e.py'),'expectationsSHA256':e.hash_file(pathlib.Path(__file__)),'python':sys.version,'cases':ROWS},indent=2,ensure_ascii=False)+'\n')
sys.exit(0 if ROWS and all(x['passed'] for x in ROWS) else 1)
