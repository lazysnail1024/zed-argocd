#!/usr/bin/env python3
"""Adversarial explicit renderer process-tree lifetime through the real CLI."""
import argparse,hashlib,json,os,pathlib,signal,subprocess,sys,time
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);p.add_argument('--modes',nargs='+',default=['hanging-tree','parent-exit','version-tree','output-tree','nonzero','signal-tree','mixed-output-tree']);a=p.parse_args();binary=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=True);rows=[]
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
def alive(identity):
 pid,start=identity
 try:
  fields=pathlib.Path(f'/proc/{pid}/stat').read_text().split(') ')[1].split()
  return fields[0]!='Z' and fields[19]==start
 except FileNotFoundError:return False
for mode in a.modes:
 folder=out/mode;folder.mkdir(exist_ok=True);ws=folder/'workspace';chart=ws/'chart';chart.mkdir(parents=True,exist_ok=True);(ws/'.argocd-ls.yaml').write_text('schemaVersion: 1\ntargetVersion: "3.5.6"\n');(chart/'Chart.yaml').write_text('apiVersion: v2\nname: sample\nversion: 0.1.0\n');pidfile=folder/'pids.json';helper=folder/'helm-probe'
 helper.write_text('#!/usr/bin/python3\nimport os,sys,time,subprocess,pathlib,json\nmode='+repr(mode)+'\nif sys.argv[1]=="version" and mode!="version-tree":\n print("v3.16.4")\n sys.exit(0)\nprint("renderer-started",flush=True)\nprint("renderer-stderr",file=sys.stderr,flush=True)\nif mode=="nonzero":sys.exit(7)\nchild=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"])\npathlib.Path('+repr(str(pidfile))+').write_text(json.dumps([[p,pathlib.Path(f"/proc/{p}/stat").read_text().split(") ")[1].split()[19]] for p in [os.getpid(),child.pid]]))\nif mode=="parent-exit":sys.exit(0)\nif mode in ["output-tree","mixed-output-tree"]:\n while True:\n  os.write(1,b"x"*65536)\n  if mode=="mixed-output-tree":os.write(2,b"y"*65536)\ntime.sleep(120)\n');helper.chmod(0o755)
 argv=[str(binary),'render-check','--root',str(ws),'--chart',str(chart),'--helm-binary',str(helper),'--output',str(folder/'generated'),'--format','json'];start=time.monotonic();proc=None;peakRSS=0;harnessKilled=False;row={'name':mode,'command':argv,'toolSHA256':sha(helper)}
 try:
  with (folder/'stdout.raw').open('wb') as so,(folder/'stderr.raw').open('wb') as se:
   proc=subprocess.Popen(argv,stdout=so,stderr=se,start_new_session=True);sent=False
   while proc.poll() is None and time.monotonic()-start<36:
    try:
     rss=int(next(x.split()[1] for x in pathlib.Path(f'/proc/{proc.pid}/status').read_text().splitlines() if x.startswith('VmRSS:')))
     peakRSS=max(peakRSS,rss)
     if rss>262144:
      harnessKilled=True;os.killpg(proc.pid,signal.SIGKILL);break
    except (OSError,StopIteration):pass
    if mode=='signal-tree' and pidfile.exists() and not sent:proc.send_signal(signal.SIGINT);sent=True
    time.sleep(.02)
   observed=proc.poll();pids=json.loads(pidfile.read_text()) if pidfile.exists() else [];survivors=[pid for pid in pids if alive(pid)]
   row.update(peakRSSKiB=peakRSS,harnessMemoryKill=harnessKilled,exit=observed,elapsed=time.monotonic()-start,survivors=survivors,pids=pids)
   if observed is None:os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=3)
  stdout=(folder/'stdout.raw').read_bytes();stderr=(folder/'stderr.raw').read_bytes();report=json.loads(stdout);renderer=report.get('renderer') or {};errors=report.get('configurationErrors',[])
  expected=3 if mode in ['hanging-tree','version-tree','signal-tree'] else 2
  row['assertions']={'terminal':observed==expected and not harnessKilled,'treeReclaimed':not survivors,'incomplete':report['analysisComplete'] is False,'noGenerated':not(folder/'generated/generated.yaml').exists(),'rawStdout':'renderer-started' in renderer.get('stdout',''),'rawStderr':'renderer-stderr' in renderer.get('stderr',''),'explicitError':bool(errors),'promptBound':mode not in ['output-tree','mixed-output-tree','parent-exit','signal-tree'] or row['elapsed']<5,'outputBound':len(renderer.get('stdout','').encode())+len(renderer.get('stderr','').encode())<=10*1024*1024,'nonzeroStatus':mode!='nonzero' or renderer.get('exit')==7}
  row['passed']=all(row['assertions'].values())
 except Exception as e:row.update(error=repr(e),passed=False)
 finally:
  if proc and proc.poll() is None:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
  if pidfile.exists():
   for pid in json.loads(pidfile.read_text()):
    try:
     if alive(pid):os.kill(pid[0],signal.SIGKILL)
    except ProcessLookupError:pass
  rows.append(row);(folder/'result.json').write_text(json.dumps(row,indent=2))
  (out/'report.json').write_text(json.dumps({'binarySHA256':sha(binary),'runnerSHA256':sha(pathlib.Path(__file__)),'rows':rows,'passed':len(rows)==len(a.modes) and all(r['passed'] for r in rows)},indent=2))
print(json.dumps(rows));sys.exit(0 if len(rows)==len(a.modes) and all(r['passed'] for r in rows) else 1)
