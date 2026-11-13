#!/usr/bin/env python3
"""Real CLI resource accounting and full-size performance samples."""
import argparse,hashlib,json,os,pathlib,signal,subprocess,sys,time
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);p.add_argument('--full',action='store_true');p.add_argument('--samples',type=int,default=5);a=p.parse_args();a.binary=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
rows=[]
def constraint():
 result={'affinity':sorted(os.sched_getaffinity(0)),'cgroup':pathlib.Path('/proc/self/cgroup').read_text()}
 base=pathlib.Path('/sys/fs/cgroup')/result['cgroup'].strip().split('::',1)[1].lstrip('/')
 for name in ['memory.max','cpu.max','memory.peak','cpu.stat']:
  try:result[name]=(base/name).read_text().strip()
  except OSError as e:result[name]=str(e)
 return result
limits=constraint()
config='schemaVersion: 1\ntargetVersion: "3.5.6"\n'
app='apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: app-NUM}\nspec:\n  project: absent\n  sources: [{repoURL: https://example.org/repo, ref: values}]\n  destination: {server: https://kubernetes.default.svc}\n'
def workspace(name,count,size=0):
 ws=out/name;ws.mkdir(exist_ok=True);(ws/'.argocd-ls.yaml').write_text(config)
 for i in range(count):
  text=app.replace('NUM',str(i))
  if size:text+='#'+('x'*(size-len(text)-2))+'\n'
  (ws/f'{i:05}.yaml').write_text(text)
 return ws

def run(name,ws,count,cancel=False):
 folder=out/name;folder.mkdir(exist_ok=True);argv=[str(a.binary),'check','--root',str(ws),'--format','json'];start=time.monotonic();proc=None;stdout=stderr=b'';status='completed';rss=0
 try:
  with (folder/'stdout.raw').open('wb') as so,(folder/'stderr.raw').open('wb') as se:
   process_start=time.monotonic();proc=subprocess.Popen(argv,stdout=so,stderr=se);sent=False
   while proc.poll() is None:
    elapsed=time.monotonic()-process_start
    try:
     stats=pathlib.Path(f'/proc/{proc.pid}/status').read_text();rss=max(rss,int(next(x.split()[1] for x in stats.splitlines() if x.startswith('VmRSS:'))))
    except (OSError,StopIteration):pass
    if cancel and not sent and elapsed>=0.02:proc.send_signal(signal.SIGINT);sent=True
    if elapsed>12:status='harness-timeout';proc.kill();break
    time.sleep(.005)
   proc.wait(timeout=3)
   process_end=time.monotonic()
  stdout=(folder/'stdout.raw').read_bytes();stderr=(folder/'stderr.raw').read_bytes();report=json.loads(stdout)
  decoded_end=time.monotonic()
  codes=[e['code'] for e in report['configurationErrors']]
  if cancel:
   checks={'exit3':proc.returncode==3,'cancelReason':'ANALYSIS_CANCELLED' in codes,'notComplete':report['analysisComplete'] is False,'notAllCounted':report['filesAnalyzed']<count and report['resourcesAnalyzed']<count}
  else:
   checks={'exit0':proc.returncode==0,'exactFiles':report['filesAnalyzed']==count+1,'exactResources':report['resourcesAnalyzed']==count,'configuration':codes==[],'exactDiagnostics':len(report['diagnostics'])==count and all(d['code']=='CD_PROJECT_NAMESPACE_UNBOUND' and d['severity']==3 for d in report['diagnostics']),'complete':report['analysisComplete'] is True,'deadline':process_end-process_start<=5}
  checks['processEnded']=status=='completed'
  verified_end=time.monotonic()
  row={'name':name,'command':argv,'exit':proc.returncode,'elapsedSeconds':verified_end-start,'totalElapsedSeconds':verified_end-start,'processElapsedSeconds':process_end-process_start,'readDecodeSeconds':decoded_end-process_end,'verificationSeconds':verified_end-process_end,'phaseSeconds':{'processStarted':process_start-start,'processObservedEnd':process_end-start,'decoded':decoded_end-start,'verified':verified_end-start},'peakRSSKiB':rss,'status':status,'assertions':checks,'passed':all(checks.values())}
 except Exception as e:
  row={'name':name,'command':argv,'error':repr(e),'status':status,'passed':False}
 finally:
  if proc and proc.poll() is None:proc.kill();proc.wait()
 rows.append(row);(folder/'result.json').write_text(json.dumps(row,indent=2));return row
try:
 if a.samples<1:raise ValueError('at least one sample required')
 tiny=workspace('tiny-input',250)
 for i in range(a.samples):run(f'tiny-{i}',tiny,250)
 full=workspace('full-input',10000,5242)
 # Exactly 50 MiB across YAML resources, independent of configuration bytes.
 first=full/'00000.yaml';first.write_text(first.read_text()+'#'+'x'*(50*1024*1024-10000*5242-2)+'\n')
 if a.full:
  for i in range(a.samples):run(f'full-{i}',full,10000)
 run('signal-cancellation',full,10000,True)
except Exception as e:rows.append({'name':'runner','error':repr(e),'passed':False})
finally:
 report={'binarySHA256':sha(a.binary),'runnerSHA256':sha(pathlib.Path(__file__)),'command':sys.argv,'platform':sys.platform,'effectiveConstraints':limits,'finalConstraints':constraint(),'cpuAffinity':sorted(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else None,'rows':rows,'passed':bool(rows) and all(r['passed'] for r in rows)}
 (out/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report));sys.exit(0 if report['passed'] else 1)
