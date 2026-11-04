#!/usr/bin/env python3
"""Current scope facts and unrelated scope identity through real LSP edits."""
import argparse,hashlib,json,pathlib,sys,time
import audit_repair_e2e as wire
ap=argparse.ArgumentParser();ap.add_argument('binary',type=pathlib.Path);ap.add_argument('output',type=pathlib.Path);a=ap.parse_args();wire.BINARY=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);root=out/'workspace';root.mkdir();rows=[];client=None
cfg='schemaVersion: 1\ntargetVersion: "3.5.6"\ncontrolPlaneNamespace: argocd\nreferencePolicy: closed\nscopes:\n  - name: first\n    include: ["first/**"]\n  - name: second\n    include: ["second/**"]\n    targetVersion: "3.4.12"\n';config=wire.write(root,'.argocd-ls.yaml',cfg)
apps={};projects={}
for scope in ['first','second']:
 apps[scope]=wire.write(root,scope+'/app.yaml',wire.APP);projects[scope]=wire.write(root,scope+'/project.yaml',wire.PROJECT)
def query(path,method,needle='project: team',delta=11):
 text=wire.APP;off=text.index(needle)+delta;before=text[:off];return client.request('textDocument/'+method,{'textDocument':{'uri':path.as_uri()},'position':{'line':before.count('\n'),'character':len(before.rsplit('\n',1)[-1])}})
def target_hover(path,version):
 deadline=time.monotonic()+5
 while time.monotonic()<deadline:
  result=query(path,'hover')
  if f'Target: {version}' in (result.get('result') or {}).get('contents',{}).get('value',''):return result
  time.sleep(.02)
 raise TimeoutError('current target hover was not observed')
def record(name,checks,observed=None):rows.append({'id':name,'checks':checks,'observed':observed,'passed':all(checks.values())})
try:
 client=wire.LSP(out,[root]);client.open(apps['first'],wire.APP,1);ds=client.diag(apps['first'],1);record('initial-first',{'noErrors':not [d for d in ds if d['severity']==1]},ds)
 client.open(apps['second'],wire.APP,1);ds=client.diag(apps['second'],1);record('initial-second',{'noErrors':not [d for d in ds if d['severity']==1]},ds)
 client.open(projects['first'],wire.PROJECT.replace('name: team','name: renamed'),1);client.diag(projects['first'],1)
 # A dependent publication can precede the project publication in a batch.
 current=[m['message']['params'] for m in client.messages if m['message'].get('method')=='textDocument/publishDiagnostics' and m['message']['params'].get('uri')==apps['first'].as_uri()][-1]
 ds=current['diagnostics']
 if not any(d.get('code')=='CD_PROJECT_UNRESOLVED' for d in ds):ds=client.wait(lambda m:m.get('method')=='textDocument/publishDiagnostics' and m.get('params',{}).get('uri')==apps['first'].as_uri() and any(d.get('code')=='CD_PROJECT_UNRESOLVED' for d in m['params']['diagnostics']))['params']['diagnostics']
 definition=query(apps['first'],'definition');other=query(apps['second'],'definition');hover=query(apps['second'],'hover')
 record('dependent-and-unrelated',{'missingProject':[(d['code'],d['severity']) for d in ds]==[('CD_PROJECT_UNRESOLVED',1)],'removedDefinition':definition.get('result')==[],'otherDefinition':len(other.get('result',[]))==1 and other['result'][0]['uri']==projects['second'].as_uri(),'otherTarget':'Target: 3.4.12' in (hover.get('result') or {}).get('contents',{}).get('value','')},{'diagnostics':ds,'definition':definition,'otherDefinition':other,'otherHover':hover})
 client.send('textDocument/didClose',{'textDocument':{'uri':projects['first'].as_uri()}})
 client.wait(lambda m:m.get('method')=='textDocument/publishDiagnostics' and m.get('params',{}).get('uri')==apps['first'].as_uri() and not m['params']['diagnostics'])
 definition=query(apps['first'],'definition');record('close-restores-disk',{'definition':len(definition.get('result',[]))==1},definition)
 client.open(config,cfg.replace('3.4.12','3.5.6'),1);client.diag(config,1);hover=query(apps['second'],'hover');record('config-target-refresh',{'currentTarget':'Target: 3.5.6' in (hover.get('result') or {}).get('contents',{}).get('value','')},hover)
 # Disk watch authority must refresh the affected scope without stale reuse.
 projects['second'].unlink();client.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':projects['second'].as_uri(),'type':3}]})
 ds=client.wait(lambda m:m.get('method')=='textDocument/publishDiagnostics' and m.get('params',{}).get('uri')==apps['second'].as_uri() and any(d.get('code')=='CD_PROJECT_UNRESOLVED' for d in m['params']['diagnostics']))['params']['diagnostics'];record('watched-delete',{'missing':[(d['code'],d['severity']) for d in ds]==[('CD_PROJECT_UNRESOLVED',1)]},ds)
 projects['second'].write_text(wire.PROJECT);client.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':projects['second'].as_uri(),'type':1}]});client.wait(lambda m:m.get('method')=='textDocument/publishDiagnostics' and m.get('params',{}).get('uri')==apps['second'].as_uri() and not m['params']['diagnostics']);record('watched-create',{'restored':len(query(apps['second'],'definition').get('result',[]))==1})
 # Root ownership changes override the parent scope and return on removal.
 nested=root/'second';wire.write(nested,'.argocd-ls.yaml','schemaVersion: 1\ntargetVersion: "3.0.0"\ncontrolPlaneNamespace: argocd\n')
 client.send('workspace/didChangeWorkspaceFolders',{'event':{'added':[{'uri':nested.as_uri(),'name':'nested'}],'removed':[]}});hover=target_hover(apps['second'],'3.0.0');record('deepest-root-added',{'target':'Target: 3.0.0' in (hover.get('result') or {}).get('contents',{}).get('value','')},hover)
 client.send('workspace/didChangeWorkspaceFolders',{'event':{'removed':[{'uri':nested.as_uri(),'name':'nested'}],'added':[]}});hover=target_hover(apps['second'],'3.5.6');record('root-removed-parent-restored',{'target':'Target: 3.5.6' in (hover.get('result') or {}).get('contents',{}).get('value','')},hover)
 # A local repository fact changes without YAML bytes changing.
 repo=root/'repo';repo.mkdir();mapped=cfg.replace('3.4.12','3.5.6')+'repositories:\n  - repoURL: https://example.org/repo\n    path: repo\n';client.change(config,mapped,2);client.diag(config,2)
 apptext=wire.APP.replace('repoURL: https://example.org/repo','repoURL: https://example.org/repo, path: child');client.change(apps['first'],apptext,2);ds=client.diag(apps['first'],2);record('local-path-missing',{'warning':[(d['code'],d['severity']) for d in ds]==[('CD_LOCAL_PATH_MISSING',2)]},ds)
 (repo/'child').mkdir();client.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':(repo/'child').as_uri(),'type':1}]});ds=client.wait(lambda m:m.get('method')=='textDocument/publishDiagnostics' and m.get('params',{}).get('uri')==apps['first'].as_uri() and m['params'].get('version')==2 and not m['params']['diagnostics'])['params']['diagnostics'];record('local-path-watch-refresh',{'clear':not ds})
 # Unnotified disk facts are eventually refreshed by the fixed consistency scan.
 (repo/'child').rmdir();ds=client.wait(lambda m:m.get('method')=='textDocument/publishDiagnostics' and m.get('params',{}).get('uri')==apps['first'].as_uri() and any(d.get('code')=='CD_LOCAL_PATH_MISSING' for d in m['params']['diagnostics']),timeout=36)['params']['diagnostics'];record('consistency-rescan',{'warning':[(d['code'],d['severity']) for d in ds]==[('CD_LOCAL_PATH_MISSING',2)]},ds)
except Exception as e:rows.append({'id':'runner','passed':False,'error':repr(e)})
finally:
 if client:client.close();rows.append({'id':'shutdown','passed':client.proc.returncode==0,'exitCode':client.proc.returncode})
 sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();report={'binarySHA256':sha(wire.BINARY),'testSHA256':sha(pathlib.Path(__file__)),'rows':rows,'passed':bool(rows) and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(rows));sys.exit(0 if report['passed'] else 1)
