#!/usr/bin/env python3
"""Portable native child/pipe/output lifetime through the real render CLI."""
import ctypes,hashlib,json,os,pathlib,subprocess,sys,time
binary=pathlib.Path(sys.argv[1]).resolve();helper=pathlib.Path(sys.argv[2]).resolve();out=pathlib.Path(sys.argv[3]).resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
def alive(pid):
 if os.name=='nt':
  kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.OpenProcess.restype=ctypes.c_void_p;handle=kernel.OpenProcess(0x1000,False,pid)
  if not handle:return False
  try:
   code=ctypes.c_ulong();kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)];return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
  finally:kernel.CloseHandle.argtypes=[ctypes.c_void_p];kernel.CloseHandle(handle)
 p=subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True);return p.returncode==0 and bool(p.stdout.strip()) and not p.stdout.strip().startswith(b'Z')
for mode in ['parent-exit','output','nonzero','deadline']:
 folder=out/mode;root=folder/'workspace';chart=root/'chart';chart.mkdir(parents=True);(chart/'Chart.yaml').write_text('apiVersion: v2\nname: native-helper\nversion: 0.1.0\n');marker=folder/'child-heartbeat';pids=folder/'pids.json';env=dict(os.environ,CD_HELPER_MODE=mode,CD_HELPER_CHILD=str(marker),CD_HELPER_PIDS=str(pids),CD_HELPER_STOP=str(folder/'stop'));cmd=[str(binary),'render-check','--root',str(root),'--chart',str(chart),'--helm-binary',str(helper),'--output',str(folder/'generated'),'--format','json'];stdout=stderr=b'';status=None;start=time.monotonic()
 try:
  p=subprocess.run(cmd,env=env,capture_output=True,timeout=36);stdout,stderr,status=p.stdout,p.stderr,p.returncode;elapsed=time.monotonic()-start;r=json.loads(stdout);before=marker.read_bytes() if marker.exists() else None;time.sleep(.25);after=marker.read_bytes() if marker.exists() else None;tool=r.get('renderer') or {};checks={'exit':status==(3 if mode=='deadline' else 2),'incomplete':r['analysisComplete'] is False,'reason':bool(r['configurationErrors']),'rawOutput':'renderer-started' in tool.get('stdout','') and 'renderer-stderr' in tool.get('stderr',''),'childStopped':before==after,'boundedOutput':len(tool.get('stdout','').encode())+len(tool.get('stderr','').encode())<=10*1024*1024,'nonzero':mode!='nonzero' or tool.get('exit')==7,'prompt':elapsed<5 if mode in ['parent-exit','output'] else elapsed<35}
  if mode!='nonzero':checks['childSpawned']=pids.exists() and len(json.loads(pids.read_text()))==2;checks['processesReclaimed']=pids.exists() and not any(alive(pid) for pid in json.loads(pids.read_text()))
  rows.append({'id':mode,'command':cmd,'exit':status,'elapsed':elapsed,'checks':checks,'passed':all(checks.values())})
 except Exception as e:
  if isinstance(e,subprocess.TimeoutExpired):stdout,stderr=e.stdout or b'',e.stderr or b''
  rows.append({'id':mode,'command':cmd,'exit':status,'error':repr(e),'passed':False})
 (folder/'stop').write_text('stop task-owned helpers');time.sleep(.1)
 (folder/'stdout').write_bytes(stdout);(folder/'stderr').write_bytes(stderr)
report={'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'helperSHA256':hashlib.sha256(helper.read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'rows':rows,'passed':len(rows)==4 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'passed':report['passed'],'rows':len(rows)}));sys.exit(0 if report['passed'] else 1)
