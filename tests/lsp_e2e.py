#!/usr/bin/env python3
"""Independent framed stdio client: buffer ownership, versions, sequential UTF-16 changes and lifecycle."""
import pathlib,json,subprocess,sys,hashlib,threading,queue,time
root=pathlib.Path(__file__).resolve().parents[1];binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve() if len(sys.argv)>2 else root/'artifacts/lsp';out.mkdir(parents=True,exist_ok=len(sys.argv)==2);folder=out/'workspace';folder.mkdir(exist_ok=True);(folder/'.argocd-ls.yaml').write_text('schemaVersion: 1\ntargetVersion: "3.0.0"\n')
project_file=folder/'project.yaml';project_file.write_text('apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata:\n  name: default\n  namespace: argocd\nspec:\n  sourceRepos: ["*"]\n  destinations: [{server: "*", namespace: "*"}]\n')
valid='apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: sample\nspec:\n  project: default\n';invalid=valid.replace('project: default','project: 42');file=folder/'app.yaml';file.write_text(valid);uri=file.as_uri();p=subprocess.Popen([str(binary),'serve','--stdio'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE);messages=queue.Queue();raw=[];sent=[];rows=[]
def read():
 try:
  while True:
   line=p.stdout.readline()
   if not line:break
   raw.append(line);length=int(line.decode().split(':',1)[1]);blank=p.stdout.readline();raw.append(blank);body=p.stdout.read(length);raw.append(body);messages.put(json.loads(body))
 except Exception as e:messages.put({'readerError':str(e)})
thread=threading.Thread(target=read,daemon=True);thread.start()
def send(method,params=None,id=None):
 m={'jsonrpc':'2.0','method':method};
 if params is not None:m['params']=params
 if id is not None:m['id']=id
 b=json.dumps(m).encode();frame=f'Content-Length: {len(b)}\r\n\r\n'.encode()+b;sent.append(frame);p.stdin.write(frame);p.stdin.flush()
def wait(predicate):
 end=time.monotonic()+15
 while time.monotonic()<end:
  m=messages.get(timeout=max(.1,end-time.monotonic()))
  if predicate(m):return m
 raise TimeoutError('expected protocol response')
# These lifecycle/domain checks require the completed workspace publication.
def diag(version,document_uri=uri):return wait(lambda m:m.get('method')=='textDocument/publishDiagnostics' and m.get('params',{}).get('uri')==document_uri and m['params'].get('version')==version and m['params'].get('x-argocd-analysis-scope','workspace')=='workspace')['params']['diagnostics']
try:
 send('initialize',{'rootUri':folder.as_uri(),'capabilities':{'workspace':{'workspaceEdit':{'documentChanges':True}},'textDocument':{'publishDiagnostics':{'versionSupport':True}}}},1);result=wait(lambda m:m.get('id')==1)['result'];caps=result['capabilities'];assert not caps.get('renameProvider');assert caps['textDocumentSync']['change']==2;rows.append({'id':'initialize','passed':True})
 send('initialized',{})
 send('textDocument/didOpen',{'textDocument':{'uri':uri,'languageId':'yaml','version':1,'text':invalid}});d=diag(1);assert any(x['code']=='SCHEMA_TYPE' and x['range']['start']['line']==5 for x in d);rows.append({'id':'unsaved-overrides-disk','passed':True})
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':2},'contentChanges':[{'range':{'start':{'line':5,'character':11},'end':{'line':5,'character':13}},'text':'default'}]});d=diag(2);assert not any(x['code']=='SCHEMA_TYPE' for x in d);rows.append({'id':'incremental-fix-clears-diagnostic','passed':True})
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':3},'contentChanges':[{'text':invalid}]});assert any(x['code']=='SCHEMA_TYPE' for x in diag(3))
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':2},'contentChanges':[{'text':valid}]})
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':4},'contentChanges':[{'range':{'start':{'line':5,'character':11},'end':{'line':5,'character':13}},'text':'43'}]});assert any(x['code']=='SCHEMA_TYPE' for x in diag(4));rows.append({'id':'stale-notification-cannot-roll-back','passed':True})
 config_uri=(folder/'.argocd-ls.yaml').as_uri()
 send('textDocument/didOpen',{'textDocument':{'uri':config_uri,'languageId':'yaml','version':1,'text':'schemaVersion: 1\ntargetVersion: \"99.0.0\"\n'}})
 bad=wait(lambda m:m.get('method')=='textDocument/publishDiagnostics' and m.get('params',{}).get('uri')==config_uri and m['params'].get('x-argocd-analysis-scope','workspace')=='workspace' and any(d.get('code')=='TARGET_UNKNOWN' for d in m['params']['diagnostics']));assert bad['params']['version']==1
 assert not any(x['code']=='SCHEMA_TYPE' for x in diag(4));rows.append({'id':'unsaved-invalid-config-invalidates-old-results','passed':True})
 send('textDocument/didChange',{'textDocument':{'uri':config_uri,'version':2},'contentChanges':[{'text':'schemaVersion: 1\ntargetVersion: \"3.5.6\"\ncontrolPlaneNamespace: argocd\n'}]});assert any(x['code']=='SCHEMA_TYPE' for x in diag(4));rows.append({'id':'unsaved-config-recovery','passed':True})
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':5},'contentChanges':[{'text':'apiVersion: argoproj.io/v1alpha1\nkind: Workflow\nspec:\n  project: 42\n'}]});assert diag(5)==[];rows.append({'id':'gvk-ownership-change-clears','passed':True})
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':6},'contentChanges':[{'text':valid}]});diag(6)
 send('textDocument/definition',{'textDocument':{'uri':uri},'position':{'line':5,'character':12}},10);locations=wait(lambda m:m.get('id')==10)['result'];assert len(locations)==1 and locations[0]['uri']==project_file.as_uri() and locations[0]['range']['start']['line']==3;rows.append({'id':'project-definition','passed':True})
 send('textDocument/references',{'textDocument':{'uri':project_file.as_uri()},'position':{'line':3,'character':9},'context':{'includeDeclaration':False}},11);refs=wait(lambda m:m.get('id')==11)['result'];assert len(refs)==1 and refs[0]['uri']==uri;rows.append({'id':'project-references','passed':True})
 send('textDocument/documentSymbol',{'textDocument':{'uri':uri}},12);symbols=wait(lambda m:m.get('id')==12)['result'];assert any(x['name']=='sample' for x in symbols);rows.append({'id':'resource-symbol','passed':True})
 send('textDocument/hover',{'textDocument':{'uri':uri},'position':{'line':5,'character':5}},13);hover=wait(lambda m:m.get('id')==13)['result'];assert 'string' in hover['contents']['value'] and '3.5.6' in hover['contents']['value'];rows.append({'id':'target-schema-hover','passed':True})
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':7},'contentChanges':[{'text':valid.replace('project: default','proj: default')}]});diag(7)
 send('textDocument/completion',{'textDocument':{'uri':uri},'position':{'line':5,'character':6}},14);items=wait(lambda m:m.get('id')==14)['result'];candidate=next(x for x in items if x['label']=='project');assert candidate['textEdit']['range']['start']=={'line':5,'character':2};rows.append({'id':'schema-prefix-completion','passed':True})
 send('textDocument/codeAction',{'textDocument':{'uri':uri},'range':{'start':{'line':5,'character':2},'end':{'line':5,'character':6}},'context':{'diagnostics':[]}},15);actions=wait(lambda m:m.get('id')==15)['result'];action=next(x for x in actions if x['data']['fixCode']=='FIX_FIELD_NAME')
 send('codeAction/resolve',action,16);resolved=wait(lambda m:m.get('id')==16)['result'];change=resolved['edit']['documentChanges'][0];assert change['textDocument']=={'uri':uri,'version':7};edit=change['edits'][0];assert edit['newText']=='project';rows.append({'id':'versioned-field-fix','passed':True})
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':8},'contentChanges':[{'range':edit['range'],'text':edit['newText']}]});assert not any(x['code']=='SCHEMA_UNKNOWN_FIELD' for x in diag(8));rows.append({'id':'field-fix-applies-through-buffer','passed':True})
 send('codeAction/resolve',action,17);assert wait(lambda m:m.get('id')==17)['error']['code']==-32801;rows.append({'id':'old-fix-rejected','passed':True})
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':9},'contentChanges':[{'text':valid.replace('project: default','proj: default')}]});assert any(x['code']=='SCHEMA_UNKNOWN_FIELD' for x in diag(9));rows.append({'id':'buffer-undo-restores-diagnostic','passed':True})
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':10},'contentChanges':[{'text':valid.replace('project: default','proj: 42')}]});diag(10)
 send('textDocument/codeAction',{'textDocument':{'uri':uri},'range':{'start':{'line':5,'character':2},'end':{'line':5,'character':6}},'context':{'diagnostics':[]}},18);unsafe=wait(lambda m:m.get('id')==18)['result'];assert not any(x['data']['fixCode']=='FIX_FIELD_NAME' for x in unsafe);rows.append({'id':'fix-cannot-introduce-type-error','passed':True})
 send('workspace/didChangeWorkspaceFolders',{'event':{'removed':[{'uri':folder.as_uri(),'name':'workspace'}],'added':[]}});wait(lambda m:m.get('method')=='textDocument/publishDiagnostics' and m.get('params',{}).get('uri')==uri and m['params'].get('x-argocd-analysis-scope','workspace')=='workspace' and m['params']['diagnostics']==[]);rows.append({'id':'removed-root-clears-diagnostics','passed':True})
 send('workspace/didChangeWorkspaceFolders',{'event':{'removed':[],'added':[{'uri':folder.as_uri(),'name':'workspace'}]}});assert any(x['code']=='SCHEMA_UNKNOWN_FIELD' for x in diag(10));rows.append({'id':'added-root-restores-buffer-analysis','passed':True})
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':11},'contentChanges':[{'text':valid.replace('project: default','project: missing')}]});assert any(x['code']=='CD_PROJECT_UNRESOLVED' for x in diag(11))
 send('textDocument/codeAction',{'textDocument':{'uri':uri},'range':{'start':{'line':5,'character':11},'end':{'line':5,'character':18}},'context':{'diagnostics':[]}},20);reference=next(x for x in wait(lambda m:m.get('id')==20)['result'] if x['data']['fixCode']=='FIX_REFERENCE_CANDIDATE');send('codeAction/resolve',reference,21);edit=wait(lambda m:m.get('id')==21)['result']['edit']['documentChanges'][0]['edits'][0]
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':12},'contentChanges':[{'range':edit['range'],'text':edit['newText']}]});assert not any(x['code']=='CD_PROJECT_UNRESOLVED' for x in diag(12));rows.append({'id':'explicit-project-candidate-fix','passed':True})
 enum_uri=(folder/'enum.yaml').as_uri();enum_text='apiVersion: argoproj.io/v1alpha1\nkind: ApplicationSet\nmetadata:\n  name: set\nspec:\n  generators: []\n  syncPolicy:\n    applicationsSync: wrong\n  template:\n    metadata:\n      name: child\n    spec:\n      project: default\n      destination: {server: x, namespace: default}\n'
 send('textDocument/didOpen',{'textDocument':{'uri':enum_uri,'languageId':'yaml','version':1,'text':enum_text}});assert any(x['code']=='SCHEMA_CONSTRAINT' for x in diag(1,enum_uri))
 send('textDocument/codeAction',{'textDocument':{'uri':enum_uri},'range':{'start':{'line':7,'character':22},'end':{'line':7,'character':27}},'context':{'diagnostics':[]}},22);enum=next(x for x in wait(lambda m:m.get('id')==22)['result'] if x['data']['fixCode']=='FIX_ENUM_VALUE' and x['data']['newText']=='\"sync\"');send('codeAction/resolve',enum,23);edit=wait(lambda m:m.get('id')==23)['result']['edit']['documentChanges'][0]['edits'][0]
 send('textDocument/didChange',{'textDocument':{'uri':enum_uri,'version':2},'contentChanges':[{'range':edit['range'],'text':edit['newText']}]});assert not any(x['code']=='SCHEMA_CONSTRAINT' for x in diag(2,enum_uri));rows.append({'id':'enum-fix-in-unsaved-new-file','passed':True})
 mixed=valid+'---\n'+valid.replace('name: sample','name: other').replace('project: default','project: missing')
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':13},'contentChanges':[{'text':mixed}]});diag(13)
 send('textDocument/codeAction',{'textDocument':{'uri':uri},'range':{'start':{'line':5,'character':11},'end':{'line':5,'character':18}},'context':{'diagnostics':[]}},24);assert not any(x['data']['fixCode']=='FIX_REFERENCE_CANDIDATE' for x in wait(lambda m:m.get('id')==24)['result']);rows.append({'id':'fix-does-not-borrow-other-document-error','passed':True})
 sources='apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: sources\nspec:\n  project: default\n  sources:\n    - repoURL: https://example.org/values\n      ref: values\n    - repoURL: https://example.org/chart\n      helm:\n        valueFiles:\n          - $values/values.yaml\n  destination: {server: x, namespace: default}\n'
 send('textDocument/didChange',{'textDocument':{'uri':uri,'version':14},'contentChanges':[{'text':sources}]});diag(14)
 send('textDocument/definition',{'textDocument':{'uri':uri},'position':{'line':12,'character':16}},25);defs=wait(lambda m:m.get('id')==25)['result'];assert len(defs)==1 and defs[0]['range']['start']['line']==8;rows.append({'id':'source-ref-definition','passed':True})
 send('textDocument/references',{'textDocument':{'uri':uri},'position':{'line':8,'character':13},'context':{'includeDeclaration':False}},26);refs=wait(lambda m:m.get('id')==26)['result'];assert len(refs)==1 and refs[0]['range']['start']['line']==12;rows.append({'id':'source-ref-references','passed':True})
 send('shutdown',{},90);assert wait(lambda m:m.get('id')==90).get('result') is None;send('exit');p.wait(timeout=5);assert p.returncode==0;rows.append({'id':'shutdown-exit','passed':True})
except Exception as e:rows.append({'id':'failure','passed':False,'error':repr(e)})
finally:
 if p.poll() is None:p.kill();p.wait()
 thread.join(timeout=2);(out/'stdout').write_bytes(b''.join(raw));(out/'stdin').write_bytes(b''.join(sent));(out/'stderr').write_bytes(p.stderr.read());(out/'results.json').write_text(json.dumps({'exit':p.returncode,'cases':rows},indent=2)+'\n');(out/'manifest.json').write_text(json.dumps({'command':[str(binary),'serve','--stdio'],'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'testSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'inputSHA256':hashlib.sha256(b''.join(sent)).hexdigest(),'python':sys.version},indent=2)+'\n')
print(json.dumps(rows));sys.exit(0 if len(rows)>=6 and all(r['passed'] for r in rows) else 1)
