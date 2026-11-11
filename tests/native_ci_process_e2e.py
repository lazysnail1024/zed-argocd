#!/usr/bin/env python3
"""Real task-tree and partial-report checks for the native CI process boundary."""
import argparse,hashlib,json,os,pathlib,subprocess,sys,time
ROOT=pathlib.Path(__file__).resolve().parents[1]

def open_active_report(path):
 # The observer must allow the writer to atomically replace its report. A
 # normal Windows open omits delete sharing and can make that replace fail.
 if os.name!='nt':return path.open(encoding='utf-8')
 import ctypes,msvcrt
 from ctypes import wintypes as w
 kernel=ctypes.WinDLL('kernel32',use_last_error=True)
 kernel.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE]
 kernel.CreateFileW.restype=w.HANDLE
 kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
 handle=kernel.CreateFileW(str(path),0x80000000,0x1|0x2|0x4,None,3,0x80,None) # READ; SHARE_READ|WRITE|DELETE; OPEN_EXISTING
 if handle==w.HANDLE(-1).value:raise ctypes.WinError(ctypes.get_last_error())
 try:fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_NOINHERIT)
 except BaseException:
  kernel.CloseHandle(handle);raise
 # The descriptor owns the handle after open_osfhandle succeeds.
 try:return os.fdopen(fd,encoding='utf-8')
 except BaseException:
  os.close(fd);raise

# Helpers terminate themselves after a finite interval even on the old broken boundary.
if len(sys.argv)>1 and sys.argv[1]=='--child':
 d=pathlib.Path(sys.argv[2]);(d/'ready').write_text(str(os.getpid()));print('child ready',flush=True);print('child stderr',file=sys.stderr,flush=True);start=time.monotonic()
 while time.monotonic()-start<8 and not (d/'stop').exists():
  (d/'heartbeat').write_text(str(time.monotonic()))
  if time.monotonic()-start>3:(d/'late-marker').write_text('descendant survived')
  time.sleep(.05)
 sys.exit(0)
if len(sys.argv)>1 and sys.argv[1]=='--parent':
 d=pathlib.Path(sys.argv[2]);p=subprocess.Popen([sys.executable,__file__,'--child',str(d)])
 deadline=time.monotonic()+5
 while not (d/'ready').exists() and time.monotonic()<deadline:time.sleep(.01)
 if not (d/'ready').exists():sys.exit(4)
 print('parent ready',flush=True)
 if sys.argv[3]=='exit':sys.exit(0)
 p.wait();sys.exit(0)
if len(sys.argv)>1 and sys.argv[1]=='--driver':
 import native_ci as ci
 d=pathlib.Path(sys.argv[2]);ci.configure(['--platform',sys.argv[3],'--output',str(d/'boundary')])
 ci.run('normal',[sys.executable,'-c','print("normal control")'])
 ci.run('tree',[sys.executable,__file__,'--parent',str(d),sys.argv[4]],timeout=2)
 ci.run('missing',[str(d/'absent-native')],timeout=2)
 sys.exit(0)
if len(sys.argv)>1 and sys.argv[1]=='--gate-driver':
 import native_ci as ci
 d=pathlib.Path(sys.argv[2]);mode=sys.argv[4];ci.configure(['--platform',sys.argv[3],'--output',str(d/'gate')])
 ci.expected_steps=['positive','regression']
 ci.run('positive',[sys.executable,'-c','print("positive consumer")'])
 if mode!='omitted':ci.run('regression',[sys.executable,'-c','import sys; print("regression consumer"); sys.exit('+('7' if mode=='failure' else '0')+')'])
 ci.completed=True;ci.save()
 sys.exit(0 if ci.accepted() else 1)
ap=argparse.ArgumentParser();ap.add_argument('output',type=pathlib.Path);ap.add_argument('--platform',required=True);a=ap.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
try:
 # Hold the observer open across replacement so this Windows sharing contract
 # is deterministic instead of depending on a short polling race.
 d=out/'report-replacement';d.mkdir();reportPath=d/'report.json';pending=d/'report.pending.json'
 before={'generation':1};after={'generation':2}
 reportPath.write_text(json.dumps(before),encoding='utf-8');pending.write_text(json.dumps(after),encoding='utf-8')
 try:
  import native_ci as ci
  with open_active_report(reportPath) as opened:
   replaceRetries=ci.replace_report(pending,reportPath)
   original=json.load(opened)
  current=json.loads(reportPath.read_text(encoding='utf-8'))
  checks={'replaceWhileObserved':True,'openedSnapshotUnchanged':original==before,'newSnapshotVisible':current==after}
  rows.append({'id':'report-replacement','checks':checks,'replaceRetries':replaceRetries,'passed':all(checks.values())})
 except Exception as e:rows.append({'id':'report-replacement','passed':False,'error':repr(e)})
 for mode in ['wait','exit']:
  d=out/mode;d.mkdir();command=[sys.executable,__file__,'--driver',str(d),a.platform,mode];start=time.monotonic();p=None
  try:
   p=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE);observed=None;deadline=time.monotonic()+6
   while time.monotonic()<deadline and p.poll() is None:
    reportPath=d/'boundary/report.json'
    if reportPath.exists():
     try:
      with open_active_report(reportPath) as opened:candidate=json.load(opened)
      if candidate.get('activeCommand',{} ) and candidate['activeCommand']['id']=='tree':observed=candidate;break
     except (OSError,ValueError):pass
    time.sleep(.01)
   (d/'observed-active-report.json').write_text(json.dumps(observed,indent=2)+'\n')
   so,se=p.communicate(timeout=15);elapsed=time.monotonic()-start
   (d/'stdout').write_bytes(so);(d/'stderr').write_bytes(se)
   # A stopped parent alone is insufficient: observe the descendant's own heartbeat and delayed marker.
   heartbeat=(d/'heartbeat').read_bytes() if (d/'heartbeat').exists() else None
   time.sleep(1.5);stable=heartbeat is not None and (d/'heartbeat').read_bytes()==heartbeat
   report=json.loads((d/'boundary/report.json').read_text());rr={x['id']:x for x in report['rows']}
   checks={'activeReport':bool(observed) and observed.get('completed') is False and observed.get('passed') is False and observed['rows'][0]['id']=='normal' and observed['rows'][0]['passed'], 'driverExit':p.returncode==0,'ready':(d/'ready').exists(),'bounded':elapsed<10,'descendantStopped':stable,'noLateMarker':not (d/'late-marker').exists(),'normal':rr.get('normal',{}).get('exit')==0,'timeout':rr.get('tree',{}).get('exit')=='timeout','missing':rr.get('missing',{}).get('exit')=='launch-failed','partialNeverPassed':report.get('completed') is False and report.get('passed') is False,'rawPartial':b'child ready' in (d/'boundary/tree.stdout').read_bytes() and b'child stderr' in (d/'boundary/tree.stderr').read_bytes()}
   rows.append({'id':mode,'command':command,'elapsedSeconds':elapsed,'checks':checks,'passed':all(checks.values())})
  except Exception as e:rows.append({'id':mode,'command':command,'passed':False,'error':repr(e)})
  finally:
   (d/'stop').write_text('test-owned helper cleanup')
   if p and p.poll() is None:p.kill();p.communicate(timeout=3)
   (out/'report.json').write_text(json.dumps({'rows':rows,'passed':False},indent=2)+'\n')
 for mode in ['success','failure','omitted']:
  d=out/('gate-'+mode);d.mkdir();command=[sys.executable,__file__,'--gate-driver',str(d),a.platform,mode]
  p=subprocess.run(command,capture_output=True,timeout=15);(d/'stdout').write_bytes(p.stdout);(d/'stderr').write_bytes(p.stderr)
  report=json.loads((d/'gate/report.json').read_text());expected=mode=='success'
  checks={'exit':p.returncode==(0 if expected else 1),'completed':report['completed'] is True,'acceptance':report['passed'] is expected,'requiredSteps':report['expectedSteps']==['positive','regression']}
  if mode=='failure':checks['retainedFailure']=report['rows'][1]['exit']==7 and (d/'gate/regression.stdout').read_text().strip()=='regression consumer'
  rows.append({'id':'gate-'+mode,'command':command,'checks':checks,'passed':all(checks.values())})
finally:
 report={'platform':a.platform,'python':sys.version,'command':sys.argv,'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'boundarySHA256':hashlib.sha256((ROOT/'tests/native_ci.py').read_bytes()).hexdigest(),'rows':rows,'passed':len(rows)==6 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
sys.exit(0 if report['passed'] else 1)
