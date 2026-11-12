#!/usr/bin/env python3
"""Bounded CLI/LSP input rejection with an exact semantic result."""
import argparse,hashlib,json,os,pathlib,queue,signal,subprocess,sys,threading,time
ap=argparse.ArgumentParser();ap.add_argument('binary',type=pathlib.Path);ap.add_argument('output',type=pathlib.Path);ap.add_argument('--mode',choices=['cli','buffers'],required=True);a=ap.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);root=out/'workspace';root.mkdir();(root/'.argocd-ls.yaml').write_text('schemaVersion: 1\ntargetVersion: "3.5.6"\n');size=5*1024*1024
base='apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: item}\nspec:\n  project: default\n  source: {repoURL: https://example.test/repo, path: app}\n  destination: {server: local}\n';text=base+'#'+'x'*(size-len(base)-2)+'\n';assert len(text.encode())==size
report={'mode':a.mode,'binarySHA256':hashlib.sha256(a.binary.read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'command':sys.argv,'snapshotBytes':size,'inputSHA256':hashlib.sha256(text.encode()).hexdigest(),'watchdogBytes':384*1024*1024,'peakRSS':0,'watchdog':False,'acceptedBudgetTermination':False,'passed':False};proc=None;done=threading.Event();messages=queue.Queue();sent=0;diagnostics={}
try:
 membership=pathlib.Path('/proc/self/cgroup').read_text();cg=pathlib.Path('/sys/fs/cgroup')/membership.strip().split('::',1)[1].lstrip('/');limit=(cg/'memory.max').read_text().strip();report['cgroup']={'membership':membership,'memory.max':limit};assert limit!='max' and int(limit)<=512*1024*1024,'probe requires an actual <=512MiB task-owned memory cgroup'
 if a.mode=='cli':
  seed=root/'000.yaml';seed.write_text(text)
  for n in range(1,100):os.link(seed,root/f'{n:03}.yaml')
  report['paths']=100;report['aggregateBytes']=size*100;cmd=[str(a.binary.resolve()),'check','--root',str(root),'--format','json']
 else:cmd=[str(a.binary.resolve()),'serve','--stdio']
 report['childCommand']=cmd;stderr=(out/'stderr').open('wb');stdout=(out/'stdout').open('wb');stdin=(out/'stdin').open('wb');start=time.monotonic();proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,start_new_session=True)
 def watch():
  while not done.wait(.01) and proc.poll() is None:
   try:rss=int(pathlib.Path(f'/proc/{proc.pid}/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE');report['peakRSS']=max(report['peakRSS'],rss)
   except (FileNotFoundError,ProcessLookupError):continue
   if rss>report['watchdogBytes']:report['watchdog']=True;proc.kill();return
 watcher=threading.Thread(target=watch,daemon=True);watcher.start()
 def read():
  if a.mode=='cli':stdout.write(proc.stdout.read());return
  try:
   while True:
    headers={}
    while True:
     line=proc.stdout.readline();stdout.write(line)
     if not line:return
     if line==b'\r\n':break
     k,v=line.decode().strip().split(':',1);headers[k.lower()]=v.strip()
    body=proc.stdout.read(int(headers['content-length']));stdout.write(body);message=json.loads(body)
    if message.get('method')=='textDocument/publishDiagnostics':diagnostics[message['params']['uri']]=message['params']
    messages.put(message)
  except Exception as e:report['readerError']=repr(e)
 reader=threading.Thread(target=read,daemon=True);reader.start()
 def send(method,params=None,ident=None):
  m={'jsonrpc':'2.0','method':method}
  if params is not None:m['params']=params
  if ident is not None:m['id']=ident
  data=json.dumps(m).encode();frame=f'Content-Length: {len(data)}\r\n\r\n'.encode()+data;stdin.write(frame);proc.stdin.write(frame);proc.stdin.flush()
 def wait(ident):
  end=time.monotonic()+6
  while time.monotonic()<end:
   m=messages.get(timeout=max(.01,end-time.monotonic()))
   if m.get('id')==ident:return m
  raise TimeoutError('request terminal response missing')
 if a.mode=='buffers':
  send('initialize',{'rootUri':root.as_uri(),'capabilities':{'textDocument':{'publishDiagnostics':{'versionSupport':True}}}},1);reply=wait(1);assert 'result' in reply and 'error' not in reply,'initialize failed';send('initialized',{})
  for n in range(100):
   send('textDocument/didOpen',{'textDocument':{'uri':(root/f'{n:03}.yaml').as_uri(),'languageId':'yaml','version':1,'text':text}})
   # An owner-loop reply drains preceding notifications without waiting for an
   # expensive analysis. This separates held buffers from a queued input flood.
   send('workspace/symbol',{'query':''},100+n);wait(100+n);sent+=1
   if proc.poll() is not None:break
  report['sentBuffers']=sent
  refused=(root/'099.yaml').as_uri();deadline=time.monotonic()+6
  while not any(d.get('code')=='INPUT_BUDGET_EXCEEDED' for d in diagnostics.get(refused,{}).get('diagnostics',[])):
   if time.monotonic()>=deadline:raise TimeoutError('the final buffer has no retained-input budget refusal')
   messages.get(timeout=max(.01,deadline-time.monotonic()))
  report['refusedBuffer']=diagnostics[refused]
  send('shutdown',{},2);report['shutdown']=wait(2);send('exit')
 proc.wait(timeout=7);report['elapsedSeconds']=time.monotonic()-start
except Exception as e:report['error']=repr(e)
finally:
 done.set()
 if proc:
  if proc.poll() is None:report['harnessTermination']=True;proc.kill()
  proc.wait(timeout=3);report['exitCode']=proc.returncode;report['sentBuffers']=sent
  if 'reader' in locals():reader.join(timeout=3)
  for f in [stdin,stdout,stderr]:f.close()
 if a.mode=='cli' and (out/'stdout').exists():
  try:
   d=json.loads((out/'stdout').read_bytes());report['cliReport']=d;report['acceptedBudgetTermination']=report.get('exitCode')==2 and d.get('analysisComplete') is False and [x.get('code') for x in d.get('configurationErrors',[])]==['INPUT_BUDGET_EXCEEDED'] and d.get('filesAnalyzed')==0 and d.get('resourcesAnalyzed')==0
  except Exception as error:report['reportError']=repr(error)
 checks={'noHarnessFailure':not any(report.get(key) for key in ['watchdog','harnessTermination','error','readerError','reportError']),'measuredRSS':0<report['peakRSS']<=report['watchdogBytes']}
 if a.mode=='cli':checks['exactBudgetRejection']=report['acceptedBudgetTermination']
 else:
  checks.update(allBuffersProcessed=sent==100,normalShutdown=report.get('exitCode')==0 and report.get('shutdown',{}).get('result','missing') is None and 'error' not in report.get('shutdown',{}),exactBudgetRejection=report.get('refusedBuffer',{}).get('version')==1 and any(d.get('code')=='INPUT_BUDGET_EXCEEDED' for d in report.get('refusedBuffer',{}).get('diagnostics',[])))
 report.update(checks=checks,passed=all(checks.values()),scope='100 input paths/buffers under a 512MiB cgroup; normal budget refusal and shutdown are required')
 (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:report.get(k) for k in ['mode','passed','checks','peakRSS','watchdog','exitCode','error','acceptedBudgetTermination']}));sys.exit(0 if report['passed'] else 1)
