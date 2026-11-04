#!/usr/bin/env python3
"""Native LSP latency, burst ownership and terminal lifecycle observations."""
import argparse,hashlib,importlib.util,json,os,pathlib,sys,time
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);p.add_argument('--full',action='store_true');a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=True);os.environ['CD_AUDIT_BINARY']=str(a.binary.resolve());spec=importlib.util.spec_from_file_location('client',pathlib.Path(__file__).with_name('audit_repair_e2e.py'));e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e);e.BINARY=a.binary.resolve();rows=[];c=None
try:
 root=out/'workspace';root.mkdir(exist_ok=True);e.write(root,'.argocd-ls.yaml',e.CONFIG);e.write(root,'project.yaml',e.PROJECT)
 for i in range(98):e.write(root,f'app-{i}.yaml',e.APP.replace('name: app','name: app-'+str(i)))
 text=e.APP+'#'+('x'*199000)+'\n';file=e.write(root,'active.yaml',text);other=out/'second-workspace';other.mkdir(exist_ok=True);e.write(other,'.argocd-ls.yaml',e.CONFIG.replace('3.0.0','3.5.6'));e.write(other,'project.yaml',e.PROJECT);e.write(other,'app.yaml',e.APP);c=e.LSP(out,[root,other]);c.open(file,text,1);c.diag(file,1);version=1
 latencies=[]
 for i in range(5):
  version+=1;start=time.monotonic();c.change(file,text,version);c.diag(file,version);latencies.append(time.monotonic()-start)
 rows.append({'name':'five-diagnostic-samples','seconds':latencies,'passed':max(latencies)<=.620})
 q=[]
 for i in range(1000 if a.full else 20):
  start=time.monotonic();r=c.request('textDocument/definition',{'textDocument':{'uri':file.as_uri()},'position':{'line':5,'character':14}});q.append(time.monotonic()-start);assert r.get('result'),'definition positive control'
 rows.append({'name':'query-latency','seconds':q,'passed':sorted(q)[int(len(q)*.95)-1]<=.1})
 # Rapid changes must settle to the newest conflicting destination, never old clean text.
 for i in range(600 if a.full else 10):
  version+=1;c.change(file,text,version);time.sleep(.1)
 version+=1;final=text.replace('destination: {server:', 'destination: {name: conflict, server:');c.change(file,final,version);d=c.diag(file,version);settled=time.monotonic();time.sleep(.3)
 stale=[m for m in c.messages if m['elapsed']>settled-c.start and m['message'].get('method')=='textDocument/publishDiagnostics' and m['message'].get('params',{}).get('uri')==file.as_uri() and m['message']['params'].get('version',version)<version]
 rows.append({'name':'sustained-newest-epoch','passed':any(x['code']=='CD_DESTINATION_CONFLICT' for x in d) and not stale,'stale':stale})
 # Closing during queued work retains the required empty replacement.
 file.unlink();version+=1;c.change(file,text,version);c.send('textDocument/didClose',{'textDocument':{'uri':file.as_uri()}})
 cleared=c.wait(lambda m:m.get('method')=='textDocument/publishDiagnostics' and m.get('params',{}).get('uri')==file.as_uri() and m['params'].get('diagnostics')==[])
 rows.append({'name':'close-clear','passed':bool(cleared)})
 if a.full:
  def rss():
   return int(next(x.split()[1] for x in pathlib.Path(f'/proc/{c.proc.pid}/status').read_text().splitlines() if x.startswith('VmRSS:')))
  cycle=root/'cycle.yaml';samples=[]
  def cycles(count):
   for i in range(count):
    c.open(cycle,e.APP,1);c.change(cycle,e.APP.replace('name: app','name: changed'),2);c.send('textDocument/didClose',{'textDocument':{'uri':cycle.as_uri()}})
   c.open(cycle,e.APP,1);c.diag(cycle,1);c.send('textDocument/didClose',{'textDocument':{'uri':cycle.as_uri()}});time.sleep(.3)
  cycles(1000);warm=rss()
  for i in range(5):cycles(2000);samples.append(rss())
  rows.append({'name':'ten-thousand-open-edit-close','warmRSSKiB':warm,'samplesRSSKiB':samples,'passed':samples[-1]<=warm*1.1,'twoActiveVersions':['3.0.0','3.5.6']})
 start=time.monotonic();response=c.request('shutdown' ,{},timeout=5);c.send('exit');c.proc.wait(timeout=5);elapsed=time.monotonic()-start;rows.append({'name':'terminal-shutdown','passed':c.proc.returncode==0 and elapsed<=5,'seconds':elapsed})
except Exception as exc:rows.append({'name':'runner','error':repr(exc),'passed':False})
finally:
 if c:c.close()
 report={'binarySHA256':hashlib.sha256(a.binary.read_bytes()).hexdigest(),'testSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'rows':rows,'full':a.full,'passed':len(rows)==(6 if a.full else 5) and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report));sys.exit(0 if report['passed'] else 1)
