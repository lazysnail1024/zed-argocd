#!/usr/bin/env python3
"""Local field proofs and complete-workspace pin proofs across real LSP events."""
import argparse,hashlib,json,os,pathlib,sys,time
import audit_repair_e2e as wire
from persistent_workspace_e2e import Client, Traces, wait_until
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);p.add_argument('--skip-periodic',action='store_true');a=p.parse_args();wire.BINARY=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);rows=[];clients=[]
os.environ['ARGOCD_LS_TRACE']='1'
CONFIG=wire.CONFIG.replace('"3.0.0"','"3.5.6"');TEXT=wire.APP.replace('project: team','proj: team')
def record(name,checks,observed=None):rows.append({'id':name,'checks':checks,'observed':observed,'passed':all(checks.values())})
def create(name):
 folder=out/name;root=folder/'workspace';root.mkdir(parents=True);app=wire.write(root,'app.yaml',TEXT);config=wire.write(root,'.argocd-ls.yaml',CONFIG);project=wire.write(root,'project.yaml',wire.PROJECT);c=Client(folder,[root]);c.trace=Traces(c);clients.append(c);c.send('workspace/didChangeConfiguration',{'settings':{'targetVersion':'3.0.0'}});c.open(config,CONFIG,11);c.open(app,TEXT,7);c.diag(app,7);ready(c,app);return c,root,app,config,project
def ready(c,path,timeout=6):
 end=time.monotonic()+timeout;responses=[]
 while time.monotonic()<end:
  r=c.request('textDocument/codeAction',{'textDocument':{'uri':path.as_uri()},'range':{'start':{'line':5,'character':0},'end':{'line':6,'character':0}},'context':{'diagnostics':[]}});responses.append(r)
  if 'result' in r:return responses
  if r.get('error',{}).get('code')!=-32801:raise AssertionError(r)
  time.sleep(.02)
 raise TimeoutError('current analysis did not complete')
def action(c,path,line,code):
 end=time.monotonic()+6
 while time.monotonic()<end:
  r=c.request('textDocument/codeAction',{'textDocument':{'uri':path.as_uri()},'range':{'start':{'line':line,'character':0},'end':{'line':line+1,'character':0}},'context':{'diagnostics':[]}})
  found=next((x for x in r.get('result',[]) if x['data']['fixCode']==code),None)
  if found is not None:return found
  time.sleep(.02)
 raise AssertionError('expected action did not become available: '+code)
def actions(c,app,config):return [action(c,app,5,'FIX_FIELD_NAME'),action(c,config,1,'FIX_PIN_VERSION')]
def apply(text,change):
 lines=text.splitlines(keepends=True)
 def offset(pos):return sum(len(x) for x in lines[:pos['line']])+len(lines[pos['line']].encode('utf-16-le')[:2*pos['character']].decode('utf-16-le')) if pos['line']<len(lines) else len(text)
 for e in reversed(change['edits']):text=text[:offset(e['range']['start'])]+e['newText']+text[offset(e['range']['end']):]
 return text
def resolve_pair(name,c,pair,app,config,positive):
 for label,item,path,version,before,after in [('field',pair[0],app,7,TEXT,wire.APP),('pin',pair[1],config,11,CONFIG,wire.CONFIG)]:
  local_unaffected=label=='field' and name in ['dependency','dependency-delete','dependency-restore','target-restore']
  expected=positive or local_unaffected
  r=c.request('codeAction/resolve',item);attempts=1;end=time.monotonic()+6
  while expected and r.get('error',{}).get('code')==-32801 and time.monotonic()<end:
   time.sleep(.02);r=c.request('codeAction/resolve',item);attempts+=1
  if expected:
   changes=r.get('result',{}).get('edit',{}).get('documentChanges',[]);checks={'oneVersionedChange':len(changes)==1 and changes[0]['textDocument']=={'uri':path.as_uri(),'version':version},'exactBytes':len(changes)==1 and apply(before,changes[0])==after}
  else:checks={'stale':r.get('error',{}).get('code')==-32801,'noEdit':'edit' not in r.get('result',{})}
  record(name+'-'+label,checks,{'response':r,'attempts':attempts,'localIndependentOfProjectInputs':local_unaffected})
try:
 c,root,app,config,project=create('unchanged');pair=actions(c,app,config);resolve_pair('initial',c,pair,app,config,True)
 for _ in range(3):
  for path in [app,config]:c.send('textDocument/didSave',{'textDocument':{'uri':path.as_uri()}})
 ready(c,app);resolve_pair('repeated-save',c,pair,app,config,True)
 c.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':project.as_uri(),'type':2}]});ready(c,app);resolve_pair('unchanged-watch',c,pair,app,config,True)
 if not a.skip_periodic:
  # Local schema actions need not become unavailable during a scan. Observe
  # the actual terminal reconciliation trace before reusing the unchanged pin.
  mark=c.trace.mark()
  def periodic_complete():
   for row in c.trace.since(mark):
    stats=row['stats']
    if row['delivered'] and stats.get('fullScan') and stats.get('state')=='Ready' and stats.get('complete') and stats.get('queueDepth')==0 and stats.get('knownFiles',0)>=2:return row
   return None
  ready_trace=wait_until(c,periodic_complete,36,'complete natural reconciliation')
  observed=c.trace.since(mark)
  record('periodic-observed',{'completeReconciliation':ready_trace['stats']['complete'] and any(row['stats'].get('fullScan') for row in observed)},observed);resolve_pair('unchanged-periodic',c,pair,app,config,True)
 c.close()
 cases=['text','same-text-new-version','config-bytes','target','scope','dependency','dependency-delete','ownership','refusal','close','text-restore','target-restore','dependency-restore']
 for name in cases:
  c,root,app,config,project=create(name);pair=actions(c,app,config)
  if name in ['text','same-text-new-version','text-restore']:
   c.change(app,TEXT if name=='same-text-new-version' else TEXT+'# changed\n',8)
   if name=='text-restore':c.change(app,TEXT,9)
  elif name=='config-bytes':c.change(config,CONFIG+'# changed\n',12)
  elif name in ['target','target-restore']:
   c.send('workspace/didChangeConfiguration',{'settings':{'targetVersion':'3.5.6'}})
   if name=='target-restore':c.send('workspace/didChangeConfiguration',{'settings':{'targetVersion':'3.0.0'}})
  elif name=='scope':c.change(config,CONFIG+'scopes:\n  - name: changed\n    include: ["*.yaml"]\n',12)
  elif name in ['dependency','dependency-delete','dependency-restore']:
   if name=='dependency-delete':project.unlink()
   else:project.write_text(wire.PROJECT+'# changed dependency\n')
   c.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':project.as_uri(),'type':3 if name=='dependency-delete' else 2}]});ready(c,app)
   if name=='dependency-restore':project.write_text(wire.PROJECT);c.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':project.as_uri(),'type':2}]})
  elif name=='ownership':c.send('workspace/didChangeWorkspaceFolders',{'event':{'removed':[{'uri':root.as_uri(),'name':root.name}],'added':[]}})
  elif name=='refusal':c.change(app,'#'+'x'*(5*1024*1024),8);c.diag(app,8)
  elif name=='close':c.send('textDocument/didClose',{'textDocument':{'uri':app.as_uri()}})
  if name!='refusal':ready(c,app)
  resolve_pair(name,c,pair,app,config,False);c.close()
except Exception as e:record('runner',{'completed':False},repr(e))
finally:
 for c in clients:
  c.close();ids=[json.loads(frame.split(b'\r\n\r\n',1)[1]).get('id') for frame in c.sent if 'id' in json.loads(frame.split(b'\r\n\r\n',1)[1])];counts={i:sum(x['message'].get('id')==i for x in c.messages) for i in ids};record(c.out.name+'-terminal',{'exit0':c.proc.returncode==0,'oneResponseEach':all(n==1 for n in counts.values())},counts)
 expected=46 if a.skip_periodic else 49;report={'binarySHA256':wire.hash_file(wire.BINARY),'runnerSHA256':wire.hash_file(pathlib.Path(__file__)),'expectedRows':expected,'periodic':'not run' if a.skip_periodic else 'natural30s timer','rows':rows,'passed':len(rows)==expected and all(x['passed'] for x in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'rows':len(rows),'failed':[x['id'] for x in rows if not x['passed']],'passed':report['passed']}))
sys.exit(0 if report['passed'] else 1)
