#!/usr/bin/env python3
"""Real LSP publication identity, lifecycle clearing and recovery controls."""
import hashlib,json,os,pathlib,sys,time
import audit_repair_e2e as wire
from persistent_workspace_e2e import Client, Traces, wait_ready, wait_until
wire.BINARY=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False);root=out/'workspace';root.mkdir();rows=[];client=None
os.environ['ARGOCD_LS_TRACE']='1'
config=wire.write(root,'.argocd-ls.yaml',wire.CONFIG);app=wire.write(root,'app.yaml',wire.APP);other=wire.write(root,'other.yaml',wire.APP.replace('name: app','name: other'));project=wire.write(root,'project.yaml',wire.PROJECT)
def record(name,checks,observed=None):rows.append({'id':name,'checks':checks,'observed':observed,'passed':all(checks.values())})
def publications(start=0,scope='workspace'):return [x['message']['params'] for x in client.messages[start:] if x['message'].get('method')=='textDocument/publishDiagnostics' and (scope is None or x['message']['params'].get('x-argocd-analysis-scope','workspace')==scope)]
def barrier():return client.request('textDocument/documentSymbol',{'textDocument':{'uri':app.as_uri()}})
def document_passes():return b''.join(client.stderr_chunks).count(b'argocd-document-analysis ')
def document_pass(mark):
 wait_until(client,lambda:True if document_passes()>mark else None,6,'accepted document analysis')
 barrier() # The trace precedes publication; the protocol response drains it.
def until(path,version,codes,since=None):
 matches=lambda m:m.get('method')=='textDocument/publishDiagnostics' and m['params'].get('uri')==path.as_uri() and m['params'].get('version')==version and m['params'].get('x-argocd-analysis-scope','workspace')=='workspace' and [d['code'] for d in m['params']['diagnostics']]==codes
 prior=next((entry['message'] for entry in client.messages[since:] if matches(entry['message'])),None) if since is not None else None
 return (prior if prior is not None else client.wait(matches))['params']
try:
 client=Client(out,[root]);traces=Traces(client);local_mark=document_passes();client.open(app,wire.APP,1);ds=client.diag(app,1)
 # A local query is not a discovery barrier. Finish initial discovery before
 # attributing later publications to the newly opened file.
 wait_ready(client,traces,0,3,15,False);document_pass(local_mark);initial=publications();record('initial-publications',{'activeEmpty':ds==[],'allResources':{app.as_uri(),other.as_uri(),project.as_uri()}<={p['uri'] for p in initial}},initial)
 start=len(client.messages);local_mark=document_passes();client.open(other,other.read_text(),1);ds=client.diag(other,1);document_pass(local_mark);observed=publications(start);local=publications(start,'document');record('unchanged-payloads-suppressed',{'newOpenVersion':ds==[],'onlyChangedURI':len(observed)==1 and observed[0]['uri']==other.as_uri() and observed[0]['version']==1,'noUnrelatedLocalRepublish':not local or len(local)==1 and local[0]['uri']==other.as_uri() and local[0]['version']==1},observed+local)
 start=len(client.messages);local_mark=document_passes();client.change(app,wire.APP,2);ds=client.diag(app,2);document_pass(local_mark);observed=publications(start);local=publications(start,'document');record('changed-empty-version-published',{'empty':ds==[],'exactOne':len(observed)==1 and observed[0]['uri']==app.as_uri() and observed[0]['version']==2,'localVersionIfPublished':not local or len(local)==1 and local[0]['uri']==app.as_uri() and local[0]['version']==2 and local[0]['diagnostics']==[]},observed+local)
 client.open(project,wire.PROJECT.replace('name: team','name: renamed'),1);client.diag(project,1);barrier();latest={p['uri']:p for p in publications()};record('cross-file-same-version-error',{'active':latest[app.as_uri()]['version']==2 and [d['code'] for d in latest[app.as_uri()]['diagnostics']]==['CD_PROJECT_UNRESOLVED'],'other':latest[other.as_uri()]['version']==1 and [d['code'] for d in latest[other.as_uri()]['diagnostics']]==['CD_PROJECT_UNRESOLVED']},latest)
 client.change(project,wire.PROJECT,2);client.diag(project,2);barrier();latest={p['uri']:p for p in publications()};record('cross-file-same-version-clear',{'active':latest[app.as_uri()]['version']==2 and latest[app.as_uri()]['diagnostics']==[],'other':latest[other.as_uri()]['version']==1 and latest[other.as_uri()]['diagnostics']==[]},latest)
 client.change(app,'#'+'x'*(5*1024*1024),3);failure=until(app,3,['INPUT_BUDGET_EXCEEDED']);record('direct-budget-failure',{'explicit':failure['version']==3},failure)
 missing=wire.APP.replace('project: team','project: absent');client.change(app,missing,4);restored=until(app,4,['CD_PROJECT_UNRESOLVED']);record('budget-full-replacement',{'currentVersion':restored['version']==4},restored)
 client.open(config,wire.CONFIG+'unknownField: true\n',1);failure=until(config,1,['CONFIG_INVALID']);barrier();latest={p['uri']:p for p in publications()};record('configuration-failure-clears-prior-error',{'configError':bool(failure),'activeCleared':latest[app.as_uri()]['version']==4 and latest[app.as_uri()]['diagnostics']==[]},latest)
 client.change(config,wire.CONFIG,2);client.diag(config,2);barrier();latest={p['uri']:p for p in publications()};record('configuration-recovery-restores-same-version-error',{'restored':latest[app.as_uri()]['version']==4 and [d['code'] for d in latest[app.as_uri()]['diagnostics']]==['CD_PROJECT_UNRESOLVED']},latest)
 # A scalar edit changes a diagnostic payload at the same open-app version.
 client.change(project,wire.PROJECT.replace('name: team','name: absent'),3);client.diag(project,3);barrier();latest={p['uri']:p for p in publications()};record('same-version-payload-change-after-recovery',{'activeClear':latest[app.as_uri()]['version']==4 and latest[app.as_uri()]['diagnostics']==[],'otherError':[d['code'] for d in latest[other.as_uri()]['diagnostics']]==['CD_PROJECT_UNRESOLVED']},latest)
 # The owner clears a closed buffer immediately; wait for actual dependent
 # workspace replacements, not a documentSymbol response from the local lane.
 start=len(client.messages);client.send('textDocument/didClose',{'textDocument':{'uri':project.as_uri()}});cleared=until(project,None,[]);until(app,4,['CD_PROJECT_UNRESOLVED'],start);until(other,1,[],start);barrier();latest={p['uri']:p for p in publications()};record('close-restores-disk',{'closedVersion':cleared.get('version') is None,'activeError':[d['code'] for d in latest[app.as_uri()]['diagnostics']]==['CD_PROJECT_UNRESOLVED'],'otherClear':latest[other.as_uri()]['diagnostics']==[]},latest)
 # Removal must clear a previously published empty open-document payload.
 other.unlink();client.send('textDocument/didClose',{'textDocument':{'uri':other.as_uri()}});removed=until(other,None,[]);record('removed-empty-document-clear',{'explicitEmpty':removed['diagnostics']==[]},removed)
 ephemeral=root/'ephemeral.yaml';client.open(ephemeral,missing,1);client.diag(ephemeral,1);client.change(ephemeral,wire.APP,2);client.send('textDocument/didClose',{'textDocument':{'uri':ephemeral.as_uri()}});cleared=until(ephemeral,None,[]);barrier();settled=time.monotonic()-client.start;time.sleep(.2);stale=[p for x in client.messages if x['elapsed']>settled for p in [x['message'].get('params',{})] if x['message'].get('method')=='textDocument/publishDiagnostics' and p.get('uri')==ephemeral.as_uri() and p.get('diagnostics')];record('cancelled-edit-close-stays-clear',{'clear':cleared['diagnostics']==[],'noStaleErrors':not stale},stale)
 # An actual Schema error must clear after repair. An early local publication
 # must have the right version/content; an already delivered stronger workspace
 # result can suppress that same input's late local publication.
 local_file=root/'local-only.yaml';start=len(client.messages);local_mark=document_passes();client.open(local_file,wire.APP.replace('project: team','project: 42'),1);before=client.diag(local_file,1);document_pass(local_mark)
 local_mark=document_passes();client.change(local_file,wire.APP,2);after=client.diag(local_file,2);document_pass(local_mark);local_mark=document_passes();client.send('textDocument/didClose',{'textDocument':{'uri':local_file.as_uri()}});cleared=until(local_file,None,[]);document_pass(local_mark)
 local=[p for p in publications(start,'document') if p['uri']==local_file.as_uri()];updates=[p for p in local if p.get('version') is not None];all_phases=[p for p in publications(start,None) if p['uri']==local_file.as_uri()]
 versions=[p['version'] for p in updates]
 record('document-schema-error-and-clear',{'localVersionsCurrent':versions==sorted(set(versions)) and set(versions)<={1,2},'localErrorWhenPublished':all(any(d['code']=='SCHEMA_TYPE' for d in p['diagnostics']) for p in updates if p['version']==1),'localClearWhenPublished':all(p['diagnostics']==[] for p in updates if p['version']==2),'localCloseClear':any(p.get('version') is None and p['diagnostics']==[] for p in local),'workspaceError':any(d['code']=='SCHEMA_TYPE' for d in before),'workspaceClear':after==[],'closeRemainsClear':bool(all_phases) and all_phases[-1].get('version') is None and all_phases[-1]['diagnostics']==[]},all_phases)
 # Zed can close/reopen the same buffer before debounce fires, resetting its
 # version. Clearing a document must also invalidate local-publication dedup.
 # An incomplete second document prevents a stronger workspace proof, so the
 # local result is required even if background analysis finishes first.
 partial='\n---\nkind: [\n'
 reopened=root/'reopened.yaml';invalid=wire.APP.replace('project: team','project: 42')+partial;local_mark=document_passes();client.open(reopened,invalid,1);client.diag(reopened,1);document_pass(local_mark);start=len(client.messages)
 local_mark=document_passes();client.send('textDocument/didClose',{'textDocument':{'uri':reopened.as_uri()}});client.open(reopened,invalid,1);restored=client.diag(reopened,1);document_pass(local_mark)
 local=[p for p in publications(start,'document') if p['uri']==reopened.as_uri()];phases=[p for p in publications(start,None) if p['uri']==reopened.as_uri()]
 # Independent workers can finish in either order. Both must restore the
 # current error after the close clear; observing one phase must not close the
 # buffer and cancel the other phase before its actual result is inspected.
 restored_phases={p.get('x-argocd-analysis-scope') for p in phases[1:] if p.get('version')==1 and any(d['code']=='SCHEMA_TYPE' for d in p['diagnostics'])}
 record('document-reopen-restores-diagnostics',{'localClearThenRestore':[p.get('version') for p in local]==[None,1],'clearPayload':bool(local) and local[0]['diagnostics']==[],'bothPhasesRestoredAfterClear':bool(phases) and phases[0].get('version') is None and phases[0]['diagnostics']==[] and restored_phases=={'document','workspace'},'workspaceRestored':any(d['code']=='SCHEMA_TYPE' for d in restored)},phases)
 local_mark=document_passes();client.send('textDocument/didClose',{'textDocument':{'uri':reopened.as_uri()}});until(reopened,None,[]);document_pass(local_mark)
 # Ordinary LSP clients replace a URI's whole diagnostic list. After a full
 # workspace result, a late Schema-only empty list must not erase its domain
 # error. Await actual local completion, then inspect the final visible list.
 domain_rounds=[]
 for iteration in range(4):
  start=len(client.messages);local_mark=document_passes()
  if iteration==0:client.change(app,missing,5);version=5
  else:client.send('textDocument/didClose',{'textDocument':{'uri':app.as_uri()}});client.open(app,missing,1);version=1
  client.diag(app,version);document_pass(local_mark)
  visible=[p for p in publications(start,None) if p['uri']==app.as_uri()]
  domain_rounds.append({'iteration':iteration,'finalHasCurrentDomainError':bool(visible) and visible[-1].get('version')==version and [d['code'] for d in visible[-1]['diagnostics']]==['CD_PROJECT_UNRESOLVED'],'publications':visible})
 record('late-local-cannot-clear-current-domain-error',{'allAcceptedInputsRetainDomainError':all(r['finalHasCurrentDomainError'] for r in domain_rounds)},domain_rounds)
 client.send('workspace/didChangeWorkspaceFolders',{'event':{'added':[],'removed':[{'uri':root.as_uri(),'name':root.name}]}});cleared=until(app,None,[]);record('root-removal-clear',{'explicitClear':cleared['diagnostics']==[]},cleared)
except Exception as e:record('runner',{'completed':False},repr(e))
finally:
 if client:client.close();record('shutdown',{'exit0':client.proc.returncode==0},client.proc.returncode)
 sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();report={'binarySHA256':sha(wire.BINARY),'runnerSHA256':sha(pathlib.Path(__file__)),'wireSHA256':sha(pathlib.Path(wire.__file__)),'rows':rows,'passed':len(rows)==18 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'rows':len(rows),'failed':[r['id'] for r in rows if not r['passed']]}))
sys.exit(0 if report['passed'] else 1)
