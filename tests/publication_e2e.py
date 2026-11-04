#!/usr/bin/env python3
"""Real LSP publication identity, lifecycle clearing and recovery controls."""
import hashlib,json,pathlib,sys,time
import audit_repair_e2e as wire
wire.BINARY=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False);root=out/'workspace';root.mkdir();rows=[];client=None
config=wire.write(root,'.argocd-ls.yaml',wire.CONFIG);app=wire.write(root,'app.yaml',wire.APP);other=wire.write(root,'other.yaml',wire.APP.replace('name: app','name: other'));project=wire.write(root,'project.yaml',wire.PROJECT)
def record(name,checks,observed=None):rows.append({'id':name,'checks':checks,'observed':observed,'passed':all(checks.values())})
def publications(start=0):return [x['message']['params'] for x in client.messages[start:] if x['message'].get('method')=='textDocument/publishDiagnostics']
def barrier():return client.request('textDocument/documentSymbol',{'textDocument':{'uri':app.as_uri()}})
def until(path,version,codes):
 return client.wait(lambda m:m.get('method')=='textDocument/publishDiagnostics' and m['params'].get('uri')==path.as_uri() and m['params'].get('version')==version and [d['code'] for d in m['params']['diagnostics']]==codes)['params']
try:
 client=wire.LSP(out,[root]);client.open(app,wire.APP,1);ds=client.diag(app,1);barrier();initial=publications();record('initial-publications',{'activeEmpty':ds==[],'allResources':{app.as_uri(),other.as_uri(),project.as_uri()}<={p['uri'] for p in initial}},initial)
 start=len(client.messages);client.open(other,other.read_text(),1);ds=client.diag(other,1);barrier();observed=publications(start);record('unchanged-payloads-suppressed',{'newOpenVersion':ds==[],'onlyChangedURI':len(observed)==1 and observed[0]['uri']==other.as_uri() and observed[0]['version']==1},observed)
 start=len(client.messages);client.change(app,wire.APP,2);ds=client.diag(app,2);barrier();observed=publications(start);record('changed-empty-version-published',{'empty':ds==[],'exactOne':len(observed)==1 and observed[0]['uri']==app.as_uri() and observed[0]['version']==2},observed)
 client.open(project,wire.PROJECT.replace('name: team','name: renamed'),1);client.diag(project,1);barrier();latest={p['uri']:p for p in publications()};record('cross-file-same-version-error',{'active':latest[app.as_uri()]['version']==2 and [d['code'] for d in latest[app.as_uri()]['diagnostics']]==['CD_PROJECT_UNRESOLVED'],'other':latest[other.as_uri()]['version']==1 and [d['code'] for d in latest[other.as_uri()]['diagnostics']]==['CD_PROJECT_UNRESOLVED']},latest)
 client.change(project,wire.PROJECT,2);client.diag(project,2);barrier();latest={p['uri']:p for p in publications()};record('cross-file-same-version-clear',{'active':latest[app.as_uri()]['version']==2 and latest[app.as_uri()]['diagnostics']==[],'other':latest[other.as_uri()]['version']==1 and latest[other.as_uri()]['diagnostics']==[]},latest)
 client.change(app,'#'+'x'*(5*1024*1024),3);failure=until(app,3,['INPUT_BUDGET_EXCEEDED']);record('direct-budget-failure',{'explicit':failure['version']==3},failure)
 missing=wire.APP.replace('project: team','project: absent');client.change(app,missing,4);restored=until(app,4,['CD_PROJECT_UNRESOLVED']);record('budget-full-replacement',{'currentVersion':restored['version']==4},restored)
 client.open(config,wire.CONFIG+'unknownField: true\n',1);failure=until(config,1,['CONFIG_INVALID']);barrier();latest={p['uri']:p for p in publications()};record('configuration-failure-clears-prior-error',{'configError':bool(failure),'activeCleared':latest[app.as_uri()]['version']==4 and latest[app.as_uri()]['diagnostics']==[]},latest)
 client.change(config,wire.CONFIG,2);client.diag(config,2);barrier();latest={p['uri']:p for p in publications()};record('configuration-recovery-restores-same-version-error',{'restored':latest[app.as_uri()]['version']==4 and [d['code'] for d in latest[app.as_uri()]['diagnostics']]==['CD_PROJECT_UNRESOLVED']},latest)
 # A scalar edit changes a diagnostic payload at the same open-app version.
 client.change(project,wire.PROJECT.replace('name: team','name: absent'),3);client.diag(project,3);barrier();latest={p['uri']:p for p in publications()};record('same-version-payload-change-after-recovery',{'activeClear':latest[app.as_uri()]['version']==4 and latest[app.as_uri()]['diagnostics']==[],'otherError':[d['code'] for d in latest[other.as_uri()]['diagnostics']]==['CD_PROJECT_UNRESOLVED']},latest)
 # Closing an opened project restores its disk value and dependent errors.
 client.send('textDocument/didClose',{'textDocument':{'uri':project.as_uri()}});cleared=until(project,None,[]);barrier();latest={p['uri']:p for p in publications()};record('close-restores-disk',{'closedVersion':cleared.get('version') is None,'activeError':[d['code'] for d in latest[app.as_uri()]['diagnostics']]==['CD_PROJECT_UNRESOLVED'],'otherClear':latest[other.as_uri()]['diagnostics']==[]},latest)
 # Removal must clear a previously published empty open-document payload.
 other.unlink();client.send('textDocument/didClose',{'textDocument':{'uri':other.as_uri()}});removed=until(other,None,[]);record('removed-empty-document-clear',{'explicitEmpty':removed['diagnostics']==[]},removed)
 ephemeral=root/'ephemeral.yaml';client.open(ephemeral,missing,1);client.diag(ephemeral,1);client.change(ephemeral,wire.APP,2);client.send('textDocument/didClose',{'textDocument':{'uri':ephemeral.as_uri()}});cleared=until(ephemeral,None,[]);barrier();settled=time.monotonic()-client.start;time.sleep(.2);stale=[p for x in client.messages if x['elapsed']>settled for p in [x['message'].get('params',{})] if x['message'].get('method')=='textDocument/publishDiagnostics' and p.get('uri')==ephemeral.as_uri() and p.get('diagnostics')];record('cancelled-edit-close-stays-clear',{'clear':cleared['diagnostics']==[],'noStaleErrors':not stale},stale)
 client.send('workspace/didChangeWorkspaceFolders',{'event':{'added':[],'removed':[{'uri':root.as_uri(),'name':root.name}]}});cleared=until(app,None,[]);record('root-removal-clear',{'explicitClear':cleared['diagnostics']==[]},cleared)
except Exception as e:record('runner',{'completed':False},repr(e))
finally:
 if client:client.close();record('shutdown',{'exit0':client.proc.returncode==0},client.proc.returncode)
 sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();report={'binarySHA256':sha(wire.BINARY),'runnerSHA256':sha(pathlib.Path(__file__)),'wireSHA256':sha(pathlib.Path(wire.__file__)),'rows':rows,'passed':len(rows)==15 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'rows':len(rows),'failed':[r['id'] for r in rows if not r['passed']]}))
sys.exit(0 if report['passed'] else 1)
