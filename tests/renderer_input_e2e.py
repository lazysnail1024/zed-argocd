#!/usr/bin/env python3
"""Explicit renderer file validation and honest failed-launch provenance."""
import hashlib,json,os,pathlib,subprocess,sys,time
binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=True);rows=[]
for mode in ['fifo','directory','missing','regular-failure','symlink-failure']:
 folder=out/mode;root=folder/'workspace';chart=root/'chart';chart.mkdir(parents=True,exist_ok=True);(root/'.argocd-ls.yaml').write_text('schemaVersion: 1\ntargetVersion: "3.5.6"\n');(chart/'Chart.yaml').write_text('apiVersion: v2\nname: test\nversion: 0.1.0\n');tool=folder/'helm'
 if mode=='fifo':os.mkfifo(tool);tool.chmod(0o700)
 elif mode=='directory':tool.mkdir()
 elif mode!='missing':
  actual=folder/'actual-helm';actual.write_text('#!/usr/bin/python3\nimport sys\nif sys.argv[1]=="version":\n print("v3.19.0")\n sys.exit(0)\nprint("render stdout",flush=True)\nprint("render stderr",file=sys.stderr,flush=True)\nsys.exit(7)\n');actual.chmod(0o700)
  if mode=='symlink-failure':tool.symlink_to(actual)
  else:tool.write_bytes(actual.read_bytes());tool.chmod(0o700)
 cmd=[str(binary),'render-check','--root',str(root),'--chart',str(chart),'--helm-binary',str(tool),'--output',str(folder/'generated'),'--format','json'];start=time.monotonic();stdout=stderr=b''
 try:
  p=subprocess.run(cmd,capture_output=True,timeout=2);stdout,stderr=p.stdout,p.stderr;r=json.loads(stdout);renderer=r.get('renderer',{});regular=mode.endswith('failure');checks={'exit2':p.returncode==2,'incomplete':not r['analysisComplete'],'explicitReason':bool(r['configurationErrors']),'toolExit':renderer.get('exit')==(7 if regular else -1),'noGenerated':not(folder/'generated/generated.yaml').exists()}
  if regular:checks.update(rawStdout=renderer.get('stdout')=='render stdout\n',rawStderr=renderer.get('stderr')=='render stderr\n',exactHash=renderer.get('binarySHA256')==hashlib.sha256(tool.read_bytes()).hexdigest())
  else:checks.update(noStdout=renderer.get('stdout')=='',noStderr=renderer.get('stderr')=='')
  rows.append({'name':mode,'command':cmd,'exit':p.returncode,'seconds':time.monotonic()-start,'assertions':checks,'passed':all(checks.values())})
 except Exception as ex:
  if isinstance(ex,subprocess.TimeoutExpired):stdout,stderr=ex.stdout or b'',ex.stderr or b''
  rows.append({'name':mode,'command':cmd,'error':repr(ex),'seconds':time.monotonic()-start,'passed':False})
 (folder/'stdout.raw').write_bytes(stdout);(folder/'stderr.raw').write_bytes(stderr)
report={'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'rows':rows,'passed':len(rows)==5 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report));sys.exit(0 if report['passed'] else 1)
