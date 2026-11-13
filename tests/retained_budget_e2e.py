#!/usr/bin/env python3
"""Independent exact retained-text boundaries and real owner-buffer recovery."""
import argparse,hashlib,json,os,pathlib,queue,subprocess,sys,threading,time
ap=argparse.ArgumentParser();ap.add_argument('binary',type=pathlib.Path);ap.add_argument('output',type=pathlib.Path);ap.add_argument('--mode',choices=['cli','lsp','lsp-multiroot'],required=True);a=ap.parse_args();binary=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);LIMIT=128*1024*1024;FILE=5*1024*1024;rows=[]
CFG='schemaVersion: 1\ntargetVersion: "3.5.6"\n'
APP='apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: item}\nspec:\n  project: default\n  source: {repoURL: https://example.test/repo, path: app}\n  destination: {server: local}\n'
def padded(size):return '#'+('x'*(size-2))+'\n'
def record(name,checks,observed=None):rows.append({'id':name,'checks':checks,'observed':observed,'passed':all(checks.values())})
def rss(pid):return int(pathlib.Path(f'/proc/{pid}/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
class Client:
 def __init__(self,root,second=None):
  self.sent=(out/'stdin').open('wb');self.raw=(out/'stdout').open('wb');self.err=(out/'stderr').open('wb');self.events=[];self.q=queue.Queue();self.seq=10
  self.p=subprocess.Popen([str(binary),'serve','--stdio'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.err)
  def read():
   try:
    while True:
     headers={}
     while True:
      line=self.p.stdout.readline();self.raw.write(line)
      if not line:return
      if line==b'\r\n':break
      k,v=line.decode().strip().split(':',1);headers[k.lower()]=v.strip()
     body=self.p.stdout.read(int(headers['content-length']));self.raw.write(body);m=json.loads(body);self.events.append(m);self.q.put(m)
   except Exception as e:self.q.put({'readerError':repr(e)})
  self.thread=threading.Thread(target=read,daemon=True);self.thread.start();self.request('initialize',{'rootUri':root.as_uri(),'initializationOptions':{'targetVersion':'3.5.6'},'workspaceFolders':[{'uri':p.as_uri(),'name':p.name} for p in [root,second] if p is not None],'capabilities':{'textDocument':{'publishDiagnostics':{'versionSupport':True}}}});self.send('initialized',{})
 def send(self,method,params=None,ident=None):
  m={'jsonrpc':'2.0','method':method}
  if params is not None:m['params']=params
  if ident is not None:m['id']=ident
  b=json.dumps(m).encode();frame=f'Content-Length: {len(b)}\r\n\r\n'.encode()+b;self.sent.write(frame);self.p.stdin.write(frame);self.p.stdin.flush()
 def wait(self,predicate,timeout=12):
  end=time.monotonic()+timeout
  while True:
   m=self.q.get(timeout=max(.001,end-time.monotonic()))
   if predicate(m):return m
 def request(self,method,params):
  self.seq+=1;i=self.seq;self.send(method,params,i);return self.wait(lambda m:m.get('id')==i)
 def barrier(self):self.request('workspace/symbol',{'query':''})
 def open(self,p,text,version=1):self.send('textDocument/didOpen',{'textDocument':{'uri':p.as_uri(),'languageId':'yaml','version':version,'text':text}});self.barrier()
 def change(self,p,version,changes):self.send('textDocument/didChange',{'textDocument':{'uri':p.as_uri(),'version':version},'contentChanges':changes});self.barrier()
 def diag(self,p,version,code=None):
  matches=lambda m:m.get('method')=='textDocument/publishDiagnostics' and m['params'].get('uri')==p.as_uri() and m['params'].get('version')==version and m['params'].get('x-argocd-analysis-scope','workspace')=='workspace' and (code is None or any(d.get('code')==code for d in m['params']['diagnostics']))
  # Owner barriers may receive an immediate refusal before their response.
  prior=next((m for m in reversed(self.events) if matches(m)),None)
  return (prior if prior is not None else self.wait(matches))['params']['diagnostics']
 def close(self,p):self.send('textDocument/didClose',{'textDocument':{'uri':p.as_uri()}});self.barrier()
 def stop(self):
  try:self.request('shutdown',{});self.send('exit');self.p.wait(timeout=5)
  finally:
   if self.p.poll() is None:self.p.kill();self.p.wait()
   self.thread.join(timeout=2)
   for f in [self.sent,self.raw,self.err]:f.close()
   (out/'messages.json').write_text(json.dumps(self.events,indent=2)+'\n')
client=None;metadata={}
try:
 cg=pathlib.Path('/sys/fs/cgroup')/pathlib.Path('/proc/self/cgroup').read_text().strip().split('::',1)[1].lstrip('/');limit=(cg/'memory.max').read_text().strip();assert limit!='max' and int(limit)==1024**3,'positive boundary runner requires actual1GiB safety scope';metadata['memory.max']=limit
 if a.mode=='cli':
  for delta in [-1,0,1]:
   folder=out/str(delta);work=folder/'workspace';work.mkdir(parents=True);(work/'.argocd-ls.yaml').write_text(CFG);(work/'owned.yaml').write_text(APP);remaining=LIMIT+delta-len(CFG)-len(APP);n=0;seed=folder/'seed';seed.write_text(padded(FILE))
   while remaining:
    size=min(FILE,remaining);p=work/f'padding-{n:03}.yaml'
    if size==FILE:os.link(seed,p)
    else:p.write_text(padded(size))
    remaining-=size;n+=1
   actual=sum(p.stat().st_size for p in work.iterdir());assert actual==LIMIT+delta;cmd=[str(binary),'check','--root',str(work),'--format','json'];p=subprocess.run(cmd,capture_output=True,timeout=12);(folder/'stdout').write_bytes(p.stdout);(folder/'stderr').write_bytes(p.stderr);d=json.loads(p.stdout)
   if delta<=0:checks={'exit':p.returncode==0,'resources':d['resourcesAnalyzed']==1,'files':d['filesAnalyzed']==n+2,'configuration':d['configurationErrors']==[],'diagnostics':[(x['code'],x['severity']) for x in d['diagnostics']]==[('CD_PROJECT_NAMESPACE_UNBOUND',3)]}
   else:checks={'exit':p.returncode==2,'resources':d['resourcesAnalyzed']==0,'files':d['filesAnalyzed']==0,'reason':[x['code'] for x in d['configurationErrors']]==['INPUT_BUDGET_EXCEEDED']}
   checks['complete']=d['analysisComplete']==(delta<=0);record('aggregate-'+str(delta),checks,{'inputBytes':actual,'command':cmd,'report':d})
 else:
  work=out/'workspace';work.mkdir();config=work/'.argocd-ls.yaml';config.write_text(CFG);app=work/'app.yaml';app.write_text(APP);second=out/'second-root' if a.mode=='lsp-multiroot' else None
  if second is not None:second.mkdir()
  client=Client(work,second)
  # The opened configuration is charged even though it declares no resource.
  cfg=CFG+padded(1024*1024-len(CFG));client.open(config,cfg);client.open(app,APP);remaining=LIMIT-len(cfg)-len(APP);paths=[];n=0
  while remaining:
   size=min(FILE,remaining);p=(second if second is not None and n%2 else work)/f'buffer-{n:03}.yaml';client.open(p,padded(size));paths.append(p);remaining-=size;n+=1
  client.diag(paths[-1],1);hover=client.request('textDocument/hover',{'textDocument':{'uri':app.as_uri()},'position':{'line':5,'character':12}});record('owner-exact-boundary',{'hover':bool(hover.get('result')),'noBudgetError':not any(d.get('code')=='INPUT_BUDGET_EXCEEDED' for m in client.events if m.get('method')=='textDocument/publishDiagnostics' for d in m['params']['diagnostics'])},{'bytes':LIMIT,'rss':rss(client.p.pid),'hover':hover})
  extra=work/'overflow.yaml';client.open(extra,'#');ds=client.diag(extra,1,'INPUT_BUDGET_EXCEEDED');record('owner-overflow-open',{'reason':any(d['code']=='INPUT_BUDGET_EXCEEDED' for d in ds)},ds)
  client.change(app,2,[{'text':APP+'#'}]);ds=client.diag(app,2,'INPUT_BUDGET_EXCEEDED');hover=client.request('textDocument/hover',{'textDocument':{'uri':app.as_uri()},'position':{'line':5,'character':12}});record('refusal-hides-old-buffer-and-disk',{'reason':any(d['code']=='INPUT_BUDGET_EXCEEDED' for d in ds),'noOldHover':'error' in hover},{'diagnostics':ds,'hover':hover})
  client.close(extra);client.close(paths[0]);client.change(app,3,[{'range':{'start':{'line':0,'character':0},'end':{'line':0,'character':0}},'text':'#'}]);ds=client.diag(app,3,'INPUT_BUDGET_EXCEEDED');record('incremental-cannot-restore-refused',{'reason':bool(ds)},ds)
  client.change(app,4,[{'text':APP}]);ds=client.diag(app,4);record('full-replacement-recovers',{'onlyExpectedDiagnostic':[(d['code'],d['severity']) for d in ds]==[('CD_PROJECT_NAMESPACE_UNBOUND',3)]},ds)
  samples=[]
  for version in range(5,10):
   client.change(app,version,[{'text':APP}]);ds=client.diag(app,version);samples.append(rss(client.p.pid));record('same-size-replacement-'+str(version),{'current':[(d['code'],d['severity']) for d in ds]==[('CD_PROJECT_NAMESPACE_UNBOUND',3)]})
  metadata['replacementRSS']=samples
  for p in paths[1:]:client.close(p)
  client.close(config);client.close(app);client.open(app,APP,20);ds=client.diag(app,20);record('close-reopen-clears-state',{'restored':[(d['code'],d['severity']) for d in ds]==[('CD_PROJECT_NAMESPACE_UNBOUND',3)]},ds)
except Exception as e:rows.append({'id':'runner','passed':False,'error':repr(e)})
finally:
 if client:
  try:client.stop();record('shutdown',{'exit':client.p.returncode==0})
  except Exception as e:rows.append({'id':'shutdown','passed':False,'error':repr(e)})
 sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();report={'mode':a.mode,'command':sys.argv,'binarySHA256':sha(binary),'runnerSHA256':sha(pathlib.Path(__file__)),'metadata':metadata,'rows':rows,'passed':bool(rows) and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'mode':a.mode,'rows':len(rows),'failed':[r['id'] for r in rows if not r['passed']]}));sys.exit(0 if report['passed'] else 1)
