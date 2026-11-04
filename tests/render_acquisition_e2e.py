#!/usr/bin/env python3
"""Cancellation before renderer startup must produce a truthful terminal report."""
import hashlib,json,pathlib,signal,subprocess,sys,time
binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=True);rows=[]
for mode in ['interrupt','deadline','invalid-config']:
 folder=out/mode;root=folder/'workspace';chart=root/'chart';chart.mkdir(parents=True,exist_ok=True);(root/'.argocd-ls.yaml').write_text('schemaVersion: '+('9' if mode=='invalid-config' else '1')+'\ntargetVersion: "3.5.6"\n');(chart/'Chart.yaml').write_text('apiVersion: v2\nname: sample\nversion: 0.1.0\n')
 if mode!='invalid-config':
  for i in range(10000):(root/f'{i:05}.yaml').write_text('kind: ConfigMap\n')
 marker=folder/'renderer-started';helper=folder/'helm';helper.write_text('#!/usr/bin/python3\nimport pathlib\npathlib.Path('+repr(str(marker))+').write_text("started")\nprint("v3.19.0")\n');helper.chmod(0o755)
 cmd=[str(binary),'render-check','--root',str(root),'--chart',str(chart),'--helm-binary',str(helper),'--output',str(folder/'generated'),'--format','json'];start=time.monotonic();proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 if mode!='invalid-config':
  time.sleep(.012)
  if mode=='interrupt':proc.send_signal(signal.SIGINT)
  else:proc.send_signal(signal.SIGSTOP);time.sleep(30.1);proc.send_signal(signal.SIGCONT)
 stdout=stderr=b''
 try:
  stdout,stderr=proc.communicate(timeout=5);checks={'exit':proc.returncode==(2 if mode=='invalid-config' else 3),'rendererNotStarted':not marker.exists(),'noGenerated':not(folder/'generated/generated.yaml').exists()}
  if mode!='invalid-config':
   r=json.loads(stdout);checks.update(incomplete=not r['analysisComplete'],reason=[x['code'] for x in r['configurationErrors']]==['RENDER_CANCELLED' if mode=='interrupt' else 'RENDER_DEADLINE_EXCEEDED'],rendererAbsent=r.get('renderer') is None,zeroCounts=r['filesAnalyzed']==0 and r['resourcesAnalyzed']==0,rawReason=bool(r['configurationErrors'][0]['message']))
  rows.append({'name':mode,'command':cmd,'exit':proc.returncode,'seconds':time.monotonic()-start,'assertions':checks,'passed':all(checks.values())})
 except Exception as ex:rows.append({'name':mode,'command':cmd,'error':repr(ex),'passed':False})
 finally:
  if proc.poll() is None:proc.kill();stdout,stderr=proc.communicate()
  (folder/'stdout.raw').write_bytes(stdout);(folder/'stderr.raw').write_bytes(stderr)
 report={'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'rows':rows,'passed':len(rows)==3 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report));sys.exit(0 if report['passed'] else 1)
